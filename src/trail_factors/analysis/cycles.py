"""M3 run/walk cycle economics — does the walk buy anything (§17.2,
docs/proposed-metrics-v1.md).

**CHOICE, not capacity (§6.6).** This measures how the athlete elected to
structure the run. It says nothing about what they could have done.

THE QUESTION IT ANSWERS. M3 tests whether a walk break buys faster running in
the following run block -- whether the walk still earns its place as fitness
rises -- and that is answerable from data already on disk.

WHY IT IS BUILT FIRST DESPITE RANKING THIRD. It needs no DEM, no GPS coverage
and no 1 Hz: the `rwd_*` splits carry `total_timer_time` on every watch-
recorded Garmin activity in both sampling regimes (§6.0). It is the only proposed metric
with
immediate history.

A CYCLE IS ONE `rwd_run` BLOCK PLUS THE WALK THAT FOLLOWS IT. `rwd_stand`
blocks between them are recorded as `stand_s` and do NOT break the cycle —
standing at a road crossing is not the athlete abandoning the structure. A run
block with no following walk (the last one, usually) is a run block, not a
cycle, and is reported with `walk_duration_s = None` rather than 0: no walk and
a zero-length walk are different things.

TWO DERIVED SERIES, BOTH WITHIN ONE RUN:
  PURCHASE  run pace of cycle n+1 against walk duration of cycle n — did the
            walk buy speed?
  DECAY     run pace by cycle index — the fade the structure defends against.

Both are reported as the PAIRED SERIES, not as a fitted slope. A slope over
four points is a number with a confidence interval wider than itself, and §6.1
already records what happens when a coefficient is trusted from one run.

THE PURCHASE CURVE ASSUMES THE WALKS WERE STRUCTURED, and this build records
nothing about what governed them. That changes what the metric MEASURES
rather than how precise it is: walks answering a heart-rate ceiling or a
timer price what the structure delivered, while walks taken by feel measure
WILLINGNESS -- the identical arithmetic, a different quantity. Pooling the two
averages two quantities under one name, which is §6.6's choice-versus-capacity
distinction arriving at a metric that did not know it needed it.

SO NO RUN'S CURVE IS POOLABLE WITH ANOTHER'S IN THIS BUILD. Each result says
`walk_protocol: null`, labelled "not collected", and `poolable_with_other_runs:
false`. Structure that has not been established cannot be assumed, and an
unknown protocol is not a by-feel one -- it is an unknown one.
"""

from __future__ import annotations

import statistics

from trail_factors.analysis.basic import MIN_CLASS_TIME_S, record_weights, weighted_mean
from trail_factors.analysis.zones import HR_LAG_S
from trail_factors.units import pace_per_mile

CALC_VERSION = 1

# A WALK MUST OUTLAST THE HR TAIL TO BE MEASURABLE.
#
# `hr_recovered_bpm` is HR at the walk's first record minus HR at its last.
# Heart rate lags effort by ~40 s (HR_LAG_S, §14a), so on a 15 s walk the
# closing reading reflects what the athlete was doing 25 s BEFORE the walk
# began -- which, on this terrain, is usually mid-climb. The figure then reads
# NEGATIVE, and it reads negative because the instrument cannot see the walk,
# not because the walk failed.
#
# Measured on the development corpus: every negative-recovery walk was
# preceded by climbing, and every positive one was longer than a minute.
#
# THE MARGIN IS DERIVED, NOT PICKED. The threshold is the lag itself plus one
# 5 s margin, so a walk has to outlast the tail AND leave a sample on the far
# side of it. It moves with HR_LAG_S by construction: a better lag estimate
# re-gates every walk in the corpus without anyone remembering to change a
# second number.
HR_RECOVERY_MARGIN_S = 5.0
MIN_WALK_FOR_RECOVERY_S = HR_LAG_S + HR_RECOVERY_MARGIN_S

# A purchase curve from two points is not a curve. The document proposes 4 and
# the development corpus supported it: about two thirds of activities had at
# least 4 run blocks, so the gate admits the majority while excluding the runs
# that would produce a two-point "trend".
MIN_CYCLES = 4


