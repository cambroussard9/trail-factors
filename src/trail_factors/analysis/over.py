"""§6 primitives over a SELECTION of runs (SPEC.md §12.6).

Each function here is one §6 primitive applied across a filtered set of runs.
The computation is exactly the per-run one in src/trail_factors/analysis/primitives.py --
these do not reimplement it, they aggregate it -- so a number returned to a
coaching chat is the same number the CLI reports for the same run.

EVERY RESULT CARRIES ITS OWN PROVENANCE: the selection rule, what was included,
what was excluded and why, which gates failed, the corpus fingerprint and the
calc_version. "Drift over five long runs" means something different if two were
dropped for coverage, and a reader cannot know that unless it is said.
"""

from __future__ import annotations

import statistics

from trail_factors import config
from trail_factors.analysis import primitives as P
from trail_factors.analysis.select import select_runs, validate_filter
from trail_factors.units import pace_per_mile

CALC_VERSION = 1


def _result(name: str, unit: str, sel: dict, per_run: list[dict],
            gates: list[dict], note: str = "",
            applied_filters: dict | None = None) -> dict:
    vals = [r["value"] for r in per_run if r["value"] is not None]
    return {
        "primitive": name,
        "unit": unit,
        # THE FILTERS THIS NUMBER WAS COMPUTED UNDER, as fields rather than
        # prose. A reader that cannot see whether walking was paired, whether
        # speed was matched, or how far apart the samples had to be does not
        # know what it is looking at -- and should not lean on the number.
        "applied_filters": applied_filters,
        "selection": {"rule": sel["rule"], "n_included": sel["n_included"],
                      "date_range": sel["date_range"],
                      "included": [r["run_id"] for r in sel["included"]]},
        "excluded": sel["excluded"] + gates,
        "n_beyond_last_n": sel["n_beyond_last_n"],
        "median": statistics.median(vals) if vals else None,
        "spread": P.spread(vals),
        "per_run": per_run,
        "n_with_value": len(vals),
        "note": note,
    }


def _over(con, filt: dict, name: str, unit: str, fn, gate_reason: str,
          needs_terrain: bool = True) -> dict:
    """Run one per-activity primitive across a selection, stating exclusions."""
    problems = validate_filter(filt)
    if problems:
        raise ValueError("; ".join(problems))
    sel = select_runs(con, filt, needs_terrain=needs_terrain)
    per_run, gates = [], []
    for row in sel["included"]:
        out = fn(con, row["activity_id"])
        if out["value"] is None:
            gates.append({"activity_id": row["activity_id"],
                          "run_id": row["run_id"], "date": row["date"],
                          "reason": gate_reason})
        per_run.append({"run_id": row["run_id"], "date": row["date"],
                        "value": out["value"], "n": out.get("n", 0)})
    filters = None
    if sel["included"]:
        probe = fn(con, sel["included"][0]["activity_id"])
        filters = probe.get("filters")
    return _result(name, unit, sel, per_run, gates, applied_filters=filters)


def drift(con, **filt) -> dict:
    """§6.7 matched-terrain drift: HR delta on the SAME ground, as bpm/hour.

    Effort-matched BY CONSTRUCTION rather than by a gate, which is why §6.4
    names it the substitute when the steady-effort gate fails. This is the
    primitive behind "HR drift on long runs over the last five sessions".
    """
    return _over(con, filt, "matched_terrain_drift", "bpm/hour",
                 P.matched_terrain_drift,
                 gate_reason="no record pairs on this run met §6.7's rule "
                             "(same ground, >=10 min apart, both rwd_run, "
                             "speed matched)")


def segment_effort_drift(con, direction: str | None = None, **filt) -> dict:
    """Effort-level drift on a matched slope, optionally BY DIRECTION.

    NOT §6.7 and never to be presented as "§6.7 by direction" -- a different
    quantity under its own name. It is the only direction-aware drift the
    design can offer honestly, because its pairs are same-direction by
    construction (see primitives.segment_effort_drift).

    READ THE CEILING-EFFECT CAVEAT in applied_filters before quoting the
    ascent/descent asymmetry.
    """
    import functools
    fn = (functools.partial(P.segment_effort_drift, direction=direction)
          if direction else P.segment_effort_drift)
    name = ("segment_effort_drift" if not direction
            else f"segment_effort_drift_{direction}")
    what = f"two {direction} efforts" if direction else "two efforts"
    return _over(con, filt, name, "bpm/hour", fn,
                 gate_reason=f"no {what} on one slope at least "
                             f"{P.DRIFT_MIN_GAP_MIN} min apart on this run")


def efficiency(con, **filt) -> dict:
    """§6.8 efficiency factor: speed per heartbeat on segment-matched terrain.

    Terrain is matched by segment identity, not by GAP -- GAP is unvalidated
    (§12.2), so an EF built on it would inherit an unvalidated transform.
    """
    return _over(con, filt, "efficiency_factor", "m/s per bpm",
                 P.efficiency_factor,
                 gate_reason="no segment efforts with heart rate on this run")


def delta_hr(con, **filt) -> dict:
    """§6.3 delta-HR: the HR excursion across an effort, by direction.

    NEVER paired with tau, which is refused on this corpus (§6.3): within one
    slope tau's SD matches its median.
    """
    problems = validate_filter(filt)
    if problems:
        raise ValueError("; ".join(problems))
    direction = filt.get("direction")
    segment_id = filt.get("segment_id")
    sel = select_runs(con, {k: v for k, v in filt.items()
                            if k not in ("direction", "segment_id")})
    per_run, gates = [], []
    for row in sel["included"]:
        val, n = con.execute("""
            SELECT median(delta_hr_bpm), count(*) FROM segment_recovery
            WHERE activity_id = ?
              AND (? IS NULL OR direction = ?)
              AND (? IS NULL OR segment_id = ?)
        """, [row["activity_id"], direction, direction,
              segment_id, segment_id]).fetchone()
        if val is None:
            # A missing value has two very different causes and the status
            # model exists to keep them apart: the run genuinely has no
            # matching efforts, or segment_recovery is STALE relative to
            # segment_efforts because `recovery` has not been run since the
            # last `match`. Reporting the first when it is the second is the
            # mislabelling the four statuses are meant to prevent.
            n_efforts = con.execute(
                "SELECT count(*) FROM segment_efforts WHERE activity_id = ? "
                "AND (? IS NULL OR direction = ?) AND (? IS NULL OR segment_id = ?)",
                [row["activity_id"], direction, direction,
                 segment_id, segment_id]).fetchone()[0]
            reason = ("no matching segment efforts on this run" if not n_efforts
                      else f"STALE: this run has {n_efforts} matching efforts but "
                           f"no segment_recovery rows -- run `recovery` "
                           f"(it must follow `match`)")
            gates.append({"activity_id": row["activity_id"],
                          "run_id": row["run_id"], "date": row["date"],
                          "reason": reason})
        per_run.append({"run_id": row["run_id"], "date": row["date"],
                        "value": val, "n": n})
    note = ""
    if direction:
        # COMPUTED OVER THIS SELECTION, never a recorded corpus figure: a
        # literal here would drift from the data and be believed anyway.
        note = (f"direction={direction}. Ascent and descent are NOT comparable "
                f"on this metric: "
                + _delta_hr_by_direction(con, [r["activity_id"]
                                               for r in sel["included"]],
                                         segment_id))
    return _result("delta_hr", "bpm", sel, per_run, gates, note, applied_filters={
        "split_type": None,
        "walking_included": True,
        "direction": direction,
        "segment_id": segment_id,
        "hr_source": "peak minus opening HR within an effort",
        "stopped_time_excluded": True,
        "terrain_matched_by": "segment identity (place, side)",
        "grain": "segment efforts",
        "note": "ascent and descent are NOT comparable on this metric.",
    })


