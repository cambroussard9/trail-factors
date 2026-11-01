"""Computation versioning (SPEC.md §12.6).

THE PROBLEM. A corpus fingerprint tells a reader whether the DATA moved. It
says nothing about whether the CODE moved. This project has changed the grade
smoothing window, the speed source, the elevation source and the segment unit —
each of which shifts every derived number without a single new run. A chat
comparing this week to last week would read a refactor as a fitness change,
confidently and with no way to notice.

THE MECHANISM — a hand-bumped ordinal PLUS an automatic digest. Two questions
need answering and one number cannot answer both:

  "is this newer?"      -> PIPELINE_VERSION, a monotonic int, bumped by hand
  "is this the same?"   -> DIGEST, derived from the parameter values themselves

The digest is the important half, because it CANNOT BE FORGOTTEN. A monotonic
int alone fails the way every hand-maintained duplicate in this project has
failed: someone changes GRADE_WINDOW_M and does not bump the int, and the data
now lies about its own provenance. Deriving the digest from the live constants
means changing a constant changes the digest whether or not anyone remembers.

The ordinal is still worth keeping, because a digest is unordered — it can say
"different" but not "later". Together they answer both.

AND THE PARAMETERS ARE RETURNED IN FULL, not just hashed. "The digest changed"
sends a reader looking; "GRADE_WINDOW_M went 25 -> 15" tells them what to think.
That is the same reasoning as the report's four statuses: an opaque difference
is nearly as bad as a hidden one.
"""

from __future__ import annotations

import hashlib
import json

from trail_factors import config


def _short_sha(value: str | None) -> str | None:
    """A value's identity without the value: None stays None (unset)."""
    if value is None:
        return None
    return hashlib.sha256(value.encode()).hexdigest()[:12]


# Bump by hand when a computation that affects STORED values changes in a way
# the parameter set below does not capture -- an algorithm change rather than a
# threshold change. The digest catches thresholds automatically; this catches
# the rest.
#
#   1  original silver layer, prominence-hill segments
#   2  slope segment unit, (place, side) identity, stopped time excluded from
#      effort rates, path-derived speed
PIPELINE_VERSION = 2