def cycles(con, activity_id: str) -> dict:
    """Run/walk cycles for one run, with the purchase and decay series."""
    splits = con.execute(
        """
        SELECT split_index, split_type, start_time, end_time,
               total_timer_time, total_distance_m
        FROM splits WHERE activity_id = ? AND split_type LIKE 'rwd_%'
        ORDER BY start_time, split_index
    """,
        [activity_id],
    ).fetchall()
    filters = _filters(con, activity_id)
    if not splits:
        # DERIVED FROM THE ACTIVITY, not asserted. The old string named the
        # Strava file and an import as "two causes, both real" -- on a hike,
        # which is neither. See basic.no_splits_reason.
        from trail_factors.analysis.basic import no_splits_reason

        why = no_splits_reason(con, activity_id)
        return {
            "cycles": [],
            "n_cycles": 0,
            "status_hint": "unavailable",
            "no_splits_cause": why["cause"],
            "reason": (
                f"this activity carries no rwd_* splits, so there "
                f"are no cycles to price: {why['reason']}"
            ),
            "filters": filters,
        }

    weights = record_weights(con, activity_id)
    t0 = splits[0][2]
    built, current = [], None
    for _idx, kind, start, end, timer, dist in splits:
        if kind == "rwd_run":
            if current is not None:
                built.append(current)
            current = {
                "run_start": start,
                "run_end": end,
                "run_duration_s": float(timer or 0.0),
                "run_distance_m": float(dist or 0.0),
                "walk_start": None,
                "walk_end": None,
                "walk_duration_s": None,
                "walk_distance_m": None,
                "stand_s": 0.0,
            }
        elif current is None:
            continue  # a walk before any run: not a cycle
        elif kind == "rwd_walk":
            if current["walk_duration_s"] is None:
                current.update(
                    {
                        "walk_start": start,
                        "walk_end": end,
                        "walk_duration_s": float(timer or 0.0),
                        "walk_distance_m": float(dist or 0.0),
                    }
                )
            else:
                # A second walk before the next run: the structure did not
                # reset, so it belongs to the same cycle.
                current["walk_duration_s"] += float(timer or 0.0)
                current["walk_distance_m"] += float(dist or 0.0)
                current["walk_end"] = end
        elif kind == "rwd_stand":
            current["stand_s"] += float(timer or 0.0)
    if current is not None:
        built.append(current)

    out = []
    for i, c in enumerate(built):
        run_s, run_m = c["run_duration_s"], c["run_distance_m"]
        # §6.0's fitted floor applies to the per-cycle pace exactly as it does
        # to a class pace: below it the figure is unstable to worse than
        # 60 s/mile. The DURATION is still reported -- a short block is data.
        pace_ok = run_s >= MIN_CLASS_TIME_S and run_m > 0
        row = {
            "cycle_index": i,
            "elapsed_at_cycle_start_s": (c["run_start"] - t0).total_seconds(),
            "run_duration_s": run_s,
            "run_distance_m": run_m,
            "run_mps": (run_m / run_s) if pace_ok else None,
            "run_pace_per_mile": pace_per_mile(run_m / run_s) if pace_ok else None,
            "run_pace_gated": not pace_ok,
            "run_mean_hr": _mean_hr(
                con, activity_id, c["run_start"], c["run_end"], weights
            ),
            "walk_duration_s": c["walk_duration_s"],
            "walk_distance_m": c["walk_distance_m"],
            "stand_s": c["stand_s"],
        }
        if c["walk_start"] is not None:
            a = _edge_hr(con, activity_id, c["walk_start"], c["walk_end"], "first")
            b = _edge_hr(con, activity_id, c["walk_start"], c["walk_end"], "last")
            row["hr_at_walk_start"] = a
            row["hr_at_walk_end"] = b
            walk_s = c["walk_duration_s"]
            if walk_s is not None and walk_s < MIN_WALK_FOR_RECOVERY_S:
                # WITHHELD, NOT COMPUTED. A number that can only be noise is
                # worse than an absence: it is read as evidence the walk did
                # not work, when it is evidence the measurement could not see
                # it. The walk still counts as a cycle -- only the recovery
                # figure is suppressed.
                row["hr_recovered_bpm"] = None
                row["hr_recovery_status"] = "shorter_than_hr_lag"
                row["hr_recovery_reason"] = (
                    f"this walk lasted {walk_s:.0f} s against an HR lag of "
                    f"{HR_LAG_S:.0f} s, so its closing reading reflects effort "
                    f"from before the walk began -- usually the climb that "
                    f"prompted it. A walk must outlast the tail "
                    f"({MIN_WALK_FOR_RECOVERY_S:.0f} s) to be measurable. This "
                    f"is a property of the INSTRUMENT, not of the walk."
                )
            else:
                # POSITIVE MEANS RECOVERED. Named for the direction it reads
                # in, because a signed quantity whose sign has to be remembered
                # is one a reader eventually gets backwards.
                row["hr_recovered_bpm"] = (
                    (a - b) if (a is not None and b is not None) else None
                )
                row["hr_recovery_status"] = (
                    "ok" if row["hr_recovered_bpm"] is not None else "unavailable"
                )
                row["hr_recovery_reason"] = (
                    None
                    if row["hr_recovered_bpm"] is not None
                    else "no heart rate at one or both edges of the walk"
                )
        else:
            row.update(
                {
                    "hr_at_walk_start": None,
                    "hr_at_walk_end": None,
                    "hr_recovered_bpm": None,
                    "hr_recovery_status": "no_walk",
                    "hr_recovery_reason": "this run block has no walk after "
                    "it; that is not a zero-length "
                    "walk",
                }
            )
        out.append(row)

    complete = [c for c in out if c["walk_duration_s"] is not None]
    status = "ok" if len(complete) >= MIN_CYCLES else "gated"
    # THE SAME COUNT AS n_cycles, AND THE OTHER ONE NAMED. The reason said
    # "3 complete run->walk cycles; 4 are needed" beside `n_cycles: 4` -- two
    # counts, one unnamed, reading as a contradiction. The
    # gate is on COMPLETE cycles, so that number stays; it is now stated as a
    # part of n_cycles rather than instead of it.
    reason = (
        None
        if status == "ok"
        else (
            f"{len(out)} run blocks (n_cycles), {len(complete)} of them complete "
            f"run->walk cycles -- a run block with no walk after it has no walk "
            f"to price; {MIN_CYCLES} complete cycles are needed before a purchase "
            f"curve is a curve rather than two points"
        )
    )
    return {
        "cycles": out,
        "n_cycles": len(out),
        "n_complete_cycles": len(complete),
        "status_hint": status,
        "reason": reason,
        "purchase": _purchase(out) if status == "ok" else [],
        "decay": _decay(out) if status == "ok" else [],
        "summary": _summary(out, complete),
        # WHAT GOVERNED THE WALKS, and therefore what the two series above
        # MEAN. Carried on the result rather than left for the reader to look
        # up: a rule the reader must remember is one the reader eventually
        # forgets, and this one changes the quantity.
        **_walk_protocol_gate(),
        "filters": filters,
    }


