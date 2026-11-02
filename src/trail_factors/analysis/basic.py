"""§6.0 basic metrics: the run's own description (SPEC.md §6.0).

These are the anchors, not the analysis. They answer "what happened" -- how far,
how fast, how hard, how many steps -- and everything in §6.1-§6.9 is read
against them. None of them was exposed before this module, and a coaching chat
with no average HR and no cadence was reasoning about drift and efficiency
without the quantities those are deviations FROM.

THREE THINGS DECIDED THE SHAPE OF THIS MODULE, all measured on the
development corpus.

1. `records_enriched` COVERS ONLY DEM-TILED ACTIVITIES. It is built from
   `records JOIN dem_elevations`, so it exists only where a DEM tile does --
   on the development corpus, MOST garmin_watch activities had ZERO enriched
   rows. The count is corpus-dependent and is computed by terrain_state(),
   never written down here. Anything read through it silently reports on a
   fraction of the corpus. So HR, cadence, pace and the windowed bests all
   read `records`, and only the grade-binned table (which genuinely needs
   `grade_pct`) reads `records_enriched`.

2. THE SPLITS CARRY DURATIONS FOR EVERY RUN. `rwd_run`/`rwd_walk`/`rwd_stand`
   splits with non-null `total_timer_time` exist on every watch-recorded
   garmin_watch running activity regardless of sampling regime -- they are Garmin's own
   segmentation, written once per split, not per record. Run/walk/stand time is
   therefore NOT sampling-regime dependent, and neither are the class paces
   derived from it. (The coaching chat believed these were 1 Hz-only.)

3. THE DEVICE ODOMETER IS THE ONLY WHOLE-CORPUS DISTANCE. `records.distance_m`
   is non-null on every record; `path_distance_m` exists only where
   enrichment does, and on a low-coverage run it is not a whole-run distance at
   all -- on one run at 25% coverage the path integral reached well under half
   the odometer's distance. Where both exist and coverage is adequate they
   agree to within about 1%. The windowed
   bests therefore use the odometer, declare `distance_source`, and carry
   `gps_coverage_fraction` so a wrist-derived best is visible rather than
   implied. This is NOT the §9 prohibition, which is on `records.speed_mps` --
   the fused instantaneous speed -- not on the cumulative odometer.
"""

from __future__ import annotations

import statistics

from trail_factors.units import METERS_PER_MILE, pace_per_mile

CALC_VERSION = 1

# A sample's weight is the gap to the next sample, CAPPED. A paused watch
# timer can leave a gap of many minutes: uncapped, one HR reading would carry
# all of it as weight. 20 s is the largest
# legitimate Smart Recording gap observed (§9), so the cap only ever fires on
# a genuine discontinuity.
MAX_WEIGHT_GAP_S = 20.0

# A class pace (running-only, walking-only) needs enough of that class to be
# stable. FITTED to leave-one-split-out jackknife SD of the class pace, by
# total class time, over the development corpus's garmin activities:
#     0-30 s   median SD 304 s/mile      120-300 s  median SD  59 s/mile
#     60-120 s median SD  86 s/mile      300-900 s  median SD  28 s/mile
# The stated tolerance is 60 s/mile, which 120-300 s meets and 60-120 s does
# not. There is no sharp knee -- the dispersion falls smoothly -- so this is a
# tolerance made explicit, not a discovered boundary. Many walk classes fall
# below it and are GATED, which is the honest outcome: a walking pace from
# a dozen seconds of walking is not a walking pace.
MIN_CLASS_TIME_S = 120.0

# Windowed bests. The athlete's rule: never an instantaneous maximum. Measured
# on the development corpus, the single-step path-derived maximum reached
# physically impossible speeds -- over 10 m/s. Even 5 s and 10 s windows stayed
# contaminated. 30 s is the shortest window where every activity landed in a
# physically plausible band.
BEST_WINDOW_S = 30.0
BEST_WINDOWS_M = (METERS_PER_MILE / 4, METERS_PER_MILE)

# A window may not span a sample gap larger than this: interpolating distance
# across a 60 s hole invents a straight line at whatever speed closes it.
# Same 20 s as the weight cap and for the same reason.
MAX_SAMPLE_GAP_S = 20.0

# Cadence. RESOLVED against the Garmin oracle (§9): steps/min is
# (cadence_raw_rpm + fractional_cadence) x 2. The competing candidate,
# cadence x 2 + fractional_cadence, is REFUTED BY PARITY -- it can only produce
# even integers or half-integers, and over half of Garmin's own
# maxRunningCadenceInStepsPerMinute values are ODD integers.
CADENCE_STEPS_PER_STRIDE = 2

# Grade bins mirror §6.2's exactly, so the cadence table and the pace table can
# be read side by side.
GRADE_BIN_PCT = 5.0

# A grade bin needs this much weighted time before its cadence is reported.
# Ten SAMPLES (§6.2's gate) measures the sampling regime, not the evidence:
# 10 samples is ~50 s at Smart Recording and 10 s at 1 Hz. Time is the
# regime-portable unit.
MIN_BIN_TIME_S = 120.0


# --------------------------------------------------------------------------
# shared: moving-time weights
# --------------------------------------------------------------------------


def paused_intervals(con, activity_id: str) -> list[tuple]:
    """Every interval the athlete was not moving, MERGED.

    TWO SOURCES, because neither is sufficient alone. The `stops` table holds
    very few rows -- it is deliberately conservative (§4.3: 120 s minimum,
    frozen distance AND zero cadence AND zero speed), so it catches long
    deliberate stops and nothing else. Garmin's own `rwd_stand` splits appear
    on most activities and catch the short ones.

    MERGED rather than subtracted independently: the two overlap, and
    subtracting each in turn would remove the same second twice and can drive a
    weight negative.
    """
    rows = con.execute(
        """
        SELECT start_utc, end_utc FROM stops WHERE activity_id = ?
        UNION ALL
        SELECT start_time, end_time FROM splits
        WHERE activity_id = ? AND split_type = 'rwd_stand'
    """,
        [activity_id, activity_id],
    ).fetchall()
    spans = sorted((a, b) for a, b in rows if a is not None and b is not None and b > a)
    merged: list[list] = []
    for a, b in spans:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged]


def moving_seconds(a, b, paused: list[tuple]) -> float:
    """Seconds between two timestamps with merged paused time removed."""
    total = (b - a).total_seconds()
    for s0, s1 in paused:
        lo, hi = max(a, s0), min(b, s1)
        if hi > lo:
            total -= (hi - lo).total_seconds()
    return max(total, 0.0)


def record_weights(
    con,
    activity_id: str,
    cap: float = MAX_WEIGHT_GAP_S,
    paused: list[tuple] | None = None,
) -> dict:
    """timestamp -> the moving seconds it stands for, over the FULL series.

    WEIGHTS ARE COMPUTED BEFORE ANY FILTER, and this function exists so that
    cannot be got wrong. Deriving a sample's weight from the gap to the next
    SURVIVING sample makes the weight a property of the filter: a running
    record that happens to sit just before a walk block would carry the walk's
    duration. Measured on a 1 Hz run, that mistake inflated the weight sum by
    about a fifth and moved mean running cadence by several spm, in the
    direction of whatever the transition samples happened to read.

    Each record carries the moving seconds to the NEXT RECORD, capped. The cap
    is what stops a paused-timer gap (which can last minutes) giving a
    single reading all of it as weight. The final record closes on the run's
    end, so a left-endpoint rule does not silently discard the closing interval.

    `paused` overrides the intervals to subtract; pass [] for TIMER-TIME
    weights rather than moving-time ones. That is not a convenience -- it is
    what makes Garmin's own basis expressible. Garmin's
    averageRunningCadenceInStepsPerMinute is over timer time INCLUDING
    standing, so reproducing it (median +0.06 spm on the development corpus) requires
    weights that do not subtract rwd_stand. Our own metrics use moving time and
    are deliberately a different quantity.
    """
    rows = con.execute(
        """
        SELECT r.timestamp,
               coalesce(a.valid_end_utc, max(r.timestamp) OVER ()) AS run_end
        FROM records r JOIN activities a USING (activity_id)
        WHERE r.activity_id = ? ORDER BY r.timestamp
    """,
        [activity_id],
    ).fetchall()
    if not rows:
        return {}
    if paused is None:
        paused = paused_intervals(con, activity_id)
    end = rows[-1][1]
    out = {}
    for i, (t, _) in enumerate(rows):
        nxt = rows[i + 1][0] if i + 1 < len(rows) else end
        out[t] = min(moving_seconds(t, nxt, paused), cap) if nxt > t else 0.0
    return out


def weighted_mean(samples: list[tuple], weights: dict) -> tuple[float, float]:
    """Weighted (sum, weight) over [(timestamp, value)] using a weight MAP.

    `samples` may be filtered to any subset; `weights` must come from
    record_weights() over the unfiltered series. Taking the map rather than
    deriving gaps from `samples` is what makes the filtered-lag error above
    impossible to write here.

    Returns (0.0, 0.0) rather than raising when nothing is usable -- the caller
    tests the weight, never the sum, because a genuinely zero sum differs from
    no data at all (CLAUDE.md).
    """
    num = den = 0.0
    for t, v in samples:
        if v is None:
            continue
        dt = weights.get(t, 0.0)
        num += v * dt
        den += dt
    return num, den


def _flag_note(con, activity_id: str) -> dict:
    """Whether §15.4 per-record flags were even computable for this activity.

    They live on `records_enriched`, which exists only where a DEM tile does.
    Saying "quality_flags_excluded: false" without saying WHY would read as a
    choice when on most activities it is an absence.
    """
    n = con.execute(
        "SELECT count(*) FROM records_enriched WHERE activity_id = ?", [activity_id]
    ).fetchone()[0]
    return {
        "quality_flags_excluded": False,
        "quality_flags_available": n > 0,
        "quality_flags_reason": "flags live on records_enriched, which is built from "
        "records JOIN dem_elevations and covers only DEM-tiled "
        "activities; these metrics read `records` so they cover all "
        "of them"
        if n == 0
        else "flags exist for this activity but are not applied: none of "
        "the five flags flags the HR or cadence channel (§15.4)",
    }


# --------------------------------------------------------------------------
# §6.0a average heart rate
# --------------------------------------------------------------------------


