"""mile_splits: per mile (or quarter mile), running-only pace, HR, DEM gain
and loss, and time by gait -- with Garmin's own laps beside it, never
substituted (rules accepted by the athlete).

(a) BOUNDARIES come from the device odometer, `records.distance_m`, the same
    source the windowed bests use -- never from 30 s bins. Boundary k is the
    instant the odometer reaches k x the unit, interpolated linearly between
    the two records either side (`interpolated_exact`). Across a flat stretch
    (standing still) the boundary takes the LAST instant at that distance, so
    the standing belongs to the unit it happened in. A boundary is placed even
    across a recording gap over MAX_SAMPLE_GAP_S, and the unit is FLAGGED
    (`contains_gap`, `boundary_in_gap`) rather than refused. The last unit is
    a PARTIAL with its real length, never stretched.

(b) RUNNING-ONLY = each rwd_run split's wall-clock span intersected with the
    unit, stopped time removed. Pace = odometer distance gained inside those
    overlaps / their moving seconds. A walk break inside a mile is excluded
    from pace and counted in `time_walking_s`; standing is its own figure.

HR, TWO FIGURES, BOTH RAW (no 40 s lag shift): `hr_running` -- PRIMARY --
time-weighted over the same rwd_run overlap the pace uses; `hr_all_gaits`
over the whole unit, every gait, stopped time removed. Each record's HR
stands for the interval to the next record, capped at MAX_WEIGHT_GAP_S, as
`basic.record_weights` defines a record's weight.

ELEVATION from records_enriched.altitude_smooth_m -- the smoothed DEM profile
grade uses, not the raw per-record sample, whose GPS wobble inflates gain --
interpolated at the boundaries. No DEM: gain/loss null with terrain_state's
reason.
"""

from __future__ import annotations

import datetime as _dt

from trail_factors.analysis import basic as B
from trail_factors.units import METERS_PER_MILE, pace_per_mile

M_TO_FT = 1.0 / 0.3048
UNITS_M = {"mile": METERS_PER_MILE, "quarter": METERS_PER_MILE / 4}


def _interp(x, xs, ys):
    """Linear interpolation on sorted xs; clamps at the ends."""
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    lo, hi = 0, len(xs) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if xs[mid] <= x:
            lo = mid
        else:
            hi = mid
    span = xs[hi] - xs[lo]
    return ys[lo] if span == 0 else ys[lo] + (ys[hi] - ys[lo]) * (x - xs[lo]) / span


def _overlap(a0, a1, b0, b1):
    lo, hi = max(a0, b0), min(a1, b1)
    return (lo, hi) if hi > lo else None


