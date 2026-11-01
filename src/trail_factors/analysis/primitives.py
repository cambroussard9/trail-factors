"""§6 analysis primitives the report reads (SPEC.md §12.5).

Every query here applies the §12.5 contract: one activity per run_id (dual
recording double-counts), stopped time already excluded upstream in
segment_efforts, terrain_usable per-portion, run and walk never pooled, and
path-derived speed rather than the grade-biased speed_mps (§9).
"""

from __future__ import annotations

import statistics

CALC_VERSION = 1

from trail_factors.transform.silver import ALL_FLAGS as _ALL_FLAGS
from trail_factors.units import pace_per_mile

# §6.7: pairs must be far enough apart in time that drift could show. The spec
# says 10-20 min; 10 is the floor so a short run can still produce a pair.
DRIFT_MIN_GAP_MIN = 10


def grade_binned_pace(con, activity_id: str) -> dict:
    """§6.2's deliverable: the full table, split by split_type.

    NEVER pooled. Pooled medians read as "pace does not vary with grade" purely
    because each bin mixes walking with running and the median lands between
    the two modes (§6.2).
    """
    rows = con.execute("""
        WITH d AS (
          SELECT e.activity_id, e.timestamp, e.grade_pct,
                 e.path_distance_m - lag(e.path_distance_m) OVER w AS dx,
                 date_diff('second', lag(e.timestamp) OVER w, e.timestamp) AS dt
          FROM records_enriched e
          WHERE e.activity_id = ? AND e.terrain_usable AND e.grade_pct IS NOT NULL
            -- §15.4: flagged records are EXCLUDED, not down-weighted. No
            -- partial credit -- an unflagged 22-sample trend beats a flagged
            -- 200-sample one.
            AND coalesce(e.quality_flags, '') = ''
          WINDOW w AS (PARTITION BY e.activity_id ORDER BY e.timestamp)
        )
        SELECT s.split_type, floor(d.grade_pct / 5) * 5 AS bin, count(*) AS n,
               median(d.dx / d.dt) AS mps, median(r.heart_rate) AS hr,
               -- RUNNING DYNAMICS PER BIN (added with the first device that
               -- records them).
               -- MEDIANS, like the pace beside them, so one artifact cannot
               -- move a bin. NULL on every FR245 run -- that watch records
               -- none of these -- which is an absence, not a zero.
               median(r.vertical_oscillation) AS vo,
               median(r.vertical_ratio) AS vr,
               median(r.stance_time) AS gct,
               median(r.step_length) AS sl
        FROM d
        JOIN records r ON r.activity_id = d.activity_id AND r.timestamp = d.timestamp
        JOIN splits s ON s.activity_id = d.activity_id
             AND d.timestamp BETWEEN s.start_time AND s.end_time
        WHERE d.dt BETWEEN 1 AND 20 AND d.dx >= 0
          AND s.split_type IN ('rwd_run', 'rwd_walk')
        GROUP BY 1, 2 HAVING count(*) >= 10 ORDER BY 1, 2
    """, [activity_id]).fetchall()
    return {
        # mps is the STORED quantity; pace_per_mile is its display form,
        # carried so a reader never converts decimal minutes by hand.
        "bins": [{"split_type": st, "grade_bin": b, "n": n, "mps": mps,
                  "pace_per_mile": pace_per_mile(mps), "hr": hr,
                  # UNITS ARE NOT THE OBVIOUS ONES and are named in the key
                  # itself, because a bare `vertical_oscillation: 80.0` reads
                  # as centimetres to anyone who has met the metric elsewhere.
                  "vertical_oscillation_mm": vo,
                  "vertical_ratio_pct": vr,
                  "stance_time_ms": gct,
                  "step_length_mm": sl}
                 for st, b, n, mps, hr, vo, vr, gct, sl in rows],
        "filters": {
            "split_type": ["rwd_run", "rwd_walk"],
            "split_types_pooled": False,
            "terrain_usable_required": True,
            "quality_flags_excluded": True,
            "running_dynamics_units": {"vertical_oscillation": "mm",
                                       "vertical_ratio": "percent",
                                       "stance_time": "ms",
                                       "step_length": "mm"},
            "running_dynamics_note": "NULL on any run recorded by a device "
                                     "without running dynamics (every FR245 "
                                     "run). Absent, never zero.",
            "quality_flags_honoured": list(_ALL_FLAGS),
            "grade_required": True,
            "speed_source": "path_derived",
            "max_sample_gap_s": 20,
            "min_samples_per_bin": 10,
            "stopped_time_excluded": False,
            "grain": "records",
            "note": "run and walk are reported separately and must never be "
                    "pooled (§6.2)."},
    }