def _delta_hr_by_direction(con, activity_ids: list[str],
                           segment_id: str | None) -> str:
    """Median delta-HR per direction over the selection, as a sentence."""
    if not activity_ids:
        return "the selection has no runs to compare directions over."
    rows = con.execute("""
        SELECT direction, median(delta_hr_bpm), count(*) FROM segment_recovery
        WHERE activity_id IN (SELECT unnest(?))
          AND delta_hr_bpm IS NOT NULL
          AND (? IS NULL OR segment_id = ?)
        GROUP BY direction ORDER BY direction
    """, [activity_ids, segment_id, segment_id]).fetchall()
    if not rows:
        return "the selection has no efforts with a delta-HR."
    return ("over this selection, median " + " vs ".join(
        f"{d} {m:.1f} bpm (n={n})" for d, m, n in rows) + ".")


def segment_history(con, segment_id: str, direction: str | None = None,
                    **filt) -> dict:
    """§6.6 segment-effort trending: one slope, every traversal, over time."""
    problems = validate_filter(dict(filt, direction=direction) if direction
                               else filt)
    if problems:
        raise ValueError("; ".join(problems))
    seg = con.execute("""
        SELECT segment_id, low_elevation_m, high_elevation_m, vertical_m,
               length_m, mean_grade_pct, n_efforts, n_ascents, n_descents
        FROM segments WHERE segment_id = ?""", [segment_id]).fetchone()
    if seg is None:
        raise LookupError(f"no segment {segment_id}")
    sel = select_runs(con, filt)
    keep = {r["activity_id"] for r in sel["included"]}
    from trail_factors.analysis import basic as B

    efforts = []
    for row in con.execute("""
        SELECT e.activity_id, a.run_id, a.start_time_utc, e.direction,
               e.duration_s, e.vertical_speed_mps, e.avg_hr, e.mean_grade_pct,
               r.delta_hr_bpm, e.length_m, e.start_utc, e.end_utc,
               t.start_distance_m, t.end_distance_m
        FROM segment_efforts e JOIN activities a USING (activity_id)
        LEFT JOIN segment_recovery r
          ON r.activity_id = e.activity_id AND r.slope_index = e.slope_index
        LEFT JOIN slope_traversals t
          ON t.activity_id = e.activity_id AND t.slope_index = e.slope_index
        WHERE e.segment_id = ? AND (? IS NULL OR e.direction = ?)
        ORDER BY a.start_time_utc
    """, [segment_id, direction, direction]).fetchall():
        if row[0] not in keep:
            continue
        # PACE, WHICH THIS PRIMITIVE DID NOT CARRY. It returned duration and
        # vertical speed, so a reader trending a slope could see it getting
        # slower but not how fast it was run. Both quantities are attached and
        # they are NOT interchangeable -- see effort_paces().
        pace = B.effort_paces(con, row[0], row[10], row[11], row[9], row[4])
        # WHAT THE EFFORT AVERAGE HIDES. A stored mean_grade_pct describes the
        # whole absorbed span, which can be a short pitch plus a long plateau
        # -- a descent can store a gentle mean while its steep part is several
        # times steeper. The composition is carried rather than the boundary
        # rule moved (§5).
        prof = B.effort_profile(con, row[0], row[12], row[13], row[3])
        efforts.append({
            "run_id": row[1], "date": str(row[2])[:10], "direction": row[3],
            "duration_s": row[4], "vertical_speed_mps": row[5],
            "avg_hr": row[6], "mean_grade_pct": row[7], "delta_hr_bpm": row[8],
            "length_m": row[9],
            "mean_mps": pace["mean_mps"],
            "mean_pace_per_mile": pace["mean_pace_per_mile"],
            "best_windows": pace["windows"],
            **prof})
    return {
        "primitive": "segment_history",
        "segment": {"segment_id": seg[0], "low_elevation_m": seg[1],
                    "high_elevation_m": seg[2], "vertical_m": seg[3],
                    "length_m": seg[4], "mean_grade_pct": seg[5],
                    "n_efforts": seg[6], "n_ascents": seg[7],
                    "n_descents": seg[8]},
        "selection": {"rule": sel["rule"], "n_included": sel["n_included"],
                      "date_range": sel["date_range"]},
        "excluded": sel["excluded"],
        "efforts": efforts,
        "n_efforts_in_selection": len(efforts),
        "profile_filters": B.profile_filters(),
        "applied_filters": (B.effort_paces(
            con, efforts_probe[0], efforts_probe[1], efforts_probe[2],
            efforts_probe[3], efforts_probe[4])["filters"]
            if (efforts_probe := _probe(con, segment_id, keep)) else None),
        "note": "vertical_speed_mps is over MOVING time and is positive in "
                "both directions -- it is a rate of vertical travel, not a "
                "signed one. mean_pace_per_mile is the WHOLE effort, summit to "
                "col; best_windows are the fastest stretches INSIDE it. They "
                "answer different questions -- how the hill went, and what it "
                "can be taken at -- and must never be substituted for each "
                "other (§6.2, §16). len_*_m and steep_mean_grade_pct say how "
                "much of the effort the stored mean_grade_pct is actually "
                "describing: an effort can be mostly ground gentler than 5%, "
                "so the average alone is not a description of the pitch.",
    }


def _probe(con, segment_id: str, keep: set):
    """One effort's identifiers, for the applied_filters probe."""
    row = con.execute("""
        SELECT activity_id, start_utc, end_utc, length_m, duration_s
        FROM segment_efforts WHERE segment_id = ? ORDER BY start_utc
    """, [segment_id]).fetchall()
    for r in row:
        if r[0] in keep:
            return r
    return None