def mile_splits(con, activity_id: str, unit: str = "mile") -> dict:
    if unit not in UNITS_M:
        raise ValueError(f"unit={unit!r} must be one of {tuple(UNITS_M)}")
    unit_m = UNITS_M[unit]
    rows = con.execute("""
        SELECT timestamp, distance_m, heart_rate FROM records
        WHERE activity_id = ? AND distance_m IS NOT NULL ORDER BY timestamp
    """, [activity_id]).fetchall()
    filters = _filters(unit, unit_m)
    if len(rows) < 2:
        return {"units": [], "status": "unavailable",
                "reason": "fewer than two records carry a distance",
                "garmin_laps": _laps(con, activity_id), "filters": filters}

    t0 = rows[0][0]
    ts = [(r[0] - t0).total_seconds() for r in rows]
    d, n_backwards = [], 0
    for r in rows:                         # clamp, and COUNT, any regression
        v = float(r[1])
        if d and v < d[-1]:
            n_backwards += 1
            v = d[-1]
        d.append(v)
    hr = [r[2] for r in rows]

    # THE WINDOWED BESTS' TIME-AT-DISTANCE RULE (basic.time_at_distance): a
    # unit STARTS at the LAST moment at its start distance and ENDS at the
    # FIRST moment at its end distance. This kept the last time at each
    # distance and interpolated last-to-last, which put standing at the NEXT
    # distance into the rise; the shared rule interpolates last_i ->
    # first_{i+1}. Standing EXACTLY on a boundary distance falls between the
    # two units and is reported as `between_units_s`, never given to either.
    inv = B.distance_inverse(ts, d)

    total = d[-1] - d[0]
    edges_m = [d[0]]
    k = 1
    while d[0] + k * unit_m < d[-1] - 1e-9:
        edges_m.append(d[0] + k * unit_m)
        k += 1
    edges_m.append(d[-1])
    if inv[0].size < 2:
        return {"units": [], "status": "unavailable",
                "reason": "the odometer never moved",
                "garmin_laps": _laps(con, activity_id), "filters": filters}
    starts_t = [float(B.time_at_distance(inv, m, end=False))
                for m in edges_m[:-1]]
    ends_t = [float(B.time_at_distance(inv, m, end=True))
              for m in edges_m[1:]]

    def at(sec):
        return t0 + _dt.timedelta(seconds=sec)

    paused = B.paused_intervals(con, activity_id)
    splits = con.execute("""
        SELECT split_type, start_time, end_time FROM splits
        WHERE activity_id = ? AND split_type LIKE 'rwd\\_%' ESCAPE '\\'
          AND start_time IS NOT NULL AND end_time IS NOT NULL
    """, [activity_id]).fetchall()
    elev = _elevation(con, activity_id, t0)
    gaps = [(ts[i], ts[i + 1]) for i in range(len(ts) - 1)
            if ts[i + 1] - ts[i] > B.MAX_SAMPLE_GAP_S]

    def hr_over(spans):
        """Time-weighted raw HR over a set of (t0, t1) seconds spans."""
        num = den = 0.0
        for i in range(len(ts)):
            if hr[i] is None:
                continue
            nxt = ts[i + 1] if i + 1 < len(ts) else ts[i]
            a, b = ts[i], min(nxt, ts[i] + B.MAX_WEIGHT_GAP_S)
            for s0, s1 in spans:
                ov = _overlap(a, b, s0, s1)
                if ov:
                    w = B.moving_seconds(at(ov[0]), at(ov[1]), paused)
                    num += hr[i] * w
                    den += w
        return (num / den if den > 0 else None), den

    out = []
    for i in range(len(edges_m) - 1):
        a_t, b_t = starts_t[i], ends_t[i]
        length = edges_m[i + 1] - edges_m[i]
        by_class = {"rwd_run": [], "rwd_walk": [], "rwd_stand": []}
        for st, s, e in splits:
            ov = _overlap(a_t, b_t, (s - t0).total_seconds(),
                          (e - t0).total_seconds())
            if ov and st in by_class:
                by_class[st].append(ov)
        run_s = sum(B.moving_seconds(at(x), at(y), paused)
                    for x, y in by_class["rwd_run"])
        run_m = sum(_interp(y, ts, d) - _interp(x, ts, d)
                    for x, y in by_class["rwd_run"])
        mps = run_m / run_s if run_s > 0 else None
        hr_run, hr_run_w = hr_over(by_class["rwd_run"])
        hr_all, _ = hr_over([(a_t, b_t)])
        in_gap = any(g0 < a_t < g1 or g0 < b_t < g1 for g0, g1 in gaps)
        contains = any(_overlap(a_t, b_t, g0, g1) for g0, g1 in gaps)
        row = {
            "index": i + 1, "partial": length < unit_m - 1e-6,
            "length_m": length, "length_mi": length / METERS_PER_MILE,
            "start_distance_m": edges_m[i], "end_distance_m": edges_m[i + 1],
            "start_elapsed_s": a_t, "end_elapsed_s": b_t,
            "elapsed_s": b_t - a_t,
            "running_mps": mps,
            "running_pace_per_mile": pace_per_mile(mps) if mps else None,
            "running_distance_m": run_m,
            "time_running_s": run_s,
            "time_walking_s": sum(B.moving_seconds(at(x), at(y), paused)
                                  for x, y in by_class["rwd_walk"]),
            "time_standing_s": sum(y - x for x, y in by_class["rwd_stand"]),
            "hr_running": hr_run, "hr_running_weight_s": hr_run_w,
            "hr_all_gaits": hr_all,
            "contains_gap": bool(contains), "boundary_in_gap": bool(in_gap),
            # Standing exactly on the boundary between this unit and the
            # next: it belongs to neither.
            "between_units_s": (starts_t[i + 1] - b_t
                                if i + 1 < len(starts_t) else 0.0),
        }
        row.update(_gain_loss(elev, a_t, b_t))
        out.append(row)

    return {"units": out, "unit": unit, "unit_m": unit_m,
            "n_units": len(out), "total_distance_m": total,
            "n_backwards_odometer_steps": n_backwards,
            "elevation_available": elev["status"] == "ok",
            "elevation_reason": elev.get("reason"),
            "garmin_laps": _laps(con, activity_id),
            "status": "ok", "filters": filters}