# §6.7's stated pairing rule. These are the ONLY tolerances the primitive uses,
# and they are reported with every result: a reader cannot know what a drift
# number means without them.
DRIFT_MAX_DISTANCE_M = 20.0     # "same ground": <10-30 m apart
DRIFT_MIN_SEPARATION_S = 600    # ">=10-20 min apart"
DRIFT_SPEED_TOL_MPS = 0.35      # "speed matched within tolerance"
DRIFT_SPLIT_TYPE = "rwd_run"    # "both rwd_run" -- walking is NOT paired


def matched_terrain_drift(con, activity_id: str) -> dict:
    """§6.7: HR delta between RECORDS on the same ground, as bpm/hour.

    PAIRS RECORDS, NOT EFFORTS, and applies every filter §6.7 names: same
    ground within DRIFT_MAX_DISTANCE_M, at least DRIFT_MIN_SEPARATION_S apart,
    BOTH rwd_run, and speed matched within DRIFT_SPEED_TOL_MPS.

    The speed match and the rwd_run filter are what make it effort-matched. An
    earlier implementation paired whole SEGMENT EFFORTS instead and dropped
    both, which is a different quantity: on the same run it read several times
    higher than this one, because a 20 bpm difference across a 15 min gap
    extrapolates to +80 bpm/hour on its own. That measure still exists as
    `segment_effort_drift`, under its own name.

    Check its stability on your own reference run by varying the tolerances
    (§6.7): a figure that moves with the thresholds is measuring them.
    """
    import numpy as np

    rows = con.execute("""
        SELECT e.timestamp, e.path_distance_m, r.lat, r.lon, r.heart_rate,
               s.split_type
        FROM records_enriched e JOIN records r USING (activity_id, timestamp)
        LEFT JOIN splits s ON s.activity_id = e.activity_id
             AND e.timestamp BETWEEN s.start_time AND s.end_time
             -- The rwd_* partition only: the FR265's `interval_active` split
             -- spans the whole run and would match every record twice (§9).
             AND s.split_type LIKE 'rwd\\_%' ESCAPE '\\' 
        WHERE e.activity_id = ? AND r.lat IS NOT NULL AND r.heart_rate IS NOT NULL
          -- §15.4: excluded, not down-weighted.
          AND coalesce(e.quality_flags, '') = ''
        ORDER BY e.timestamp
    """, [activity_id]).fetchall()
    if len(rows) < 20:
        return {"value": None, "n": 0, "filters": drift_filters()}

    from trail_factors.transform import matcher as M
    xs, ys = M.to_utm([r[2] for r in rows], [r[3] for r in rows])
    secs = np.array([(r[0] - rows[0][0]).total_seconds() for r in rows])
    hr = np.array([r[4] for r in rows], dtype=float)
    dist = np.array([r[1] for r in rows], dtype=float)
    is_run = np.array([r[5] == DRIFT_SPLIT_TYPE for r in rows])

    # Path-derived speed (§9: never records.speed_mps).
    dt = np.diff(secs, prepend=secs[0] - 1)
    dx = np.diff(dist, prepend=dist[0])
    speed = np.where((dt > 0) & (dt <= 20), dx / np.maximum(dt, 1e-9), np.nan)

    ok = is_run & np.isfinite(speed)
    idx = np.flatnonzero(ok)
    rates = []
    for i in idx:
        later = idx[idx > i]
        if later.size == 0:
            continue
        gap = secs[later] - secs[i]
        m = ((gap >= DRIFT_MIN_SEPARATION_S)
             & (np.hypot(xs[later] - xs[i], ys[later] - ys[i]) <= DRIFT_MAX_DISTANCE_M)
             & (np.abs(speed[later] - speed[i]) <= DRIFT_SPEED_TOL_MPS))
        if m.any():
            rates.extend((hr[later][m] - hr[i]) / (gap[m] / 3600.0))
    return {"value": float(np.median(rates)) if rates else None,
            "n": len(rates), "filters": drift_filters()}