def cadence_table(con, **filt) -> dict:
    """§6.0 cadence by grade bin, pooled across a selection, split by split_type.

    Weighted by TIME, not by run: a 90-minute run and a 25-minute one do not
    carry the same evidence about a bin, and averaging their per-run means
    would give them equal say. NEVER pooled across run and walk -- the two
    modes sit tens of spm apart, so a pooled bin reports a number neither of
    them took (§6.2's rule, and the reason it exists).

    Needs `grade_pct`, so it sees only DEM-covered runs. `n_runs_without_grade`
    says how many of the selection had none, rather than leaving the reader to
    assume the selection was fully represented.
    """
    from trail_factors.analysis import basic as B

    problems = validate_filter(filt)
    if problems:
        raise ValueError("; ".join(problems))
    sel = select_runs(con, filt, needs_terrain=False)
    bins: dict[tuple, list] = {}
    n_without = 0
    for row in sel["included"]:
        r = B.cadence_by_grade(con, row["activity_id"])
        if not r["bins"]:
            n_without += 1
            continue
        for g in r["bins"]:
            bins.setdefault((g["split_type"], g["grade_bin"]), []).append(g)
    table = []
    for (split_type, b), rows in sorted(bins.items()):
        weight = sum(r["weight_s"] for r in rows)
        table.append({
            "split_type": split_type, "grade_bin": b,
            "n_runs": len(rows), "weight_s": weight,
            "steps_per_min": (sum(r["steps_per_min"] * r["weight_s"] for r in rows)
                              / weight) if weight > 0 else None,
        })
    probe = (B.cadence_by_grade(con, sel["included"][0]["activity_id"])["filters"]
             if sel["included"] else None)
    return {
        "primitive": "cadence_by_grade",
        "unit": "steps_per_minute",
        "selection": {"rule": sel["rule"], "n_included": sel["n_included"],
                      "date_range": sel["date_range"]},
        "excluded": sel["excluded"],
        "n_runs_without_grade": n_without,
        "table": table,
        "applied_filters": probe,
        "note": "time-weighted across runs, split by split_type. Running "
                "cadence typically varies little with grade, so speed is "
                "modulated mostly by stride length; check this table before "
                "reading a cadence change as terrain.",
    }


def grade_table(con, **filt) -> dict:
    """§6.2 grade-binned pace, pooled across a selection, split by split_type.

    NEVER pooled across run and walk: each bin would mix slow hiking with
    faster running and the median would land between the modes, reading as
    "pace does not vary with grade".
    """
    problems = validate_filter(filt)
    if problems:
        raise ValueError("; ".join(problems))
    sel = select_runs(con, filt)
    bins: dict[tuple, list] = {}
    for row in sel["included"]:
        for g in P.grade_binned_pace(con, row["activity_id"])["bins"]:
            bins.setdefault((g["split_type"], g["grade_bin"]), []).append(g)
    table = []
    for (split_type, b), rows in sorted(bins.items()):
        n = sum(r["n"] for r in rows)
        mps = statistics.median([r["mps"] for r in rows])
        table.append({
            "split_type": split_type, "grade_bin": b, "n": n,
            "n_runs": len(rows),
            "mps": mps,
            # Formatted from THIS median, not carried up from the per-run
            # bins: the median of medians is a different number.
            "pace_per_mile": pace_per_mile(mps),
            "hr": statistics.median([r["hr"] for r in rows if r["hr"]]),
        })
    return {
        "primitive": "grade_binned_pace",
        "selection": {"rule": sel["rule"], "n_included": sel["n_included"],
                      "date_range": sel["date_range"]},
        "excluded": sel["excluded"],
        "table": table,
        "applied_filters": P.grade_binned_pace(con, sel["included"][0]["activity_id"])
        ["filters"] if sel["included"] else None,
        "note": "median of per-run medians, split by split_type. Pace is "
                "path-derived; records.speed_mps is grade-biased (§9).",
    }


def max_hr_over(con, **filt) -> dict:
    """The highest HR in a selection, WITH THE RUN IT CAME FROM (§6.0, §14e).

    A CEILING IS A CLAIM ABOUT ONE RUN, so the run travels with it. "Highest
    ever recorded" is only usable with a date and a run_id attached, and the
    CIRCUMSTANCES matter more here than almost anywhere else: **a maximum found
    on a hot or compromised run is a different fact from one found on a hard
    effort**, and the athlete's zone boundaries should not be anchored to the
    former without that being visible.

    So each winner carries its date, run_id and dew point. A hot day does not
    disqualify a peak -- a real heart rate on a hot day is a real heart rate --
    but it is a different fact from a peak on a hard effort in the cool.
    run_type and exclude_from_trend are carried as null, labelled "not
    collected": this build has no per-run source for either, and absent
    context is not FALSE context -- a run nobody described has not been
    declared representative, it has not been declared at all.

    BOTH CEILINGS ARE RANKED SEPARATELY. The single-sample maximum and the 30 s
    windowed one can come from DIFFERENT RUNS, and when they do that is worth
    seeing: it means the corpus's highest spike and its highest sustained
    effort are not the same session.
    """
    from trail_factors.analysis.basic import max_hr
    from trail_factors.ingest.answers import NOT_COLLECTED

    problems = validate_filter(filt)
    if problems:
        raise ValueError("; ".join(problems))
    # needs_terrain=False: a heart rate does not need GPS, and the indoor runs
    # are exactly where a treadmill ceiling might live.
    sel = select_runs(con, filt, needs_terrain=False)

    per_run, gates = [], []
    for row in sel["included"]:
        m = max_hr(con, row["activity_id"])
        rec, w30 = m["max_hr_recorded"], m["max_hr_30s"]
        if rec["value"] is None:
            gates.append({"activity_id": row["activity_id"],
                          "run_id": row["run_id"], "date": row["date"],
                          "reason": rec["reason"]})
            continue
        dew = con.execute("""
            SELECT max(dew_point_c) FROM weather_records WHERE activity_id = ?
        """, [row["activity_id"]]).fetchone()[0]
        per_run.append({
            "run_id": row["run_id"], "activity_id": row["activity_id"],
            "date": row["date"],
            "max_hr_recorded": rec["value"],
            "max_hr_30s": w30["value"],
            "session_agrees": m["session_agrees"],
            "dew_point_c": dew,
            # PER-RUN CONTEXT, carried as a labelled absence. Absent is not
            # False: a run nobody described has not been declared
            # representative, it has not been declared at all.
            "run_type": None,
            "run_type_status": NOT_COLLECTED,
            "exclude_from_trend": None,
            "exclude_from_trend_status": NOT_COLLECTED,
        })

    def _winner(key):
        vals = [r for r in per_run if r[key] is not None]
        if not vals:
            return None
        best = max(vals, key=lambda r: r[key])
        return {**best, "value": best[key], "basis": key}

    top_recorded, top_30s = _winner("max_hr_recorded"), _winner("max_hr_30s")
    out = {
        "primitive": "max_hr_over",
        "unit": "bpm",
        "selection": {"rule": sel["rule"], "n_included": len(per_run),
                      "date_range": sel["date_range"]},
        "excluded": sel["excluded"] + gates,
        "max_hr_recorded": top_recorded,
        "max_hr_30s": top_30s,
        "same_run": (top_recorded is not None and top_30s is not None
                     and top_recorded["run_id"] == top_30s["run_id"]),
        # Rows without a recorded max are skipped before per_run is built, so
        # the key is always a number; no falsy default is needed.
        "per_run": sorted(per_run, key=lambda r: r["max_hr_recorded"],
                          reverse=True),
        "applied_filters": (max_hr(con, sel["included"][0]["activity_id"])["filters"]
                            if sel["included"] else None),
    }
    out["declared_ceiling"] = _declared_ceiling(
        con, top_30s, top_recorded, per_run=per_run,
        end_date=filt.get("end_date"),
        evidence=ceiling_evidence(con, filt.get("end_date")))
    return out


