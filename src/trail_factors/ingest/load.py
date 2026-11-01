"""Build rows from a parsed .fit file and write them into DuckDB.

Milestone 0 (SPEC.md §12): one file, one source. Re-running replaces that
file's rows rather than duplicating them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb

from trail_factors.paths import DB_FILE

from . import fit_file as ff
from . import validate as V
from .schema import DDL, PIPELINE_VERSION

DB_PATH = DB_FILE  # repo-root anchored (src/trail_factors/paths.py)


def activity_id_for(file_sha256: str) -> str:
    """Deterministic, so re-ingesting the same file yields the same id."""
    return file_sha256[:16]


# --------------------------------------------------------------------------
# THE CONFIRMED WINDOW IS APPLIED BEFORE VALIDATION (§4.3)
#
# THE BUG THIS FIXES. `ingest()` read window_confirmations and set the
# activities.valid_start_utc / valid_end_utc COLUMNS -- and nothing else.
# Records, session totals and splits all kept whole-file values, so the window
# was recorded and never applied. Worse, validation runs inside `build_rows`,
# BEFORE `ingest()` is reached at all, so a file whose tail is a car ride was
# rejected outright and the athlete's confirmation could never save it.
#
# So the trim happens HERE, first, and everything downstream -- the checks, the
# session totals, the splits -- sees only the confirmed activity.
#
# IT READS THE FILE, NOT THE DATABASE. `build_rows` has no connection and must
# not acquire one: the confirmation's durable home is
# data/athlete/window_confirmations.json (the DuckDB table is a projection of
# it, and a confirmation that lives only in the database is lost to a routine
# rebuild).
# --------------------------------------------------------------------------

SPLIT_PARTITION = ("rwd_run", "rwd_walk", "rwd_stand")


def confirmed_window(digest: str):
    """(valid_start, valid_end) for a file sha, or (None, None)."""
    from trail_factors.ingest.athlete_state import _dt, load_confirmations

    for r in load_confirmations():
        if r.get("file_sha256") == digest:
            return _dt(r.get("valid_start_utc")), _dt(r.get("valid_end_utc"))
    return None, None


def _clip_splits(raw_splits: list, start, end) -> tuple[list, dict]:
    """Clip Garmin's splits to the confirmed window.

    THE RULE, AND WHY IT IS NOT THE OTHER ONE:

      wholly outside  -> DROPPED. When a file's tail is a car ride, the
                         splits past the window can include an `rwd_run` that
                         is the drive itself, segmented by the watch as
                         RUNNING. Keeping it is how a drive becomes running
                         distance.
      straddling      -> TRUNCATED, not dropped. Illustratively: a 400 s
                         rwd_walk that overhangs the boundary by 10 s is
                         390 s of real walking. Dropping it to remove the
                         10 s of contamination would be the wrong trade by
                         well over an order of magnitude.
      wholly inside   -> untouched.

    A TRUNCATED SPLIT'S DURATION IS RECOMPUTED, AND THAT IS THE ONE PLACE THIS
    DEPARTS FROM "total_timer_time IS AUTHORITATIVE". It has to: Garmin's
    figure describes a span that extends past the window, so using it would
    credit the activity with time outside its own window. The new figure is
    `window_end - split_start`, which is a single wall-clock span and NOT the
    end-minus-start-of-records sum the project forbids -- that rule is about
    accumulating 1-second truncation across hundreds of splits, and this is
    one subtraction at one boundary.

    EVERY TRUNCATED SPLIT IS FLAGGED `truncated: True` with its original
    duration kept, so nothing can read it as Garmin's own segmentation.
    """
    out, stats = [], {"dropped": 0, "truncated": 0, "kept": 0}
    for sp in raw_splits:
        st = ff.to_utc_naive(sp.get("start_time"))
        dur = sp.get("total_timer_time")
        if st is None or dur is None:
            out.append(sp)
            continue
        en = st + timedelta(seconds=dur)
        if start is not None and en <= start:
            stats["dropped"] += 1
            continue
        if end is not None and st >= end:
            stats["dropped"] += 1
            continue
        lo = max(st, start) if start is not None else st
        hi = min(en, end) if end is not None else en
        if lo <= st and hi >= en:
            stats["kept"] += 1
            out.append(sp)
            continue
        clipped = dict(sp)
        # KEEP THE ORIGINAL OBJECT WHEN ONLY THE END MOVED. `ff.to_utc_naive`
        # returns a naive datetime while an untouched split's start_time is
        # tz-AWARE straight from the FIT, and mixing the two raised
        # "can't compare offset-naive and offset-aware datetimes" the moment
        # the splits were sorted. The start only changes when the START
        # boundary is what clipped it, which is not the common case.
        if lo != st:
            clipped["start_time"] = lo
        clipped["total_timer_time"] = max((hi - lo).total_seconds(), 0.0)
        clipped["truncated"] = True
        clipped["untruncated_timer_time"] = dur
        # DISTANCE IS NOT SCALED. It is recomputed from the trimmed records by
        # `_recompute_clipped_distance` below -- distance is not linear in
        # time, and a proportional split would invent a number that looks
        # measured -- several metres wrong on a piece of a few hundred metres
        # is a typical size, and it is the size that passes for a reading.
        clipped["clipped_window"] = (lo, hi)
        clipped["untruncated_distance_m"] = sp.get("total_distance")
        stats["truncated"] += 1
        out.append(clipped)
    return out, stats


def _recompute_clipped_distance(splits: list, records: list) -> None:
    """Rewrite each truncated split's distance from the trimmed records.

    IT WRITES `total_distance`, THE FIT'S OWN KEY, because that is the key the
    row insert reads. An earlier version set `total_distance_m` and nulled it,
    which never reached the table at all: the truncated split went in with its
    duration clipped and its distance still Garmin's whole-split figure -- a
    clipped split carrying the unclipped split's ground. Internally
    inconsistent, and typically well under 1% wrong, which is the size that
    survives review.
    """
    if not records:
        return
    stamps = [ff.to_utc_naive(r["timestamp"]) for r in records]
    dists = [r.get("distance") for r in records]

    def odometer_at(ts):
        best = None
        for s, d in zip(stamps, dists, strict=False):
            if d is None or s > ts:
                continue
            best = d
        return best

    for sp in splits:
        if not sp.get("truncated"):
            continue
        lo, hi = sp.get("clipped_window", (None, None))
        if lo is None or hi is None:
            continue
        d0, d1 = odometer_at(lo), odometer_at(hi)
        if d0 is not None and d1 is not None and d1 >= d0:
            sp["total_distance"] = d1 - d0
            sp["distance_basis"] = "recomputed from trimmed records"
        else:
            # ABSENT, NOT SCALED. A split whose odometer cannot be read over
            # the kept interval has no distance we can stand behind.
            sp["total_distance"] = None
            sp["distance_basis"] = "unavailable: odometer unreadable in window"


def _trimmed_totals(records: list, splits: list, start, end) -> dict:
    """Session totals for a trimmed activity, PER FIELD, with provenance.

    The spec allowed either recomputing or marking whole-file. The answer is
    different for each field, because the fields mean different things:

      total_distance_m  RECOMPUTED. `records.distance` is a cumulative
                        odometer, so last minus first is the distance actually
                        covered inside the window. When the file's tail is a
                        car ride, the whole-file figure is inflated by exactly
                        the drive.
      duration_s        RECOMPUTED as end minus start. Elapsed time IS a
                        wall-clock span, so one subtraction at the window's
                        own boundaries is the definition, not an
                        approximation.
      timer_time_s      RECOMPUTED AS THE SUM OF THE CLIPPED rwd_* SPLITS,
                        never as end-minus-start of records. Those splits
                        partition the activity and each carries an
                        authoritative total_timer_time, so summing them is
                        the project's own rule applied to a smaller set.
      avg/max HR        RECOMPUTED from the trimmed records. A max over a
                        window that includes a car ride is not this run's max.
                        The mean is TIME-WEIGHTED, because an unweighted mean
                        over records is wrong on Smart Recording.

    Garmin's whole-file values are KEPT ALONGSIDE under `*_whole_file`, never
    discarded: they are what the device said about the file, and the file is
    still the file. Nothing may read them as the activity's own.
    """
    d0 = next(
        (r.get("distance") for r in records if r.get("distance") is not None), None
    )
    d1 = next(
        (r.get("distance") for r in reversed(records) if r.get("distance") is not None),
        None,
    )
    dist = (d1 - d0) if (d0 is not None and d1 is not None) else None
    # A partition with no timer at all is None, never a measured 0.0; a real
    # zero-length partition sums to a real 0.0.
    timers = [
        sp.get("total_timer_time")
        for sp in splits
        if sp.get("split_type") in SPLIT_PARTITION
        and sp.get("total_timer_time") is not None
    ]
    timer = float(sum(timers)) if timers else None
    hrs = [
        (r.get("heart_rate"), r.get("timestamp"))
        for r in records
        if r.get("heart_rate") is not None
    ]
    avg_hr = max_hr = None
    if hrs:
        max_hr = max(h for h, _ in hrs)
        num = den = 0.0
        for i, (h, ts) in enumerate(hrs):
            nxt = hrs[i + 1][1] if i + 1 < len(hrs) else ts
            w = max((ff.to_utc_naive(nxt) - ff.to_utc_naive(ts)).total_seconds(), 0.0)
            num += h * w
            den += w
        avg_hr = (num / den) if den > 0 else sum(h for h, _ in hrs) / len(hrs)
    return {
        "total_distance_m": dist,
        "duration_s": (
            (end - start).total_seconds()
            if start is not None and end is not None
            else None
        ),
        "timer_time_s": timer,
        "avg_heart_rate": avg_hr,
        "session_max_hr": max_hr,
    }


def build_rows(path: Path) -> dict:
    messages = ff.read_messages(path)
    digest = ff.sha256(path)
    aid = activity_id_for(digest)
    source = ff.source_from_manufacturer(messages)
    session = ff.session_of(messages)
    start = ff.activity_start_utc(messages)
    tz_offset = ff.local_tz_offset_hours(messages)
    serial, model = ff.device_identity(messages)

    # Coalesce BEFORE detecting the regime: on Strava the raw messages come in
    # pairs sharing a timestamp, which would show as a median gap of 0 s.
    raw_records = messages.get("record", [])
    ordered = ff.coalesce_records(raw_records)

    # THE CONFIRMED WINDOW, APPLIED HERE AND NOWHERE ELSE. Before the regime
    # detection, before coverage, and above all BEFORE the checks: a file
    # whose tail is a car ride must be validated as the activity the athlete
    # confirmed, not as the file.
    raw_splits = messages.get("split", [])
    win_start, win_end = confirmed_window(digest)
    window = {
        "applied": False,
        "valid_start_utc": None,
        "valid_end_utc": None,
        "n_records_file": len(ordered),
        "n_records_trimmed": len(ordered),
        "splits": {"dropped": 0, "truncated": 0, "kept": len(raw_splits)},
    }
    trimmed_totals = None
    if win_start is not None or win_end is not None:
        kept = [
            r
            for r in ordered
            if (win_start is None or ff.to_utc_naive(r["timestamp"]) >= win_start)
            and (win_end is None or ff.to_utc_naive(r["timestamp"]) <= win_end)
        ]
        if not kept:
            msg = (
                f"the confirmed window {win_start} .. {win_end} contains "
                f"none of this file's {len(ordered)} records. A window that "
                f"selects nothing is a wrong window, not an empty activity."
            )
            raise V.ValidationError(
                msg,
                [
                    V.Finding(
                        "confirmed_window_empty",
                        V.FATAL,
                        V.BRONZE,
                        msg,
                        observed=0.0,
                        expected=float(len(ordered)),
                    )
                ],
            )
        raw_splits, split_stats = _clip_splits(raw_splits, win_start, win_end)
        eff_start = win_start or ff.to_utc_naive(ordered[0]["timestamp"])
        eff_end = win_end or ff.to_utc_naive(ordered[-1]["timestamp"])
        _recompute_clipped_distance(raw_splits, kept)
        trimmed_totals = _trimmed_totals(kept, raw_splits, eff_start, eff_end)
        window = {
            "applied": True,
            "valid_start_utc": eff_start,
            "valid_end_utc": eff_end,
            "n_records_file": len(ordered),
            "n_records_trimmed": len(kept),
            "n_records_dropped": len(ordered) - len(kept),
            "splits": split_stats,
            "source": "athlete confirmation (window_confirmations)",
            "note": (
                "records, splits and session totals below describe "
                "the CONFIRMED window, not the whole file. Garmin's "
                "whole-file figures are kept under *_whole_file."
            ),
        }
        ordered = kept

    regime, median_gap = ff.detect_sampling_regime(ordered)
    coverage = ff.gps_coverage(ordered)

    # Validate BEFORE building any rows. A fatal problem rejects the file
    # outright rather than writing partial or corrupt data (§9) -- and, since
    # the window is applied first, validate the TRIMMED activity rather than
    # the file, or an athlete's confirmation can never rescue a file the tail
    # invalidated.
    findings = V.bronze_checks(ordered, session, messages) + V.silver_checks(
        ordered, coverage, session.get("sport")
    )
    det_moving_s, det_stopped_s = V.moving_time_s(ordered)
    stops = V.detect_stops(ordered)

    # Garmin's own rwd_* segmentation is PRIMARY where present: it is in the
    # file rather than server-computed, finer-grained, and distinguishes run
    # from walk. Our detector is retained as the independent channel — it is
    # the only one whose threshold can be stated (§15.1). Strava files carry
    # no split messages, so there ours is the only source.
    #
    # Duration comes from total_timer_time, NOT end_time - start_time (§9).
    # Record timestamps are 1-second resolution, so end-start truncates
    # sub-second split boundaries and understates every split it touches:
    # nearly every split-type group comes out LOW. A predominantly
    # one-directional error does not average away -- it shortens
    # moving_time_s and so inflates moving pace. total_timer_time reproduces
    # Garmin's own totals exactly.
    # raw_splits is CLIPPED above when a window applies; never re-read from
    # `messages` here, or the moving/stopped totals would be whole-file again.
    split_moving = split_stopped = 0.0
    for sp_msg in raw_splits:
        dur = sp_msg.get("total_timer_time")
        if dur is None:
            st_, en_ = sp_msg.get("start_time"), sp_msg.get("end_time")
            if st_ is None or en_ is None:
                continue
            dur = (en_ - st_).total_seconds()
        if sp_msg.get("split_type") in ("rwd_run", "rwd_walk"):
            split_moving += dur
        elif sp_msg.get("split_type") == "rwd_stand":
            split_stopped += dur
    if split_moving > 0:
        moving_s, stopped_s, moving_src = split_moving, split_stopped, "splits"
    else:
        moving_s, stopped_s, moving_src = det_moving_s, det_stopped_s, "detector"

    ledger = dict(
        file_sha256=digest,
        original_filename=path.name,
        current_path=str(path),
        source=source,
        device_serial=serial,
        device_model=model,
        activity_start_utc=ff.to_utc_naive(start),
        local_tz_offset=tz_offset,
        file_bytes=path.stat().st_size,
        ingested_at=datetime.now(UTC).replace(tzinfo=None),
        parse_status="ok",
    )

    activity = dict(
        activity_id=aid,
        run_id=None,  # assigned at write time by resolve_run()
        file_hash=digest,
        source_file=str(path),
        start_time_utc=ff.to_utc_naive(start),
        local_tz=tz_offset,
        sport=session.get("sport"),
        sub_sport=session.get("sub_sport"),
        device=model,
        total_distance_m=session.get("total_distance"),
        total_ascent_m=session.get("total_ascent"),
        duration_s=session.get("total_elapsed_time"),
        moving_time_s=moving_s,
        total_stopped_s=stopped_s,
        moving_time_source=moving_src,
        moving_time_detector_s=det_moving_s,
        total_stopped_detector_s=det_stopped_s,
        timer_time_s=session.get("total_timer_time"),
        source=source,
        sampling_regime=regime,
        median_gap_s=median_gap,
        valid_start_utc=ff.to_utc_naive(ordered[0]["timestamp"]) if ordered else None,
        valid_end_utc=ff.to_utc_naive(ordered[-1]["timestamp"]) if ordered else None,
        window_applied=window["applied"],
        coverage_fraction=coverage["coverage_fraction"],
        session_max_hr=session.get("max_heart_rate"),
        declared_max_hr=_declared_max_hr(messages),
        gps_contiguous=coverage["contiguous"],
        pipeline_version=PIPELINE_VERSION,
    )

    # THE TRIMMED TOTALS REPLACE GARMIN'S, AND GARMIN'S ARE KEPT ALONGSIDE.
    #
    # A trimmed activity that keeps whole-file session totals is the worst of
    # the available outcomes: with a car ride in the tail, the session
    # distance and timer are both inflated while LOOKING like measurements, in
    # fields everything downstream divides by. The error has ONE SIGN -- a window
    # only ever removes -- so it accumulates in a weekly sum rather than
    # averaging away, which is the class this project treats as worst.
    #
    # Garmin's figures move to `*_whole_file` rather than being dropped: they
    # are what the device said about the FILE, which is still true about the
    # file. Nothing may read them as the activity's own.
    if trimmed_totals is not None:
        activity["session_distance_m_whole_file"] = activity["total_distance_m"]
        activity["timer_time_s_whole_file"] = activity["timer_time_s"]
        activity["duration_s_whole_file"] = activity["duration_s"]
        activity["session_max_hr_whole_file"] = activity["session_max_hr"]
        for k in ("total_distance_m", "duration_s", "timer_time_s", "session_max_hr"):
            if trimmed_totals.get(k) is not None:
                activity[k] = trimmed_totals[k]
        activity["avg_heart_rate_trimmed"] = trimmed_totals["avg_heart_rate"]

    # PROVENANCE IS MEASURED FROM THE TWO CHANNELS, and the device map is kept
    # only to disagree with it (§9). The map alone is what failed on the first
    # FR265 file: the watch reports `fr265_large`, the map knew `fr265`, and
    # every record would have carried altitude_source='unknown'.
    prov = ff.altitude_provenance(ordered)
    expected = ff.altitude_source(model, source)
    if source == "strava_phone":
        # §5a-bis.1: Strava's elevation balances far too well for GPS and is
        # most likely a server-side DEM. It has no gps_metadata channel to
        # measure against, so the evidence-based label stands unmeasured.
        measured, basis, agrees = expected, "declared", None
    elif prov["n_pairs"] >= ff.ALTITUDE_MIN_PAIRS:
        measured, basis = prov["source"], "measured"
        # AGREEMENT IS ONLY MEANINGFUL WHERE BOTH HAVE AN OPINION.
        agrees = (measured == expected) if expected != "unknown" else None
    else:
        # THE MEASUREMENT WAS NOT POSSIBLE, WHICH IS NOT A DISAGREEMENT. Every
        # such file in this corpus is an INDOOR run: coverage 0.0, no position,
        # no gps_metadata, and therefore no altitude at all. Labelling those
        # `unknown` put treadmill runs on the provenance-mystery pile and
        # marked them as contradicting the device map, which they cannot do.
        # The map is the fallback here -- that is what it is FOR -- and the
        # basis field says the label was not measured.
        measured, basis, agrees = expected, "device_map", None
    activity.update(
        altitude_source=measured,
        altitude_source_basis=basis,
        altitude_source_device=expected,
        altitude_source_agrees=agrees,
        altitude_divergence_m=prov["median_abs_diff_m"],
        altitude_identical_frac=prov["identical_fraction"],
        altitude_sd_record_m=prov["sd_record_m"],
        altitude_sd_gnss_m=prov["sd_gnss_m"],
        **_self_evaluation(session),
    )
    alt_source = measured
    records = []
    for r in ordered:
        ts = r["timestamp"]
        records.append(
            (
                aid,
                ff.to_utc_naive(ts),
                (ts - start).total_seconds() if start else None,
                ff.semicircles_to_degrees(r.get("position_lat")),
                ff.semicircles_to_degrees(r.get("position_long")),
                r.get("enhanced_altitude"),  # prefer enhanced_* (§9)
                alt_source,  # provenance, not a join (§14d)
                # THE INDEPENDENT CHANNEL, paired by FILE ORDER in read_messages
                # because gps_metadata carries no timestamp (§9). None on the
                # first record of a file, which has no fix written before it.
                r.get(ff.GNSS_ALTITUDE_KEY),
                r.get("distance"),
                r.get("heart_rate"),
                r.get("cadence"),
                r.get("fractional_cadence"),
                r.get("enhanced_speed"),
                r.get("gps_accuracy"),
                r.get("temperature"),
                # cycle_length16 is deliberately NOT read, and the REASON changed
                # with the FR265 (§9). On the FR245 it is present in nearly every
                # record and ZERO in every one. On the FR265 it is present in a
                # minority of records with many distinct values -- so "constant
                # zero" is no longer true and is no longer the argument.
                #
                # It is still skipped because it is still UNINTERPRETED: it is not
                # step_length (the FR265 records that separately, and the ratio
                # between them wanders with zeros mixed in), and it does not match
                # metres-per-step derived from speed and cadence (around one metre
                # against a cycle_length16 two orders of magnitude larger). A column
                # for a number nobody can name is worse than no column.
                r.get("vertical_oscillation"),
                r.get("stance_time"),
                r.get("stance_time_percent"),
                r.get("vertical_ratio"),
                r.get("step_length"),
                r.get("power"),
            )
        )

    # Strava lap messages are corrupt duplicates and carry no usable
    # segmentation (§9) — never ingest them.
    laps = []
    lap_messages = [] if source == "strava_phone" else messages.get("lap", [])
    for i, lp in enumerate(lap_messages):
        laps.append(
            (
                # BOTH, and total_timer_time is the one to use. Storing only
                # total_elapsed_time was the same defect already fixed for splits:
                # the two differ on any lap containing auto-paused time, and where
                # that lands on the shortest lap it disagrees with Garmin's
                # minActivityLapDuration -- an ORACLE field (§4.2).
                aid,
                i,
                ff.to_utc_naive(lp.get("start_time")),
                lp.get("total_elapsed_time"),
                lp.get("total_timer_time"),
                lp.get("total_distance"),
                lp.get("avg_heart_rate"),
                lp.get("max_heart_rate"),
                lp.get("avg_cadence"),
                lp.get("avg_speed"),
                lp.get("lap_trigger"),
            )
        )

    splits = []
    # SORT THE CLIPPED LIST, NEVER RE-READ `messages`. This line used to go
    # back to messages.get("split") and undid the window entirely: the records
    # trimmed correctly while every split survived, including the `rwd_run`
    # that is the car ride. THE TILING IDENTITY is the sharpest check: the
    # rwd_* splits partition the activity, so their timer sum must equal its
    # duration -- and here it read longer than the activity it partitions.
    raw_splits = sorted(
        (s for s in raw_splits if s.get("start_time")),
        # NORMALISED KEY: a clipped split may carry a naive start_time while an
        # untouched one is tz-aware, and sorted() cannot compare the two.
        key=lambda s: ff.to_utc_naive(s["start_time"]),
    )
    for i, sp in enumerate(raw_splits):
        st, en = sp.get("start_time"), sp.get("end_time")
        splits.append(
            (
                aid,
                i,
                sp.get("split_type"),
                ff.to_utc_naive(st),
                ff.to_utc_naive(en),
                # Retained for provenance only. total_timer_time is what analysis
                # reads -- see the note above and §9.
                (en - st).total_seconds() if st and en else None,
                sp.get("total_elapsed_time"),
                sp.get("total_timer_time"),
                sp.get("total_distance"),
                sp.get("avg_speed"),
                sp.get("max_speed"),
                sp.get("total_ascent"),
                sp.get("total_descent"),
                sp.get("avg_vert_speed"),
                ff.semicircles_to_degrees(sp.get("start_position_lat")),
                ff.semicircles_to_degrees(sp.get("start_position_long")),
            )
        )

    return dict(
        ledger=ledger,
        activity=activity,
        records=records,
        laps=laps,
        splits=splits,
        activity_id=aid,
        file_sha256=digest,
        findings=findings,
        stops=stops,
        window=window,
        hr_zones=_hr_zones(aid, messages),
    )


# GARMIN'S OWN POST-RUN SELF-EVALUATION, first seen on the FR265.
#
# THE SCALE TRAVELS WITH THE VALUE: a later firmware could change the
# encoding, and a stored 4 that silently means something else is worse than a
# missing one.
#
#   workout_rpe  is 10x the 1-10 answer            -> 40 means 4
#   workout_feel is a 5-point scale on 0/25/50/75/100 -> 50 is the middle point
#
# THE RPE DECODING IS CERTAIN (Garmin's API returns directWorkoutRpe on the
# same 10x scale, and the scale is documented as 1-10). THE FEEL DIRECTION IS
# NOT VERIFIED: 0 is taken as the weakest end and 100 the strongest, which is
# the order Garmin's UI presents, and it was verified on a single observation
# from the middle of the scale -- which is consistent with either direction. The scale
# string
# says so, and a run with a non-middle answer settles it.
SELFEVAL_SCALE = (
    "garmin-connect-v1: rpe = workout_rpe/10 (1-10); "
    "feel = workout_feel/25 + 1 (1-5, 1 weakest .. 5 strongest, "
    "DIRECTION UNVERIFIED on one middle-valued observation)"
)


def _self_evaluation(session: dict) -> dict:
    """Garmin's immediate single-channel rating, decoded with its scale.

    ONE channel, asked seconds after the activity. It is the perceived-effort
    input to session load (src/trail_factors/analysis/session_load.py: garmin_rpe x
    timer
    minutes). An activity the athlete did not rate stores NULL -- absent, never
    a default rating, because a default would price an unrated session.
    """
    rpe = session.get("workout_rpe")
    feel = session.get("workout_feel")
    if rpe is None and feel is None:
        return {"garmin_rpe": None, "garmin_feel": None, "garmin_selfeval_scale": None}
    return {
        "garmin_rpe": None if rpe is None else rpe / 10.0,
        # Integer division against the 25-point spacing. A value off the grid
        # would round rather than raise, so it is guarded: anything not on the
        # documented steps is stored as NULL with the raw scale recorded.
        "garmin_feel": (
            None if feel is None or feel % 25 != 0 else int(feel // 25) + 1
        ),
        "garmin_selfeval_scale": SELFEVAL_SCALE,
    }


def _declared_max_hr(messages: dict) -> int | None:
    """The max HR this file's zones are built on (§14e).

    SESSION SCOPE, like the boundaries themselves. It is the denominator of
    every `percent_max_hr` boundary in the file, which is what makes the gap
    between it and the demonstrated ceiling worth reporting.
    """
    msgs = messages.get("time_in_zone") or []
    sess = [m for m in msgs if m.get("reference_mesg") == "session"] or msgs
    return sess[0].get("max_heart_rate") if sess else None


def _hr_zones(aid: str, messages: dict) -> list[tuple]:
    """The zone boundaries this file was recorded under (§14e).

    Prefers the SESSION-scope `time_in_zone` message; lap-scope messages carry
    the same boundaries but one set per lap. Returns [] when the file has none,
    which is a real state and not an error -- the caller must then fall back to
    the athlete's dated configuration and say that it did.
    """
    msgs = messages.get("time_in_zone") or []
    sess = [m for m in msgs if m.get("reference_mesg") == "session"] or msgs
    if not sess:
        return []
    m = sess[0]
    highs = m.get("hr_zone_high_boundary")
    if not highs:
        return []
    times = m.get("time_in_hr_zone") or ()
    calc = m.get("hr_calc_type")
    out = []
    # `hr_zone_high_boundary` IS THE ARRAY OF ZONE FLOORS, DESPITE ITS NAME,
    # plus max HR as the final entry. An illustrative (synthetic) array
    # [100, 120, 140, 160, 175, 190] is z1 100-119, z2 120-139, z3 140-159,
    # z4 160-174, z5 175-190.
    #
    # Reading it as highs shifts every zone up by one bpm, and that is not a
    # rounding difference: recomputing time-in-zone against Garmin's own
    # `time_in_hr_zone` agrees closely under the floors reading and is
    # several percent out under the highs reading. An athlete whose habitual
    # effort sits near a zone line has real minutes moved across it by a
    # one-bpm shift.
    for z in range(1, min(len(highs), 6)):
        lo = highs[z - 1]
        hi = (highs[z] - 1) if z < 5 else highs[z]
        t = times[z] if z < len(times) else None
        out.append(
            (aid, z, lo, hi, calc, float(t) if t is not None else None, "fit_session")
        )
    return out


def _persist_rejection(path: Path, db_path: Path, exc) -> None:
    """Record a rejection raised INSIDE build_rows, before any connection.

    The one such rejection today is a confirmed window that selects nothing.
    Without this it left no trace: the findings of the PREVIOUS rejection
    stayed in validation_results, so `rejected-files` described the file as
    it was before the window was confirmed -- the wrong reason, with the
    wrong next step.
    """
    findings = getattr(exc, "findings", None)
    if not findings:
        return
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path))
    try:
        con.execute(DDL)
        V.persist(con, ff.sha256(path), None, None, findings)
    finally:
        con.close()


def ingest(path: Path, db_path: Path | None = None) -> dict:
    # RESOLVED AT CALL TIME, not bound as a default: a default argument is
    # evaluated once at import, so redirecting DB_PATH afterwards (a test, or
    # a caller that sets it) would leave this writing to the old path.
    db_path = DB_PATH if db_path is None else db_path
    try:
        rows = build_rows(path)
    except V.ValidationError as exc:
        _persist_rejection(path, db_path, exc)
        raise
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path))
    findings = rows["findings"]
    fatal = [f for f in findings if f.is_fatal]
    try:
        con.execute(DDL)
        # Persist findings FIRST and commit, so a rejected file leaves a
        # queryable trace even though it gets no activity row (§15.4).
        V.persist(
            con,
            rows["file_sha256"],
            None if fatal else rows["activity_id"],
            None,
            findings,
        )
        if fatal:
            raise V.ValidationError("; ".join(f.message for f in fatal), fatal)

        con.execute("BEGIN")
        aid, digest = rows["activity_id"], rows["file_sha256"]
        # Idempotent re-run: clear this file's rows first.
        for table in ("records", "laps", "splits", "stops", "activity_hr_zones"):
            con.execute(f"DELETE FROM {table} WHERE activity_id = ?", [aid])
        con.execute("DELETE FROM activities WHERE activity_id = ?", [aid])
        con.execute("DELETE FROM ingest_ledger WHERE file_sha256 = ?", [digest])

        # Insert by NAME, not position: a new column must not break callers
        # that build a row dict (this broke the test suite twice).
        led = rows["ledger"]
        con.execute(
            f"INSERT INTO ingest_ledger ({','.join(led)}) VALUES "
            f"({','.join(['?'] * len(led))})",
            list(led.values()),
        )
        # THE WINDOW IS ALREADY APPLIED. This used to read
        # window_confirmations here and set the two COLUMNS, which was the
        # whole defect: records, splits and session totals stayed whole-file,
        # and validation had already run and rejected the file long before
        # this line was reached. `build_rows` now trims first (see
        # confirmed_window / _clip_splits), so there is nothing to override.
        #
        # The DB table stays a projection of the JSON file and this code still
        # never writes it (CLAUDE.md).

        run_id = resolve_run(con, rows["activity"])
        rows["activity"]["run_id"] = run_id
        act = rows["activity"]
        con.execute(
            f"INSERT INTO activities ({','.join(act)}) VALUES "
            f"({','.join(['?'] * len(act))})",
            list(act.values()),
        )
        refresh_run_start(con, run_id)
        for i, s in enumerate(rows["stops"]):
            con.execute(
                "INSERT INTO stops VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    aid,
                    i,
                    ff.to_utc_naive(s["start_utc"]),
                    ff.to_utc_naive(s["end_utc"]),
                    s["duration_s"],
                    s["position"],
                    s["hr_at_stop"],
                    s["hr_at_resume"],
                    s["hr_min"],
                    s["hr_drop"],
                    s["lat"],
                    s["lon"],
                ],
            )
        if rows["hr_zones"]:
            con.executemany(
                "INSERT INTO activity_hr_zones VALUES (?,?,?,?,?,?,?)", rows["hr_zones"]
            )
        for table in ("records", "laps", "splits"):
            batch = rows[table]
            if not batch:
                continue
            placeholders = ",".join(["?"] * len(batch[0]))
            con.executemany(f"INSERT INTO {table} VALUES ({placeholders})", batch)
        con.execute("COMMIT")
    except V.ValidationError:
        # Raised before BEGIN: the findings are already committed and there is
        # no transaction to roll back. Re-raise unchanged so the caller sees
        # the rejection rather than a transaction error.
        raise
    except Exception:
        con.execute("ROLLBACK")
        raise
    finally:
        con.close()
    return rows


# --- Run pairing (SPEC.md §4, §9) -------------------------------------------
# Deliberately simple: dual-source recording (watch + phone) is a validation
# period, not the steady state (§14). It must work; it need not be fast.

PAIR_START_TOLERANCE_S = 180.0


def run_id_for(start_utc) -> str:
    """UTC-anchored identity, created once from the OPENING file's start and
    never recomputed, so it stays stable when a second source attaches (§4).

    It is an IDENTIFIER, not a queryable timestamp. Because runs.start_time_utc
    is min() across sources, the two can disagree by a minute when sources
    straddle a minute boundary. Always query time on start_time_utc.
    """
    return start_utc.strftime("%Y-%m-%dT%H%MZ")


def local_start_display(start_utc, tz_offset_hours) -> str | None:
    """Human-readable local start. DISPLAY ONLY — never a join or group key."""
    if start_utc is None or tz_offset_hours is None:
        return None
    local = start_utc + timedelta(hours=tz_offset_hours)
    return f"{local:%Y-%m-%d %H:%M} UTC{tz_offset_hours:+.0f}"


def find_matching_run(con, start, duration_s, sport, source) -> str | None:
    """Same sport, start within tolerance, time ranges overlap, and no
    existing member from the same source. Never exact equality (§9)."""
    if start is None:
        return None
    end = start + timedelta(seconds=duration_s or 0)
    rows = con.execute("""
        SELECT r.run_id, r.start_time_utc, r.sport,
               a.source, a.start_time_utc, a.duration_s
        FROM runs r JOIN activities a ON a.run_id = r.run_id
    """).fetchall()
    grouped: dict = {}
    for run_id, r_start, r_sport, m_source, m_start, m_dur in rows:
        g = grouped.setdefault(
            run_id, {"start": r_start, "sport": r_sport, "members": []}
        )
        g["members"].append((m_source, m_start, m_dur))
    for run_id, g in grouped.items():
        if g["sport"] != sport:
            continue
        if any(m[0] == source for m in g["members"]):
            continue  # one file per source
        if abs((g["start"] - start).total_seconds()) > PAIR_START_TOLERANCE_S:
            continue
        overlaps = any(
            m_start < end and start < m_start + timedelta(seconds=m_dur or 0)
            for _, m_start, m_dur in g["members"]
        )
        if overlaps:
            return run_id
    return None


def allocate_run_id(con, start) -> str:
    """An id for a run that pairing declined to join.

    The base id is only unique per UTC minute, so two DIFFERENT outings
    starting in the same minute — a stop/restart mid-session, or a run and a
    hike back to back — would collide with the run pairing just rejected and
    silently merge into it. Suffix instead.

    An existing id with NO member activities is reused rather than suffixed:
    that is a re-ingest, where the activity row was deleted before this ran.
    """
    base = run_id_for(start)
    candidate, n = base, 1
    while True:
        exists = con.execute(
            "SELECT 1 FROM runs WHERE run_id = ?", [candidate]
        ).fetchone()
        members = con.execute(
            "SELECT count(*) FROM activities WHERE run_id = ?", [candidate]
        ).fetchone()[0]
        if not exists or members == 0:
            return candidate
        n += 1
        candidate = f"{base}-{n}"


def refresh_run_start(con, run_id: str) -> None:
    """A run started when its EARLIEST source started.

    Recomputed whenever a source attaches, so start_time_utc does not depend
    on ingest order. local_start_display is derived from it and follows.
    """
    row = con.execute(
        "SELECT min(start_time_utc) FROM activities WHERE run_id = ?", [run_id]
    ).fetchone()
    if row is None or row[0] is None:
        return  # no members yet; leave as-is
    earliest = row[0]
    tz = con.execute("SELECT local_tz FROM runs WHERE run_id = ?", [run_id]).fetchone()[
        0
    ]
    con.execute(
        "UPDATE runs SET start_time_utc = ?, local_start_display = ? WHERE run_id = ?",
        [earliest, local_start_display(earliest, tz), run_id],
    )


def resolve_run(con, activity) -> str:
    """Attach this activity to an existing run, or open a new one."""
    start = activity["start_time_utc"]
    run_id = find_matching_run(
        con, start, activity["duration_s"], activity["sport"], activity["source"]
    )
    if run_id:
        return run_id
    run_id = allocate_run_id(con, start)
    exists = con.execute("SELECT 1 FROM runs WHERE run_id = ?", [run_id]).fetchone()
    if not exists:
        con.execute(
            "INSERT INTO runs VALUES (?,?,?,?,?,?,?)",
            [
                run_id,
                start,
                local_start_display(start, activity["local_tz"]),
                activity["local_tz"],
                activity["sport"],
                activity["sub_sport"],
                datetime.now(UTC).replace(tzinfo=None),
            ],
        )
    return run_id