def drift_filters() -> dict:
    """The filters §6.7's drift applied, as FIELDS rather than prose.

    A chat reading a drift number cannot know what it is looking at unless the
    result says whether walking was paired, whether speed was matched, and how
    far apart the records had to be. Stated, not implied.
    """
    return {
        "split_type": DRIFT_SPLIT_TYPE,
        "walking_paired": False,
        "max_pair_distance_m": DRIFT_MAX_DISTANCE_M,
        "min_separation_s": DRIFT_MIN_SEPARATION_S,
        "speed_matched_within_mps": DRIFT_SPEED_TOL_MPS,
        "speed_source": "path_derived",
        "terrain_usable_required": False,
        "stopped_time_excluded": False,
        "quality_flags_excluded": True,
        "quality_flags_honoured": list(_ALL_FLAGS),
        "grain": "record pairs",
        "note": "records only, not whole efforts; see segment_effort_drift for "
                "the effort-level measure, which is a different quantity",
    }


def segment_effort_drift(con, activity_id: str,
                         direction: str | None = None) -> dict:
    """HR delta between whole EFFORTS on one slope, as bpm/hour.

    NOT §6.7. It pairs effort AVERAGES on a matched slope, applies no speed
    match and no split_type filter, and normalises by the gap between effort
    starts -- so a short gap extrapolates hard: +20 bpm over 15 min reads as
    +80 bpm/hour. It can read several times higher than §6.7 on the same run.

    Kept because terrain matching by SEGMENT IDENTITY is stronger than matching
    by a distance threshold, and because it works where §6.7 finds no pairs.
    But it is noisier (a handful of effort pairs against hundreds of record
    pairs on the same run) and must never be presented as "the" drift.

    IT IS ALSO THE ONLY DIRECTION-AWARE DRIFT THE DESIGN CAN HONESTLY OFFER,
    which is why `direction` lives here and not on §6.7. Its SQL joins
    `a.direction = b.direction`, so every pair it forms is same-direction BY
    CONSTRUCTION. §6.7 cannot do this: a direction is a property of a RECORD
    and a §6.7 pair has two, and a large share of §6.7 pairs join records
    travelling in OPPOSITE directions -- because §14b names out-and-back
    structure as that metric's own precondition, and an out-and-back is exactly
    what makes the revisit a reverse traversal. Filtering §6.7 to
    same-direction pairs discards most of them and can flip the ascent-descent
    sign between runs.

    NEVER PRESENT THIS AS "§6.7 BY DIRECTION". It is a different quantity with
    its own name, and the §6.7 mislabelling has already happened once (§6.7's
    reconciliation table).
    """
    rows = con.execute("""
        SELECT a.segment_id, a.direction,
               date_diff('second', a.start_utc, b.start_utc) / 3600.0 AS hours,
               b.avg_hr - a.avg_hr AS d_hr
        FROM segment_efforts a
        JOIN segment_efforts b
          ON a.segment_id = b.segment_id AND a.activity_id = b.activity_id
         AND a.direction = b.direction AND a.start_utc < b.start_utc
        WHERE a.activity_id = ?
          AND date_diff('minute', a.start_utc, b.start_utc) >= ?
          AND a.avg_hr IS NOT NULL AND b.avg_hr IS NOT NULL
          AND (? IS NULL OR a.direction = ?)
    """, [activity_id, DRIFT_MIN_GAP_MIN, direction, direction]).fetchall()
    rates = [d_hr / hours for _, _, hours, d_hr in rows if hours > 0]
    return {"value": statistics.median(rates) if rates else None,
            "n": len(rates), "pairs": rows,
            "filters": segment_drift_filters(direction)}