def _elevation(con, activity_id: str, t0) -> dict:
    pts = con.execute("""
        SELECT timestamp, altitude_smooth_m FROM records_enriched
        WHERE activity_id = ? AND altitude_smooth_m IS NOT NULL
          AND terrain_usable
        ORDER BY timestamp""", [activity_id]).fetchall()
    if not pts:
        state = B.terrain_state(con, activity_id)
        return {"status": "unavailable",
                "reason": (f"no usable DEM elevation on this activity "
                           f"({state.get('state')}): {state.get('reason')}")}
    return {"status": "ok", "t": [(p[0] - t0).total_seconds() for p in pts],
            "z": [float(p[1]) for p in pts]}


def _gain_loss(elev: dict, a_t: float, b_t: float) -> dict:
    none = {"gain_m": None, "loss_m": None, "gain_ft": None, "loss_ft": None}
    if elev["status"] != "ok":
        return {**none, "elevation_reason": elev["reason"]}
    t, z = elev["t"], elev["z"]
    inside = [(x, y) for x, y in zip(t, z) if a_t < x < b_t]
    if not inside and not (t[0] <= a_t and t[-1] >= b_t):
        return {**none, "elevation_reason":
                "no usable DEM elevation falls inside this unit"}
    prof = ([(a_t, _interp(a_t, t, z))] + inside + [(b_t, _interp(b_t, t, z))])
    gain = sum(max(0.0, q[1] - p[1]) for p, q in zip(prof, prof[1:]))
    loss = sum(max(0.0, p[1] - q[1]) for p, q in zip(prof, prof[1:]))
    return {"gain_m": gain, "loss_m": loss, "gain_ft": gain * M_TO_FT,
            "loss_ft": loss * M_TO_FT, "elevation_reason": None}


def _laps(con, activity_id: str) -> list[dict]:
    """Garmin's own laps, AS RECORDED, for cross-check. Never substituted."""
    out = []
    for idx, start, timer, dist, avg_hr, trig in con.execute("""
            SELECT lap_index, start_time, total_timer_time, distance_m,
                   avg_hr, trigger FROM laps WHERE activity_id = ?
            ORDER BY lap_index""", [activity_id]).fetchall():
        mps = dist / timer if timer and dist is not None else None
        out.append({"lap_index": idx, "start_utc": str(start),
                    "timer_s": timer, "distance_m": dist, "avg_hr": avg_hr,
                    "trigger": trig, "mps": mps,
                    "pace_per_mile": pace_per_mile(mps) if mps else None})
    return out


def _filters(unit: str, unit_m: float) -> dict:
    return {
        "unit": unit, "unit_m": unit_m,
        "boundary_mode": "interpolated_exact",
        "boundary_rule": "basic.time_at_distance, the windowed bests' rule: "
                         "a unit STARTS at the LAST moment at its start "
                         "distance and ENDS at the FIRST moment at its end "
                         "distance, interpolated between the records either "
                         "side. Standing exactly on a boundary is in "
                         "between_units_s, in neither unit. Never 30 s bins.",
        "distance_source": "records.distance_m (device odometer)",
        "gap_rule": f"a boundary is placed across a gap over "
                    f"{B.MAX_SAMPLE_GAP_S:.0f} s and the unit is flagged, "
                    f"not refused",
        "pace": "running-only: odometer distance / moving seconds inside "
                "rwd_run splits intersected with the unit; walking excluded "
                "from pace and counted in time_walking_s",
        "hr_running": "PRIMARY. Time-weighted raw HR over the same rwd_run "
                      "overlap the pace uses.",
        "hr_all_gaits": "time-weighted raw HR over the whole unit, every gait, "
                        "stopped time removed",
        "hr_raw": True, "hr_lag_shifted": False,
        "hr_weighting": f"each record to the next, capped at "
                        f"{B.MAX_WEIGHT_GAP_S:.0f} s (basic.record_weights)",
        "elevation_source": "records_enriched.altitude_smooth_m (smoothed "
                            "DEM) where terrain_usable, interpolated at the "
                            "boundaries -- the same gate grade-binned pace "
                            "applies",
        "stopped_time_excluded": True,
        "garmin_laps": "returned AS RECORDED for cross-check, never "
                       "substituted; only distance-triggered laps are miles",
        "grain": "records",
    }
