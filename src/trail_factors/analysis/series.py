"""§17.1b the binned session series — the data behind Garmin's charts.

WHAT THIS IS FOR. The coach reasons "over the run": where the drift started,
where the walking clustered, whether cadence held on the last climb. None of
that is answerable from run-level scalars, and none of it needs a picture.

NOTHING HAS TO BE FETCHED. The cached Garmin summary carries no series at all —
only scalars and `splitSummaries` — so the data behind Garmin's own charts IS
the FIT already in `records`. HR, cadence, run/walk state and the odometer are
present on every watch run; elevation and grade only where a DEM tile covers
the run.

WHY IT IS BINNED RATHER THAN RAW, AND WHY THE BIN CARRIES SECONDS.

A raw series is a bronze row, and §3 plus the §14m audit establish that no tool
returns one. Binning is what keeps that invariant intact: a bin is an
aggregate, not a record.

THE BIN WIDTH IS DECIDED BY THE WALK SPLITS, not by payload size. In a
run/walk practice most `rwd_walk` splits can be shorter than a 30 s bin (and
shorter still than a 100 m one). A bin that carried a MAJORITY run/walk label
would therefore erase most of the walking on a run whose run/walk alternation
is the whole point of the series.

So: **every bin carries run_s / walk_s / stand_s as SECONDS.** A bin that was
20 s running and 10 s walking says so, and no alternation is lost. This is the
one design rule here that is not negotiable — a majority label is the failure
this measurement exists to avoid.

EVERY BIN CARRIES BOTH INDICES. Elapsed seconds AND path distance, so "at
minute 40" and "at mile 4" are both answerable from one payload without a
second series.

ELEVATION BY DISTANCE IS A ROUTE FINGERPRINT, SO IT IS OPT-IN. Matched against
a known trail's profile it can identify where the athlete runs, without a
single coordinate. Bins carry `elevation_m` only when the athlete config sets
`route_fingerprint_accepted: true`; otherwise every bin's elevation is null and
the payload says why and how to opt in. Read at CALL time, never cached.
Position (latitude/longitude) is never emitted either way.
"""

from __future__ import annotations

from trail_factors import config
from trail_factors.analysis import basic as B
from trail_factors.units import METERS_PER_MILE, pace_per_mile

CALC_VERSION = 1

# §17.1b-bis RUNNING DYNAMICS PER BIN. Same four fields as §6.0's
# running_dynamics, same units, same rwd_run-only gate -- reused rather than
# restated, so the two cannot drift.
#
# `vertical_ratio` IS THE DEVICE'S OWN FIELD AND IS NEVER RECOMPUTED from
# oscillation / step length. It is Garmin's published quantity; deriving it
# here would silently substitute our arithmetic for theirs on records where
# the device wrote a ratio and dropped one of its inputs -- and it drops them
# independently (below).
#
# N IS PER FIELD, NOT PER BIN, AND THAT IS MEASURED, NOT DEFENSIVE. The device
# drops these four independently: a run can carry stance_time on a handful of
# records while step_length and vertical oscillation are on thousands. A
# single per-bin n would present a 5-sample stance time as though ~25 samples
# stood behind it.
DYNAMICS_FIELDS = B.RUNNING_DYNAMICS

# 30 s. Chosen from the walk-split distribution above rather than a size
# budget: it is about the largest bin that still resolves a typical short walk
# as a distinguishable fraction of a bin rather than as noise.
BIN_S = 30.0

# A bin needs at least this fraction of its span covered by records before its
# means are reported. Below it the bin spans a sample gap and its pace would be
# a straight line drawn across missing ground.
MIN_BIN_COVERAGE = 0.5