def segment_drift_filters(direction: str | None = None) -> dict:
    return {
        "split_type": None,
        "direction": direction,
        "same_direction_pairs_only": True,
        "walking_paired": True,
        "terrain_matched_by": "segment identity (place, side)",
        "min_separation_min": DRIFT_MIN_GAP_MIN,
        "speed_matched_within_mps": None,
        "hr_source": "effort mean, time-weighted, stopped time excluded",
        "stopped_time_excluded": True,
        # Reads segment_efforts, which aggregates records upstream. Per-record
        # §15.4 flags are NOT applied here -- stated rather than implied.
        "quality_flags_excluded": False,
        "grain": "effort pairs",
        "note": "NOT §6.7 and never to be called '§6.7 by direction'. Pairs "
                "are same-direction by construction, which is why direction "
                "is available here and not on §6.7. Normalising a short gap "
                "to an hour amplifies: +20 bpm over 15 min reads as "
                "+80 bpm/hour.",
        "ceiling_effect_caveat":
            "ASCENT AND DESCENT DO NOT START FROM THE SAME PLACE. An athlete "
            "who holds effort near a habitual HR begins an ascent close to "
            "that ceiling, with little room to rise, and a descent low, with "
            "room. So a descent drift much larger than the ascent drift can be "
            "REAL and still not measure eccentric cost: if the later efforts "
            "in both directions converge on the same HR, part of the gap is "
            "headroom, not physiology. Compare the opening HR by direction "
            "before reading the asymmetry as a cost ratio.",
    }


def efficiency_factor(con, activity_id: str) -> dict:
    """§6.8: speed per heartbeat on MATCHED terrain.

    Terrain is matched by segment identity, not by GAP. GAP is not validated
    (§12.2, §6.1), so an EF defined as GAP/HR would inherit an unvalidated
    transform; matching on the segment library achieves the same thing with
    measurements that stand on their own.

    Reported in m/s per bpm, aggregated over the run's segment efforts.
    """
    rows = con.execute("""
        SELECT segment_id, direction, length_m, duration_s, avg_hr
        FROM segment_efforts
        WHERE activity_id = ? AND avg_hr IS NOT NULL AND duration_s > 0
    """, [activity_id]).fetchall()
    efs = [(ln / dur) / hr for _, _, ln, dur, hr in rows if hr and hr > 0]
    return {"value": statistics.median(efs) if efs else None, "n": len(efs),
            "filters": {
                "split_type": None,
                "walking_included": True,
                "terrain_matched_by": "segment identity (place, side)",
                "speed_source": "segment length / moving duration",
                "stopped_time_excluded": True,
                "gap_used": False,
                "quality_flags_excluded": False,
                "grain": "segment efforts",
                "note": "NOT GAP/HR -- GAP is unvalidated (§12.2), so terrain "
                        "is matched by segment identity instead."}}


def climb_signature(con, activity_id: str) -> dict:
    """§6.5: the run as a sorted list of slopes.

    8 rollers and 3 sustained climbs have different signatures despite
    identical totals, which a single ascent number cannot express.
    """
    rows = con.execute("""
        SELECT direction, length_m, vertical_m, mean_grade_pct
        FROM slope_traversals WHERE activity_id = ?
        ORDER BY vertical_m DESC
    """, [activity_id]).fetchall()
    asc = [r for r in rows if r[0] == "ascent"]
    return {"n_slopes": len(rows), "n_ascents": len(asc),
            "n_descents": len(rows) - len(asc),
            "total_vertical_m": sum(r[2] for r in asc),
            "largest": rows[:5],
            "filters": {
                "unit": "slope (monotonic stretch), not prominence hill",
                "min_vertical_m": 8.0,
                "terrain_usable_required": True,
                "grain": "slope traversals",
                "note": "ascent totals here are DEM-derived; Garmin's are "
                        "amplitude-inflated 2.08x (§5a-bis.4)."}}


def percentile_of(value: float | None, population: list[float]) -> float | None:
    """Where `value` sits in `population`, 0-100.

    Presentation, not verdict: it is what lets a reader tell an unusual number
    from an ordinary one. Without it the reader has raw values and no scale.
    """
    pop = [p for p in population if p is not None]
    if value is None or len(pop) < 3:
        return None
    below = sum(1 for p in pop if p < value)
    ties = sum(1 for p in pop if p == value)
    return 100.0 * (below + 0.5 * ties) / len(pop)


def spread(population: list[float]) -> dict:
    """median / p25 / p75 / n for a comparison population."""
    pop = sorted(p for p in population if p is not None)
    if not pop:
        return {"median": None, "p25": None, "p75": None, "n": 0}
    def q(f):
        return pop[min(int(f * len(pop)), len(pop) - 1)]
    return {"median": statistics.median(pop), "p25": q(0.25), "p75": q(0.75),
            "n": len(pop)}