def average_hr(con, activity_id: str) -> dict:
    """Run-level average HR: TIME-WEIGHTED, stopped time excluded.

    AN ANCHOR, NOT A SIGNAL, and the athlete said so before it was built. The
    corpus bears it out: HR is nearly flat across grade (§6.2's finding 1), so
    the run mean carries almost no terrain information. It is here because
    every drift, efficiency and decoupling number is a deviation from it, and a
    chat that cannot see the level cannot size the deviation.

    Run and walk are POOLED in the headline figure -- it is a whole-run anchor,
    and a total over a total is well defined -- but the decomposition is
    returned beside it so the pooling is visible rather than hidden. §6.2's
    ban on pooling is about GRADE BINS, where mixing two gait modes puts the
    median between the modes and reads as "pace does not vary with grade".
    """
    rows = con.execute(
        """
        SELECT timestamp, heart_rate FROM records
        WHERE activity_id = ? ORDER BY timestamp
    """,
        [activity_id],
    ).fetchall()
    if len(rows) < 2:
        return {
            "value": None,
            "n": 0,
            "status_hint": "unavailable",
            "reason": "fewer than two records",
            "filters": _hr_filters(con, activity_id),
        }

    weights = record_weights(con, activity_id)
    num, den = weighted_mean(rows, weights)
    if den <= 0:
        return {
            "value": None,
            "n": 0,
            "status_hint": "unavailable",
            "reason": "no heart rate on this run",
            "filters": _hr_filters(con, activity_id),
        }

    by_class = {}
    for cls in ("rwd_run", "rwd_walk"):
        sub = con.execute(
            """
            SELECT r.timestamp, r.heart_rate
            FROM records r JOIN splits s ON s.activity_id = r.activity_id
                 AND r.timestamp BETWEEN s.start_time AND s.end_time
            WHERE r.activity_id = ? AND s.split_type = ? ORDER BY r.timestamp
        """,
            [activity_id, cls],
        ).fetchall()
        # The SAME weight map, not a re-derivation from this filtered subset.
        n2, d2 = weighted_mean(sub, weights)
        # GATED ON THE SAME MINIMUM AS THE CLASS PACES. Ten seconds of walking
        # does not produce a walking heart rate any more than it produces a
        # walking pace, and an unqualified walking HR next to a running HR
        # reads as a real contrast when it is one brief transition.
        by_class[cls] = (n2 / d2) if d2 >= MIN_CLASS_TIME_S else None

    n_hr = sum(1 for _, h in rows if h is not None)
    return {
        "value": num / den,
        "n": n_hr,
        "weight_seconds": den,
        "avg_hr_running": by_class["rwd_run"],
        "avg_hr_walking": by_class["rwd_walk"],
        "filters": _hr_filters(con, activity_id),
    }


def _hr_filters(con, activity_id: str) -> dict:
    return {
        "weighting": "time",
        "max_weight_gap_s": MAX_WEIGHT_GAP_S,
        "stopped_time_excluded": True,
        "stopped_time_source": "stops table UNION rwd_stand splits, merged",
        "stopped_time_resolution": "split boundary timestamps, 1 s",
        "split_type": None,
        "split_types_pooled": True,
        "pooling_note": "whole-run anchor; avg_hr_running and avg_hr_walking "
        "are returned beside it so the pooling is visible",
        "source_table": "records",
        "grain": "records",
        **_flag_note(con, activity_id),
    }


# --------------------------------------------------------------------------
# §6.0b run / walk / stand durations, and the three paces
# --------------------------------------------------------------------------


def durations(con, activity_id: str) -> dict:
    """Run, walk and stand time from the rwd_* splits' total_timer_time.

    REGIME-INDEPENDENT. Splits are Garmin's own run/walk segmentation, written
    once per split rather than per record, so these durations are identical in
    kind on a Smart Recording file and a 1 Hz one. Every watch-recorded
    running activity carries them.

    NEVER `end_time - start_time` and never `sum(duration_s)`: split boundary
    timestamps are 1-second resolution and truncate, which understated nearly
    every split-type group and shortened `moving_time_s` by over a minute on
    some runs (CLAUDE.md). Some splits have end_time == start_time while
    carrying real timer time.

    ZERO IS NOT MISSING. Some activities have no rwd_walk split and some no
    rwd_stand; those are genuinely 0 s of walking or standing, not absent data.
    `n_splits` per class is returned so the two can be told apart -- a class
    with n_splits 0 and seconds 0.0 is a real zero.
    """
    rows = dict(
        (st, (tt, dm, n))
        for st, tt, dm, n in con.execute(
            """
        SELECT split_type, sum(total_timer_time), sum(total_distance_m), count(*)
        FROM splits WHERE activity_id = ? AND split_type LIKE 'rwd_%'
        GROUP BY 1""",
            [activity_id],
        ).fetchall()
    )
    if not rows:
        # DERIVED, NOT ASSERTED. See no_splits_reason.
        why = no_splits_reason(con, activity_id)
        return {
            "value": None,
            "status_hint": "unavailable",
            "no_splits_cause": why["cause"],
            "reason": f"this activity carries no rwd_* splits: {why['reason']}",
            "filters": _duration_filters(),
        }

    # TWO KINDS OF NOTHING, AND THEY ARE NOT THE SAME FACT.
    #
    # The class being ABSENT is a real zero -- some activities never walk,
    # some never stand -- and that is what the `rows.get` default says.
    #
    # The class being PRESENT while its sum is NULL is not zero: the splits
    # exist and the device recorded no distance for them. Measured: the
    # FR265 writes a zero-duration `rwd_stand` split with no distance
    # field, and `sum()` over a single NULL returns NULL. `float(None)` then
    # raised TypeError and took the WHOLE report down -- every other section of
    # that report was computable.
    #
    # Coercing it to 0.0 would be the `value or default` bug this codebase
    # forbids by name: a distance that was not measured would enter the record
    # as a distance that was measured and was zero, and nothing downstream
    # could tell the difference. So it is None, and `unavailable_fields` says
    # which and why.
    out = {}
    unavailable = {}
    for cls, key in (("rwd_run", "run"), ("rwd_walk", "walk"), ("rwd_stand", "stand")):
        present = cls in rows
        tt, dm, n = rows.get(cls, (0.0, 0.0, 0))
        out[f"{key}_s"] = float(tt) if tt is not None else None
        out[f"{key}_distance_m"] = float(dm) if dm is not None else None
        out[f"n_{key}_splits"] = int(n)
        for field, v in ((f"{key}_s", tt), (f"{key}_distance_m", dm)):
            if v is None and present:
                unavailable[field] = (
                    f"{n} {cls} split(s) exist and none carries this field; "
                    f"the device did not record it. NOT a zero."
                )
    out["unavailable_fields"] = unavailable

    # PARTIAL SUMS ARE ALSO NOT WHOLE SUMS. sum() SKIPS nulls, so a class where
    # some splits carry a distance and some do not reports the subset without
    # saying so -- understating with ONE SIGN, which accumulates rather than
    # averaging away. Measured on several FR265 class-groups.
    partial = {}
    for cls, key in (("rwd_run", "run"), ("rwd_walk", "walk"), ("rwd_stand", "stand")):
        if cls not in rows:
            continue
        n_dist = con.execute(
            "SELECT count(total_distance_m), count(*) FROM splits "
            "WHERE activity_id = ? AND split_type = ?",
            [activity_id, cls],
        ).fetchone()
        if 0 < n_dist[0] < n_dist[1]:
            partial[f"{key}_distance_m"] = (
                f"summed over {n_dist[0]} of {n_dist[1]} splits; the rest "
                f"carry no distance, so this is a LOWER BOUND"
            )
    out["partial_sums"] = partial

    timer = con.execute(
        "SELECT timer_time_s, duration_s FROM activities WHERE activity_id = ?",
        [activity_id],
    ).fetchone()
    # The total sums what EXISTS and declares what it could not include,
    # rather than treating an unmeasured class as 0 s.
    parts = [out["run_s"], out["walk_s"], out["stand_s"]]
    total = sum(v for v in parts if v is not None)
    out["split_total_s"] = total
    out["split_total_complete"] = all(v is not None for v in parts)
    out["timer_time_s"] = timer[0]
    # The splits tile the timer almost exactly: median |difference| well under
    # a tenth of a second, worst a few seconds. Reported rather than asserted, so a
    # future file that does NOT tile is visible instead of silently absorbed.
    out["tiling_residual_s"] = (total - timer[0]) if timer[0] is not None else None
    out["paused_s"] = (
        (timer[1] - timer[0]) if timer[0] is not None and timer[1] is not None else None
    )
    out["filters"] = _duration_filters()
    return out


def _duration_filters() -> dict:
    return {
        "duration_source": "splits.total_timer_time",
        "never": "end_time - start_time, and never sum(splits.duration_s)",
        "sampling_regime_dependent": False,
        "regime_note": "splits are written once per split, not per record, so "
        "these durations do not depend on Smart Recording vs 1 Hz",
        "split_types_pooled": False,
        "zero_vs_missing": "n_*_splits distinguishes a real 0 s from no data; "
        "a field the device never wrote is None and is named "
        "in unavailable_fields, never coerced to 0",
        "grain": "splits",
    }


def paces(con, activity_id: str) -> dict:
    """Three paces, never pooled: overall, running-only, walking-only.

    OVERALL IS GARMIN CONTINUITY, and that is a specific denominator rather
    than a vague intention. Measured against the cached Garmin listing:
    `activities.timer_time_s` IS Garmin's `duration` (max
    difference 0.0002 s) and `activities.duration_s` IS its `elapsedDuration`
    (same). So distance / timer_time_s is the number the Garmin app shows.
    `activities.moving_time_s` is NOT Garmin's `movingDuration` -- they differ
    by seconds to about a minute -- and it is also circular here,
    because `src/trail_factors/ingest/load.py` DEFINES it as sum(rwd_run + rwd_walk).

    §4.3 requires elapsed and moving never be reported one without the other,
    so both are returned; `overall` is the Garmin-continuity one and is named.

    RUNNING AND WALKING COME FROM THE SPLITS and are never pooled with each
    other or with the overall. A class below MIN_CLASS_TIME_S is GATED with its
    time stated, not dropped and not returned as a bare null.
    """
    d = durations(con, activity_id)
    a = con.execute(
        """
        SELECT total_distance_m, duration_s, timer_time_s, moving_time_s,
               coverage_fraction
        FROM activities WHERE activity_id = ?""",
        [activity_id],
    ).fetchone()
    if a is None:
        raise LookupError(f"no activity {activity_id}")
    dist, elapsed_s, timer_s, moving_s, cov = a

    def entry(name, metres, secs, source):
        if metres is None or secs is None or secs <= 0:
            return {
                "name": name,
                "status": "unavailable",
                "mps": None,
                "pace_per_mile": None,
                "seconds": secs,
                "meters": metres,
                "source": source,
                "reason": "no distance or no duration for this class",
            }
        if metres <= 0:
            return {
                "name": name,
                "status": "gated",
                "mps": None,
                "pace_per_mile": None,
                "seconds": secs,
                "meters": metres,
                "source": source,
                "reason": f"{secs:.0f} s of this class covered no distance",
            }
        return {
            "name": name,
            "status": "ok",
            "mps": metres / secs,
            "pace_per_mile": pace_per_mile(metres / secs),
            "seconds": secs,
            "meters": metres,
            "source": source,
        }

    def class_entry(name, key):
        secs, metres = d.get(f"{key}_s"), d.get(f"{key}_distance_m")
        if secs is None:
            return {
                "name": name,
                "status": "unavailable",
                "mps": None,
                "pace_per_mile": None,
                "seconds": None,
                "meters": None,
                "source": "splits",
                "reason": "no rwd_* splits on this activity",
            }
        if secs < MIN_CLASS_TIME_S:
            return {
                "name": name,
                "status": "gated",
                "mps": None,
                "pace_per_mile": None,
                "seconds": secs,
                "meters": metres,
                "source": "splits",
                "reason": (
                    f"{secs:.0f} s of {key} is below the "
                    f"{MIN_CLASS_TIME_S:.0f} s minimum; below it the "
                    f"class pace is unstable to worse than "
                    f"60 s/mile under leave-one-split-out"
                ),
            }
        return entry(name, metres, secs, "splits")

    out = [
        entry("overall", dist, timer_s, "activities.timer_time_s (Garmin duration)"),
        entry(
            "overall_elapsed",
            dist,
            elapsed_s,
            "activities.duration_s (Garmin elapsedDuration)",
        ),
        class_entry("running", "run"),
        class_entry("walking", "walk"),
    ]
    return {"paces": out, "filters": _pace_filters(cov, d)}