# A CEILING COMPARISON IS ONLY MEANINGFUL AGAINST A SELECTION THAT REACHED A
# CEILING -- AND WHETHER IT DID IS READ OFF THE SELECTION ITSELF. The
# declared-vs-demonstrated gap is reported only when at least one run IN THE
# SELECTION produced a max_hr_30s within CEILING_BAND_BPM of the demonstrated
# 30 s ceiling. No list of "hard" run types and no grouping: a selection of
# easy runs that never came near the ceiling is refused, whatever it is called,
# and one that did is compared, whatever it is called.
#
# THE CEILING IS THE 30 s ONE, like for like with max_hr_30s. It is read from
# the sport's runs up to the selection's end_date -- the same no-look-ahead
# bound the declared values carry -- so a later run cannot move the band under
# an earlier selection. Inherent and stated: a selection that CONTAINS the
# run setting the ceiling is always in its own band, so it always qualifies.
CEILING_BAND_BPM = 5.0

# The gate's rule as a choice, registered in calc_params beside the band: a
# change to WHAT is compared (the selection's own 30 s figure against the
# demonstrated 30 s ceiling, bounded by end_date) must move the digest the way
# a change to the band's width does.
CEILING_GATE_RULE = ("selection_max_hr_30s_within_band_of_demonstrated_30s_"
                     "ceiling_up_to_end_date")


def selection_reach(rows: list[dict], ceiling_30s: float,
                    band_bpm: float = CEILING_BAND_BPM) -> dict:
    """Whether a selection reached the band below the ceiling, and how close.

    `rows` carry max_hr_30s and run_id. A run with no 30 s figure counts as a
    run but can never reach. PURE, so the rule is testable on a fixture with
    no database.
    """
    floor = ceiling_30s - band_bpm
    n_reaching, best, best_run = 0, None, None
    for r in rows:
        v = r.get("max_hr_30s")
        if v is None:
            continue
        if v >= floor - 1e-9:
            n_reaching += 1
        if best is None or v > best:
            best, best_run = v, r.get("run_id")
    return {"n_runs": len(rows), "n_reaching": n_reaching,
            "best_30s_bpm": best, "best_run_id": best_run,
            "below_ceiling_bpm": (ceiling_30s - best
                                  if best is not None else None),
            "band_floor_bpm": floor, "reaches": n_reaching > 0}


def ceiling_evidence(con, end_date: str | None = None) -> dict:
    """The demonstrated 30 s ceiling up to end_date, and the band below it.
    The gate's whole basis, returned so the payload shows it."""
    from trail_factors.analysis.basic import max_hr

    sel = select_runs(con, {"end_date": end_date} if end_date else {},
                      needs_terrain=False)
    rows = []
    for row in sel["included"]:
        m = max_hr(con, row["activity_id"])
        rows.append({"max_hr_30s": m["max_hr_30s"]["value"],
                     "max_hr_recorded": m["max_hr_recorded"]["value"],
                     "run_id": row["run_id"]})
    vals = [r for r in rows if r["max_hr_30s"] is not None]
    if not vals:
        return {"status": "unavailable",
                "reason": "no run in the corpus up to end_date has a 30 s HR"}
    top = max(vals, key=lambda r: r["max_hr_30s"])
    ceiling = top["max_hr_30s"]
    # THE WHOLE DEMONSTRATED CEILING, not only the 30 s figure the band is
    # measured from: the single highest record and the watch's session
    # summary over the same runs, under the same end_date bound. These are
    # what a declared anchor is compared against once the gate passes -- a
    # selection only has to come WITHIN the band, so its own peak can sit
    # below what the heart had already done.
    recs = [r for r in rows if r["max_hr_recorded"] is not None]
    top_rec = (max(recs, key=lambda r: r["max_hr_recorded"]) if recs
               else {"max_hr_recorded": None, "run_id": None})
    ids = [row["activity_id"] for row in sel["included"]]
    sess = con.execute("""
        SELECT max(session_max_hr), arg_max(activity_id, session_max_hr)
        FROM activities WHERE activity_id IN (SELECT unnest(?))
    """, [ids]).fetchone()
    return {"status": "ok", "ceiling_30s": ceiling,
            "ceiling_run_id": top["run_id"],
            "max_recorded_bpm": top_rec["max_hr_recorded"],
            "max_recorded_run_id": top_rec["run_id"],
            "session_max_bpm": sess[0],
            "session_max_activity_id": sess[1],
            "band_bpm": CEILING_BAND_BPM,
            "band_floor_bpm": ceiling - CEILING_BAND_BPM,
            "as_of": end_date or "the whole corpus",
            "rule": (f"the comparison is reported only when a run in the "
                     f"selection itself produced a max_hr_30s within "
                     f"{CEILING_BAND_BPM:.0f} bpm of the demonstrated 30 s "
                     f"ceiling; measured on the selection, never a hand-set "
                     f"list of run types"),
            "circularity": ("a selection containing the run that sets the "
                            "ceiling is always in its own band, so it always "
                            "qualifies")}