def _walk_protocol_gate() -> dict:
    """What governed the walks -- not collected in this build, so never pooled.

    A constant, and deliberately so: there is no per-run source for the
    protocol, so every run reports the same labelled absence rather than a
    default standing in for an answer.
    """
    from trail_factors.ingest.answers import NOT_COLLECTED

    return {
        "walk_protocol": None,
        "walk_protocol_status": NOT_COLLECTED,
        "poolable_with_other_runs": False,
        "pooling_note": (
            "walk protocols are not collected in this build, so nothing says "
            "whether the walks were triggered by a heart-rate ceiling, a clock or "
            "feel -- and those make purchase and decay different "
            "quantities. No run's walks are pooled with another's; read "
            "this run on its own."
        ),
    }


def _mean_hr(con, activity_id, start, end, weights) -> float | None:
    """Time-weighted mean HR over a block, using the shared weight map."""
    if start is None or end is None:
        return None
    rows = con.execute(
        """
        SELECT timestamp, heart_rate FROM records
        WHERE activity_id = ? AND timestamp BETWEEN ? AND ? ORDER BY timestamp
    """,
        [activity_id, start, end],
    ).fetchall()
    num, den = weighted_mean(rows, weights)
    return (num / den) if den > 0 else None


def _edge_hr(con, activity_id, start, end, which: str) -> float | None:
    """HR at the first or last record inside a block.

    A single sample rather than a windowed mean, and that is deliberate: a
    run/walk walk is often well under 30 s, so a 10 s window would be most of
    the block and "start" and "end" would overlap. HR is smooth at this grain; the risk
    of a
    single bad sample is smaller than the risk of measuring the same thing
    twice and calling it a recovery.
    """
    order = "ASC" if which == "first" else "DESC"
    row = con.execute(
        f"""
        SELECT heart_rate FROM records
        WHERE activity_id = ? AND timestamp BETWEEN ? AND ?
          AND heart_rate IS NOT NULL
        ORDER BY timestamp {order} LIMIT 1
    """,
        [activity_id, start, end],
    ).fetchone()
    return float(row[0]) if row else None