def _pace_filters(coverage_fraction, d: dict) -> dict:
    return {
        "split_types_pooled": False,
        "overall_denominator": "timer_time_s == Garmin `duration` "
        "(verified against the Garmin listing, max diff 0.0002 s)",
        "class_duration_source": "splits.total_timer_time",
        "class_distance_source": "splits.total_distance_m",
        "min_class_time_s": MIN_CLASS_TIME_S,
        "stopped_time_excluded": False,
        "stopped_time_note": "rwd_stand time is excluded from the CLASS paces "
        "by construction (it is its own class) but is "
        "inside the overall, which is Garmin's own basis",
        "quality_flags_excluded": False,
        "quality_flags_reason": "splits are Garmin's segmentation and sit "
        "upstream of records_enriched, where the flags "
        "live -- they cannot reach this grain",
        "gps_coverage_fraction": coverage_fraction,
        "distance_provenance": _provenance(coverage_fraction),
        "sampling_regime_dependent": False,
        "grain": "splits",
    }


def _provenance(coverage_fraction) -> str:
    """Where the distance came from. NOT a boolean -- a cliff at zero would
    call a 1%-coverage run 'gps' and a 0% one 'wrist' (SPEC §9)."""
    if coverage_fraction is None:
        return "unknown"
    if coverage_fraction >= 0.95:
        return "gps"
    if coverage_fraction > 0.0:
        return "mixed"
    return "wrist_accelerometer"


# --------------------------------------------------------------------------
# §6.0c windowed best paces
# --------------------------------------------------------------------------


def windowed_bests(con, activity_id: str) -> dict:
    """Best 30 s, best 0.25 mi, best 1 mi -- sliding windows, never a maximum.

    AN INSTANTANEOUS MAXIMUM IS A JITTER MAXIMUM and the corpus says so: the
    single-step maximum reached physically impossible speeds (over 10 m/s) on
    the development corpus. Even 5 s and 10 s windows stayed contaminated. The
    three windows the athlete asked for are the shortest ones that hold.

    BOUNDARIES ARE INTERPOLATED to hit exactly 30 s / exactly a quarter mile /
    exactly a mile. The alternative -- take the smallest window that meets or
    exceeds the target -- reads as the conservative choice and is not: it
    reports a DIFFERENT QUANTITY on every run (best-0.25 mi windows covered
    402.9-415.9 m, so "quarter mile" would mean 3.4% further on one run than
    another) and its error is two-signed, because a longer window reads FASTER
    when the athlete was accelerating through the boundary. Against the 1 Hz
    ground truth, decimation trials put interpolation ahead at every window
    size: mean absolute error 3.03 vs 4.70 s/mile at 30 s, 1.78 vs 2.66 at
    0.25 mi, 0.30 vs 0.36 at 1 mile.

    QUALITY FLAGS ARE NOT APPLIED, and the reason is measured rather than
    chosen: 100% of one-mile windows on every activity that has a mile contain
    at least one flagged record, so a reject-on-any-flag rule returns nothing,
    ever. Excluding the flagged record from the middle of a window and joining
    its neighbours is exactly the straight-line invention the gap rule exists
    to prevent. So the flag content of the winning window is REPORTED as a
    field instead. That matters here more than elsewhere because a MAXIMUM
    selects for positive measurement error in a way a mean does not.
    """
    import numpy as np

    rows = con.execute(
        """
        SELECT timestamp, distance_m FROM records
        WHERE activity_id = ? AND distance_m IS NOT NULL ORDER BY timestamp
    """,
        [activity_id],
    ).fetchall()
    cov = con.execute(
        "SELECT coverage_fraction FROM activities WHERE activity_id = ?", [activity_id]
    ).fetchone()
    cov = cov[0] if cov else None
    base = _best_filters(con, activity_id, cov)
    if len(rows) < 3:
        return {
            "windows": [],
            "status_hint": "unavailable",
            "reason": "fewer than three records with distance",
            "filters": base,
        }

    t0 = rows[0][0]
    t = np.array([(r[0] - t0).total_seconds() for r in rows], dtype=float)
    d = np.array([float(r[1]) for r in rows], dtype=float)
    # The odometer must be non-decreasing for the interpolation to mean
    # anything. Any decrease is a device artifact; it is clamped and COUNTED,
    # never silently smoothed.
    n_backwards = int((np.diff(d) < 0).sum())
    d = np.maximum.accumulate(d)

    paused = paused_intervals(con, activity_id)
    # A step is unusable if it spans a sample gap over the cap or overlaps
    # non-moving time. Windows are built only inside maximal clean stretches.
    gaps = np.diff(t)
    bad = gaps > MAX_SAMPLE_GAP_S
    for i in range(len(rows) - 1):
        if bad[i]:
            continue
        a, b = rows[i][0], rows[i + 1][0]
        if any(min(b, s1) > max(a, s0) for s0, s1 in paused):
            bad[i] = True
    blocks, start = [], 0
    for i, is_bad in enumerate(bad):
        if is_bad:
            if i + 1 - start >= 2:
                blocks.append((start, i + 1))
            start = i + 1
    if len(t) - start >= 2:
        blocks.append((start, len(t)))

    windows = []
    windows.append(_best_time_window(t, d, blocks, BEST_WINDOW_S))
    for metres in BEST_WINDOWS_M:
        windows.append(_best_distance_window(t, d, blocks, metres))

    for w in windows:
        if w["mps"] is not None:
            w["pace_per_mile"] = pace_per_mile(w["mps"])
            w.update(
                _window_composition(
                    con, activity_id, t0, w["start_elapsed_s"], w["end_elapsed_s"]
                )
            )
    return {
        "windows": windows,
        "n_backwards_odometer_steps": n_backwards,
        "n_clean_blocks": len(blocks),
        "filters": base,
    }


def distance_inverse(t, d):
    """(distinct odometer values, FIRST time at each, LAST time at each).

    The odometer is flat while standing, so one distance can hold for a span
    of time. Between two distinct values the athlete is moving, so
    time-at-distance rises along (x_i, last_i) -> (x_{i+1}, first_{i+1}).
    `d` must be non-decreasing.
    """
    import numpy as np

    d = np.asarray(d, dtype=float)
    t = np.asarray(t, dtype=float)
    xu, idx_first = np.unique(d, return_index=True)
    idx_last = len(d) - 1 - np.unique(d[::-1], return_index=True)[1]
    return xu, t[idx_first], t[idx_last]


def time_at_distance(inv, q, end: bool):
    """Time at distance q (scalar or array): the FIRST arrival when `end`,
    the LAST departure when not. THE one rule for "when was the athlete at
    this distance", shared by the windowed bests and mile_splits: a stretch
    STARTS when the athlete leaves its start distance and ENDS when they first
    reach its end distance -- the shortest time to cover it. Off a
    breakpoint the two coincide; AT one, the start takes last_i and the end
    first_i."""
    import numpy as np

    xu, first_t, last_t = inv
    q = np.asarray(q, dtype=float)
    i = np.searchsorted(xu, q, side="left" if end else "right") - 1
    i = np.clip(i, 0, xu.size - 2)
    x0, x1 = xu[i], xu[i + 1]
    t0, t1 = last_t[i], first_t[i + 1]
    out = t0 + (t1 - t0) * (q - x0) / (x1 - x0)
    out = np.where(end & (q <= xu[0] + 1e-12), first_t[0], out)
    out = np.where(~np.bool_(end) & (q >= xu[-1] - 1e-12), last_t[-1], out)
    return out


def _best_time_window(t, d, blocks, target_s: float) -> dict:
    """Fastest distance covered in exactly `target_s`, interpolated at the ends.

    D is piecewise linear in t, so (D(s + T) - D(s)) / T is maximised at a
    breakpoint: either the window's start or its end sits on a sample. Both
    families are enumerated, which makes the optimum exact rather than searched.
    """
    import numpy as np

    best = None
    for lo, hi in blocks:
        tb, db = t[lo:hi], d[lo:hi]
        if tb[-1] - tb[0] < target_s:
            continue
        for anchor_is_end in (True, False):
            if anchor_is_end:
                ends = tb
                starts = tb - target_s
            else:
                starts = tb
                ends = tb + target_s
            ok = (starts >= tb[0] - 1e-9) & (ends <= tb[-1] + 1e-9)
            if not ok.any():
                continue
            s, e = starts[ok], ends[ok]
            gained = np.interp(e, tb, db) - np.interp(s, tb, db)
            j = int(np.argmax(gained))
            if best is None or gained[j] > best[0]:
                best = (float(gained[j]), float(s[j]), float(e[j]))
    if best is None:
        return {
            "window": f"best_{target_s:.0f}s",
            "target_s": target_s,
            "mps": None,
            "pace_per_mile": None,
            "status": "unavailable",
            "reason": (
                f"no {target_s:.0f} s stretch free of sample gaps "
                f"over {MAX_SAMPLE_GAP_S:.0f} s and of stopped time"
            ),
        }
    gained, s, e = best
    return {
        "window": f"best_{target_s:.0f}s",
        "target_s": target_s,
        "status": "ok",
        "mps": gained / target_s,
        "pace_per_mile": None,
        "meters": gained,
        "seconds": target_s,
        "start_elapsed_s": s,
        "end_elapsed_s": e,
    }


def _best_distance_window(t, d, blocks, target_m: float) -> dict:
    """Fastest time to cover exactly `target_m`, interpolated at the ends."""
    import numpy as np

    label = (
        "best_1mi"
        if abs(target_m - METERS_PER_MILE) < 1e-6
        else f"best_{target_m / METERS_PER_MILE:.2f}mi"
    )
    best = None
    for lo, hi in blocks:
        tb, db = t[lo:hi], d[lo:hi]
        if db[-1] - db[0] < target_m:
            continue
        # THE WINDOW STARTS AT THE LAST TIME AT ITS START DISTANCE AND ENDS AT
        # THE FIRST TIME AT ITS END DISTANCE -- the shortest time to cover the
        # distance (athlete's rule). The odometer is flat while
        # standing, so a distance can hold for a span of time: x_i from
        # first_i to last_i. Between two distinct values the athlete is MOVING,
        # so time-at-distance rises along (x_i, last_i) -> (x_{i+1},
        # first_{i+1}), and BOTH ends of a window read those same segments.
        # They differ only AT an exact breakpoint: there the start takes
        # last_i (it may not include standing before it) and the end takes
        # first_i (it may not include standing after it).
        #
        # THE OLD CODE AND COMMENT DISAGREED, AND BOTH WERE WRONG. The comment
        # said "keep the LAST time at each distance"; the code kept the FIRST
        # (`[True] + diff > 0`) and interpolated first_i -> first_{i+1},
        # which spreads the standing at x_i across the rise to x_{i+1}.
        inv = distance_inverse(tb, db)
        xu = inv[0]
        if xu.size < 2:
            continue

        def at(q, end: bool, inv=inv):
            return time_at_distance(inv, q, end)

        # Time-to-cover is piecewise linear in the start distance with kinks
        # only where the start OR the end sits on a distinct odometer value,
        # so those two anchor families contain the exact optimum.
        for anchor_is_end in (True, False):
            if anchor_is_end:
                ends_d, starts_d = xu, xu - target_m
            else:
                starts_d, ends_d = xu, xu + target_m
            ok = (starts_d >= xu[0] - 1e-9) & (ends_d <= xu[-1] + 1e-9)
            if not ok.any():
                continue
            sd, ed = starts_d[ok], ends_d[ok]
            t_start, t_end = at(sd, end=False), at(ed, end=True)
            took = t_end - t_start
            valid = took > 0
            if not valid.any():
                continue
            j = int(np.argmin(np.where(valid, took, np.inf)))
            if best is None or took[j] < best[0]:
                best = (float(took[j]), float(t_start[j]), float(t_end[j]))
    if best is None:
        return {
            "window": label,
            "target_m": target_m,
            "mps": None,
            "pace_per_mile": None,
            "status": "unavailable",
            "reason": (
                f"no {target_m:.0f} m stretch free of sample gaps "
                f"over {MAX_SAMPLE_GAP_S:.0f} s and of stopped time"
            ),
        }
    took, s, e = best
    return {
        "window": label,
        "target_m": target_m,
        "status": "ok",
        "mps": target_m / took,
        "pace_per_mile": None,
        "meters": target_m,
        "seconds": took,
        "start_elapsed_s": s,
        "end_elapsed_s": e,
    }