def _declared_ceiling(con, top_30s, top_recorded, per_run=None,
                      end_date: str | None = None,
                      evidence: dict | None = None) -> dict:
    """The max HR the athlete's zones are ANCHORED to, against the demonstrated
    one (§14e).

    THIS IS WHY THE METRIC EXISTS. Every FIT file declares `max_heart_rate` in
    its `time_in_zone` message, and every zone boundary is a percentage of it.
    If the athlete has never reached that number, **every boundary is shifted
    up by the gap** -- the zones describe a heart that is not the athlete's.

    It is reported, never corrected. Changing a zone configuration is an
    athlete decision with a coach behind it, not a consequence a tool draws.
    """
    # DECLARED AS OF THE SELECTION'S END, NOT AS OF TODAY. The declared values
    # came from EVERY file, so a selection ending before the athlete changed
    # their declared max was compared against the later declaration -- a gap
    # against a configuration that did not yet exist. Bounded by the same
    # date convention select_runs applies to the selection (the UTC date of
    # start_time_utc <= end_date). No end_date: the whole corpus, as before.
    # Only the END is bounded: a declaration made before the selection
    # started was still in force during it.
    #
    # AND ONLY THE SELECTION'S OWN SPORT. Garmin keeps a SEPARATE max HR per
    # sport -- a ride and a hike can each declare a different one -- and
    # pooling them puts a cycling anchor beside running HR. The sport is read off the
    # selected activities, not assumed, so a selection spanning sports
    # compares against declarations from those sports only. "Highest declared
    # up to end_date" is unchanged.
    ids = [r["activity_id"] for r in (per_run or [])]
    sports = sorted(r[0] for r in con.execute(
        "SELECT DISTINCT sport FROM activities WHERE activity_id IN "
        "(SELECT unnest(?))", [ids]).fetchall()) if ids else []
    rows = con.execute("""
        SELECT DISTINCT declared_max_hr FROM activities
        WHERE declared_max_hr IS NOT NULL
          AND sport IN (SELECT unnest(?))
          AND (? IS NULL OR CAST(start_time_utc AS DATE) <= CAST(? AS DATE))
        ORDER BY 1
    """, [sports, end_date, end_date]).fetchall()
    declared = [r[0] for r in rows]
    declared_as_of = ((f"files on or before {end_date} (the selection's "
                       f"end_date)" if end_date else
                       "every file (the selection has no end_date)")
                      + f", sport {' / '.join(sports) or 'none selected'} "
                        f"only (Garmin keeps a max per sport)")
    demonstrated = (top_recorded or {}).get("value")
    sustained = (top_30s or {}).get("value")
    if not declared or demonstrated is None:
        return {"declared": declared or None, "demonstrated": demonstrated,
                "declared_as_of": declared_as_of,
                "status": "unavailable",
                "reason": f"no declared max in {declared_as_of}, or no HR in "
                          f"the selection"}
    # GATED ON THE SELECTION. "The athlete has never reached the declared
    # max" is a claim about the HEART; computed over easy runs it is a claim
    # about the SELECTION, and the two are indistinguishable in the output.
    # A large gap over a week of recovery runs means nothing at all,
    # and printing it beside the sentence "every zone boundary is shifted up by
    # the gap" invites exactly the wrong conclusion.
    #
    # WHETHER THE SELECTION REACHED A CEILING IS MEASURED ON THE SELECTION
    # (selection_reach), never inferred from what its runs were called. With
    # no per_run there is no sport and so no declared value: the unavailable
    # return above has already been taken.
    ev = evidence if evidence is not None else {}
    sess = con.execute("""
        SELECT max(session_max_hr), arg_max(activity_id, session_max_hr)
        FROM activities WHERE activity_id IN (SELECT unnest(?))
    """, [ids]).fetchone() if ids else (None, None)
    # THE SELECTION'S OWN PEAK, LABELLED AS SUCH. The gate only asks that
    # the selection came WITHIN the band, so its peak can sit below what the
    # heart had already done; it is reported beside the ceiling, never as it.
    selection_peak = {
        "single_sample": demonstrated,
        "sustained_30s": sustained,
        "session_summary": sess[0],
        "session_summary_activity": sess[1],
        "label": ("this SELECTION's own peak, not the demonstrated ceiling: "
                  "a selection need only reach within "
                  f"{CEILING_BAND_BPM:.0f} bpm of the ceiling to qualify"),
    }
    # THE DEMONSTRATED CEILING: the corpus's own runs up to the selection's
    # end_date (ceiling_evidence), the same no-look-ahead bound the declared
    # values carry. The selection is drawn from those runs, so its peak is
    # folded in only as a floor -- a ceiling is never below a figure the
    # selection itself shows.
    def _top(*vals):
        vals = [v for v in vals if v is not None]
        return max(vals) if vals else None

    ceiling = {
        "single_sample": _top(ev.get("max_recorded_bpm"), demonstrated),
        "sustained_30s": _top(ev.get("ceiling_30s"), sustained),
        "session_summary": _top(ev.get("session_max_bpm"), sess[0]),
        "basis": (f"the sport's runs up to {ev['as_of']} (ceiling_evidence), "
                  f"not this selection" if ev.get("status") == "ok" else
                  "no demonstrated ceiling up to the selection's end_date"),
    }
    reach = (selection_reach(per_run or [], ev["ceiling_30s"])
             if ev.get("status") == "ok" else None)
    if reach is None or not reach["reaches"]:
        if reach is None:
            why = ("no demonstrated 30 s ceiling exists up to the "
                   "selection's end_date, so there is no band to reach")
        else:
            best = reach["best_30s_bpm"]
            why = ("the selection's best 30 s HR is "
                   + (f"{best:.1f} bpm, {reach['below_ceiling_bpm']:.1f} "
                      f"below the demonstrated 30 s ceiling of "
                      f"{ev['ceiling_30s']:.1f}"
                      if best is not None else "absent")
                   + f"; the band is {CEILING_BAND_BPM:.0f} bpm (a run "
                     f"must reach {reach['band_floor_bpm']:.1f})")
        return {
            "declared": declared,
            "declared_source": "time_in_zone.max_heart_rate, per file",
            "declared_as_of": declared_as_of,
            "demonstrated_single_sample": ceiling["single_sample"],
            "demonstrated_30s": ceiling["sustained_30s"],
            "demonstrated_session_summary": ceiling["session_summary"],
            "demonstrated_basis": ceiling["basis"],
            "selection_peak": selection_peak,
            "gap_bpm": None,
            "status": "refused",
            "reason": (
                f"no run in this selection came near a ceiling, so the "
                f"gap would measure the SELECTION and not the heart: "
                f"{why}. The demonstrated ceiling and the selection's own "
                f"peak above are still real; it is the COMPARISON that is "
                f"refused."),
            "selection_reach": reach,
            "ceiling_evidence": evidence,
        }
    # AGAINST THE CEILING, NOT THE SELECTION'S PEAK. A selection inside the
    # band can still sit below a run the heart had already done; measuring
    # the gap from the selection's own peak then reads the anchor as above
    # "what this heart has actually done" when the evidence in the same
    # payload says otherwise.
    #
    # THE SESSION SUMMARY IS THE BETTER CEILING. A declared anchor equal to
    # the true peak can read as ABOVE the record stream's maximum while the
    # same run's session summary matches it: the record stream is a
    # DECIMATED view of what the watch sampled (Smart Recording can
    # understate a peak by a beat or two, never overstate), so it cannot be
    # the whole demonstrated ceiling. Both are reported; the record stream
    # stays because only it is placeable in time and checkable for cadence
    # lock.
    rec_ceiling = ceiling["single_sample"]
    sess_ceiling = ceiling["session_summary"]
    gap = declared[-1] - rec_ceiling
    gap_sess = declared[-1] - sess_ceiling if sess_ceiling is not None else None
    best = _top(rec_ceiling, sess_ceiling)
    sel_best = _top(demonstrated, sess[0])
    return {
        "declared": declared,
        "declared_source": "time_in_zone.max_heart_rate, per file",
        "declared_as_of": declared_as_of,
        "demonstrated_single_sample": rec_ceiling,
        "demonstrated_30s": ceiling["sustained_30s"],
        "demonstrated_session_summary": sess_ceiling,
        "demonstrated_session_summary_activity": (
            ev.get("session_max_activity_id")
            if sess_ceiling == ev.get("session_max_bpm") else sess[1]),
        "demonstrated_basis": ceiling["basis"],
        "selection_peak": selection_peak,
        "gap_bpm": gap,
        "gap_vs_session_summary_bpm": gap_sess,
        "gap_basis": ("both gaps are against the DEMONSTRATED CEILING up to "
                      "the selection's end_date, never the selection's own "
                      "peak (selection_peak). gap_bpm is against the RECORD "
                      "STREAM's single highest sample; "
                      "gap_vs_session_summary_bpm is against the watch's "
                      "session summary, which sees every sample the record "
                      "stream decimates and is the better ceiling"),
        "selection_gate": ("satisfied: the selection contains a run whose "
                           "30 s HR reached within "
                           f"{CEILING_BAND_BPM:.0f} bpm of the demonstrated "
                           "30 s ceiling"),
        "selection_reach": reach,
        "ceiling_evidence": evidence,
        "status": "ok",
        "note": ((f"the zones are anchored to {declared[-1]:.0f} bpm and the "
                  f"demonstrated ceiling up to {ev['as_of']} is {best:.0f} "
                  f"(record stream {rec_ceiling:.0f}, "
                  f"{ceiling['sustained_30s']:.0f} sustained over 30 s). "
                  f"Every boundary is a percentage of the declared figure, so "
                  f"a gap of {declared[-1] - best:.0f} bpm shifts every one "
                  f"of them upward relative to what this heart has actually "
                  f"done."
                  if declared[-1] > best else
                  f"the demonstrated ceiling up to {ev['as_of']} ({best:.0f}; "
                  f"record stream {rec_ceiling:.0f}) meets or exceeds the "
                  f"declared anchor ({declared[-1]:.0f}); the zones are not "
                  f"anchored above the demonstrated ceiling.")
                 + f" This selection itself peaks at {sel_best:.0f} "
                   f"(selection_peak)."),
        "do_not": "this is REPORTED, not corrected. Re-anchoring zones is an "
                  "athlete decision, not a consequence a tool draws.",
    }


