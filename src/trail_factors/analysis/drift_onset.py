"""§6.9 drift onset: WHEN in a run cardiac drift begins (SPEC.md §6.9).

A VIEW OF §6.7, NOT A NEW DETECTOR. `matched_terrain_drift` already yields
pairs of records on the same ground, at least 10 minutes apart, both `rwd_run`,
speed matched within 0.35 m/s. Those pairs are already the hard part. This
module bins them by the LATER record's time into the run and reports the drift
rate as a curve against time-into-run. No new baseline, no fitted decay
constant, no changepoint search -- everything it stands on is §6.7's, which
was measured stable across 18 tolerance combinations.

IT IS REFUSED UNTIL THE CORPUS SUPPORTS IT, and the refusal is the point
rather than a placeholder. The acceptance criteria were set before it was
built:
  (a) split-half stability on odd/even seconds within one bin, and
  (b) across at least 8 same-cluster runs at comparable dew point,
      SD(onset) < 0.5 x median(onset).
(a) is NOT implemented, deliberately: odd and even seconds of a 1 Hz file are
the same passage over the same ground a second apart, so they cannot disagree
unless the sensor is broken, and a test that cannot fail is not evidence. (b)
cannot be EVALUATED, let alone passed, until enough 1 Hz long runs exist.
`gate_status()` computes it from the live corpus, so this starts reporting on
its own when the runs exist -- the same construction as tau's MAX_TAU_CV
(§6.3), which is a measured constant rather than a verdict someone has to
remember to revisit.

FOUR THINGS THAT WOULD MAKE A CONFIDENT WRONG ANSWER, all measured on the
development corpus and recorded in §6.9 so they are not re-derived:

1. PSEUDO-REPLICATION. A 5-minute bin can hold 900+ pairs, and they are not
   900 observations -- they are drawn from a few dozen records, each appearing
   in many pairs. A sign test over raw pairs returns p ~ 1e-73 on noise. The
   independent unit is a PASSAGE: one contiguous traversal of the terrain.
2. ONSET IS LARGELY RUN LENGTH. Only a minority of DEM-covered runs produce
   an onset at all, and across those few onset correlated strongly with
   duration -- though a handful of runs is itself too small to lean on, which
   is part of the same problem. A cohort that varies in duration will show "onset"
   varying with
   duration and nothing else.
3. THE X-AXIS IS THE Y-AXIS'S OWN WINDOW. On an out-and-back, ground visited at
   time t is revisited near T - t, so the pair gap is forced to T - 2t. Time
   into run and pair separation are structurally coupled, and the rate divides
   by the separation.
4. THE FLOOR IS THE METRIC'S OWN. §6.7 requires pairs at least 600 s apart, so
   no onset earlier than 10 minutes plus the time to the first terrain revisit
   is representable. An onset at that floor is the filter, not the athlete.
"""

from __future__ import annotations

import statistics

from trail_factors.analysis import primitives as P

CALC_VERSION = 1

# 5-minute bins, as specified. The bin width DECIDES THE ANSWER and that is not
# a defect of this width in particular: on one run onset moved by about a
# third of its value across widths from 60 to 600 s. The width is
# therefore reported as a field and the sensitivity is reported beside the
# value, rather than the value being presented as width-free.
ONSET_BIN_S = 300.0

# Pairs whose LATER records are more than this far apart in time belong to
# different traversals of the ground. This is what turns a bin's hundreds of
# correlated pairs into a countable number of independent passages.
PASSAGE_SPLIT_GAP_S = 30.0

# A bin needs this many independent passages before its sign is read at all.
MIN_PASSAGES_PER_BIN = 3

# A curve needs this many usable bins before an onset can be located: one bin
# cannot show "leaves zero AND STAYS THERE".
MIN_USABLE_BINS = 3

# Transient-class analysis is invalid below 1 Hz (§15.4). Drift onset asks WHEN
# something began, which is a timing question, and Smart Recording's ~4 s
# median gap with 20 s excursions cannot place a 5-minute boundary reliably.
REQUIRED_SAMPLING_REGIME = "1hz"