def _purchase(rows: list[dict]) -> list[dict]:
    """Did the walk buy speed? Cycle n's walk against cycle n+1's run pace.

    Returned as PAIRS, not as a fitted coefficient. `elapsed_at_cycle_start_s`
    travels with each pair because time into the run is the obvious confound —
    later walks are longer AND later runs are slower, and a slope through that
    would report fatigue as if it were the walk's doing.
    """
    out = []
    for a, b in zip(rows, rows[1:], strict=False):
        if a["walk_duration_s"] is None or b["run_mps"] is None:
            continue
        out.append(
            {
                "walk_of_cycle": a["cycle_index"],
                "walk_duration_s": a["walk_duration_s"],
                "hr_recovered_bpm": a["hr_recovered_bpm"],
                "next_run_cycle": b["cycle_index"],
                "next_run_mps": b["run_mps"],
                "next_run_pace_per_mile": b["run_pace_per_mile"],
                "elapsed_at_next_run_s": b["elapsed_at_cycle_start_s"],
            }
        )
    return out


def _decay(rows: list[dict]) -> list[dict]:
    """Run pace by cycle index — the fade the structure exists to defend."""
    return [
        {
            "cycle_index": c["cycle_index"],
            "elapsed_at_cycle_start_s": c["elapsed_at_cycle_start_s"],
            "run_mps": c["run_mps"],
            "run_pace_per_mile": c["run_pace_per_mile"],
            "run_mean_hr": c["run_mean_hr"],
        }
        for c in rows
        if c["run_mps"] is not None
    ]


def _summary(rows: list[dict], complete: list[dict]) -> dict:
    walks = [c["walk_duration_s"] for c in complete if c["walk_duration_s"] is not None]
    runs = [c["run_duration_s"] for c in rows if c["run_duration_s"] > 0]
    rec = [c["hr_recovered_bpm"] for c in complete if c["hr_recovered_bpm"] is not None]
    paced = [c["run_mps"] for c in rows if c["run_mps"] is not None]
    return {
        "n_run_blocks": len(rows),
        "n_complete_cycles": len(complete),
        "median_run_duration_s": statistics.median(runs) if runs else None,
        "median_walk_duration_s": statistics.median(walks) if walks else None,
        # THE MEDIAN IS OVER MEASURABLE WALKS ONLY, and the count of the rest
        # sits beside it: a median over 4 of 25 walks is a different claim from
        # a median over 25, and the difference is invisible without the count.
        "median_hr_recovered_bpm": statistics.median(rec) if rec else None,
        "n_recovery_measurable": len(rec),
        "n_recovery_shorter_than_hr_lag": sum(
            1 for c in complete if c.get("hr_recovery_status") == "shorter_than_hr_lag"
        ),
        "n_run_blocks_below_pace_gate": sum(1 for c in rows if c["run_pace_gated"]),
        "first_run_pace_per_mile": (pace_per_mile(paced[0]) if paced else None),
        "last_run_pace_per_mile": (pace_per_mile(paced[-1]) if paced else None),
    }


def _filters(con, activity_id: str) -> dict:
    regime = con.execute(
        "SELECT sampling_regime FROM activities WHERE activity_id = ?", [activity_id]
    ).fetchone()
    return {
        "cycle_definition": "one rwd_run block plus the rwd_walk that follows; "
        "rwd_stand does not break a cycle",
        "duration_source": "splits.total_timer_time",
        "sampling_regime_dependent": False,
        "sampling_regime": regime[0] if regime else None,
        "min_class_time_s": MIN_CLASS_TIME_S,
        "min_cycles": MIN_CYCLES,
        "hr_lag_s": HR_LAG_S,
        "min_walk_for_recovery_s": MIN_WALK_FOR_RECOVERY_S,
        "hr_recovery_gate": f"hr_recovered_bpm is WITHHELD below "
        f"{MIN_WALK_FOR_RECOVERY_S:.0f} s "
        f"(HR_LAG_S + {HR_RECOVERY_MARGIN_S:.0f} s margin): "
        f"a shorter walk's closing HR reflects effort from "
        f"before the walk started, so the figure can only "
        f"be noise. The walk still counts as a cycle.",
        "hr_weighting": "time, over the shared record weight map",
        "hr_edge_sampling": "single first/last record in the walk block; the "
        "walks are often under 30 s, so a window would "
        "overlap",
        "stopped_time_excluded": True,
        "quality_flags_excluded": False,
        "grain": "run/walk cycles",
        "side": "choice (§6.6) -- this is how the run was structured, not what "
        "the athlete could have done",
        "walk_protocol_gate": "walk protocols are not collected in this "
        "build, so no run's purchase and decay are "
        "poolable with another's: walks taken by feel "
        "measure willingness, walks on a rule measure "
        "structure, and nothing here says which. Read "
        "`poolable_with_other_runs`.",
        "note": "purchase and decay are returned as PAIRED SERIES, never as a "
        "fitted slope: a coefficient over four points is wider than "
        "itself, and time into the run confounds both.",
    }