def _window_composition(con, activity_id: str, t0, start_s, end_s) -> dict:
    """What the winning window was made of: gait mix, and flag content.

    BY TIME, not by record count. Smart Recording samples denser where values
    change, so a count-weighted fraction over-weights the variable sections --
    the same reason every mean here is time-weighted.
    """
    import datetime as dt

    a = t0 + dt.timedelta(seconds=start_s)
    b = t0 + dt.timedelta(seconds=end_s)
    span = max((b - a).total_seconds(), 1e-9)
    out = {}
    for cls, key in (("rwd_run", "run"), ("rwd_walk", "walk")):
        secs = con.execute(
            """
            SELECT coalesce(sum(date_diff('millisecond',
                       greatest(start_time, ?), least(end_time, ?)) / 1000.0), 0)
            FROM splits WHERE activity_id = ? AND split_type = ?
              AND end_time > ? AND start_time < ?
        """,
            [a, b, activity_id, cls, a, b],
        ).fetchone()[0]
        out[f"{key}_time_fraction"] = max(float(secs), 0.0) / span
    flagged, total = con.execute(
        """
        SELECT count(*) FILTER (WHERE coalesce(quality_flags, '') <> ''), count(*)
        FROM records_enriched
        WHERE activity_id = ? AND timestamp BETWEEN ? AND ?
    """,
        [activity_id, a, b],
    ).fetchone()
    out["flagged_record_fraction"] = (flagged / total) if total else None
    out["n_enriched_records_in_window"] = total
    return out


def _best_filters(con, activity_id: str, coverage_fraction) -> dict:
    regime = con.execute(
        "SELECT sampling_regime FROM activities WHERE activity_id = ?", [activity_id]
    ).fetchone()
    return {
        "boundary_mode": "interpolated_exact",
        "distance_source": "records.distance_m (device odometer)",
        "distance_source_note": "NOT records.speed_mps, which is the fused "
        "instantaneous speed §9 prohibits; the "
        "cumulative odometer is a different field and "
        "agrees with path_distance_m to within 1% "
        "wherever both exist at adequate coverage",
        "max_sample_gap_s": MAX_SAMPLE_GAP_S,
        "stopped_time_excluded": True,
        "stopped_time_source": "stops table UNION rwd_stand splits, merged",
        "split_type": None,
        "walking_included": True,
        "gait_note": "a best window is one contiguous stretch of real ground, "
        "not a pooled aggregate; restricting to rwd_run destroys "
        "the mile (on a run/walk run a fully-running mile is "
        "rare)",
        "sampling_regime": regime[0] if regime else None,
        "comparable_across_sampling_regimes": False,
        "regime_note": "the path a maximum finds depends on sampling density, "
        "so bests must not be compared across a switch "
        "between Smart Recording and 1 Hz",
        "gps_coverage_fraction": coverage_fraction,
        "distance_provenance": _provenance(coverage_fraction),
        "grain": "records",
        **_flag_note(con, activity_id),
        "quality_flags_reason": "100% of one-mile windows on every activity that has a "
        "mile "
        "contain at least one flagged record, so rejecting on any flag "
        "returns nothing; the winning window's flagged fraction is "
        "reported instead. A maximum selects for positive measurement "
        "error in a way a mean does not -- read that field.",
    }


# --------------------------------------------------------------------------
# §6.0d cadence
# --------------------------------------------------------------------------


def _cadence_expr() -> str:
    return (
        f"({CADENCE_STEPS_PER_STRIDE}.0 * "
        f"(r.cadence_raw_rpm + coalesce(r.fractional_cadence, 0.0)))"
    )


def mean_running_cadence(con, activity_id: str) -> dict:
    """Mean cadence in STEPS PER MINUTE over running splits, time-weighted.

    THE UNIT IS THE WHOLE RISK. `cadence_raw_rpm` is one-leg strides per
    minute -- typically in the 80s on running splits, which is a plausible
    number for nothing. Reported raw it understates by exactly 2x.

    RESOLVED against Garmin's own summary, closing §9's open
    question. steps/min = (cadence_raw_rpm + fractional_cadence) x 2:
      - time-weighted, it matches averageRunningCadenceInStepsPerMinute to a
        median of +0.06 spm, nearly always within 2 spm;
      - it reproduces maxRunningCadenceInStepsPerMinute EXACTLY on most
        activities, against a minority for the un-fractioned form;
      - §9's other candidate, cadence x 2 + fractional, is REFUTED BY PARITY:
        it yields only even integers or half-integers, and over half of
        Garmin's max values are ODD.
    The AVERAGE could not settle it -- the two candidates' medians differ by
    0.36 spm, exactly as §9 predicted. The MAX settles it, and needed no
    manually counted run.

    THIS IS NOT GARMIN'S NUMBER AND MUST NOT BE RECONCILED TO IT. Garmin's
    average includes zero-cadence records despite its name; restricting to
    non-zero moves it +3.64 spm. This figure is running-only and non-zero-only
    on purpose -- it answers "how fast were the legs turning over while
    running", which is the trainable variable (§14a).
    """
    rows = con.execute(
        f"""
        SELECT r.timestamp, {_cadence_expr()}
        FROM records r JOIN splits s ON s.activity_id = r.activity_id
             AND r.timestamp BETWEEN s.start_time AND s.end_time
        WHERE r.activity_id = ? AND s.split_type = 'rwd_run'
          AND r.cadence_raw_rpm IS NOT NULL AND r.cadence_raw_rpm > 0
        ORDER BY r.timestamp
    """,
        [activity_id],
    ).fetchall()
    filters = _cadence_filters(con, activity_id)
    if len(rows) < 2:
        no_cad = (
            con.execute(
                "SELECT count(cadence_raw_rpm) FROM records WHERE activity_id = ?",
                [activity_id],
            ).fetchone()[0]
            == 0
        )
        return {
            "value": None,
            "n": 0,
            "status_hint": "unavailable",
            "reason": (
                "this source records no cadence at all (the Strava "
                "phone file has none -- NULL, not zero)"
                if no_cad
                else "too few running records with cadence"
            ),
            "filters": filters,
        }
    num, den = weighted_mean(rows, record_weights(con, activity_id))
    if den <= 0:
        return {
            "value": None,
            "n": len(rows),
            "status_hint": "unavailable",
            "reason": "no weighted running time",
            "filters": filters,
        }
    # The denominator of the weighted mean, next to the authoritative running
    # time it should approximate. They are not the same quantity -- the weight
    # sum omits records with no cadence and is capped -- so the pair is
    # reported and a divergence is visible instead of absorbed.
    rwd_run_s = con.execute(
        "SELECT coalesce(sum(total_timer_time), 0) FROM splits "
        "WHERE activity_id = ? AND split_type = 'rwd_run'",
        [activity_id],
    ).fetchone()[0]
    n_high = sum(1 for _, c in rows if c is not None and c >= 200)
    return {
        "value": num / den,
        "n": len(rows),
        "weight_seconds": den,
        "rwd_run_timer_time_s": float(rwd_run_s),
        # A maximum-adjacent artifact worth counting rather than averaging
        # in: §14a measured a genuine spike well above 200 spm immediately
        # after a walk->run boundary, at a near-constant speed.
        "n_above_200_spm": n_high,
        "filters": filters,
    }


# THE FOUR RUNNING-DYNAMICS CHANNELS, with the units Garmin actually uses.
#
# STATING THE UNIT IS THE POINT. None of these are the obvious choice: vertical
# oscillation and step length are MILLIMETRES (not cm, not m), ground contact
# is MILLISECONDS, and vertical ratio is a PERCENT that is already a ratio of
# the first two. A reader who assumes centimetres is out by 10x in a number
# that still looks plausible.
#
# NOTHING COMPUTES ON THESE YET. They are reported so that form-against-grade
# is readable before M1 exists -- M1 (the gait detector) is what was waiting on
# this data, and it needs labelled training data that does not exist yet.
RUNNING_DYNAMICS = {
    "vertical_oscillation": ("mm", "vertical_oscillation_mm"),
    "vertical_ratio": ("percent", "vertical_ratio_pct"),
    "stance_time": ("ms", "stance_time_ms"),
    "step_length": ("mm", "step_length_mm"),
}