# MEASURED on the development corpus, and it is the fact that decides the
# timeline. Running `curve()` over every DEM-covered garmin activity, most are
# `insufficient`, and NO RUN UNDER 62 MINUTES PRODUCED AN ONSET. The reason is
# structural rather than incidental -- §6.7 requires pairs at least 600 s
# apart, the first terrain revisit adds more, and three usable 5-minute bins
# must fit after that. Among runs at least this long about two thirds yielded
# an onset. CONSEQUENCE: short runs can never contribute to this metric,
# however many of them there are; only the long runs count.
MIN_ONSET_DURATION_MIN = 62.0

# The acceptance criteria, as constants so the gate lifts itself.
MIN_ONSET_RUNS = 8
MAX_ONSET_CV = 0.5
# "Comparable dew point": within this of the cohort median. Dew point is the
# variable that matters for heat stress and transfers well over distance (§8);
# 3 C is roughly the width of the band one month's runs typically occupy.
DEW_POINT_BAND_C = 3.0


def _bin_index(elapsed_s: float) -> int:
    return int(elapsed_s // ONSET_BIN_S)


def _passages(times: list[float]) -> int:
    """How many contiguous traversals a bin's later-records represent."""
    if not times:
        return 0
    ts = sorted(times)
    n = 1
    for a, b in zip(ts, ts[1:], strict=False):
        if b - a > PASSAGE_SPLIT_GAP_S:
            n += 1
    return n


def curve(con, activity_id: str) -> dict:
    """The drift curve for one run: bpm/hour against time into the run.

    Every bin carries its sign counts INCLUDING TIES. A pair whose two heart
    rates are equal is evidence FOR the null, not a missing observation, and
    on a typical run every bin holds some. Dropping them would
    inflate the apparent majority in exactly the bins that decide onset.
    """
    import numpy as np

    meta = con.execute(
        """
        SELECT sampling_regime, duration_s FROM activities WHERE activity_id = ?
    """,
        [activity_id],
    ).fetchone()
    if meta is None:
        raise LookupError(f"no activity {activity_id}")
    regime, duration_s = meta
    dew = con.execute(
        """
        SELECT min(dew_point_c), max(dew_point_c), any_value(weather_source)
        FROM weather_records WHERE activity_id = ?""",
        [activity_id],
    ).fetchone()

    rows = con.execute(
        """
        SELECT e.timestamp, e.path_distance_m, r.lat, r.lon, r.heart_rate,
               s.split_type
        FROM records_enriched e JOIN records r USING (activity_id, timestamp)
        LEFT JOIN splits s ON s.activity_id = e.activity_id
             AND e.timestamp BETWEEN s.start_time AND s.end_time
             -- The rwd_* partition only: the FR265's `interval_active` split
             -- spans the whole run and would match every record twice (§9).
             AND s.split_type LIKE 'rwd\\_%' ESCAPE '\\'
        WHERE e.activity_id = ? AND r.lat IS NOT NULL AND r.heart_rate IS NOT NULL
          AND coalesce(e.quality_flags, '') = ''
        ORDER BY e.timestamp
    """,
        [activity_id],
    ).fetchall()
    base = _filters(regime, dew)
    if len(rows) < 20:
        return {
            "bins": [],
            "onset_min": None,
            "onset_status": "insufficient",
            "reason": "fewer than 20 usable positioned records with HR",
            # THE SAME KEYS AS THE FULL RETURN. gate_status reads
            # duration_min off every curve, and a missing key turned one
            # thin run into a KeyError that cost the whole section.
            "duration_min": (duration_s / 60.0) if duration_s is not None else None,
            "dew_point_c": (dew[0], dew[1]) if dew else (None, None),
            "sampling_regime": regime,
            "filters": base,
        }

    from trail_factors.transform import matcher as M

    xs, ys = M.to_utm([r[2] for r in rows], [r[3] for r in rows])
    # Elapsed time is measured from the ACTIVITY start, not from the first
    # positioned record. §6.7's own implementation uses the first row it has,
    # which is fine for a difference but wrong for an x-axis: on the runs whose
    # GPS fixes late, that would shift every bin by up to an hour.
    t_start = con.execute(
        "SELECT min(timestamp) FROM records WHERE activity_id = ?", [activity_id]
    ).fetchone()[0]
    secs = np.array([(r[0] - t_start).total_seconds() for r in rows])
    hr = np.array([r[4] for r in rows], dtype=float)
    dist = np.array([r[1] for r in rows], dtype=float)
    is_run = np.array([r[5] == P.DRIFT_SPLIT_TYPE for r in rows])

    rel = np.diff(secs, prepend=secs[0] - 1)
    dx = np.diff(dist, prepend=dist[0])
    speed = np.where((rel > 0) & (rel <= 20), dx / np.maximum(rel, 1e-9), np.nan)

    ok = is_run & np.isfinite(speed)
    idx = np.flatnonzero(ok)
    per_bin: dict[int, dict] = {}
    for i in idx:
        later = idx[idx > i]
        if later.size == 0:
            continue
        gap = secs[later] - secs[i]
        m = (
            (gap >= P.DRIFT_MIN_SEPARATION_S)
            & (np.hypot(xs[later] - xs[i], ys[later] - ys[i]) <= P.DRIFT_MAX_DISTANCE_M)
            & (np.abs(speed[later] - speed[i]) <= P.DRIFT_SPEED_TOL_MPS)
        )
        if not m.any():
            continue
        for j, g in zip(later[m], gap[m], strict=False):
            b = per_bin.setdefault(
                _bin_index(secs[j]),
                {"rates": [], "signs": [], "later_t": [], "gaps": []},
            )
            b["rates"].append(float((hr[j] - hr[i]) / (g / 3600.0)))
            b["signs"].append(int(np.sign(hr[j] - hr[i])))
            b["later_t"].append(float(secs[j]))
            b["gaps"].append(float(g))

    bins = []
    for k in sorted(per_bin):
        b = per_bin[k]
        n_pass = _passages(b["later_t"])
        pos = sum(1 for s in b["signs"] if s > 0)
        neg = sum(1 for s in b["signs"] if s < 0)
        tie = sum(1 for s in b["signs"] if s == 0)
        bins.append(
            {
                "bin_start_min": k * ONSET_BIN_S / 60.0,
                "bin_end_min": (k + 1) * ONSET_BIN_S / 60.0,
                "median_bpm_per_hour": statistics.median(b["rates"]),
                "n_pairs": len(b["rates"]),
                "n_passages": n_pass,
                "n_positive": pos,
                "n_negative": neg,
                "n_zero": tie,
                "median_pair_gap_s": statistics.median(b["gaps"]),
                # Usable means "the sign of this bin may be read at all". A bin
                # below the passage floor is REPORTED with its numbers and marked
                # unusable, never silently dropped.
                "usable": n_pass >= MIN_PASSAGES_PER_BIN,
            }
        )

    onset_min, onset_status, reason = _locate_onset(bins)
    return {
        "bins": bins,
        "onset_min": onset_min,
        "onset_status": onset_status,
        "reason": reason,
        "n_usable_bins": sum(1 for b in bins if b["usable"]),
        "duration_min": (duration_s / 60.0) if duration_s is not None else None,
        # Dew point travels WITH the value. §8: it is the variable that matters
        # for heat stress, and an onset time without it is uninterpretable --
        # 92 F at a 72 F dew point is a different run from 92 F at 50 F.
        "dew_point_c": (dew[0], dew[1]) if dew else (None, None),
        "weather_source": dew[2] if dew else None,
        "sampling_regime": regime,
        "filters": base,
    }


def _locate_onset(bins: list[dict]) -> tuple:
    """First usable bin that leaves zero and STAYS there.

    "Leaves zero" is a majority of that bin's independent PASSAGES drifting
    upward, not a threshold on the rate. A threshold would be a new fitted
    constant, which is exactly what this metric was specified to avoid; and the
    rate's own magnitude is the least trustworthy thing here, because
    normalising to bpm/hour divides by a pair separation that is itself
    structurally coupled to time into the run.

    "Stays there" means every LATER usable bin also leaves zero. A single
    positive bin followed by a negative one is not an onset, it is noise.
    """
    usable = [b for b in bins if b["usable"]]
    if len(usable) < MIN_USABLE_BINS:
        return (
            None,
            "insufficient",
            f"{len(usable)} usable bins; {MIN_USABLE_BINS} are needed to "
            f"tell 'leaves zero and stays' from a single excursion",
        )
    up = [b["n_positive"] > b["n_negative"] + b["n_zero"] for b in usable]
    for i, b in enumerate(usable):
        if up[i] and all(up[i:]):
            return (b["bin_start_min"], "onset", None)
    # A FLAT CURVE IS A RESULT. Counting it as "no onset" rather than returning
    # nothing is what keeps the category countable: on the development corpus
    # all three outcomes occurred in quantity, and collapsing the middle group
    # into the last would make drift look universal.
    return (
        None,
        "no_onset",
        "the curve does not leave zero and stay there on this run",
    )


def gate_status(con) -> dict:
    """Is drift onset reportable at all? Measured from the live corpus.

    Criterion (b), computed rather than asserted, so this
    lifts itself when the data arrives instead of waiting for someone to
    remember. Same construction as §6.3's MAX_TAU_CV.
    """
    # ONE ACTIVITY PER run_id, AND THE REGIME IS READ OFF THE PICKED ONE.
    # A run can be a smart_recording watch file plus a 1 Hz PHONE file, and a
    # phone file carries no heart rate. Filtering on "any 1 Hz file for this
    # run" would admit it and promise HR drift on a file that has none.
    runs = con.execute(
        """
        WITH picked AS (
            SELECT a.activity_id, a.run_id, a.start_time_utc, a.sampling_regime,
                   row_number() OVER (PARTITION BY a.run_id ORDER BY
                       CASE a.source WHEN 'garmin_watch' THEN 0 ELSE 1 END,
                       a.activity_id) AS pick
            FROM activities a
            WHERE a.sport = 'running'    -- a HOLD, not a rule (§4.1): see below
        )
        SELECT p.activity_id, p.run_id, p.start_time_utc,
               (SELECT median(w.dew_point_c) FROM weather_records w
                WHERE w.activity_id = p.activity_id) AS dew
        FROM picked p
        WHERE p.pick = 1 AND p.sampling_regime = ?
        ORDER BY p.start_time_utc
    """,
        [REQUIRED_SAMPLING_REGIME],
    ).fetchall()

    onsets, cohort = [], []
    for aid, run_id, start, dew in runs:
        try:
            c = curve(con, aid)
        except LookupError:
            continue
        cohort.append(
            {
                "run_id": run_id,
                "activity_id": aid,
                "date": str(start)[:10],
                "dew_point_c": dew,
                "onset_status": c["onset_status"],
                "onset_min": c["onset_min"],
                "duration_min": c["duration_min"],
            }
        )
        if c["onset_status"] == "onset":
            onsets.append((c["onset_min"], dew))

    n_1hz = len(runs)
    if len(onsets) < MIN_ONSET_RUNS:
        return {
            "reportable": False,
            "n_1hz_runs": n_1hz,
            "n_with_onset": len(onsets),
            "cohort": cohort,
            "cv": None,
            "reason": (
                f"{len(onsets)} of {n_1hz} runs at {REQUIRED_SAMPLING_REGIME} "
                f"produce an onset; {MIN_ONSET_RUNS} at a comparable dew point "
                f"are required before the curve's repeatability can be tested "
                f"at all. This is a data-collection wait, not a defect -- but "
                f"it is a wait for LONG runs specifically: a run under "
                f"{MIN_ONSET_DURATION_MIN:.0f} minutes cannot produce an "
                f"onset, because §6.7's 600 s minimum separation plus the "
                f"first terrain revisit leaves no room for three 5-minute "
                f"bins. {_projection(con)}"
            ),
        }
    # Restrict to a comparable dew-point band before testing repeatability:
    # drift at a 20 C dew point and drift at a 10 C one are not the same
    # quantity, and pooling them would fail the CV test for the wrong reason.
    med_dew = (
        statistics.median([d for _, d in onsets if d is not None])
        if any(d is not None for _, d in onsets)
        else None
    )
    band = [
        o
        for o, d in onsets
        if med_dew is None or d is None or abs(d - med_dew) <= DEW_POINT_BAND_C
    ]
    if len(band) < MIN_ONSET_RUNS:
        return {
            "reportable": False,
            "n_1hz_runs": n_1hz,
            "n_with_onset": len(onsets),
            "cohort": cohort,
            "cv": None,
            "reason": (
                f"{len(band)} of {len(onsets)} onsets fall within "
                f"{DEW_POINT_BAND_C:.0f} C of the median dew point "
                f"({med_dew:.1f} C); onset is uninterpretable "
                f"pooled across dew point (§8)"
            ),
        }
    med = statistics.median(band)
    sd = statistics.pstdev(band)
    cv = (sd / med) if med else None
    ok = cv is not None and cv <= MAX_ONSET_CV
    return {
        "reportable": ok,
        "n_1hz_runs": n_1hz,
        "n_with_onset": len(onsets),
        "n_in_dew_band": len(band),
        "median_onset_min": med,
        "sd_onset_min": sd,
        "cv": cv,
        "cohort": cohort,
        "reason": None
        if ok
        else (
            f"onset SD {sd:.1f} min against a median of {med:.1f} min "
            f"(CV {cv:.2f} > {MAX_ONSET_CV}) -- onset does not repeat across "
            f"comparable runs, so it is noise"
        ),
    }


def _projection(con) -> str:
    """When this could start reporting, from the athlete's OWN long-run rate.

    Derived from the corpus rather than from an assumed schedule: counting
    every run would be wrong here, because short runs can never qualify.
    """
    n_long, weeks = con.execute(
        """
        SELECT count(*),
               greatest(1.0, date_diff('day', min(start_time_utc),
                                       max(start_time_utc)) / 7.0)
        FROM activities
        WHERE source = 'garmin_watch' AND duration_s >= ? * 60
          AND start_time_utc >= (SELECT max(start_time_utc) FROM activities)
                                - INTERVAL 90 DAY
    """,
        [MIN_ONSET_DURATION_MIN],
    ).fetchone()
    if not n_long:
        return (
            "No run in the last 90 days is long enough to qualify, so "
            "there is no rate to project from."
        )
    per_week = n_long / weeks
    # Only about two thirds of qualifying runs yielded an onset on the
    # development corpus, so the run count needed is larger than the onset
    # count. A declared planning figure, not a fit to this corpus.
    needed = MIN_ONSET_RUNS / 0.67
    if per_week <= 0:
        return "No qualifying-run rate to project from."
    return (
        f"At the athlete's recent rate of {per_week:.1f} qualifying runs "
        f"per week and an assumed yield of two onsets per three qualifying "
        f"runs, "
        f"{MIN_ONSET_RUNS} onsets is roughly {needed / per_week:.0f} weeks "
        f"of long runs away -- materially later than a count of all runs "
        f"would suggest."
    )


def _filters(regime, dew) -> dict:
    """§6.7's filters, unchanged, plus this view's own."""
    f = dict(P.drift_filters())
    f.update(
        {
            "view_of": "§6.7 matched_terrain_drift",
            "bin_by": "later record's elapsed time from activity start",
            "bin_width_s": ONSET_BIN_S,
            "bin_statistic": "median of per-pair bpm/hour",
            "zero_test": "majority of independent PASSAGES positive; ties counted "
            "as evidence for the null",
            "independence_unit": "passage",
            "passage_split_gap_s": PASSAGE_SPLIT_GAP_S,
            "min_passages_per_bin": MIN_PASSAGES_PER_BIN,
            "min_usable_bins": MIN_USABLE_BINS,
            "ties_counted": True,
            "required_sampling_regime": REQUIRED_SAMPLING_REGIME,
            "sampling_regime": regime,
            "dew_point_required": True,
            "dew_point_c": (dew[0], dew[1]) if dew else (None, None),
            "grain": "record pairs, binned by time into run",
            "note": "a view of §6.7, not a changepoint detector: no new baseline "
            "and no fitted constant. Onset is confounded with run length "
            "and its floor is §6.7's own 600 s minimum separation -- read "
            "§6.9 before using it.",
        }
    )
    return f