def calc_params() -> dict:
    """Every parameter that moves a stored number, read LIVE from source.

    Covers the calculation parameters named in the spec, plus the ones that
    have already changed once in this project's history.
    """
    from trail_factors.analysis import basic as B
    from trail_factors.analysis import cycles as CY
    from trail_factors.analysis import drift_onset as DO
    from trail_factors.analysis import over as O
    from trail_factors.analysis import primitives as PR
    from trail_factors.analysis import recovery as RC
    from trail_factors.analysis import series as SR
    from trail_factors.analysis import venues as VN
    from trail_factors.analysis import zones as ZN
    from trail_factors.ingest import fit_file as FF
    from trail_factors.ingest.load import SELFEVAL_SCALE as LOAD_SELFEVAL_SCALE
    from trail_factors.transform import dem as D
    from trail_factors.transform import matcher as M
    from trail_factors.transform import silver as S

    return {
        # --- §6.7 matched-terrain drift ------------------------------------
        # THE DIGEST WAS BLIND TO THESE. Changing DRIFT_SPEED_TOL_MPS from
        # 0.35 to 0.50 moved every drift number in the corpus and left the
        # digest unchanged -- exactly the failure the digest exists to
        # prevent, in the primitive the report leads with.
        "drift_max_distance_m": PR.DRIFT_MAX_DISTANCE_M,
        "drift_min_separation_s": PR.DRIFT_MIN_SEPARATION_S,
        "drift_speed_tol_mps": PR.DRIFT_SPEED_TOL_MPS,
        "drift_split_type": PR.DRIFT_SPLIT_TYPE,
        "drift_min_gap_min": PR.DRIFT_MIN_GAP_MIN,
        # --- §6.3 recovery --------------------------------------------------
        # MAX_TAU_CV decides whether tau is REPORTED AT ALL. A change to it
        # changes what the corpus says about itself, so it belongs here more
        # than most.
        "tau_max_cv": RC.MAX_TAU_CV,
        "tau_min_clear_window_s": RC.MIN_CLEAR_WINDOW_S,
        "tau_max_tail_s": RC.MAX_TAIL_S,
        "recovery_min_delta_hr_bpm": RC.MIN_DELTA_HR_BPM,
        # --- §6.0e/f per-effort pace and profile ---------------------------
        "effort_windows_s": list(B.EFFORT_WINDOWS_S),
        "effort_min_samples_per_window": B.MIN_SAMPLES_PER_WINDOW,
        "grade_band_steep_pct": B.GRADE_BAND_STEEP_PCT,
        "grade_band_moderate_pct": B.GRADE_BAND_MODERATE_PCT,
        "grade_band_gentle_pct": B.GRADE_BAND_GENTLE_PCT,
        # --- §9 altitude provenance, MEASURED --------------------------------
        # These decide what `altitude_source` SAYS about every record in the
        # corpus, and §15.1 makes provenance decide whether the elevation
        # channel counts as independent of our DEM. A threshold change here
        # silently reclassifies a whole file.
        "altitude_identical_min": FF.ALTITUDE_IDENTICAL_MIN,
        "altitude_min_pairs": FF.ALTITUDE_MIN_PAIRS,
        # --- §19 venue identity: which runs a comparison set may hold ---
        "venue_link_share_min": VN.LINK_SHARE_MIN,
        "gnss_unknown_age_after_gap_s": FF.GNSS_UNKNOWN_AGE_AFTER_GAP_S,
        "altitude_gnss_pairing": "the last gps_metadata frame written BEFORE "
        "the record, by file order (gps_metadata "
        "carries no timestamp)",
        "altitude_source_rule": "measured from the two channels; the device "
        "map is a cross-check that flags disagreement",
        # --- session load ---------------------------------------------------
        # The DECODING of Garmin's self-evaluation and the FORMULA built on
        # it. Either changing moves every session load in the corpus.
        "garmin_selfeval_scale": LOAD_SELFEVAL_SCALE,
        "session_load_formula": "garmin_rpe x timer_time_s/60",
        "session_load_feel_used": False,
        # --- M7 time in zone by gait (§17.2) ---------------------------------
        "zone_hr_lag_s": ZN.HR_LAG_S,
        "zone_boundary_source": "activity's own FIT, athlete config as fallback",
        # --- M3 run/walk cycles (§17.2) --------------------------------------
        "cycles_min_cycles": CY.MIN_CYCLES,
        # THE WALK LENGTH BELOW WHICH RECOVERY IS NOT MEASURABLE. Derived from
        # HR_LAG_S, so a better lag estimate re-gates every walk in the corpus
        # -- and moves this digest, which is the point.
        "cycles_min_walk_for_recovery_s": CY.MIN_WALK_FOR_RECOVERY_S,
        "cycles_hr_recovery_margin_s": CY.HR_RECOVERY_MARGIN_S,
        # --- §6.0 HR ceiling -------------------------------------------------
        "max_hr_window_s": B.MAX_HR_WINDOW_S,
        "max_hr_series": "raw records.heart_rate, NOT lag-shifted",
        # The gate that decides whether a declared-vs-demonstrated gap is
        # reported at all: moving the band flips selections between ok and
        # refused, so it moves the digest.
        "max_hr_ceiling_band_bpm": O.CEILING_BAND_BPM,
        "max_hr_ceiling_gate": O.CEILING_GATE_RULE,
        # --- §17.1b binned series -------------------------------------------
        "series_bin_s": SR.BIN_S,
        "series_min_bin_coverage": SR.MIN_BIN_COVERAGE,
        # --- §6.0 basic metrics --------------------------------------------
        "hr_weight_max_gap_s": B.MAX_WEIGHT_GAP_S,
        "min_class_time_s": B.MIN_CLASS_TIME_S,
        "best_window_s": B.BEST_WINDOW_S,
        "best_windows_m": list(B.BEST_WINDOWS_M),
        "best_boundary_mode": "interpolated_exact",
        "best_distance_source": "records.distance_m",
        "best_max_sample_gap_s": B.MAX_SAMPLE_GAP_S,
        "cadence_formula": "(cadence_raw_rpm + fractional_cadence) x 2",
        "cadence_min_bin_time_s": B.MIN_BIN_TIME_S,
        # --- §6.9 drift onset ----------------------------------------------
        "onset_bin_s": DO.ONSET_BIN_S,
        "onset_passage_split_gap_s": DO.PASSAGE_SPLIT_GAP_S,
        "onset_min_passages_per_bin": DO.MIN_PASSAGES_PER_BIN,
        "onset_min_usable_bins": DO.MIN_USABLE_BINS,
        "onset_min_runs": DO.MIN_ONSET_RUNS,
        "onset_max_cv": DO.MAX_ONSET_CV,
        # --- grade smoothing --------------------------------------------
        "grade_window_m": S.GRADE_WINDOW_M,
        "elev_smooth_window_m": S.ELEV_SMOOTH_M,
        # --- speed source ------------------------------------------------
        # Not a constant but a CHOICE, and the one most likely to be silently
        # reverted: records.speed_mps is grade-biased (§9) and every pace
        # number must come from differenced path_distance_m instead.
        "speed_source": "path_derived_from_path_distance_m",
        # --- DEM source and resolution -----------------------------------
        # No tile name and no CRS: each tile carries its own CRS, and which
        # tiles are loaded is DATA (the corpus fingerprint's job), not a
        # calculation parameter -- a tile name here also published where the
        # athlete runs in every calc_version payload.
        "dem_resolution_m": 1.0,
        "dem_interpolation": "bilinear",
        # --- projection ---------------------------------------------------
        # The configured metric CRS sets every path distance, slope endpoint
        # and segment position, so changing it must move the digest. It is
        # registered as a short HASH: the CRS itself names the UTM zone the
        # athlete runs in, which a payload has no reason to carry.
        "projection_crs_sha": _short_sha(config.get().projection_crs),
        # With no configured CRS the zone is DERIVED per run from its own
        # positions. That rule is a choice, registered for the same reason
        # `speed_source` is: choices are what get reverted quietly. It is the
        # NAMED constant the derivation is built around, not a sentence here:
        # UTM zone (WGS84) of each run's own median position when
        # projection_crs is unset; the zone is part of segment identity.
        "projection_default": D.DERIVED_PROJECTION_RULE,
        # --- segment unit -------------------------------------------------
        "segment_unit": "slope",
        "segment_identity": "(place, side)",
        "slope_enter_pct": S.SLOPE_ENTER_PCT,
        "slope_exit_pct": S.SLOPE_EXIT_PCT,
        "slope_min_vertical_m": S.SLOPE_MIN_VERTICAL_M,
        "bench_max_m": S.BENCH_MAX_M,
        "match_radius_m": M.MATCH_RADIUS_M,
        "match_elev_tol_m": M.ELEV_TOL_M,
        "match_side_tol_deg": M.SIDE_TOL_DEG,
        "peak_snap_m": M.PEAK_SNAP_M,
        # --- GAP curve fit -------------------------------------------------
        # Unbuilt (§6.1). Recorded as absent rather than omitted, so the day a
        # curve is fitted the digest changes and every prior number is
        # distinguishable from every later one.
        "gap_curve": "unfitted_population_minetti",
        "gap_validated": False,
        # --- stopped-time definition ---------------------------------------
        "stopped_time_excluded_from_rates": True,
        "stopped_time_source": "stops table for segment-grain rates; for "
        "§6.0 the union of stops and rwd_stand splits, "
        "merged. Never dropped from bronze (§4.3).",
        # --- other choices that have already changed once ------------------
        "prominence_min_m": S.PROMINENCE_MIN_M,
        "hr_lag_s": 40,
        "dual_source_rule": "one activity per run_id, garmin_watch preferred",
    }


def digest(params: dict | None = None) -> str:
    """Content digest of the parameter set. Changing any value changes this."""
    p = calc_params() if params is None else params
    blob = json.dumps(p, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()[:12]


def calc_version() -> dict:
    """The FULL version: the digest AND the parameters behind it.

    Served by spec_context only. An opaque digest is nearly as bad as a hidden
    one, so the parameters are always one call away -- but not in every
    envelope, where they were 3,959 of a 4,489-byte sync_status response.
    """
    params = calc_params()
    return {
        "pipeline_version": PIPELINE_VERSION,
        "digest": digest(params),
        "params": params,
    }


def calc_version_ref() -> dict:
    """What every tool result carries: enough to tell two codes apart.

    A changed digest says the CODE moved; spec_context's calc_version says
    which parameter did.
    """
    return {"pipeline_version": PIPELINE_VERSION, "digest": digest()}