def running_dynamics(con, activity_id: str) -> dict:
    """Mean VO / VR / GCT / step length over RUNNING splits, time-weighted.

    RUNNING SPLITS ONLY, and the same gate as every other class figure: these
    are properties of a running gait, and a walking stance time pooled into the
    mean makes the number belong to neither.

    QUALITY FLAGS ARE HONOURED HERE (§15.4), unlike the windowed bests, which
    cannot honour them. These are record-grain means over `records_enriched`
    where flags exist; where that table has no rows for a run -- it is
    `records JOIN dem_elevations`, so it covers only DEM-tiled activities --
    the figure falls back to `records` and SAYS SO in `filters`, because a
    silently unfiltered mean is the thing §15.4 exists to prevent.

    THE ABSENT CASE IS NOT A FAILURE. The FR245 records none of these fields
    (no accessory), so every FR245 activity returns `unavailable` with a
    reason naming the hardware, not a zero.
    """
    filters = _dynamics_filters(con, activity_id)
    weights = record_weights(con, activity_id)
    out = {"filters": filters}
    for field, (unit, key) in sorted(RUNNING_DYNAMICS.items()):
        rows = con.execute(
            f"""
            SELECT r.timestamp, r.{field}
            FROM records r JOIN splits s ON s.activity_id = r.activity_id
                 AND r.timestamp BETWEEN s.start_time AND s.end_time
            WHERE r.activity_id = ? AND s.split_type = 'rwd_run'
              AND r.{field} IS NOT NULL
              {filters["_flag_clause"]}
            ORDER BY r.timestamp
        """,
            [activity_id],
        ).fetchall()
        if len(rows) < 2:
            out[key] = {
                "value": None,
                "unit": unit,
                "n": 0,
                "status_hint": "unavailable",
                "reason": "this device records no "
                + field
                + " (the FR245 has no running dynamics; "
                "stance_time_balance and "
                "stance_time_percent need an HRM-Pro or "
                "RD pod even on the FR265)",
            }
            continue
        num, den = weighted_mean(rows, weights)
        vals = sorted(v for _, v in rows)
        out[key] = {
            "value": (num / den) if den > 0 else None,
            "unit": unit,
            "n": len(rows),
            "weight_seconds": den,
            "median": vals[len(vals) // 2],
            "p25": vals[len(vals) // 4],
            "p75": vals[3 * len(vals) // 4],
        }
    return out


def _dynamics_filters(con, activity_id: str) -> dict:
    """What the dynamics means included, and whether flags could be applied."""
    n_flagged, n_enriched = con.execute(
        """
        SELECT count(*) FILTER (WHERE coalesce(quality_flags, '') <> ''), count(*)
        FROM records_enriched WHERE activity_id = ?""",
        [activity_id],
    ).fetchone()
    enriched = n_enriched > 0
    return {
        "split_type": "rwd_run",
        "walking_paired": False,
        "stopped_time_excluded": True,
        "grain": "record",
        "units": {k: v[0] for k, v in sorted(RUNNING_DYNAMICS.items())},
        "units_note": "Garmin's units are NOT the obvious ones: vertical "
        "oscillation and step length are MILLIMETRES, ground "
        "contact time is MILLISECONDS, vertical ratio is a "
        "PERCENT (oscillation / step length)",
        "quality_flags_excluded": enriched,
        "quality_flags_available": enriched,
        "quality_flags_reason": (
            f"{n_flagged} of {n_enriched} enriched records flagged and excluded (§15.4)"
            if enriched
            else "this run has no records_enriched rows -- it is `records JOIN "
            "dem_elevations`, so it exists only where a DEM tile does. The "
            "mean is over unfiltered `records` and is NOT §15.4-clean."
        ),
        # The clause is carried so the query and the declaration cannot drift.
        "_flag_clause": (
            "AND EXISTS (SELECT 1 FROM records_enriched e "
            " WHERE e.activity_id = r.activity_id AND e.timestamp = r.timestamp "
            "   AND coalesce(e.quality_flags, '') = '')"
            if enriched
            else ""
        ),
        "computes_nothing": "these are ANCHORS. No metric is derived from them "
        "yet -- M1 (mode detection) is the build that was "
        "waiting on this data",
    }


def no_splits_reason(con, activity_id: str) -> dict:
    """Why an activity has no rwd_* splits, DERIVED from what it actually is.

    THE STRING THIS REPLACES blamed "the Strava phone file" on a hike -- a
    garmin_watch file, the only activity for its run_id. Measured across the
    development corpus's activities with no rwd_* splits: exactly ONE was the
    Strava file. Most were non-running sports on the watch, a cause the
    string did not know about at all. It named the rarest case as the certain
    one.

    So the cause is read off the activity: its source, its sport, and whether
    it carries position. Where two causes remain possible they are BOTH named
    as possibilities, because asserting one that was not checked is the defect
    being fixed, not a style preference.
    """
    row = con.execute(
        "SELECT source, sport, sub_sport, coverage_fraction FROM activities "
        "WHERE activity_id = ?",
        [activity_id],
    ).fetchone()
    if row is None:
        return {"cause": "unknown_activity", "reason": f"no activity {activity_id}"}
    source, sport, sub_sport, cov = row
    if source != "garmin_watch":
        return {
            "cause": "not_the_watch",
            "source": source,
            "sport": sport,
            "reason": (
                f"this is the {source} file, and run/walk detection "
                f"is the WATCH's -- the phone app does not segment, "
                f"so no rwd_* split exists to read"
            ),
        }
    if sport != "running":
        return {
            "cause": "non_running_sport",
            "source": source,
            "sport": sport,
            "reason": (
                f"the watch recorded this as {sport!r}"
                + (f"/{sub_sport!r}" if sub_sport else "")
                + ", and run/walk detection only runs on a RUNNING "
                "activity. There is no walk to detect on a bike "
                "or in a pool, so the absence is correct and is "
                "not a gap in the file."
            ),
        }
    # Running, on the watch, and still no segmentation. TWO causes are possible
    # and this cannot tell them apart, so it says so rather than picking.
    return {
        "cause": "running_without_segmentation",
        "source": source,
        "sport": sport,
        "coverage_fraction": cov,
        "reason": (
            "the watch recorded this as running but wrote no "
            "run/walk segmentation. Two causes are possible and "
            "this cannot distinguish them: an activity IMPORTED "
            "into Garmin rather than recorded on the watch "
            "(segmentation happens on the device, during the "
            "activity -- a file that arrives in a bulk upload "
            "can carry a full record stream and no splits), "
            "or a recording where the feature was off. This run's "
            "GPS coverage is "
            + (f"{cov:.2f}" if cov is not None else "unknown")
            + ", which is the only hint available here."
        ),
    }


# --------------------------------------------------------------------------
# WHY TERRAIN IS ABSENT — FOUR STATES, DECIDED BY LOOKING (§5a)
#
# THE BUG THIS REPLACES. cadence_by_grade said "no DEM tile covers this run
# (N of M garmin activities)" on a hike while `terrain_coverage` in the SAME
# payload said covered, 100%. Both halves were wrong: the tile DOES cover
# that ground -- the running-only sport hold is what kept it out of
# dem_elevations -- and the count was a dead literal from a corpus that had
# since tripled.
#
# A reason string must not ASSERT a cause it did not check, and a count in a
# reason must be computed at call time or not be there at all.
# --------------------------------------------------------------------------

# The filter that excludes non-running activities from the terrain layers.
# Quoted with file, function and the filter itself because "held by a sport
# filter" is unactionable without saying WHICH filter -- and NOT with a line
# number, which drifts with every edit to that file and would then point at
# the wrong line with full confidence.
SPORT_HOLD_SITE = (
    "src/trail_factors/cli/terrain.py, dem_populate (`a.sport = 'running'`)"
)


def db_elevation(con, activity_id: str) -> dict:
    """How much of this run's ground already has DEM elevation IN THE DATABASE.

    THE DATABASE, NOT THE TILE DIRECTORY, says whether a run has elevation.
    Tiles are only the means of populating it: a database built elsewhere (the
    demo, a copy, a machine whose tiles were cleared) carries complete
    elevation with no tile on disk, and asking the tiles alone told those runs
    to fetch-tile ground they already had. A positioned record counts if
    dem_elevations or records_enriched holds its elevation. `complete` is the
    question every caller asks; `dem` writes a run all-or-nothing, so partial
    coverage is a stale state, not a normal one.
    """
    n_points, n_with = con.execute(
        """
        SELECT count(*),
               count(*) FILTER (WHERE
                   EXISTS (SELECT 1 FROM dem_elevations d
                           WHERE d.activity_id = r.activity_id
                             AND d.timestamp = r.timestamp)
                   OR EXISTS (SELECT 1 FROM records_enriched e
                              WHERE e.activity_id = r.activity_id
                                AND e.timestamp = r.timestamp
                                AND e.altitude_dem_m IS NOT NULL))
        FROM records r WHERE r.activity_id = ? AND r.lat IS NOT NULL
    """,
        [activity_id],
    ).fetchone()
    return {
        "n_points": n_points,
        "n_with_elevation": n_with,
        "complete": n_points > 0 and n_with == n_points,
    }


def terrain_state(con, activity_id: str) -> dict:
    """Why this activity has no enriched records -- or that it has.

    FOUR STATES, and which one it is is DETERMINED, never assumed:

      ok                   enriched rows exist
      held_by_sport        a loaded tile covers the ground, but the activity is
                           not running and the terrain layers are held to
                           running (§4.1's deferred decision)
      outside_loaded_tiles no loaded tile covers the ground
      unpopulated          a tile covers it, the sport is running, and
                           dem_elevations simply has no rows -- `dem` has not
                           been run since the tile or the activity arrived --
                           OR dem_elevations is complete and `silver` has not
                           been run, whatever tiles are on disk

    The tile question is answered by ASKING THE TILES, which is the step whose
    absence produced a payload that contradicted itself -- but only once the
    database has said the elevation is absent (db_elevation).
    """
    n_enr = con.execute(
        "SELECT count(*) FROM records_enriched WHERE activity_id = ?", [activity_id]
    ).fetchone()[0]
    if n_enr:
        return {"state": "ok", "enriched_records": n_enr, "reason": None}

    sport = con.execute(
        "SELECT sport FROM activities WHERE activity_id = ?", [activity_id]
    ).fetchone()
    sport = sport[0] if sport else None
    elev = db_elevation(con, activity_id)
    if elev["complete"]:
        return {
            "state": "unpopulated",
            "enriched_records": 0,
            "sport": sport,
            "covered_fraction": 1.0,
            "reason": (
                f"dem_elevations holds an elevation for all "
                f"{elev['n_points']:,} positions on this run, but "
                f"records_enriched has no rows for it: `silver` has not "
                f"been run. Run `uv run trail-factors silver {activity_id}`."
            ),
        }
    pts = con.execute(
        "SELECT lat, lon FROM records WHERE activity_id = ? AND lat IS NOT NULL",
        [activity_id],
    ).fetchall()
    if not pts:
        return {
            "state": "outside_loaded_tiles",
            "enriched_records": 0,
            "sport": sport,
            "covered_fraction": None,
            "reason": "this activity has no position at all, so no tile "
            "can cover it (indoor, or a file with no GPS)",
        }
    try:
        from trail_factors.transform.dem import TileSet

        cov = TileSet().coverage([r[0] for r in pts], [r[1] for r in pts])
        covered = cov["fraction_covered"]
        missing = cov["missing_tiles"]
    except Exception as exc:
        return {
            "state": "unpopulated",
            "enriched_records": 0,
            "sport": sport,
            "covered_fraction": None,
            "reason": f"the DEM tiles could not be read to decide this "
            f"({type(exc).__name__}: {exc})",
        }

    if missing:
        return {
            "state": "outside_loaded_tiles",
            "enriched_records": 0,
            "sport": sport,
            "covered_fraction": covered,
            "missing_tiles": missing,
            "reason": (
                f"no loaded DEM tile covers "
                f"{cov['n_uncovered']:,} of {cov['n_points']:,} "
                f"positions on this run "
                f"({covered:.0%} covered). Run the fetch command "
                f"each missing cell names, where it names one -- "
                f"ground with no USGS grid has none, and says "
                f"why."
            ),
        }
    if sport != "running":
        return {
            "state": "held_by_sport",
            "enriched_records": 0,
            "sport": sport,
            "covered_fraction": covered,
            "hold_site": SPORT_HOLD_SITE,
            "reason": (
                f"a loaded DEM tile DOES cover this run "
                f"({covered:.0%} of {cov['n_points']:,} positions), "
                f"but its sport is {sport!r} and the terrain layers "
                f"are held to running by {SPORT_HOLD_SITE}. That "
                f"hold is §4.1's deferred decision about how each "
                f"activity type is used -- NOT a terrain gap, and "
                f"not a property of this run."
            ),
        }
    return {
        "state": "unpopulated",
        "enriched_records": 0,
        "sport": sport,
        "covered_fraction": covered,
        "reason": (
            f"a loaded tile covers this run "
            f"({covered:.0%} of {cov['n_points']:,} positions) and "
            f"its sport is running, but dem_elevations has no rows "
            f"for it. Run `uv run trail-factors retile`."
        ),
    }


def cadence_by_grade(con, activity_id: str) -> dict:
    """Cadence per 5% grade bin, split by split_type. Never pooled.

    Mirrors §6.2's grade-binned pace bin-for-bin so the two tables read side by
    side. It needs `grade_pct`, so unlike every other metric here it reads
    `records_enriched` and is therefore UNAVAILABLE on the
    garmin_watch activities with no DEM tile. That is stated, not implied.

    THE WEIGHTS ARE COMPUTED BEFORE THE FILTERS, not after. Taking lag() over
    an already-filtered set makes a sample's weight the gap to the next
    SURVIVING sample, which silently inflates the weight of records next to
    excluded ones -- the bin totals then measure the filter rather than the
    time.
    """
    rows = con.execute(
        f"""
        WITH w AS (
          SELECT r.timestamp, r.activity_id, {_cadence_expr()} AS spm,
                 date_diff('second', r.timestamp,
                     lead(r.timestamp) OVER (ORDER BY r.timestamp)) AS dt
          FROM records r WHERE r.activity_id = ?
        )
        SELECT s.split_type, floor(e.grade_pct / ?) * ? AS bin,
               sum(w.spm * w.dt) / sum(w.dt) AS spm,
               sum(w.dt) AS weight_s, count(*) AS n
        FROM w
        JOIN records_enriched e USING (activity_id, timestamp)
        JOIN splits s ON s.activity_id = w.activity_id
             AND w.timestamp BETWEEN s.start_time AND s.end_time
        WHERE w.dt BETWEEN 1 AND ? AND w.spm > 0
          AND e.terrain_usable AND e.grade_pct IS NOT NULL
          AND coalesce(e.quality_flags, '') = ''
          AND s.split_type IN ('rwd_run', 'rwd_walk')
        GROUP BY 1, 2 HAVING sum(w.dt) >= ?
        ORDER BY 1, 2
    """,
        [activity_id, GRADE_BIN_PCT, GRADE_BIN_PCT, MAX_SAMPLE_GAP_S, MIN_BIN_TIME_S],
    ).fetchall()
    # DECIDED BY LOOKING, not asserted. The old string claimed "no DEM tile
    # covers this run", with a hard-coded corpus count, without checking
    # either half, and contradicted terrain_coverage in the same payload.
    ts = terrain_state(con, activity_id)
    n_enr = ts["enriched_records"]
    return {
        "bins": [
            {
                "split_type": st,
                "grade_bin": b,
                "steps_per_min": spm,
                "weight_s": ws,
                "n": n,
            }
            for st, b, spm, ws, n in rows
        ],
        "enriched_records": n_enr,
        "status_hint": "unavailable" if n_enr == 0 else "ok",
        "terrain_state": ts["state"],
        "reason": ts["reason"] if n_enr == 0 else None,
        "filters": {
            **_cadence_filters(con, activity_id),
            "terrain_usable_required": True,
            "grade_required": True,
            "grade_bin_pct": GRADE_BIN_PCT,
            "min_bin_time_s": MIN_BIN_TIME_S,
            "quality_flags_excluded": True,
            "split_types_pooled": False,
            "weights_computed_before_filters": True,
            "source_table": "records JOIN records_enriched",
        },
    }


def _cadence_filters(con, activity_id: str) -> dict:
    return {
        "cadence_formula": "(cadence_raw_rpm + fractional_cadence) x 2",
        "cadence_unit": "steps_per_minute",
        "cadence_formula_status": "confirmed against Garmin "
        "averageRunningCadenceInStepsPerMinute and "
        "maxRunningCadenceInStepsPerMinute (§9)",
        "cadence_zero_excluded": True,
        "split_type": "rwd_run",
        "split_types_pooled": False,
        "weighting": "time",
        "max_sample_gap_s": MAX_SAMPLE_GAP_S,
        "stopped_time_excluded": True,
        "speed_source": "not_used",
        "source_table": "records",
        "grain": "records",
        "comparable_to_garmin_summary": False,
        "garmin_note": "Garmin's averageRunningCadence includes zero-cadence "
        "records despite its name (+3.64 spm difference); this "
        "figure is running-only and non-zero-only",
        **_flag_note(con, activity_id),
    }


# --------------------------------------------------------------------------
# the whole block, as one call
# --------------------------------------------------------------------------

# THE WINDOW A CEILING IS DEFENSIBLE OVER.
#
# A single record is a SPIKE CANDIDATE -- optical HR misreads, and a maximum
# selects for positive measurement error exactly as the windowed bests do
# (§6.0). A 30 s rolling mean cannot be produced by one bad sample and is short
# enough that a real peak survives it: a real peak is held for longer than
# a single sample.
#
# BOTH ARE REPORTED. The single record is what the athlete's watch shows and
# what Garmin's summary carries, so hiding it would produce a number that
# disagrees with the device for no visible reason.
MAX_HR_WINDOW_S = 30.0


def max_hr(con, activity_id: str) -> dict:
    """The run's HR ceiling, two ways, from RAW `records.heart_rate`.

    RAW, NOT LAG-SHIFTED, and that is a deliberate difference from §17.2's
    zone work. The 40 s shift aligns HR to the effort that caused it, which is
    what MATCHING needs; a ceiling wants the highest rate the heart actually
    reached, at the moment the monitor recorded it. Shifting would move the
    peak onto a neighbouring sample and change nothing but its timestamp --
    except at the edges of the run, where it would drop it entirely.

    `max_hr_recorded` is the single highest record. `max_hr_30s` is the highest
    30 s rolling mean with §15.4-flagged records EXCLUDED, and it is the
    defensible ceiling: a maximum selects for positive measurement error, so
    the figure a spike can reach is not the figure to quote.

    THE SESSION SUMMARY IS CROSS-CHECKED, NOT DEFERRED TO -- AND IT IS NOT A
    SECOND SOURCE. `activities.session_max_hr` and the record stream come from
    the SAME FILE, written by the same watch; the old name implied a contrast
    with something non-device and there is none.

    WHAT THE DIFFERENCE MEASURES IS RESOLUTION, NOT INGEST. The watch samples
    HR continuously and writes a record only on the Smart Recording schedule,
    while the summary is computed over the full internal stream -- so the
    record-stream maximum is a DECIMATED view of it. Measured on the
    development corpus: 1 Hz files agree EXACTLY; Smart Recording files often
    disagree, always with the summary HIGHER, by 1-2 bpm, never lower.
    Skipping a sample can only lose a peak, never invent one.

    CONSEQUENCE FOR A CEILING: on a Smart Recording run, `max_hr_recorded`
    UNDERSTATES what the watch saw by up to 2 bpm. The summary is the better
    figure for "how high did this heart go"; the record stream is the only one
    that can be placed in time or checked for cadence lock.
    """
    rows = con.execute(
        """
        SELECT timestamp, heart_rate FROM records
        WHERE activity_id = ? AND heart_rate IS NOT NULL AND heart_rate > 0
        ORDER BY timestamp
    """,
        [activity_id],
    ).fetchall()
    device, avg_row = con.execute(
        """
        SELECT session_max_hr, NULL FROM activities WHERE activity_id = ?
    """,
        [activity_id],
    ).fetchone() or (None, None)
    filters = _max_hr_filters(con, activity_id)
    if not rows:
        # THE SAME ASSERTED-CAUSE FAMILY, and it misfires the same way: of the
        # development corpus's activities with no heart rate at all, nearly
        # all were watch swims (the optical sensor does not read underwater)
        # and one was the Strava file. It named the rare case as the certain
        # one.
        _hr = con.execute(
            "SELECT source, sport FROM activities WHERE activity_id = ?", [activity_id]
        ).fetchone()
        _src, _sport = _hr if _hr is not None else (None, None)
        _why = (
            "this is the strava_phone file, which carries no heart rate at "
            "all -- NULL, not zero"
            if _src != "garmin_watch"
            else f"the watch recorded this as {_sport!r} and wrote no heart "
            f"rate; on a swim the optical sensor does not read underwater"
            if _sport == "swimming"
            else f"the watch recorded this as {_sport!r} and wrote no heart "
            f"rate -- NULL, not zero. The cause is not determinable from "
            f"the file"
        )
        return {
            "max_hr_recorded": {
                "value": None,
                "status_hint": "unavailable",
                "reason": f"this activity records no heart rate: {_why}",
                "source": "records.heart_rate",
            },
            "max_hr_30s": {
                "value": None,
                "status_hint": "unavailable",
                "reason": "no heart rate to window",
                "source": f"records.heart_rate, {MAX_HR_WINDOW_S:.0f} s rolling mean",
            },
            "session_max_hr": device,
            "session_agrees": None,
            "filters": filters,
        }

    peak = max(rows, key=lambda r: r[1])
    recorded = {
        "value": float(peak[1]),
        "at_utc": str(peak[0]),
        "status_hint": "ok",
        "reason": None,
        "source": "records.heart_rate, single highest sample",
        "caveat": "A SINGLE SAMPLE IS A SPIKE CANDIDATE. Quote max_hr_30s "
        "unless you specifically want what the watch displayed.",
    }
    window = _max_hr_window(con, activity_id, rows)
    agrees = None
    if device is not None:
        # Equality, not a tolerance: both are integers over the same samples,
        # so any difference at all is worth seeing rather than absorbing.
        agrees = abs(float(device) - recorded["value"]) < 0.5
    return {
        "max_hr_recorded": recorded,
        "max_hr_30s": window,
        "session_max_hr": None if device is None else float(device),
        "session_agrees": agrees,
        "session_note": (
            None
            if agrees is not False
            else f"the watch's session summary says {device:.0f} bpm "
            f"and its record stream peaks at "
            f"{recorded['value']:.0f}. BOTH ARE THE SAME WATCH, "
            f"same file. The summary is computed over continuous "
            f"internal sampling; the record stream is written on "
            f"the Smart Recording schedule and is a DECIMATED "
            f"view of it, so it can only understate. Not a tie to "
            f"break -- the summary is the better ceiling, the "
            f"records are the only thing placeable in time."
        ),
        "filters": filters,
    }


def _max_hr_window(con, activity_id: str, rows: list) -> dict:
    """Highest `MAX_HR_WINDOW_S` rolling mean, flagged records excluded."""
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
    clean = [r for r in rows if r[0] not in flagged]
    src = f"records.heart_rate, highest {MAX_HR_WINDOW_S:.0f} s rolling mean" + (
        ", §15.4-flagged records excluded" if flagged else ""
    )
    if len(clean) < 2:
        return {
            "value": None,
            "status_hint": "unavailable",
            "reason": "fewer than two unflagged heart-rate records",
            "source": src,
        }
    best = None
    for i, (t0, _) in enumerate(clean):
        j, total, weight = i, 0.0, 0.0
        while j + 1 < len(clean):
            dt = (clean[j + 1][0] - clean[j][0]).total_seconds()
            if weight + dt > MAX_HR_WINDOW_S:
                break
            # TIME-WEIGHTED, because sampling is not uniform (CLAUDE.md): a
            # plain mean over records over-weights the denser stretches, and
            # Smart Recording is denser exactly where HR is changing.
            total += clean[j][1] * dt
            weight += dt
            j += 1
        if weight < MAX_HR_WINDOW_S * 0.8:
            continue  # a window that never filled
        mean = total / weight
        if best is None or mean > best[0]:
            best = (mean, t0, clean[j][0], weight)
    if best is None:
        return {
            "value": None,
            "status_hint": "gated",
            "reason": f"no {MAX_HR_WINDOW_S:.0f} s window of unflagged "
            f"records could be filled on this run",
            "source": src,
        }
    mean, start, end, weight = best
    # THE FLAGGED FRACTION OF THE WINNING WINDOW, exactly as windowed_bests
    # reports it: a ceiling drawn from a stretch that was half-excluded is a
    # different claim from one drawn from clean data.
    in_span = [r for r in rows if start <= r[0] <= end]
    n_flagged = sum(1 for r in in_span if r[0] in flagged)
    return {
        "value": mean,
        "status_hint": "ok",
        "reason": None,
        "source": src,
        "window_start_utc": str(start),
        "window_end_utc": str(end),
        "window_seconds": weight,
        "flagged_record_fraction": (n_flagged / len(in_span)) if in_span else None,
        "n_records_in_window": len(in_span),
    }


def _max_hr_filters(con, activity_id: str) -> dict:
    n_enriched = con.execute(
        "SELECT count(*) FROM records_enriched WHERE activity_id = ?", [activity_id]
    ).fetchone()[0]
    return {
        "hr_series": "RAW records.heart_rate -- NOT lag-shifted. The 40 s "
        "shift aligns HR to the effort that caused it, which is "
        "what matching needs; a ceiling wants the peak as "
        "recorded.",
        "window_s": MAX_HR_WINDOW_S,
        "window_weighting": "time-weighted; sampling is not uniform",
        "stopped_time_excluded": False,
        "stopped_time_note": "a peak during a stop is still a peak the heart "
        "reached; this is not a rate",
        "quality_flags_excluded": n_enriched > 0,
        "quality_flags_available": n_enriched > 0,
        "quality_flags_reason": (
            "§15.4-flagged records are excluded from the windowed figure; the "
            "single-sample figure is NOT filtered, because it is defined as "
            "what was recorded"
            if n_enriched > 0
            else "this run has no records_enriched rows, so no per-record flags "
            "exist to apply -- the windowed figure is unfiltered and is NOT "
            "§15.4-clean"
        ),
        "grain": "record",
    }


def basic_metrics(con, activity_id: str) -> dict:
    """Every §6.0 anchor for one run, each with its own filters."""
    return {
        "average_hr": average_hr(con, activity_id),
        "durations": durations(con, activity_id),
        "paces": paces(con, activity_id),
        "windowed_bests": windowed_bests(con, activity_id),
        "mean_running_cadence": mean_running_cadence(con, activity_id),
        "cadence_by_grade": cadence_by_grade(con, activity_id),
        "running_dynamics": running_dynamics(con, activity_id),
        "max_hr": max_hr(con, activity_id),
    }


def summarise(values: list[float | None]) -> dict:
    """median / p25 / p75 / n, for a comparison population."""
    pop = sorted(v for v in values if v is not None)
    if not pop:
        return {"median": None, "p25": None, "p75": None, "n": 0}

    def q(f):
        return pop[min(int(f * len(pop)), len(pop) - 1)]

    return {
        "median": statistics.median(pop),
        "p25": q(0.25),
        "p75": q(0.75),
        "n": len(pop),
    }


# --------------------------------------------------------------------------
# §6.0e per-effort pace and windowed bests within one effort
# --------------------------------------------------------------------------

# The athlete asked for a 10 s window inside an effort as well as 30 s. It is
# kept, and its limitation is stated rather than the window being refused: a
# path-derived speed is a BACKWARD-LOOKING AVERAGE over the sampling interval,
# so at Smart Recording's ~4 s median gap a 10 s window rests on two or three
# intervals. `n_samples` is returned with every window so a reader can see how
# much is behind it, and a window with fewer than this many samples is
# `unavailable` rather than a number with nothing under it.
EFFORT_WINDOWS_S = (10.0, 30.0)
MIN_SAMPLES_PER_WINDOW = 3


def effort_paces(
    con,
    activity_id: str,
    start_utc,
    end_utc,
    length_m: float | None,
    duration_s: float | None,
) -> dict:
    """Effort-mean pace and the best windows INSIDE one segment effort.

    TWO DIFFERENT QUANTITIES, AND THE DISTINCTION IS THE POINT.

      `mean_pace_per_mile` is the whole effort, summit to col, over MOVING
      time. It is what the terrain cost, and it includes every part of the
      slope -- the technical entry, the run-out, the bit that was hiked.

      `best_10s` / `best_30s` are the fastest sliding windows INSIDE the
      effort. They are what was actually run fast on that slope.

    A segment whose mean is 14:00/mi and whose best 30 s is 7:30/mi is not a
    contradiction; it is a description of how the effort was run. Reporting
    only the mean answers "how did this hill go", and reporting only the best
    answers "what can this hill be taken at" -- §6.2 and §16 turn on keeping
    those apart, because a grade-binned median reports how the athlete CHOSE
    to run and a windowed best reports capacity.
    """
    out = {
        "mean_mps": None,
        "mean_pace_per_mile": None,
        "windows": [],
        "filters": _effort_pace_filters(con, activity_id),
    }
    if (
        length_m is not None
        and duration_s is not None
        and duration_s > 0
        and length_m > 0
    ):
        out["mean_mps"] = length_m / duration_s
        out["mean_pace_per_mile"] = pace_per_mile(out["mean_mps"])

    rows = con.execute(
        """
        SELECT timestamp, distance_m FROM records
        WHERE activity_id = ? AND distance_m IS NOT NULL
          AND timestamp BETWEEN ? AND ? ORDER BY timestamp
    """,
        [activity_id, start_utc, end_utc],
    ).fetchall()
    for target in EFFORT_WINDOWS_S:
        out["windows"].append(
            _effort_window(con, activity_id, rows, target, start_utc, end_utc)
        )
    return out


def _effort_window(
    con, activity_id: str, rows: list, target_s: float, start_utc, end_utc
) -> dict:
    """One sliding window inside an effort, or a stated reason there is none."""
    import numpy as np

    label = f"best_{target_s:.0f}s"
    span = (end_utc - start_utc).total_seconds()
    if span < target_s:
        return {
            "window": label,
            "target_s": target_s,
            "status": "unavailable",
            "mps": None,
            "pace_per_mile": None,
            "n_samples": len(rows),
            "reason": (
                f"the effort lasts {span:.0f} s, shorter than the "
                f"{target_s:.0f} s window"
            ),
        }
    if len(rows) < MIN_SAMPLES_PER_WINDOW:
        return {
            "window": label,
            "target_s": target_s,
            "status": "unavailable",
            "mps": None,
            "pace_per_mile": None,
            "n_samples": len(rows),
            "reason": (
                f"{len(rows)} samples in the effort, below the "
                f"{MIN_SAMPLES_PER_WINDOW} a window needs"
            ),
        }

    t = np.array([(r[0] - rows[0][0]).total_seconds() for r in rows], dtype=float)
    d = np.maximum.accumulate(np.array([float(r[1]) for r in rows], dtype=float))
    paused = paused_intervals(con, activity_id)
    gaps = np.diff(t)
    bad = gaps > MAX_SAMPLE_GAP_S
    for i in range(len(rows) - 1):
        if bad[i]:
            continue
        a, b = rows[i][0], rows[i + 1][0]
        if any(min(b, s1) > max(a, s0) for s0, s1 in paused):
            bad[i] = True
    blocks, start = [], 0
    for i, is_bad in enumerate(bad):
        if is_bad:
            if i + 1 - start >= 2:
                blocks.append((start, i + 1))
            start = i + 1
    if len(t) - start >= 2:
        blocks.append((start, len(t)))

    w = _best_time_window(t, d, blocks, target_s)
    w["n_samples"] = len(rows)
    # How many samples actually sit inside the winning window -- the honest
    # measure of how much is behind the number at Smart Recording.
    if w["status"] == "ok":
        inside = int(((t >= w["start_elapsed_s"]) & (t <= w["end_elapsed_s"])).sum())
        w["n_samples_in_window"] = inside
        w["pace_per_mile"] = pace_per_mile(w["mps"])
        if inside < MIN_SAMPLES_PER_WINDOW:
            w["status"] = "gated"
            w["mps"], w["pace_per_mile"] = None, None
            w["reason"] = (
                f"only {inside} samples inside the {target_s:.0f} s "
                f"window; path-derived speed is an average over the "
                f"sampling interval, so this would be one or two "
                f"intervals read as a peak"
            )
    return w


def _effort_pace_filters(con, activity_id: str) -> dict:
    regime = con.execute(
        "SELECT sampling_regime FROM activities WHERE activity_id = ?", [activity_id]
    ).fetchone()
    return {
        "mean_grain": "whole effort, summit to col, over moving time",
        "mean_source": "segment_efforts.length_m / duration_s",
        "mean_stopped_time_excluded": True,
        "window_grain": "sliding window inside the effort",
        "window_source": "records.distance_m (device odometer)",
        "window_boundary_mode": "interpolated_exact",
        "window_targets_s": list(EFFORT_WINDOWS_S),
        "max_sample_gap_s": MAX_SAMPLE_GAP_S,
        "min_samples_per_window": MIN_SAMPLES_PER_WINDOW,
        "sampling_regime": regime[0] if regime else None,
        "comparable_across_sampling_regimes": False,
        "quality_flags_excluded": False,
        "note": "the mean and the windowed best are DIFFERENT QUANTITIES: the "
        "mean is what the terrain cost over the whole slope, the "
        "window is the part that was actually run fast. Never "
        "substitute one for the other.",
    }


# --------------------------------------------------------------------------
# §6.0f effort grade profile -- what the effort average is hiding
# --------------------------------------------------------------------------

# Grade bands, SIGNED BY THE EFFORT'S OWN DIRECTION. An ascent's "steep" band is
# +10% or more; a descent's is -10% or less. Stating that explicitly matters
# because an unsigned |grade| definition silently counts a descent inside an
# ascent as steep climbing, and the two definitions disagree on exactly the
# efforts this measurement exists to expose.
#
# TWO THRESHOLDS, AND THE FIELD NAMES DO NOT SAY WHICH. The BANDS
# (len_steep_m etc.) cut at 10/5/1. But `steep_mean_grade_pct` and
# `tail_after_last_steep_m` select MODERATE-OR-STEEPER ground -- >= 5% in the
# effort's direction (GRADE_BAND_MODERATE_PCT) -- not the 10% steep band. The
# filters text once said 10% for those while the code used 5%; profile_filters
# now states each threshold beside the field it governs.
GRADE_BAND_STEEP_PCT = 10.0
GRADE_BAND_MODERATE_PCT = 5.0
GRADE_BAND_GENTLE_PCT = 1.0


def effort_profile(
    con,
    activity_id: str,
    start_distance_m: float | None,
    end_distance_m: float | None,
    direction: str,
) -> dict:
    """The grade composition of one effort, distance-weighted.

    WHY THIS EXISTS RATHER THAN A CHANGED BOUNDARY RULE. A stored
    `mean_grade_pct` of about +1% over most of a kilometre can be a short,
    steep climb with a long plateau absorbed onto it, and the average alone
    cannot show that. The obvious repair -- vetoing a merge on the grade of
    the PIECE being absorbed rather than on the cumulative result -- was
    implemented and measured across all DEM-covered activities of the
    development trail, and it does NOT remove the problem, it MIRRORS it:
    ascents became short and steep, and the long, gentle efforts reappeared
    as descents instead.

    The plateau simply moves onto the descents. The root cause is terrain,
    not the rule: that plateau's texture sat below PROMINENCE_MIN_M, so the
    project's own significance threshold sits ABOVE the thing it would need
    to resolve. Fitting a new one here would be a threshold fitted on one
    trail, which CLAUDE.md records as having produced two corrected design
    errors already.

    So the rule is left alone and the composition is made VISIBLE at the point
    of use. A reader who can see that a steep-looking descent is mostly
    gentler than -5% does not need the boundary moved.

    Distance-weighted with midpoint intervals, never by record count: Smart
    Recording samples denser where values change, so a count-weighted profile
    over-weights exactly the varying sections it is meant to characterise.
    """
    out = {
        "len_steep_m": None,
        "len_moderate_m": None,
        "len_gentle_m": None,
        "len_flat_m": None,
        "wrong_sign_m": None,
        "steep_mean_grade_pct": None,
        "tail_after_last_steep_m": None,
        "grade_coverage_fraction": None,
    }
    if (
        start_distance_m is None
        or end_distance_m is None
        or end_distance_m <= start_distance_m
    ):
        return out
    rows = con.execute(
        """
        SELECT path_distance_m, grade_pct FROM records_enriched
        WHERE activity_id = ? AND path_distance_m BETWEEN ? AND ?
        ORDER BY path_distance_m
    """,
        [activity_id, start_distance_m, end_distance_m],
    ).fetchall()
    if len(rows) < 3:
        return out

    sign = 1.0 if direction == "ascent" else -1.0
    span = end_distance_m - start_distance_m
    lo, hi = start_distance_m, end_distance_m
    bands = {"steep": 0.0, "moderate": 0.0, "gentle": 0.0, "flat": 0.0}
    wrong = covered = steep_num = steep_den = 0.0
    last_steep_d = None
    for i, (d, g) in enumerate(rows):
        # Midpoint interval: half the gap to each neighbour, clipped to the
        # effort's own span so the parts sum to the length rather than
        # overhanging it.
        a = lo if i == 0 else (rows[i - 1][0] + d) / 2
        b = hi if i == len(rows) - 1 else (d + rows[i + 1][0]) / 2
        w = max(0.0, min(b, hi) - max(a, lo))
        if w <= 0 or g is None:
            continue
        covered += w
        signed = sign * g
        if signed >= GRADE_BAND_STEEP_PCT:
            bands["steep"] += w
        elif signed >= GRADE_BAND_MODERATE_PCT:
            bands["moderate"] += w
        elif signed >= GRADE_BAND_GENTLE_PCT:
            bands["gentle"] += w
        else:
            bands["flat"] += w
        if signed < 0:
            wrong += w
        if signed >= GRADE_BAND_MODERATE_PCT:
            steep_num += g * w
            steep_den += w
            last_steep_d = d
    out.update(
        {
            "len_steep_m": bands["steep"],
            "len_moderate_m": bands["moderate"],
            "len_gentle_m": bands["gentle"],
            "len_flat_m": bands["flat"],
            "wrong_sign_m": wrong,
            "steep_mean_grade_pct": (steep_num / steep_den) if steep_den > 0 else None,
            # The run-out: how much of the effort comes AFTER the last sample that
            # was still steep. Descent-biased on the development trail, but not
            # the typical case -- typically about a tenth of the length, and about
            # one descent in six exceeded a third.
            "tail_after_last_steep_m": (hi - last_steep_d)
            if last_steep_d is not None
            else None,
            "grade_coverage_fraction": covered / span if span > 0 else None,
        }
    )
    return out


def profile_filters() -> dict:
    return {
        "weighting": "distance, midpoint intervals clipped to the effort span",
        "band_definition": f"SIGNED by the effort's own direction: an "
        f"ascent's steep band is >= +"
        f"{GRADE_BAND_STEEP_PCT:.0f}%, a descent's is <= "
        f"-{GRADE_BAND_STEEP_PCT:.0f}%",
        # steep_mean_grade_pct and tail_after_last_steep_m do NOT use the
        # steep band: they select moderate-or-steeper ground.
        "steep_threshold_pct": GRADE_BAND_MODERATE_PCT,
        "steep_threshold_definition": (
            f"steep_mean_grade_pct and tail_after_last_steep_m select "
            f"MODERATE-OR-STEEPER ground: >= {GRADE_BAND_MODERATE_PCT:.0f}% "
            f"in the effort's own direction (an ascent's >= "
            f"+{GRADE_BAND_MODERATE_PCT:.0f}%, a descent's <= "
            f"-{GRADE_BAND_MODERATE_PCT:.0f}%), not the "
            f"{GRADE_BAND_STEEP_PCT:.0f}% steep band"
        ),
        "steep_mean_estimator": (
            "distance-weighted mean of the per-record grade_pct over that "
            "ground; each grade_pct is a short-window (25 m) grade, not the "
            "effort's end-to-end rise over run. On a short effort with few "
            "records the two estimators can differ, and the steep mean can "
            "read SHALLOWER than the effort's mean_grade_pct while keeping "
            "its sign -- a property of the estimators, not a sign error"
        ),
        "bands_pct": {
            "steep": f">= {GRADE_BAND_STEEP_PCT:.0f}",
            "moderate": f"{GRADE_BAND_MODERATE_PCT:.0f}-{GRADE_BAND_STEEP_PCT:.0f}",
            "gentle": f"{GRADE_BAND_GENTLE_PCT:.0f}-{GRADE_BAND_MODERATE_PCT:.0f}",
            "flat": f"< {GRADE_BAND_GENTLE_PCT:.0f} (includes wrong-sign)",
        },
        "grade_source": "records_enriched.grade_pct",
        "boundary_rule_unchanged": True,
        "note": "the profile is carried rather than the boundary moved: the "
        "obvious detector fix mirrors the artifact onto descents "
        "instead of removing it, and the residual needs a threshold "
        "that one trail's data cannot fit (§5).",
    }


# --------------------------------------------------------------------------
# §6.0g per-effort pace BY DIRECTION -- the athlete's second focus metric
# --------------------------------------------------------------------------


def effort_pace_by_direction(con, activity_id: str) -> dict:
    """Every slope effort on one run, plus a distance-weighted split by direction.

    WHY THIS FUNCTION EXISTS. The per-effort pace was computable but not
    callable: `effort_paces` was reached from exactly one place,
    `over.segment_history(segment_id=...)`, so assembling "average pace on
    hills, ascent and descent separately" meant one call per segment and
    hand-aggregation, and the §16.2 report carried no effort pace at all --
    `climb_signature` renders direction, length, vertical and grade, with no
    pace and no HR. The data was fully stored and direction-labelled the whole
    time. It was one GROUP BY away and invisible.

    THE DIRECTION AGGREGATE CARRIES A CONFOUND AND MUST SAY SO. Ascents and
    descents on rolling terrain NEED NOT COVER THE SAME GROUND: on the
    development trail, several two-directional segments had an ascent more
    than twice the descent's length, because plateau absorption glues flat
    ground onto the ascent direction (§14k). So an ascent-vs-descent pace difference is
    partly a
    difference of terrain, not of the athlete. `comparable_ground` reports how
    far apart the two directions' total distances are, so a reader can see how
    much of the gap that could explain.
    """
    rows = con.execute(
        """
        SELECT e.segment_id, e.slope_index, e.direction, e.length_m,
               e.duration_s, e.mean_grade_pct, e.avg_hr, e.vertical_m,
               e.start_utc, e.end_utc, t.start_distance_m, t.end_distance_m
        FROM segment_efforts e
        LEFT JOIN slope_traversals t
          ON t.activity_id = e.activity_id AND t.slope_index = e.slope_index
        WHERE e.activity_id = ?
        ORDER BY e.start_utc
    """,
        [activity_id],
    ).fetchall()

    efforts = []
    for (
        sid,
        idx,
        direction,
        length_m,
        dur,
        grade,
        hr,
        vert,
        s_utc,
        e_utc,
        d0,
        d1,
    ) in rows:
        pace = effort_paces(con, activity_id, s_utc, e_utc, length_m, dur)
        best30 = next((w for w in pace["windows"] if w["window"] == "best_30s"), None)
        prof = effort_profile(con, activity_id, d0, d1, direction)
        efforts.append(
            {
                "segment_id": sid,
                "slope_index": idx,
                "direction": direction,
                "length_m": length_m,
                "duration_s": dur,
                "mean_grade_pct": grade,
                "vertical_m": vert,
                "avg_hr": hr,
                "mean_mps": pace["mean_mps"],
                "mean_pace_per_mile": pace["mean_pace_per_mile"],
                "best_30s_pace_per_mile": (
                    best30.get("pace_per_mile") if best30 is not None else None
                ),
                "best_30s_status": (
                    best30.get("status") if best30 is not None else None
                ),
                "steep_mean_grade_pct": prof["steep_mean_grade_pct"],
            }
        )

    by_direction = {}
    for direction in ("ascent", "descent"):
        sel = [
            e
            for e in efforts
            if e["direction"] == direction and e["length_m"] and e["duration_s"]
        ]
        dist = sum(e["length_m"] for e in sel)
        secs = sum(e["duration_s"] for e in sel)
        hrs = [(e["avg_hr"], e["duration_s"]) for e in sel if e["avg_hr"]]
        hr_num = sum(h * d for h, d in hrs)
        hr_den = sum(d for _, d in hrs)
        by_direction[direction] = {
            "n_efforts": len(sel),
            "distance_m": dist,
            "moving_s": secs,
            # DISTANCE-WEIGHTED, not a mean of per-effort paces: a 40 m effort
            # and a 900 m one do not carry the same evidence about the pace.
            "mps": (dist / secs) if secs > 0 else None,
            "pace_per_mile": pace_per_mile(dist / secs) if secs > 0 else None,
            "avg_hr": (hr_num / hr_den) if hr_den > 0 else None,
            "mean_grade_pct": (
                sum(e["mean_grade_pct"] * e["length_m"] for e in sel) / dist
            )
            if dist > 0
            else None,
        }

    a, d = by_direction["ascent"]["distance_m"], by_direction["descent"]["distance_m"]
    ratio = (max(a, d) / min(a, d)) if a > 0 and d > 0 else None
    return {
        "efforts": efforts,
        "by_direction": by_direction,
        "comparable_ground": {
            "ascent_distance_m": a,
            "descent_distance_m": d,
            "length_ratio": ratio,
            "note": "ascents and descents need not cover the same ground -- "
            "plateau absorption glues flat ground onto the ascent "
            "direction (§14k). A ratio far from 1 means the "
            "direction difference is partly terrain, not the athlete.",
        },
        "filters": {
            **_effort_pace_filters(con, activity_id),
            "grain": "segment efforts, grouped by direction",
            "aggregate_weighting": "distance (sum length / sum moving time)",
            "hr_weighting": "effort duration",
            "stopped_time_excluded": True,
            "quality_flags_excluded": False,
            "confound": "ascent and descent cover different ground; "
            "read comparable_ground.length_ratio",
        },
    }