# THE DEFAULT THRESHOLD AND LADDER ARE THE ATHLETE'S, read from config at CALL
# time (`hr_above_default_bpm`, `time_above_ladder_bpm`). Neither is fitted:
# the default belongs near the top of the athlete's own distribution, just
# under the declared zone anchor, which is the band where the ceiling question
# actually lives (§14e-ter) -- and that band is a property of one heart, not of
# the code.

# AN EXCURSION BREAKS WHEN A SAMPLE IS MISSING, and "missing" is measured
# against the run's OWN sampling rather than a fixed number of seconds. Three
# times the median gap is a hole at any regime; a fixed 10 s would be a hole at
# 1 Hz and a normal interval under Smart Recording.
EXCURSION_GAP_MULTIPLE = 3.0

# The window reported around a peak. Same 30 s as §6.0's max_hr_30s, so the two
# figures are on the same footing -- but CENTRED on the peak rather than chosen
# to maximise, because here the question is what surrounded this reading.
PEAK_WINDOW_S = 30.0

# THE RUN WHOSE PROVENANCE IS OPEN (§14e-bis) is config, `unresolved_peak_run`.
# Set, it is marked, never excluded, and every corpus total is reported a
# second time without it. Unset, nothing is split -- the payload says none is
# configured rather than inventing a with/without comparison.


def _unresolved_peak_run() -> str | None:
    return config.get().unresolved_peak_run


def _not_configured_split() -> dict:
    return {"status": "not_configured", "run_excluded": None,
            "reason": "no unresolved_peak_run is set in the athlete config, "
                      "so no total is reported a second time without one "
                      "run. Set it to a run_id whose peak's provenance is "
                      "open to see the corpus both ways."}


def hr_above(con, threshold: float | None = None, **filt) -> dict:
    """Every contiguous excursion at or above `threshold`, record grain (§14e-ter).

    THE QUESTION THIS ANSWERS IS NOT "what is the highest number". It is
    "where has this heart been near its ceiling, and what was happening around
    it" -- so the unit is the EXCURSION, not the record. A sustained bout would
    otherwise arrive as hundreds of rows and a single spike as one, with
    nothing in the shape to tell them apart.

    RAW `records.heart_rate`, NEVER lag-shifted. The 40 s shift aligns HR to
    the effort that caused it, which is what matching needs; a ceiling wants
    the reading as the monitor recorded it.

    FLAGGED RECORDS ARE REPORTED, NOT EXCLUDED. At the top of the range a
    §15.4 flag may mark a real artifact or a real spike, and which it is cannot
    be decided by the flag alone -- so `n_flagged` and the flag names travel
    with each excursion and the athlete judges.

    PACE, GRADE AND CADENCE TRAVEL WITH EACH EXCURSION where the inputs exist.
    The same peak while climbing slowly and while running fast on the flat
    are different facts about the same number.

    `threshold` None means the configured `hr_above_default_bpm`.
    """
    if threshold is None:
        threshold = config.get().hr_above_default_bpm
    unresolved = _unresolved_peak_run()
    problems = validate_filter(filt)
    if problems:
        raise ValueError("; ".join(problems))
    sel = select_runs(con, filt, needs_terrain=False)
    excursions, gated = [], []
    for row in sel["included"]:
        got = _excursions_for(con, row, threshold, unresolved)
        if got is None:
            gated.append({"run_id": row["run_id"], "date": row["date"],
                          "reason": "this activity records no heart rate"})
            continue
        excursions.extend(got)
    excursions.sort(key=lambda e: -e["peak_hr"])
    if unresolved is None:
        without = _not_configured_split()
    else:
        clean = [e for e in excursions if e["run_id"] != unresolved]
        without = {
            "status": "ok",
            "run_excluded": unresolved,
            "why": "its provenance is open (§14e-bis, configured as "
                   "unresolved_peak_run). Reported here WITHOUT it so the "
                   "corpus can be read both ways -- never silently.",
            "n_excursions": len(clean),
            "n_runs_with_any": len({e["run_id"] for e in clean}),
            "peak_hr": max((e["peak_hr"] for e in clean), default=None),
        }
    return {
        "primitive": "hr_above",
        "threshold_bpm": threshold,
        "unit": "bpm",
        "selection": {"rule": sel["rule"], "n_included": len(sel["included"]),
                      "date_range": sel["date_range"]},
        "excluded": sel["excluded"] + gated,
        "n_excursions": len(excursions),
        "n_runs_with_any": len({e["run_id"] for e in excursions}),
        "excursions": excursions,
        # THE UNRESOLVED PEAK IS NEVER DROPPED, only shown both ways (§14e-bis).
        "without_unresolved_peak": without,
        "applied_filters": _hr_above_filters(threshold, unresolved),
    }