def _dynamics_by_bin(con, activity_id: str, t0, bin_s: float, weights: dict):
    """rwd_run dynamics records, bucketed, plus what the FILE actually carries.

    EXISTS, NOT A JOIN. `running_dynamics` joins records to splits on a
    BETWEEN, which duplicates a record that falls inside two rwd_* splits --
    the same shape as the FR265 `interval_active` split that doubled the
    silver layer. EXISTS cannot duplicate whatever the splits do.

    THE FLAG CLAUSE IS BORROWED FROM §6.0, not rewritten (§15.4). Where
    records_enriched covers the run, flagged records are excluded; where it
    does not -- it is `records JOIN dem_elevations`, so it exists only where a
    DEM tile does -- every record is included and `flags_available` says so.
    The same figure with and without flags is NOT the same quantity.
    """
    dyn = B._dynamics_filters(con, activity_id)
    cols = sorted(DYNAMICS_FIELDS)
    rows = con.execute(
        f"""
        SELECT r.timestamp, {", ".join("r." + c for c in cols)}
        FROM records r
        WHERE r.activity_id = ? AND EXISTS (
            SELECT 1 FROM splits s
            WHERE s.activity_id = r.activity_id AND s.split_type = 'rwd_run'
              AND r.timestamp BETWEEN s.start_time AND s.end_time)
          {dyn["_flag_clause"]}
        ORDER BY r.timestamp
    """,
        [activity_id],
    ).fetchall()

    # WHAT THE FILE CARRIES, asked of the WHOLE activity rather than of the
    # running records: a field the device never wrote anywhere is a hardware
    # or activity-type fact, and it must not be confused with a field that is
    # simply absent from the bins that happen to contain running.
    present = con.execute(
        f"""
        SELECT {", ".join(f"count(r.{c})" for c in cols)}
        FROM records r WHERE r.activity_id = ?""",
        [activity_id],
    ).fetchone()
    available = {c: (present[i] or 0) > 0 for i, c in enumerate(cols)}

    buckets: dict[int, dict] = {}
    for row in rows:
        ts = row[0]
        k = int((ts - t0).total_seconds() // bin_s)
        b = buckets.setdefault(k, {"n_run": 0, **{c: [] for c in cols}})
        b["n_run"] += 1
        w = weights.get(ts, 0.0)
        for i, c in enumerate(cols, start=1):
            if row[i] is not None:
                b[c].append((float(row[i]), w))
    return buckets, available, dyn


def _dynamics_for_bin(bucket, available) -> dict:
    """The four fields for one bin, and the REASON when they are null.

    THREE ABSENCES, NEVER 0.0, AND ALL THREE DISTINGUISHABLE:

      reason "no_device_channel"  -- no record in this ACTIVITY carries any of
                                     these fields. The FR245 has no running
                                     dynamics at all, and an FR265 hike writes
                                     none either.
      reason "no_running_in_bin"  -- the device records them, but this bin has
                                     no rwd_run records: walking, standing, or
                                     a gap. Walking dynamics are not running
                                     dynamics and are not substituted.
      reason null, field n == 0   -- there IS running here and the device
                                     records the field, but it wrote nothing
                                     for it in this bin. That is the device
                                     dropping a field mid-run, which it does
                                     independently per field.

    A mean over a SUBSET of the bin's running records is fine and is not
    hidden: n is the count that carried the field, per field, so a 5-sample
    mean and a 28-sample mean are never confusable.
    """
    out = {}
    if not any(available.values()):
        for _field, (unit, key) in sorted(DYNAMICS_FIELDS.items()):
            out[key] = {"value": None, "n": 0, "unit": unit}
        return {"dynamics": out, "dynamics_reason": "no_device_channel"}
    if bucket is None or bucket["n_run"] == 0:
        for _field, (unit, key) in sorted(DYNAMICS_FIELDS.items()):
            out[key] = {"value": None, "n": 0, "unit": unit}
        return {"dynamics": out, "dynamics_reason": "no_running_in_bin"}
    for field, (unit, key) in sorted(DYNAMICS_FIELDS.items()):
        samples = bucket[field]
        den = sum(w for _, w in samples)
        # TIME-WEIGHTED, like every other mean over records in this project:
        # sampling is irregular and a plain mean over-weights dense stretches.
        # Falls back to the unweighted mean only when the weights sum to zero
        # (every sample inside a stop), which is stated rather than silent.
        if not samples:
            out[key] = {"value": None, "n": 0, "unit": unit}
        elif den > 0:
            out[key] = {
                "value": sum(v * w for v, w in samples) / den,
                "n": len(samples),
                "unit": unit,
            }
        else:
            out[key] = {
                "value": sum(v for v, _ in samples) / len(samples),
                "n": len(samples),
                "unit": unit,
                "weighting": "unweighted (all samples inside a stop)",
            }
    return {"dynamics": out, "dynamics_reason": None}


def binned_series(
    con, activity_id: str, bin_s: float = BIN_S, include_dynamics: bool = True
) -> dict:
    """Pace, HR, elevation, cadence and run/walk/stand state, per time bin.

    Indexed by BOTH elapsed time and path distance. Elevation is present only
    where a DEM tile covers the run AND the config accepts the route
    fingerprint; `elevation_available` says whether the DEM covers the run and
    `elevation_emitted` whether the bins carry it, rather than leaving a
    reader to infer either from nulls.

    `include_dynamics=False` omits the per-bin running-dynamics block from
    EVERY bin (never some bins and not others) -- it is the bulk of the
    payload. The payload-level availability fields are still computed, so a
    reader knows whether asking for them would return anything.
    """
    rows = con.execute(
        """
        SELECT r.timestamp, r.elapsed_s, r.distance_m, r.heart_rate,
               r.cadence_raw_rpm, r.fractional_cadence, e.altitude_dem_m
        FROM records r
        LEFT JOIN records_enriched e USING (activity_id, timestamp)
        WHERE r.activity_id = ? ORDER BY r.timestamp
    """,
        [activity_id],
    ).fetchall()
    if len(rows) < 3:
        return {
            "bins": [],
            "status_hint": "unavailable",
            "reason": "fewer than three records",
            "filters": _filters(con, activity_id, bin_s, False),
        }

    t0 = rows[0][0]
    weights = B.record_weights(con, activity_id)
    splits = con.execute(
        """
        SELECT split_type, start_time, end_time FROM splits
        WHERE activity_id = ? AND split_type LIKE 'rwd_%' ORDER BY start_time
    """,
        [activity_id],
    ).fetchall()

    dyn_buckets, dyn_available, dyn_filters = _dynamics_by_bin(
        con, activity_id, t0, bin_s, weights
    )
    have_elev = any(r[6] is not None for r in rows)
    # THE FINGERPRINT GATE, read now so a test (or a config change) applies to
    # this call. Elevation is dropped at the bucket, so no bin can carry it.
    emit_elev = config.get().route_fingerprint_accepted
    span = (rows[-1][0] - t0).total_seconds()
    n_bins = int(span // bin_s) + 1
    buckets: dict[int, dict] = {}
    for ts, _elapsed, dist, hr, cad, frac, elev in rows:
        k = int((ts - t0).total_seconds() // bin_s)
        b = buckets.setdefault(
            k, {"d": [], "hr": [], "cad": [], "elev": [], "w": 0.0, "t": []}
        )
        w = weights.get(ts, 0.0)
        b["t"].append(ts)
        b["w"] += w
        if dist is not None:
            b["d"].append(dist)
        if hr is not None:
            b["hr"].append((hr, w))
        if cad is not None and cad > 0:
            b["cad"].append((B.CADENCE_STEPS_PER_STRIDE * (cad + (frac or 0.0)), w))
        if elev is not None and emit_elev:
            b["elev"].append(elev)

    import datetime

    out = []
    for k in range(n_bins):
        b = buckets.get(k)
        start = t0 + datetime.timedelta(seconds=k * bin_s)
        end = start + datetime.timedelta(seconds=bin_s)
        row = {"bin": k, "elapsed_start_s": k * bin_s, "elapsed_end_s": (k + 1) * bin_s}
        # STATE AS SECONDS, NEVER A MAJORITY LABEL -- most short walks fit
        # inside one bin, so a label would erase them.
        for cls, key in (
            ("rwd_run", "run_s"),
            ("rwd_walk", "walk_s"),
            ("rwd_stand", "stand_s"),
        ):
            # float() because sum() over an empty generator returns int 0,
            # and a payload field whose type depends on the data is a contract
            # a reader has to guess at.
            row[key] = float(
                sum(
                    max(0.0, (min(end, e) - max(start, s)).total_seconds())
                    for st, s, e in splits
                    if st == cls
                    and s is not None
                    and e is not None
                    and e > start
                    and s < end
                )
            )
        # THE DYNAMICS BLOCK IS ADDED TO EVERY BIN, INCLUDING THIS EMPTY-BIN
        # PATH. A key that appears on some bins and not others makes every
        # consumer write a .get() and guess what its absence meant. When it is
        # not requested it is absent from ALL bins, for the same reason.
        if include_dynamics:
            row.update(_dynamics_for_bin(dyn_buckets.get(k), dyn_available))
        if not b:
            row.update(
                {
                    "distance_start_m": None,
                    "distance_end_m": None,
                    "mps": None,
                    "pace_per_mile": None,
                    "avg_hr": None,
                    "steps_per_min": None,
                    "elevation_m": None,
                    "coverage": 0.0,
                    "n_records": 0,
                }
            )
            out.append(row)
            continue
        covered = (b["t"][-1] - b["t"][0]).total_seconds()
        coverage = covered / bin_s if bin_s > 0 else 0.0
        moving = b["w"]
        # NOT DISTANCE-DERIVED, SO NOT GATED ON DISTANCE. A bin with heart-rate
        # records and fewer than two distance records used to report every
        # field null -- avg_hr included -- and n_records 0, discarding HR
        # that was measured. Only the distance-derived fields go null now.
        row.update(
            {
                "n_records": len(b["t"]),
                "coverage": coverage,
                "avg_hr": (sum(h * w for h, w in b["hr"]) / sum(w for _, w in b["hr"]))
                if sum(w for _, w in b["hr"]) > 0
                else None,
                "steps_per_min": (
                    sum(c * w for c, w in b["cad"]) / sum(w for _, w in b["cad"])
                )
                if sum(w for _, w in b["cad"]) > 0
                else None,
                "elevation_m": (sum(b["elev"]) / len(b["elev"])) if b["elev"] else None,
            }
        )
        if len(b["d"]) < 2:
            row.update(
                {
                    "distance_start_m": None,
                    "distance_end_m": None,
                    "mps": None,
                    "pace_per_mile": None,
                    "distance_reason": (
                        f"{len(b['d'])} record(s) in this bin carry a "
                        f"distance; two are needed for distance or pace"
                    ),
                }
            )
            out.append(row)
            continue
        d0, d1 = b["d"][0], b["d"][-1]
        row.update(
            {
                "distance_start_m": d0,
                "distance_end_m": d1,
                "distance_start_mi": d0 / METERS_PER_MILE,
                # Pace over MOVING seconds in the bin, so a bin containing a stop
                # is not reported as slow running.
                "mps": ((d1 - d0) / moving)
                if moving > 0 and coverage >= MIN_BIN_COVERAGE
                else None,
            }
        )
        row["pace_per_mile"] = pace_per_mile(row["mps"])
        out.append(row)

    return {
        "bins": out,
        "n_bins": len(out),
        "bin_s": bin_s,
        "elevation_available": have_elev,
        "elevation_emitted": have_elev and emit_elev,
        "elevation_withheld_reason": (
            None if (emit_elev or not have_elev) else _WITHHELD_NOTE
        ),
        "dynamics_included": include_dynamics,
        "dynamics_included_note": (
            None
            if include_dynamics
            else "per-bin running dynamics were NOT requested and are absent "
            "from every bin; call session_series(include_dynamics=true) "
            "for them. dynamics_available below says whether they exist."
        ),
        # PAYLOAD-LEVEL, so a reader knows before walking 150 bins whether
        # the nulls mean "this watch cannot" or "this bin did not".
        "dynamics_available": any(dyn_available.values()),
        "dynamics_fields_available": {
            DYNAMICS_FIELDS[f][1]: v for f, v in sorted(dyn_available.items())
        },
        "dynamics_reason": (
            None
            if any(dyn_available.values())
            else "no record in this activity carries running dynamics. The "
            "FR245 records none of them (no accessory), and an FR265 "
            "writes none on a non-running activity. This is the device "
            "and the activity type, not a gap in the run."
        ),
        # THE SAME FIGURE WITH AND WITHOUT §15.4 FLAGS IS NOT THE SAME
        # QUANTITY, so the payload says which one it is rather than
        # leaving a reader to assume the stricter one.
        "flags_available": dyn_filters["quality_flags_available"],
        "filters": _filters(con, activity_id, bin_s, have_elev, dyn_filters),
    }


_WITHHELD_NOTE = (
    "a DEM covers this run, but elevation indexed by distance is a route "
    "fingerprint and route_fingerprint_accepted is false in the athlete "
    "config, so every bin's elevation_m is null. Set "
    "route_fingerprint_accepted: true in config/athlete.yaml to include it."
)


def _elevation_note(con) -> str:
    """How DEM-gated the corpus is, COMPUTED at call time. A count written
    into a string is a dead literal the moment the next run arrives."""
    try:
        n_enr, n_act = con.execute(
            "SELECT (SELECT count(DISTINCT activity_id) FROM records_enriched), "
            "(SELECT count(*) FROM activities WHERE source = 'garmin_watch')"
        ).fetchone()
        return f"DEM-gated: present on {n_enr} of {n_act} garmin activities"
    except Exception:
        return (
            "DEM-gated: present only where a tile covers the run "
            "(the corpus count could not be read here)"
        )


def _filters(
    con, activity_id: str, bin_s: float, have_elev: bool, dyn: dict | None = None
) -> dict:
    regime = con.execute(
        "SELECT sampling_regime, coverage_fraction FROM activities "
        "WHERE activity_id = ?",
        [activity_id],
    ).fetchone()
    return {
        "bin_s": bin_s,
        "bin_width_rationale": "chosen from the walk-split distribution: in a "
        "run/walk practice most walks can be shorter "
        "than a 30 s bin",
        "state_representation": "SECONDS per state (run_s/walk_s/stand_s)",
        "state_is_majority_label": False,
        "state_note": "a majority label would erase most walks; the seconds "
        "preserve the run/walk alternation",
        "indexed_by": ["elapsed_s", "distance_m"],
        "pace_denominator": "moving seconds in the bin",
        "hr_weighting": "time",
        "cadence_weighting": "time",
        "cadence_zero_excluded": True,
        "min_bin_coverage": MIN_BIN_COVERAGE,
        "elevation_source": "records_enriched.altitude_dem_m (USGS 1 m lidar)",
        "elevation_available": have_elev,
        # COMPUTED, NOT WRITTEN DOWN. A count in a payload string must be
        # computed at call time or not be there; a literal goes stale with the
        # next run.
        # Computed, and DEFENSIVE: this runs against synthetic fixture
        # databases too, which carry only the columns the series itself reads.
        # A note is a note -- it must never be the reason a payload fails.
        "elevation_note": _elevation_note(con),
        "grain": "time bins, not records",
        "grain_note": "a bin is an aggregate, which is what keeps §3's "
        "no-bronze-row invariant intact",
        "sampling_regime": regime[0] if regime else None,
        "gps_coverage_fraction": regime[1] if regime else None,
        "position_emitted": False,
        "route_fingerprint_accepted": config.get().route_fingerprint_accepted,
        "route_fingerprint_note": "elevation indexed by distance IS a route "
        "fingerprint against a "
        "known trail. It is emitted only when route_fingerprint_accepted "
        "is true in the athlete config (currently "
        + (
            "true"
            if config.get().route_fingerprint_accepted
            else "false, so elevation_m is null in every bin"
        )
        + "). No latitude or longitude is ever emitted either way.",
        "quality_flags_excluded": False,
        # GAIT CONVENTIONS DIFFER BETWEEN FIELDS IN THIS PAYLOAD, AND SAYING
        # SO IS THE POINT. `steps_per_min` is computed over ALL records in the
        # bin with a non-zero cadence -- running, walking and everything
        # between -- while the four dynamics fields are rwd_run ONLY. They are
        # therefore over DIFFERENT RECORD SETS in the same bin and must not be
        # read as describing the same gait. steps_per_min is left exactly as
        # it was; this declares the difference rather than silently changing a
        # number every existing consumer already reads.
        "gait_conventions": {
            "steps_per_min": "ALL records in the bin with cadence > 0 "
            "(running, walking and standing steps pooled); "
            "§15.4 flags NOT applied",
            "avg_hr": "all records in the bin; §15.4 flags NOT applied",
            "mps": "bin distance over MOVING seconds; flags NOT applied",
            **{
                DYNAMICS_FIELDS[f][
                    1
                ]: "rwd_run records ONLY; §15.4 flags applied where "
                "records_enriched covers the run (see flags_available)"
                for f in sorted(DYNAMICS_FIELDS)
            },
        },
        "dynamics_units": {
            DYNAMICS_FIELDS[f][1]: DYNAMICS_FIELDS[f][0]
            for f in sorted(DYNAMICS_FIELDS)
        },
        "dynamics_units_note": "Garmin's units, unchanged: vertical oscillation and "
        "step length "
        "are MILLIMETRES, ground contact time MILLISECONDS, vertical "
        "ratio a PERCENT. vertical_ratio is the DEVICE'S OWN field and is "
        "never recomputed from the other two.",
        "dynamics_split_type": "rwd_run",
        "dynamics_weighting": "time",
        "dynamics_n_is_per_field": True,
        "dynamics_n_note": "n counts the rwd_run records that carried THAT field, not "
        "the "
        "bin's running records. The device drops the four independently "
        "-- one field can be on a handful of records where another is on "
        "thousands -- so a per-bin n could misstate the evidence behind a "
        "field by orders of magnitude.",
        "dynamics_flags_excluded": bool(dyn and dyn["quality_flags_available"]),
        "dynamics_flags_reason": (dyn or {}).get("quality_flags_reason"),
        "dynamics_absence_vocabulary": {
            "no_device_channel": "no record in this ACTIVITY carries these "
            "fields (FR245, phone file, or a non-running "
            "sport on an FR265)",
            "no_running_in_bin": "the device records them; this bin has no "
            "rwd_run records (walking, standing, a gap)",
            "null_reason_with_n_0": "running present and the device records "
            "the field, but it wrote none in this "
            "bin -- the device dropping a field "
            "mid-run",
        },
    }
