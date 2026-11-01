"""M7 time in HR zone BY GAIT (§17.2).

WHY IT EXISTS. Zone-2 time pooled across gaits cannot show whether a run/walk
structure is working, and a caller that needs the split and finds no tool will
go around the tools and get ungated numbers. **The fix for "no tool answers
this" is a tool, not a query.**

WHAT MAKES THE SPLIT WORTH HAVING. "N minutes in zone 2" pools a walk up a
climb with running on the flat. Those are different training — §6.2 keeps
the two gaits apart because they sit far apart in both cadence and speed — and
a run/walk structure exists to keep running minutes above a pace floor. Zone
time that cannot distinguish them cannot see whether the structure is working.

EVERY GATE THE OTHER PRIMITIVES APPLY:

  ONE ACTIVITY PER run_id     dual recording would count every minute twice
  stops excluded              the `stops` table only -- see below
  rwd_* splits for gait       Garmin's own segmentation, written on the watch
  hr_shifted at 40 s          HR lags effort, so the HR answering the gait at
                              time t is the HR at t + 40 (§6.1)
  quality flags honoured      where records_enriched exists (DEM-covered runs)
  the pipeline's weight map   basic.record_weights, so a Smart Recording
                              sample carries the seconds it stands for

STANDING IS A GAIT HERE, NOT PAUSED TIME, and that is a deliberate departure.
`record_weights` normally subtracts `stops` UNION `rwd_stand`; this passes the
`stops` rows alone, because "zone 2 while standing" is one of the three
quantities asked for and zeroing rwd_stand would make it unmeasurable. Long
deliberate stops are still removed.

BOUNDARIES COME FROM THE FILE FIRST (§14e). Each activity's own
`activity_hr_zones` rows are used when present; the athlete's dated
configuration is the fallback, and `boundary_source` says which was used. A
corpus can hold SEVERAL DISTINCT BOUNDARY SETS, so applying today's
configuration retroactively would silently restate history on the wrong scale.
Knowing the scale does NOT make pooling across a change valid — it makes it
detectable, and `boundary_set` travels with every result so a reader can see
when two runs are not on the same ruler.
"""

from __future__ import annotations

import json

from trail_factors.analysis.basic import MAX_WEIGHT_GAP_S, record_weights
from trail_factors.paths import ATHLETE_DIR

CALC_VERSION = 1

HR_LAG_S = 40.0
GAITS = ("rwd_run", "rwd_walk", "rwd_stand")
GAIT_LABEL = {"rwd_run": "running", "rwd_walk": "walking", "rwd_stand": "standing"}
ZONE_CONFIG = ATHLETE_DIR / "hr_zones.json"


def _configured_zones(when: str | None = None) -> tuple[list[tuple], str]:
    """The athlete's dated configuration, as [(zone, lo, hi)] plus a label."""
    if not ZONE_CONFIG.exists():
        return [], "none configured"
    sets = json.loads(ZONE_CONFIG.read_text())
    chosen = None
    for s in sets:
        if s.get("sport") != "RUNNING":
            continue
        if when is None or s.get("effective_from", "") <= when:
            chosen = s
    if chosen is None:
        return [], "none effective at this date"
    floors = {int(k): v for k, v in chosen["zone_floors"].items()}
    mx = chosen.get("max_hr", 220)
    out = []
    for z in sorted(floors):
        hi = floors.get(z + 1, mx + 1) - 1 if z + 1 in floors else mx
        out.append((z, floors[z], hi))
    return out, f"athlete config effective {chosen.get('effective_from')}"


def configured_zones() -> dict:
    """The CURRENT configured running zones, for display (e.g. spec_context).

    Read from hr_zones.json at call time. `status` is "unconfigured" when no
    running set is configured -- a stated absence, never a default ladder.
    The file's own per-activity boundaries remain the authority for any time
    in zone (zone_boundaries); this is only today's configuration.
    """
    try:
        zones, label = _configured_zones()
    except (OSError, ValueError, KeyError, TypeError) as e:
        return {
            "status": "unreadable",
            "zones": [],
            "source": f"{ZONE_CONFIG.name}: {type(e).__name__}",
        }
    if not zones:
        return {"status": "unconfigured", "zones": [], "source": label}
    return {"status": "ok", "zones": zones, "source": label}


def zone_boundaries(con, activity_id: str, run_date: str | None = None) -> dict:
    """This activity's zones — from ITS OWN file where possible (§14e)."""
    rows = con.execute(
        """
        SELECT zone, low_bpm, high_bpm, hr_calc_type FROM activity_hr_zones
        WHERE activity_id = ? ORDER BY zone""",
        [activity_id],
    ).fetchall()
    if rows and all(r[1] is not None and r[2] is not None for r in rows):
        return {
            "zones": [(r[0], r[1], r[2]) for r in rows],
            "boundary_source": "the activity's own FIT file",
            "hr_calc_type": rows[0][3],
            "boundary_set": [(r[1], r[2]) for r in rows],
        }
    zones, label = _configured_zones(run_date)
    return {
        "zones": zones,
        "boundary_source": f"FALLBACK: {label} (this file recorded none)",
        "hr_calc_type": None,
        "boundary_set": [(lo, hi) for _, lo, hi in zones],
    }