def _excursions_for(con, row: dict, threshold: float,
                    unresolved: str | None = None) -> list[dict] | None:
    """Contiguous above-threshold stretches within one activity."""
    import statistics

    from trail_factors.analysis.basic import record_weights

    aid, run_id = row["activity_id"], row["run_id"]
    rows = con.execute("""
        SELECT r.timestamp, r.heart_rate, r.distance_m, r.cadence_raw_rpm,
               r.fractional_cadence, coalesce(e.quality_flags, '') AS flags,
               e.grade_pct, e.path_distance_m
        FROM records r
        LEFT JOIN records_enriched e
          ON e.activity_id = r.activity_id AND e.timestamp = r.timestamp
        WHERE r.activity_id = ? AND r.heart_rate IS NOT NULL AND r.heart_rate > 0
        ORDER BY r.timestamp
    """, [aid]).fetchall()
    if len(rows) < 2:
        return None
    gaps = [(rows[i + 1][0] - rows[i][0]).total_seconds() for i in range(len(rows) - 1)]
    median_gap = statistics.median(gaps) if gaps else 1.0
    break_gap = EXCURSION_GAP_MULTIPLE * max(median_gap, 1e-6)
    # TIMER-TIME WEIGHTS (paused=[]): time at a heart rate is a DOSE, not a
    # rate, and the heart does not stop at a road crossing. Same reasoning
    # M7 records for treating standing as a gait.
    weights = record_weights(con, aid, paused=[])
    regime = row.get("sampling_regime")
    t0 = rows[0][0]

    out, cur = [], []
    for i, r in enumerate(rows):
        above = r[1] >= threshold
        if above and cur and (r[0] - cur[-1][0]).total_seconds() > break_gap:
            out.append(_excursion(cur, rows, run_id, row, regime, t0, weights,
                                  median_gap, threshold, unresolved))
            cur = []
        if above:
            cur.append(r)
        elif cur:
            out.append(_excursion(cur, rows, run_id, row, regime, t0, weights,
                                  median_gap, threshold, unresolved))
            cur = []
    if cur:
        out.append(_excursion(cur, rows, run_id, row, regime, t0, weights,
                              median_gap, threshold, unresolved))
    return out


def _excursion(block, allrows, run_id, row, regime, t0, weights, median_gap,
               threshold, unresolved: str | None = None) -> dict:
    """One contiguous stretch, with what was happening around it."""
    import statistics

    hrs = [b[1] for b in block]
    peak_i = max(range(len(block)), key=lambda i: block[i][1])
    peak = block[peak_i]
    start, end = block[0][0], block[-1][0]
    offset = (start - t0).total_seconds()
    flags = [f for b in block for f in (b[5].split(",") if b[5] else []) if f]
    # THE 30 s AROUND THE PEAK, centred, clipped to the run. Not the maximising
    # window §6.0 reports -- the question here is what surrounded THIS reading.
    lo = peak[0] - __import__("datetime").timedelta(seconds=PEAK_WINDOW_S / 2)
    hi = peak[0] + __import__("datetime").timedelta(seconds=PEAK_WINDOW_S / 2)
    win = [r for r in allrows if lo <= r[0] <= hi]
    wn = sum(weights.get(r[0], 0.0) for r in win)
    wsum = sum(r[1] * weights.get(r[0], 0.0) for r in win)
    return {
        "run_id": run_id,
        "date": row["date"],
        "sampling_regime": regime,
        "start_offset": f"{int(offset // 60)}:{int(offset % 60):02d}",
        "start_offset_s": offset,
        "span_s": (end - start).total_seconds(),
        "weighted_s": sum(weights.get(b[0], 0.0) for b in block),
        "n_records": len(block),
        "median_sample_gap_s": median_gap,
        "peak_hr": float(peak[1]),
        "peak_at_utc": str(peak[0]),
        "mean_hr": statistics.mean(hrs),
        "peak_window_s": PEAK_WINDOW_S,
        "peak_window_mean_hr": (wsum / wn) if wn > 0 else None,
        "peak_window_n_records": len(win),
        # FLAGS REPORTED, NOT APPLIED. At the top of the range the flag does
        # not settle whether a reading is an artifact or a real spike.
        "n_flagged": sum(1 for b in block if b[5]),
        "flags": sorted(set(flags)),
        "unresolved_provenance": unresolved is not None and run_id == unresolved,
        **_context(block),
    }


def _context(block) -> dict:
    """Pace, grade and cadence over the excursion, where the inputs exist."""
    import statistics

    span_s = (block[-1][0] - block[0][0]).total_seconds()
    # PREFER path_distance_m (§9's speed rule); fall back to the device
    # odometer and SAY WHICH, rather than reporting no pace at all on the
    # activities with no DEM tile.
    path = [b[7] for b in block if b[7] is not None]
    odo = [b[2] for b in block if b[2] is not None]
    if len(path) >= 2 and span_s > 0:
        dist, basis = path[-1] - path[0], "records_enriched.path_distance_m"
    elif len(odo) >= 2 and span_s > 0:
        dist, basis = odo[-1] - odo[0], "records.distance_m (device odometer)"
    else:
        dist, basis = None, "unavailable"
    mps = (dist / span_s) if (dist is not None and span_s > 0 and dist >= 0) else None
    grades = [b[6] for b in block if b[6] is not None]
    cads = [(b[3] + (b[4] or 0.0)) * 2 for b in block if b[3] is not None and b[3] > 0]
    return {
        "mps": mps,
        "pace_per_mile": pace_per_mile(mps) if mps else None,
        "pace_basis": basis,
        "median_grade_pct": statistics.median(grades) if grades else None,
        "grade_available": bool(grades),
        "median_cadence_spm": statistics.median(cads) if cads else None,
    }


def _hr_above_filters(threshold: float, unresolved: str | None = None) -> dict:
    from trail_factors.analysis.basic import MAX_WEIGHT_GAP_S
    return {
        "hr_series": "RAW records.heart_rate -- NEVER hr_shifted. The 40 s lag "
                     "correction aligns HR to effort for MATCHING; a ceiling "
                     "wants the reading as recorded.",
        "threshold_bpm": threshold,
        "grain": "contiguous excursion (record grain within it)",
        "excursion_break": f"a below-threshold record, or a sample gap over "
                           f"{EXCURSION_GAP_MULTIPLE}x this run's own median "
                           f"gap -- measured per run, not a fixed number of "
                           f"seconds, because a hole at 1 Hz is a normal "
                           f"interval under Smart Recording",
        "quality_flags_excluded": False,
        "quality_flags_reason": "REPORTED, not applied. At the top of the "
                                "range a flag may mark a real artifact or a "
                                "real spike; the flag alone cannot say which.",
        "time_basis": f"timer time, capped at {MAX_WEIGHT_GAP_S:.0f} s per "
                      f"record. Time at a heart rate is a DOSE, not a rate, so "
                      f"stopped time is NOT subtracted -- the heart does not "
                      f"stop at a road crossing.",
        "regime_note": "a 4 s Smart-Recording excursion and a 1 Hz one are not "
                       "the same resolution; `sampling_regime` and "
                       "`median_sample_gap_s` travel with every excursion",
        "unresolved_peak": (
            f"{unresolved} is configured as unresolved_peak_run: its "
            f"provenance is open (§14e-bis). It is MARKED and never "
            f"excluded; totals are also reported without it."
            if unresolved is not None else
            "none configured (unresolved_peak_run is unset), so no run is "
            "marked and no total is reported a second time"),
    }


def time_above_hr(con, thresholds=None, **filt) -> dict:
    """Time at or above each threshold, per run and corpus-wide (§14e-ter).

    THE LADDER ANSWERS A DIFFERENT QUESTION FROM THE EXCURSIONS: is the top
    of the range a place this athlete has been repeatedly, or twice? A single
    excursion list sorted by peak cannot show that -- one run contributing six
    excursions and six runs contributing one each look the same at the top of
    the list.

    `thresholds` None means the configured `time_above_ladder_bpm`.
    """
    from trail_factors.analysis.basic import record_weights

    problems = validate_filter(filt)
    if problems:
        raise ValueError("; ".join(problems))
    sel = select_runs(con, filt, needs_terrain=False)
    if thresholds is None:
        thresholds = config.get().time_above_ladder_bpm
    thresholds = tuple(sorted(thresholds))
    if not thresholds:
        raise ValueError("time_above_hr needs at least one threshold")
    unresolved = _unresolved_peak_run()
    per_run, gated = [], []
    for row in sel["included"]:
        rows = con.execute("""
            SELECT timestamp, heart_rate FROM records
            WHERE activity_id = ? AND heart_rate IS NOT NULL AND heart_rate > 0
            ORDER BY timestamp""", [row["activity_id"]]).fetchall()
        if len(rows) < 2:
            gated.append({"run_id": row["run_id"], "date": row["date"],
                          "reason": "no heart rate"})
            continue
        w = record_weights(con, row["activity_id"], paused=[])
        seconds = {t: sum(w.get(ts, 0.0) for ts, hr in rows if hr >= t)
                   for t in thresholds}
        per_run.append({
            "run_id": row["run_id"], "date": row["date"],
            "run_type": row["run_type"],
            "run_type_status": row["run_type_status"],
            "sampling_regime": row.get("sampling_regime"),
            "peak_hr": float(max(hr for _, hr in rows)),
            "seconds_at_or_above": seconds,
            "unresolved_provenance": (unresolved is not None
                                      and row["run_id"] == unresolved),
        })
    per_run.sort(key=lambda r: -r["peak_hr"])

    def totals(runs):
        return {t: {"seconds": sum(r["seconds_at_or_above"][t] for r in runs),
                    "n_runs": sum(1 for r in runs
                                  if r["seconds_at_or_above"][t] > 0)}
                for t in thresholds}
    if unresolved is None:
        without = _not_configured_split()
    else:
        clean = [r for r in per_run if not r["unresolved_provenance"]]
        without = {"status": "ok", "run_excluded": unresolved,
                   "totals": totals(clean)}
    return {
        "primitive": "time_above_hr",
        "thresholds_bpm": list(thresholds),
        "selection": {"rule": sel["rule"], "n_included": len(sel["included"]),
                      "date_range": sel["date_range"]},
        "excluded": sel["excluded"] + gated,
        "per_run": per_run,
        "corpus": totals(per_run),
        "corpus_without_unresolved_peak": without,
        "applied_filters": _hr_above_filters(min(thresholds), unresolved),
    }


def zone_by_gait(con, **filt) -> dict:
    """M7 time in HR zone by gait, per run and rolled up per week (§17.2).

    WEEKS ARE LOCAL ISO WEEKS. Grouping by UTC date would put a late-evening
    run on the following day and, at a week boundary, in the following week
    (CLAUDE.md) -- which is exactly the kind of error a weekly training total
    would carry silently.

    THE WEEKLY ROLLUP ONLY SUMS RUNS ON ONE BOUNDARY SET. A corpus can hold
    several sets, and a minute in "zone 2" means a different thing under each, so
    a week spanning a boundary change is reported as SPLIT rather than summed
    into a number that is not on any scale.
    """
    import datetime
    from collections import defaultdict

    from trail_factors.analysis import zones as Z

    problems = validate_filter(filt)
    if problems:
        raise ValueError("; ".join(problems))
    sel = select_runs(con, filt, needs_terrain=False)

    per_run, excluded = [], []
    for row in sel["included"]:
        r = Z.time_in_zone_by_gait(con, row["activity_id"])
        if r["status"] != "ok":
            excluded.append({"activity_id": row["activity_id"],
                             "run_id": row["run_id"], "date": row["date"],
                             "reason": r["reason"]})
            continue
        tz = con.execute("SELECT local_tz FROM activities WHERE activity_id = ?",
                         [row["activity_id"]]).fetchone()[0]
        tz = tz if tz is not None else 0.0
        start = con.execute("SELECT start_time_utc FROM activities "
                            "WHERE activity_id = ?",
                            [row["activity_id"]]).fetchone()[0]
        local = start + datetime.timedelta(hours=tz)
        iso = local.isocalendar()
        per_run.append({**r, "local_date": local.date().isoformat(),
                        "iso_week": f"{iso[0]}-W{iso[1]:02d}"})

    weeks = defaultdict(lambda: {"runs": 0, "boundary_sets": set(),
                                 "zone2_minutes": defaultdict(float),
                                 "total_minutes": defaultdict(float)})
    for r in per_run:
        w = weeks[r["iso_week"]]
        w["runs"] += 1
        w["boundary_sets"].add(tuple(map(tuple, r["boundary_set"])))
        for gait, mins in r["zone2_minutes"].items():
            w["zone2_minutes"][gait] += mins
        for gait, v in r["by_gait"].items():
            w["total_minutes"][gait] += v["total_s"] / 60.0

    weekly = []
    for wk in sorted(weeks):
        w = weeks[wk]
        mixed = len(w["boundary_sets"]) > 1
        weekly.append({
            "iso_week": wk, "n_runs": w["runs"],
            "zone2_minutes": None if mixed else dict(w["zone2_minutes"]),
            "total_minutes": None if mixed else dict(w["total_minutes"]),
            "n_boundary_sets": len(w["boundary_sets"]),
            "status": "split" if mixed else "ok",
            "reason": ("this week spans more than one HR zone boundary set; "
                       "a minute in zone 2 means a different thing under each, "
                       "so the runs are NOT summed (§14e)") if mixed else None,
        })

    probe = per_run[0]["filters"] if per_run else None
    return {
        "primitive": "zone_by_gait",
        "unit": "minutes",
        "selection": {"rule": sel["rule"], "n_included": sel["n_included"],
                      "date_range": sel["date_range"]},
        "excluded": sel["excluded"] + excluded,
        "per_run": per_run,
        "weekly": weekly,
        "applied_filters": probe,
        "note": "weeks are LOCAL ISO weeks. Zone time is comparable only "
                "within one boundary set; a week spanning a change is split, "
                "not summed.",
    }