def time_in_zone_by_gait(con, activity_id: str) -> dict:
    """Seconds in each HR zone, split by gait, for one activity."""
    import numpy as np

    meta = con.execute(
        """
        SELECT run_id, strftime(start_time_utc, '%Y-%m-%d'), sampling_regime
        FROM activities WHERE activity_id = ?""",
        [activity_id],
    ).fetchone()
    if meta is None:
        raise LookupError(f"no activity {activity_id}")
    run_id, run_date, regime = meta
    bounds = zone_boundaries(con, activity_id, run_date)
    if not bounds["zones"]:
        return {
            "run_id": run_id,
            "status": "unavailable",
            "reason": "no zone boundaries: the file carries none and no "
            "athlete configuration is effective at this date",
            "by_gait": {},
            "filters": _filters(con, activity_id, bounds, regime),
        }

    hr_rows = con.execute(
        """
        SELECT timestamp, heart_rate FROM records
        WHERE activity_id = ? AND heart_rate IS NOT NULL ORDER BY timestamp
    """,
        [activity_id],
    ).fetchall()
    if len(hr_rows) < 2:
        return {
            "run_id": run_id,
            "status": "unavailable",
            "reason": "this activity has no heart rate",
            "by_gait": {},
            "filters": _filters(con, activity_id, bounds, regime),
        }
    t_arr = np.array([t.timestamp() for t, _ in hr_rows])
    h_arr = np.array([h for _, h in hr_rows], dtype=float)

    # STOPS ONLY, so standing stays measurable as its own gait.
    stops = [
        (a, b)
        for a, b in con.execute(
            "SELECT start_utc, end_utc FROM stops WHERE activity_id = ?", [activity_id]
        ).fetchall()
        if a is not None and b is not None and b > a
    ]
    weights = record_weights(con, activity_id, paused=stops)

    flagged = {
        r[0]
        for r in con.execute(
            """
        SELECT timestamp FROM records_enriched
        WHERE activity_id = ? AND coalesce(quality_flags, '') <> ''
    """,
            [activity_id],
        ).fetchall()
    }
    n_enriched = con.execute(
        "SELECT count(*) FROM records_enriched WHERE activity_id = ?", [activity_id]
    ).fetchone()[0]

    by_gait = {
        GAIT_LABEL[g]: {f"zone_{z}": 0.0 for z, _, _ in bounds["zones"]} for g in GAITS
    }
    for g in GAITS:
        by_gait[GAIT_LABEL[g]]["unclassified_s"] = 0.0
        by_gait[GAIT_LABEL[g]]["total_s"] = 0.0
    excluded_flagged_s = 0.0
    no_hr_s = 0.0

    for gait in GAITS:
        rows = con.execute(
            """
            SELECT r.timestamp FROM records r
            JOIN splits s ON s.activity_id = r.activity_id
                 AND r.timestamp BETWEEN s.start_time AND s.end_time
            WHERE r.activity_id = ? AND s.split_type = ?
        """,
            [activity_id, gait],
        ).fetchall()
        label = GAIT_LABEL[gait]
        for (ts,) in rows:
            w = weights.get(ts, 0.0)
            if w <= 0:
                continue
            if ts in flagged:
                excluded_flagged_s += w
                continue
            # HR LAGS EFFORT. The HR that answers the gait at time t is the HR
            # at t + 40 s -- attributing HR(t) would credit the previous
            # gait's cost to this one at every transition.
            want = ts.timestamp() + HR_LAG_S
            if want < t_arr[0] or want > t_arr[-1]:
                no_hr_s += w
                continue
            hr = float(np.interp(want, t_arr, h_arr))
            by_gait[label]["total_s"] += w
            placed = False
            for z, lo, hi in bounds["zones"]:
                if lo <= hr <= hi:
                    by_gait[label][f"zone_{z}"] += w
                    placed = True
                    break
            if not placed:
                by_gait[label]["unclassified_s"] += w

    zone2 = {g: v.get("zone_2", 0.0) / 60.0 for g, v in by_gait.items()}
    return {
        "run_id": run_id,
        "date": run_date,
        "status": "ok",
        "reason": None,
        "by_gait": by_gait,
        # THE HEADLINE the metric was asked for.
        "zone2_minutes": zone2,
        "zone2_total_minutes": sum(zone2.values()),
        "excluded_flagged_s": excluded_flagged_s,
        "no_hr_after_lag_s": no_hr_s,
        "quality_flags_available": n_enriched > 0,
        **{k: bounds[k] for k in ("boundary_source", "boundary_set", "hr_calc_type")},
        "filters": _filters(con, activity_id, bounds, regime),
    }


def _filters(con, activity_id: str, bounds: dict, regime: str | None) -> dict:
    return {
        "gait_source": "rwd_run / rwd_walk / rwd_stand splits",
        "split_types_pooled": False,
        "hr_lag_s": HR_LAG_S,
        "hr_lag_note": "the HR answering the gait at time t is the HR at "
        "t + 40 s; attributing HR(t) credits the previous "
        "gait's cost to this one at every transition",
        "weighting": "time, via the pipeline's shared record weight map",
        "max_weight_gap_s": MAX_WEIGHT_GAP_S,
        "stopped_time_excluded": True,
        "stopped_time_source": "the `stops` table ONLY -- rwd_stand is a GAIT "
        "here, not paused time, or standing zone time "
        "would be unmeasurable",
        "quality_flags_excluded": True,
        "quality_flags_available": bounds is not None,
        "boundary_source": bounds["boundary_source"],
        "boundary_set": bounds["boundary_set"],
        "sampling_regime": regime,
        "dual_source_rule": "one activity per run_id, garmin_watch preferred",
        "grain": "records, aggregated to (gait, zone) seconds",
        "note": "a corpus can hold several distinct boundary sets. Zone "
        "time is comparable only within one set -- `boundary_set` "
        "says which ruler this run was measured with. And where an "
        "athlete's habitual HR band straddles a zone line, small HR "
        "differences move large amounts of time across it (§6.2).",
    }
