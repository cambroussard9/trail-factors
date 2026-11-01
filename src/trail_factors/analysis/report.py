"""§16.2 post-run report: one run against recent history (SPEC.md §12.5).

PRESENT-ONLY. This builds the thing the coaching chat reads; it does not judge.
§16.3's elimination needs the weather baseline and the athlete's own account of
the run in weighable form, and the chat has both while this function has
neither. So every number here is presented with its scale and its provenance,
and no verdict is attached.

THE OUTPUT SCHEMA IS THE CONTRACT. The chat reasons from this structure and
cannot see the code, so anything the structure cannot express is invisible.

FOUR STATUSES, NEVER A BARE NULL. They mean different things and a reader that
conflates them will either over-claim or refuse to say anything useful:

  ok           measured -- use it
  gated        computed, then rejected by a STATED gate. The run does not
               support the claim; it does NOT mean the athlete did badly.
  refused      the analysis class is invalid at this sampling_regime (§15.4).
               Not a finding. Not worth mentioning.
  unavailable  an input is missing (no GPS, no HR, no DEM tile, not built yet).

A single `null` collapses all four, and that is the main way this output could
mislead.
"""

from __future__ import annotations

from trail_factors.analysis import basic as B
from trail_factors.analysis import cycles as CY
from trail_factors.analysis import drift_onset as DO
from trail_factors.analysis import primitives as P
from trail_factors.analysis import recovery as R
from trail_factors.analysis.session_load import UNRATED_REASON as SL_UNRATED_REASON
from trail_factors.config import ConfigError
from trail_factors.units import METERS_PER_MILE, pace_over, pace_per_mile

CALC_VERSION = 1

OK, GATED, REFUSED, UNAVAILABLE = "ok", "gated", "refused", "unavailable"

# §16.2 compares one run against RECENT HISTORY. Comparing like to like is the
# whole point of the segment library, and a time window would mix trails.
# A narrowing step (same venue) applies only where it leaves at least this
# many members, else the cluster stands. It is also the floor below which a
# percentile has no meaning. (A same-run_type step will return with
# user-defined run types; run_type is not collected in this build.)
MIN_SAME_TYPE = 3
HISTORY_N = 8


def cluster_of(con, activity_id: str) -> str:
    """Cluster from the TRACE, not the start point (§14g).

    §9 records that startLatitude is the first KNOWN position rather than the
    start location, so a start-point classifier is wrong by construction on a
    run that begins with a GPS gap -- a trailhead under canopy, say.

    RETURNS A NAME, NEVER A POSITION. The answer is one of the clusters the
    athlete configured (`config/athlete.yaml`), "OTHER" for a positioned run
    outside all of them -- every positioned run, when none is configured --
    or "NO_GPS". This value reaches the report, the comparison rule and the
    MCP payload, where position is never emitted, so anything else coming
    back from the classifier (a coordinate-bearing label, say) is collapsed
    to "OTHER" here rather than trusted. Config is read at CALL time.
    """
    from trail_factors import config
    from trail_factors.cli.report import assign_cluster

    row = con.execute(
        "SELECT median(lat), median(lon) FROM records "
        "WHERE activity_id = ? AND lat IS NOT NULL",
        [activity_id],
    ).fetchone()
    if not row or row[0] is None:
        return "NO_GPS"
    name = assign_cluster(row[0], row[1])
    allowed = {c.name for c in config.get().clusters} | {"OTHER", "NO_GPS"}
    return name if name in allowed else "OTHER"


def comparison_set(con, activity_id: str, run_id: str) -> dict:
    """Which runs this one is measured against, and the rule that chose them.

    SAME SPORT AS THE SUBJECT, NOT "RUNNING". The old filter hard-coded
    `sport = 'running'` for the CANDIDATES and never looked at the subject, so
    a hike's report and several GPS rides' reports were measured against
    eight runs. Comparison sets still have no meaning ACROSS
    sports (§4.1); matching the subject's sport is what that rule says.

    EXCLUSIONS ARE STATED, as in select_runs: a different sport leaves the
    set with a reason. `excluded` lists those inside the window the members
    were drawn from -- the runs that WOULD have been members -- and
    `n_excluded_by_reason` counts every exclusion in the cluster, so a
    cluster full of another sport is not a page of noise. Nothing is excluded
    on the athlete's say-so: per-run trend exclusion is not collected in this
    build, and the rule string says so.

    CLUSTER -> VENUE (§19.4), the venue step only where it leaves at least
    MIN_SAME_TYPE members. A cluster can pool venues with a several-fold
    spread in climbing, so venue narrows it. A run that is not ASSIGNED a
    venue -- `mixed` (a link run), `unassigned`, `none`, or `not_configured`
    (no venue is defined at all) -- falls back to the cluster, and the rule
    says which; a venue too small for the step says so
    rather than borrowing another venue. A same-run_type step follows venue
    once user-defined run types exist; in this build run_type is not
    collected, so there is no such step and the rule says that too.
    """
    from trail_factors.analysis import venues as V
    from trail_factors.ingest.answers import NOT_COLLECTED

    cluster = cluster_of(con, activity_id)
    my_sport, my_start = con.execute(
        "SELECT sport, start_time_utc FROM activities WHERE activity_id = ?",
        [activity_id],
    ).fetchone()
    candidates, excluded = [], []
    # NO LOOK-AHEAD. A run is measured against runs that had happened BEFORE
    # it. The query took the most recent runs in the cluster with no date
    # bound, so re-running an old report compared it against runs that came
    # after it -- a percentile against the future, invisible until old
    # reports were re-run. Strictly earlier than the subject's start; the
    # subject's own Strava twin shares its start and is excluded too.
    for aid, rid, start, sport in con.execute(
        """
        SELECT a.activity_id, a.run_id, a.start_time_utc, a.sport
        FROM activities a
        WHERE a.source = 'garmin_watch' AND a.activity_id <> ?
          AND a.start_time_utc < ?
        ORDER BY a.start_time_utc DESC""",
        [activity_id, my_start],
    ).fetchall():
        if cluster_of(con, aid) != cluster:
            continue
        if sport != my_sport:
            excluded.append(
                {
                    "activity_id": aid,
                    "run_id": rid,
                    "date": str(start)[:10],
                    "start": start,
                    "kind": "sport",
                    "reason": f"sport {sport} differs from this activity's {my_sport}",
                }
            )
            continue
        candidates.append((aid, rid, start))

    rule = f"same cluster ({cluster})"
    assignment = V.assign_all(con)["by_run"]
    venue = assignment.get(run_id) or {
        "state": V.NONE,
        "venue": None,
        "venues_touched": [],
        "reason": "not in the runs table",
    }
    if venue["state"] == V.ASSIGNED:
        same = [
            c
            for c in candidates
            if (assignment.get(c[1]) or {}).get("venue") == venue["venue"]
        ]
        if len(same) >= MIN_SAME_TYPE:
            candidates = same
            rule += f"; same venue ({venue['venue']})"
        else:
            rule += (
                f"; venue step skipped: {len(same)} earlier run(s) at "
                f"{venue['venue']}, fewer than {MIN_SAME_TYPE}"
            )
    else:
        rule += f"; venue {venue['state']}, so the cluster stands" + (
            f" ({venue['reason']})" if venue.get("reason") else ""
        )
    rule += (
        f"; run_type {NOT_COLLECTED} in this build, so no run_type "
        f"step and no run excluded from the trend on the athlete's "
        f"say-so"
    )
    members = candidates[:HISTORY_N]
    # The window the members came from. A FULL set: anything newer than the
    # oldest member would have displaced one. A short set: every exclusion
    # would have been a member, so the newest HISTORY_N are shown and the
    # counts carry the rest.
    if len(members) == HISTORY_N:
        shown = [e for e in excluded if e["start"] > members[-1][2]]
    else:
        shown = excluded[:HISTORY_N]
    by_reason: dict[str, int] = {}
    for e in excluded:
        by_reason[e["kind"]] = by_reason.get(e["kind"], 0) + 1
    return {
        "rule": rule,
        "cluster": cluster,
        "run_type": None,
        "run_type_status": NOT_COLLECTED,
        "sport": my_sport,
        "venue": {
            k: venue.get(k) for k in ("state", "venue", "venues_touched", "reason")
        },
        "n": len(members),
        "members": [m[0] for m in members],
        "date_range": (str(members[-1][2])[:10], str(members[0][2])[:10])
        if members
        else None,
        # THE WINDOW, STATED: what bounded the candidates, and where the
        # members actually fell inside it.
        "window": {
            "bound": "start_time_utc < the subject's start",
            "subject_start_utc": str(my_start),
            "earliest_member_utc": (str(members[-1][2]) if members else None),
            "latest_member_utc": (str(members[0][2]) if members else None),
        },
        "excluded": [{k: v for k, v in e.items() if k != "start"} for e in shown],
        "n_excluded_by_reason": by_reason,
    }


# SHADE IS NOT COLLECTED IN THIS BUILD, so the open-sky bound is never
# corrected and the note says why rather than leaving the reader to wonder
# whether a correction was considered.
SUN_EXPOSURE_NOTE = (
    "sun exposure (shade) is not collected in this build, so "
    "the open-sky upper bound stands uncorrected"
)


def _sun_exposure() -> dict:
    """The shade context for the WBGT bound: not collected, stated."""
    from trail_factors.ingest.answers import NOT_COLLECTED

    return {"status": NOT_COLLECTED, "value": None, "note": SUN_EXPOSURE_NOTE}


def _estimated_wbgt(con, activity_id: str, source, shortwave: dict) -> dict:
    """Estimated WBGT beside temperature: a (min, max) over the run's
    records like temp_c, plus a duration-weighted run value. DISPLAY ONLY."""
    from trail_factors.ingest import weather as W

    sun = _sun_exposure()

    filters = {
        "approximation": W.WBGT_APPROXIMATION,
        "formula": W.WBGT_FORMULA,
        "inputs": "temp_c and humidity_pct per weather record",
        "radiation_and_wind": (
            "NOT inputs: the approximation assumes moderately high radiation "
            "and light wind. The open-sky shortwave figure beside it is the "
            "evidence of how much sun there was."
        ),
        "canopy": (
            "shortwave is OPEN-SKY (the model sees cloud, not trees), and the "
            "approximation assumes sun regardless, so under canopy this is an "
            "UPPER BOUND on the heat load the athlete met."
        ),
        "correction": sun["note"],
        "use": (
            "DISPLAY AND STORAGE ONLY. Not a §2 covariate: nothing new "
            "enters the residual model before its n threshold is met."
        ),
    }
    if source == "assumed_indoor":
        return {
            "min": None,
            "max": None,
            "run_weighted": None,
            "status": "unavailable",
            "reason": "conditions are ASSUMED indoor; an outdoor WBGT "
            "approximation that assumes sun does not apply",
            "sun_exposure": sun,
            "filters": filters,
        }
    vals = [
        W.estimated_wbgt_c(t, rh)
        for t, rh in con.execute(
            "SELECT temp_c, humidity_pct FROM weather_records WHERE activity_id = ?",
            [activity_id],
        ).fetchall()
    ]
    vals = [v for v in vals if v is not None]
    hourly = [
        {"timestamp": t, "wbgt": W.estimated_wbgt_c(tc, rh)}
        for t, tc, rh in con.execute(
            "SELECT timestamp, temp_c, humidity_pct FROM weather_hourly "
            "WHERE activity_id = ? ORDER BY timestamp",
            [activity_id],
        ).fetchall()
    ]
    start, end = con.execute(
        "SELECT min(timestamp), max(timestamp) FROM records WHERE activity_id = ?",
        [activity_id],
    ).fetchone()
    run = (
        W.duration_weighted(hourly, start, end, "wbgt", preceding_hour_mean=False)
        if hourly and start is not None
        else {"value": None}
    )
    return {
        "min": min(vals) if vals else None,
        "max": max(vals) if vals else None,
        "run_weighted": run["value"],
        "status": "ok" if vals else "unavailable",
        "reason": None if vals else "no temperature/humidity on this run",
        "shortwave_wm2_beside_it": shortwave.get("value"),
        "sun_exposure": sun,
        "filters": filters,
    }


def _run_shortwave(con, activity_id: str) -> dict:
    """Run-level shortwave, DURATION-WEIGHTED over the run's hours (§8).

    Open-Meteo's value is a preceding-hour mean of global horizontal
    irradiance under the MODEL's sky: it sees cloud, not canopy. So this is
    what reached open ground, and under trees it overstates what reached the
    athlete -- an upper bound, never a measurement of exposure.
    """
    from trail_factors.ingest import weather as W

    # A READ-ONLY CONNECTION NEVER CHANGES THE SCHEMA:
    # a database built before the column existed does not have it until a CLI
    # write applies the DDL, and a missing column must not take the whole
    # report down with it.
    if not con.execute(
        "SELECT count(*) FROM information_schema.columns WHERE "
        "table_name = 'weather_hourly' AND column_name = 'shortwave_wm2'"
    ).fetchone()[0]:
        return {
            "value": None,
            "unit": "W/m2",
            "status": "unavailable",
            "reason": "the database predates the shortwave column; any "
            "CLI write (e.g. `weather`) runs the migration",
        }
    hourly = [
        {"timestamp": t, "shortwave_wm2": v}
        for t, v in con.execute(
            "SELECT timestamp, shortwave_wm2 FROM weather_hourly "
            "WHERE activity_id = ? ORDER BY timestamp",
            [activity_id],
        ).fetchall()
    ]
    start, end = con.execute(
        "SELECT min(timestamp), max(timestamp) FROM records WHERE activity_id = ?",
        [activity_id],
    ).fetchone()
    if not hourly or start is None:
        return {
            "value": None,
            "unit": "W/m2",
            "reason": "no hourly weather series (assumed-indoor or unfetched)",
        }
    out = W.duration_weighted(
        hourly, start, end, "shortwave_wm2", preceding_hour_mean=True
    )
    out.update({"unit": "W/m2", "sky": "open sky (model cloud, not canopy)"})
    if out["value"] is None:
        out["reason"] = (
            "the cached series has no shortwave_radiation; "
            "`weather --backfill shortwave_radiation` adds it"
        )
    return out


def _with_history(name, unit, value, n, population, note=None) -> dict:
    """One primitive plus its scale. Percentile is presentation, not verdict."""
    sp = P.spread(population)
    if value is None:
        return {
            "name": name,
            "unit": unit,
            "value": None,
            "n": n,
            "status": UNAVAILABLE,
            "reason": note or "no usable samples on this run",
            "history": sp,
            "percentile": None,
        }
    return {
        "name": name,
        "unit": unit,
        "value": value,
        "n": n,
        "status": OK,
        "reason": None,
        "history": sp,
        "percentile": P.percentile_of(value, population),
    }


# WHY OUR ELEVATION READS LOWER THAN GARMIN'S -- it keeps coming up, so it is
# printed where the DEM figures are, in the report itself (§5a-bis.4, §9.2).
ELEVATION_NOTE = (
    "DEM elevation reads LOWER than Garmin's on purpose: the FR245's GPS "
    "altitude is amplitude-inflated ~2x (2.08x, §5a-bis.4; the FR265 "
    "barometer ~1.08x) and the DEM profile is smoothed 1 m lidar -- the DEM "
    "is the reference; never reconcile to Garmin."
)

# A primitive outside this band is worth looking at first. Still presentation:
# "unusual against your own recent history on this cluster" is an observation,
# not a judgement about the run.
P_HIGH, P_LOW = 90.0, 10.0

# Salience ranks, highest first. The chat reads top-down, so the order encodes
# WHERE TO LOOK, nothing more.
#   fatal flag        the data may be wrong -- everything below is suspect
#   extreme percentile the run is unusual against its own comparison set
#   failed gate       a claim CANNOT be made; not that the run was bad
#   warning flag      a quality caveat on an otherwise usable run
#   info flag         noted, rarely load-bearing
_SALIENCE = {"fatal": 1000, "warning": 500, "info": 100}


def notable(rep: dict) -> list[dict]:
    """What to look at first. PRESENTATION ONLY -- no verdict, no cause.

    Nothing here decides whether a run was good. It says which numbers are
    unusual against this athlete's own recent history on this cluster, which
    gates refused to produce a claim, and which flags fired. §16.3's
    elimination remains the chat's, which has the context this function lacks.
    """
    items = []

    # A TERRAIN GAP OUTRANKS EVERYTHING EXCEPT A FATAL FLAG. Without it the
    # reader sees a page of absent terrain numbers and no reason, which reads
    # as breakage rather than as a missing tile with a known fix.
    cov = rep.get("terrain_coverage") or {}
    if cov.get("status") == "outside_loaded_tiles":
        need = "; ".join(
            f"{m['usgs_cell']} (E {m['easting_range'][0]:,}-"
            f"{m['easting_range'][1]:,}, N {m['northing_range'][0]:,}-"
            f"{m['northing_range'][1]:,})"
            for m in cov["missing_tiles"]
        )
        # A cell with no USGS grid (a southern zone, or one past NAD83's)
        # has NO fetch command; its reason is shown instead of a None.
        cmd = " && ".join(
            m["fetch_command"]
            for m in cov["missing_tiles"]
            if m.get("fetch_command") is not None
        )
        unfetchable = [
            f"{m['usgs_cell']}: {m['fetch_unavailable_reason']}"
            for m in cov["missing_tiles"]
            if m.get("fetch_command") is None
        ]
        # THE FIX SENTENCE MUST BE TRUE OF THIS GROUND: where no cell has a
        # command, nothing will be resolved or downloaded, so say that
        # instead of describing what fetch-tile would have done.
        if cmd:
            fix = (
                f"TO FIX: {cmd} — it resolves the real USGS filename for "
                f"{need} against the 3DEP index, verifies the download and "
                f"re-runs retile."
            )
            if unfetchable:
                fix += " No command exists for " + "; ".join(unfetchable) + "."
        else:
            fix = (
                "NO TILE CAN BE FETCHED FOR THIS GROUND by this tool: "
                + "; ".join(unfetchable)
                + ". A DEM tile placed in the DEM "
                "directory by hand is picked up by `retile`."
            )
        items.append(
            {
                "kind": "terrain_gap",
                "severity": "gated",
                "salience": 900,
                "text": (
                    f"NO DEM TILE COVERS {cov['n_uncovered']} of "
                    f"{cov['n_points']} positions on this run "
                    f"({cov['fraction_covered']:.0%} covered). Every terrain "
                    f"primitive is REFUSED for that reason, not because the "
                    f"run was bad. {fix}"
                ),
            }
        )

    for f in rep["flags"]:
        sev = (f["severity"] or "info").lower()
        items.append(
            {
                "kind": "flag",
                "severity": sev,
                "salience": _SALIENCE.get(sev, 100),
                "text": f"[{sev}] {f['check']}: {f['message']}",
            }
        )

    for p in rep["primitives"]:
        pct = p["percentile"]
        if pct is not None and (pct >= P_HIGH or pct <= P_LOW):
            h = p["history"]
            side = "high" if pct >= P_HIGH else "low"
            items.append(
                {
                    "kind": "extreme",
                    "severity": side,
                    "salience": 600 + abs(pct - 50.0),
                    "text": (
                        f"{p['name']} {p['value']:+.4g} {p['unit']} is {side} "
                        f"against the comparison set (p{pct:.0f}; median "
                        f"{h['median']:+.4g}, p25 {h['p25']:+.4g}, "
                        f"p75 {h['p75']:+.4g}, n={h['n']})"
                    ),
                }
            )
        elif p["status"] == GATED:
            items.append(
                {
                    "kind": "gate",
                    "severity": "gated",
                    "salience": 550,
                    "text": (
                        f"{p['name']} was computed then REJECTED by its gate: "
                        f"{p['reason']}. The run does not support the claim -- "
                        f"that is not the same as the run being bad."
                    ),
                }
            )

    return sorted(items, key=lambda i: -i["salience"])


def enforce_status_contract(prims: list) -> list:
    """§16.2's four statuses, enforced at assembly rather than trusted.

    STATUS `ok` REQUIRES A VALUE. An `ok` wrapping a None is the fourth status
    collapsing into the first: it tells a reader the number is good and then
    hands them nothing, and every downstream consumer that formats it dies --
    which is exactly how this surfaced, as `abs(None)` in render() on a
    payload that had built perfectly.

    THE SOURCE FIX IS STILL THE REAL FIX. A primitive that produced no value
    must downgrade ITSELF, because only it knows WHY. This is the backstop
    that makes the next one impossible to ship silently: the demotion carries
    `defect: true` so it reads as our bug rather than as a property of the run.
    """
    out = []
    for p in prims:
        if p.get("status") == OK and p.get("value") is None:
            out.append(
                {
                    **p,
                    "status": UNAVAILABLE,
                    "defect": True,
                    "reason": (
                        f"CONTRACT BREACH: {p.get('name')} reported status "
                        f"'ok' with value None. §16.2's `ok` means the "
                        f"number is good; a null under it is the status "
                        f"collapse the contract exists to prevent. "
                        f"Demoted here, but the DEFECT IS IN THE "
                        f"PRIMITIVE, which should have downgraded itself "
                        f"and said why."
                        + (
                            f" Its own reason was: {p['reason']}"
                            if p.get("reason")
                            else ""
                        )
                    ),
                }
            )
        else:
            out.append(p)
    return out


def _config_reason(exc: ConfigError) -> str:
    """A missing-configuration refusal, phrased for a REPORT.

    The ConfigError's own text is deliberately NOT copied in: it can name the
    UTM zone the run's positions sit in, which is useful at a terminal and is
    still a statement about where the athlete runs -- and a report is output
    that ends up in a chat. The field name is enough to act on.
    """
    field = str(exc).split(" ", 1)[0]
    return (
        f"not computable: the athlete configuration has no {field}, which "
        f"this needs. Set it in config/athlete.yaml "
        f"(config/athlete.example.yaml documents every field). This is a "
        f"missing setting, not a property of the run."
    )


def _config_prim(name: str, unit: str, exc: ConfigError) -> dict:
    """A primitive that could not run for want of configuration."""
    return {
        "name": name,
        "unit": unit,
        "value": None,
        "n": 0,
        "status": UNAVAILABLE,
        "reason": _config_reason(exc),
        "config_required": str(exc).split(" ", 1)[0],
        "history": P.spread([]),
        "percentile": None,
    }


def _section(name: str, fn, *, default=None):
    """Run one section and never let it take the report down with it.

    THE BUG THIS EXISTS FOR. `float(None)` inside
    basic_metrics -> durations raised TypeError, `_call` caught it at the
    boundary, and the WHOLE report came back outcome "broken" with payload
    null -- for a run whose other eighteen sections were all computable. A
    report that cannot render one number is not a report that knows nothing.

    IT DEGRADES TO `unavailable`, ONE OF §16.2's FOUR, and never invents a
    fifth. But it is not silent about which kind of unavailable it is: an
    absent input and a crashed computation are different facts, so `error`
    carries the exception and `defect` says plainly that this one is OURS. A
    section that degrades without saying why is the status collapse the
    contract exists to prevent, committed one level down.
    """
    try:
        return fn()
    except ConfigError as exc:
        # A MISSING SETTING IS AN ABSENT INPUT, NOT OUR DEFECT: `unavailable`
        # with the field named, and no `defect` flag that would misdirect.
        return {
            "status": UNAVAILABLE,
            "value": None,
            "reason": _config_reason(exc),
            "section": name,
            "config_required": str(exc).split(" ", 1)[0],
            **({"partial": default} if default is not None else {}),
        }
    except Exception as exc:
        return {
            "status": UNAVAILABLE,
            "value": None,
            "reason": f"this section could not be computed: "
            f"{type(exc).__name__}: {exc}",
            "error": f"{type(exc).__name__}: {exc}",
            "section": name,
            "defect": "This is a DEFECT IN THIS CODE, not a property of "
            "the run or a missing input -- every other section "
            "of this report was computed. Do not read it as "
            "'the data is not there'.",
            **({"partial": default} if default is not None else {}),
        }


def build(con, activity_id: str) -> dict:
    """The §16.2 payload for one run."""
    act = con.execute(
        """
        SELECT run_id, start_time_utc, local_tz, sport, total_distance_m,
               duration_s, moving_time_s, total_stopped_s, sampling_regime,
               coverage_fraction, source_file
        FROM activities WHERE activity_id = ?""",
        [activity_id],
    ).fetchone()
    if act is None:
        raise SystemExit(f"no activity {activity_id}")
    (run_id, start, tz, sport, dist, dur, moving, stopped, regime, cov, src_file) = act

    comp = comparison_set(con, activity_id, run_id)
    others = comp["members"]

    def population(fn):
        out = []
        for aid in others:
            try:
                v = fn(con, aid)
            except ConfigError:
                continue  # the subject reports why
            if v["value"] is not None:
                out.append(v["value"])
        return out

    def with_history(name, unit, fn, note):
        """One history-bearing primitive; a missing setting degrades it
        alone, to `unavailable`, rather than taking the report down."""
        try:
            got = fn(con, activity_id)
        except ConfigError as exc:
            return _config_prim(name, unit, exc)
        p = _with_history(name, unit, got["value"], got["n"], population(fn), note=note)
        p["applied_filters"] = got["filters"]
        return p

    prims = []

    # --- TWO DRIFT MEASURES, NEITHER OF WHICH IS "THE" DRIFT ----------------
    # They can differ several-fold on the same run because they are
    # different quantities: §6.7 pairs RECORDS on matched ground with speed
    # matched and walking excluded; the segment version pairs whole EFFORT
    # averages with neither filter, and normalising a short gap to an hour
    # amplifies. Both are reported, both named, neither promoted.
    prims.append(
        with_history(
            "matched_terrain_drift",
            "bpm/hour",
            P.matched_terrain_drift,
            note="no record pairs met §6.7's rule on this run",
        )
    )
    prims.append(
        with_history(
            "segment_effort_drift",
            "bpm/hour",
            P.segment_effort_drift,
            note=f"needs two efforts on one slope >= {P.DRIFT_MIN_GAP_MIN} min apart",
        )
    )

    # --- §6.8 efficiency factor on matched terrain -------------------------
    prims.append(
        with_history(
            "efficiency_factor",
            "m/s per bpm",
            P.efficiency_factor,
            note="needs segment efforts with heart rate",
        )
    )

    # --- §6.3 delta-HR by direction ----------------------------------------
    for direction in ("ascent", "descent"):
        row = con.execute(
            """
            SELECT median(delta_hr_bpm), count(*) FROM segment_recovery
            WHERE activity_id = ? AND direction = ?""",
            [activity_id, direction],
        ).fetchone()
        pop = (
            [
                r[0]
                for r in con.execute(
                    """
            SELECT median(delta_hr_bpm) FROM segment_recovery
            WHERE direction = ? AND activity_id IN (SELECT unnest(?))
            GROUP BY activity_id""",
                    [direction, others],
                ).fetchall()
                if r[0] is not None
            ]
            if others
            else []
        )
        p_dhr = _with_history(
            f"delta_hr_{direction}",
            "bpm",
            row[0],
            row[1],
            pop,
            note="no efforts of this direction on this run",
        )
        # These were the ONLY `ok` primitives in the payload with no filter
        # block, against the rule that every result states its filters as
        # FIELDS. A chat that cannot see them cannot know what the number is.
        p_dhr["applied_filters"] = {
            "split_type": None,
            "walking_included": True,
            "direction": direction,
            "hr_source": "peak minus opening HR within an effort",
            "min_delta_hr_bpm": R.MIN_DELTA_HR_BPM,
            "stopped_time_excluded": True,
            "terrain_matched_by": "segment identity (place, side)",
            "quality_flags_excluded": False,
            "grain": "segment efforts",
            "note": "ascent and descent are NOT comparable on this metric: "
            "an ascent typically raises HR far more than a descent. "
            "Never rank recovery without the delta-HR beside it.",
        }
        prims.append(p_dhr)

    # --- §6.3 tau: refused, and refused is not a finding --------------------
    verdict = con.execute(
        "SELECT bool_or(tau_reportable) FROM segment_recovery"
    ).fetchone()[0]
    # WHY tau is refused, diagnosed from THIS run rather than from a constant.
    # The old message read "requires 1 Hz (§15.4); this run is 1hz", which is
    # self-contradictory on a 1 Hz file -- and, measured on the first one, wrong
    # as a diagnosis. The blocker was TERRAIN, not sampling rate: every effort
    # on that run had well under the 120 s clear a decay needs before the next
    # effort, and the one with 300 s clear had almost no excursion.
    clear_med, n_short = con.execute(
        """
        SELECT median(clear_window_s),
               count(*) FILTER (WHERE clear_window_s < ?)
        FROM segment_recovery WHERE activity_id = ?""",
        [R.MIN_CLEAR_WINDOW_S, activity_id],
    ).fetchone()
    n_tau = con.execute(
        "SELECT count(tau_s) FROM segment_recovery WHERE activity_id = ?", [activity_id]
    ).fetchone()[0]
    # THE VALUE, WHICH WAS NEVER WIRED. `value` below was hardcoded None while
    # this branch could set status OK, so every activity with a tau estimate
    # rendered as ok-with-no-number and render() died on abs(None). Many
    # activities were on that path, and it only began crashing when the
    # corpus-wide gate flipped true -- the defect arrived with data, not with
    # code.
    #
    # `tau_reportable` IS A CORPUS VERDICT STAMPED ON EVERY ROW, not a
    # per-effort flag -- `recovery` computes one verdict from the whole
    # corpus's within-slope repeatability and writes it to every row. An
    # earlier version of this comment called it per-effort and was wrong.
    #
    # THE GATE CAN OPEN ON A THIN BASIS: a CV well under MAX_TAU_CV from only
    # a handful of (segment, direction) groups having the 3+ estimates the
    # gate requires. §6.3 says tau starts reporting on its own when the data
    # supports it, and by its own rule it then does; whether a few groups is
    # support is the athlete's call, so the basis travels WITH the number
    # rather than being left in a commit message.
    # n BESIDE THE VALUE IS THE n THE VALUE IS A MEDIAN OF. `n` was n_tau --
    # every estimate on the run -- while the value is the median over the
    # REPORTABLE ones only, so the count beside an accepted tau described a
    # different set from the one that produced it.
    tau_value, n_qualifying = con.execute(
        "SELECT median(tau_s), count(*) FROM segment_recovery "
        "WHERE activity_id = ? AND tau_reportable AND tau_s IS NOT NULL",
        [activity_id],
    ).fetchone()
    if n_tau and verdict and tau_value is not None:
        gate = con.execute("""
            SELECT count(*) FROM (
                SELECT segment_id, direction FROM segment_recovery
                WHERE tau_s IS NOT NULL
                GROUP BY 1, 2 HAVING count(*) >= 3)""").fetchone()[0]
        tau_prim = {
            "status": OK,
            "reason": None,
            "gate_basis": {
                "n_groups_supporting": gate,
                "note": (
                    f"§6.3's repeatability gate opened on "
                    f"{gate} (segment, direction) group(s) with "
                    f"3+ tau estimates. Read the figure against "
                    f"that basis: the gate lifting is not the "
                    f"same as tau being well-founded, and "
                    f"whether this basis is enough is an athlete "
                    f"decision §6.3 reserves."
                ),
            },
        }
    elif n_tau and verdict:
        # THE GATE IS OPEN AND THIS RUN STILL HAS NOTHING. A computation that
        # produced no value downgrades ITSELF rather than leaving a status its
        # value cannot support.
        tau_prim = {
            "status": UNAVAILABLE,
            "reason": (
                f"the corpus gate is open, but none of this "
                f"run's {n_tau} tau estimate(s) sits on an "
                f"effort marked reportable, so there is no "
                f"value to give"
            ),
        }
    else:
        why = []
        if regime != "1hz":
            why.append(
                f"tau is transient-class and needs 1 Hz (§15.4); this run is {regime}"
            )
        if n_short:
            why.append(
                f"{n_short} efforts had under "
                f"{R.MIN_CLEAR_WINDOW_S:.0f}s clear before the next "
                f"effort (median {clear_med:.0f}s) -- this terrain does "
                f"not provide a recovery interval"
            )
        if not verdict:
            why.append(
                "tau does not repeat within one slope across the corpus "
                "(§6.3), so it is not reportable even where estimated"
            )
        tau_prim = {
            "status": REFUSED,
            "reason": "; ".join(why) or "no tau estimates on this run",
        }
    accepted = tau_prim["status"] == OK
    prims.append(
        {
            "name": "climb_recovery_tau",
            "unit": "s",
            "value": tau_value if accepted else None,
            "n": n_qualifying if accepted else n_tau,
            "n_basis": (
                "qualifying efforts: reportable tau estimates "
                "on this run, which the value is the median of"
                if accepted
                else "tau estimates on this run (none accepted)"
            ),
            "n_qualifying": n_qualifying,
            "n_estimates": n_tau,
            "history": P.spread([]),
            "percentile": None,
            "applied_filters": {
                "min_clear_window_s": R.MIN_CLEAR_WINDOW_S,
                "min_delta_hr_bpm": R.MIN_DELTA_HR_BPM,
                "sampling_regime": regime,
                "grain": "segment efforts",
            },
            **tau_prim,
        }
    )

    # --- §6.9 drift onset: a VIEW of §6.7, gated on the live corpus --------
    try:
        onset_gate = DO.gate_status(con)
        oc = DO.curve(con, activity_id) if onset_gate["reportable"] else None
    except ConfigError as exc:
        # The gate projects positions to pair records, so without a
        # projection it cannot run at all: that is the reason, not a refusal.
        onset_gate, oc = None, None
        prims.append(_config_prim("drift_onset", "min into run", exc))
    if onset_gate is not None and onset_gate["reportable"]:
        prims.append(
            {
                "name": "drift_onset",
                "unit": "min into run",
                "value": oc["onset_min"],
                "n": oc["n_usable_bins"],
                "status": OK if oc["onset_status"] == "onset" else GATED,
                "reason": None if oc["onset_status"] == "onset" else oc["reason"],
                "history": P.spread([]),
                "percentile": None,
                "applied_filters": oc["filters"],
            }
        )
    elif onset_gate is not None:
        prims.append(
            {
                "name": "drift_onset",
                "unit": "min into run",
                "value": None,
                "n": 0,
                "status": REFUSED,
                "reason": onset_gate["reason"],
                "history": P.spread([]),
                "percentile": None,
                "applied_filters": {
                    "required_sampling_regime": DO.REQUIRED_SAMPLING_REGIME,
                    "min_onset_runs": DO.MIN_ONSET_RUNS,
                    "min_onset_duration_min": DO.MIN_ONSET_DURATION_MIN,
                    "max_onset_cv": DO.MAX_ONSET_CV,
                    "grain": "record pairs, binned by time into run",
                },
            }
        )

    # --- §6.4 decoupling: not built; say so rather than imply it passed -----
    prims.append(
        {
            "name": "aerobic_decoupling",
            "unit": "%",
            "value": None,
            "n": 0,
            "status": UNAVAILABLE,
            "reason": (
                "not built (§12.5). §6.7 matched-terrain drift is the "
                "documented substitute and is effort-matched by construction."
            ),
            "history": P.spread([]),
            "percentile": None,
        }
    )

    # --- anything GAP-based is refused until §6.1 refits --------------------
    prims.append(
        {
            "name": "grade_adjusted_pace",
            "unit": "m/s",
            "value": None,
            "n": 0,
            "status": REFUSED,
            "reason": (
                "GAP is not validated (§12.2): it beats raw speed in one of "
                "four comparisons and is worse within running samples. §6.1's "
                "personal refit is the prerequisite."
            ),
            "history": P.spread([]),
            "percentile": None,
        }
    )

    # --- conditions, with provenance travelling WITH the numbers ------------
    wx = con.execute(
        """
        SELECT min(temp_c), max(temp_c), min(dew_point_c), max(dew_point_c),
               max(precip_prev_24h_mm), any_value(weather_source),
               bool_and(is_final)
        FROM weather_records WHERE activity_id = ?""",
        [activity_id],
    ).fetchone()
    shortwave = _run_shortwave(con, activity_id)
    wbgt = _estimated_wbgt(con, activity_id, wx[5], shortwave)

    # --- athlete input ------------------------------------------------------
    # STALE IS NOT EMPTY. `annotations` is a projection of the athlete's
    # notes file, refreshed only by a write connection, so a reader that finds
    # it out of date must say so rather than report "no notes" -- a stale
    # projection read as empty once hid real entries for days.
    from trail_factors.analysis.queries import projection_status

    notes_projection = projection_status(con, "annotations")
    notes = (
        [
            r[0]
            for r in con.execute(
                "SELECT note FROM annotations WHERE run_id = ?", [run_id]
            ).fetchall()
        ]
        if notes_projection["status"] == "ok"
        else []
    )

    # §15.4 per-record artifacts, now that records_enriched.quality_flags
    # exists. What IS and is NOT enforced is stated rather than implied.
    qrows = con.execute(
        """
        SELECT quality_flags, count(*) FROM records_enriched
        WHERE activity_id = ? AND coalesce(quality_flags, '') <> ''
        GROUP BY 1""",
        [activity_id],
    ).fetchall()
    n_records = con.execute(
        "SELECT count(*) FROM records_enriched WHERE activity_id = ?", [activity_id]
    ).fetchone()[0]
    per_flag: dict[str, int] = {}
    for combo, n in qrows:
        for f in combo.split(","):
            per_flag[f] = per_flag.get(f, 0) + n
    qflags = {
        "n_records": n_records,
        "n_flagged": sum(n for _, n in qrows),
        "by_flag": dict(sorted(per_flag.items(), key=lambda kv: -kv[1])),
    }
    unenforced = [
        "Segment-grain primitives (efficiency_factor, delta_hr, "
        "segment_effort_drift) read segment_efforts, which aggregates records "
        "upstream of the flags -- per-record exclusion does NOT reach them. "
        "Each states quality_flags_excluded=false in its applied_filters.",
        "Arm-motion decoupling is NOT flagged: §14a measured it and found the "
        "trend sign opposite to the hypothesis, so a flag would encode an "
        "unsupported claim as data (§15.4).",
    ]

    flags = con.execute(
        """
        SELECT check_name, severity, message FROM validation_results
        WHERE activity_id = ? ORDER BY severity, check_name""",
        [activity_id],
    ).fetchall()

    rep = {
        "run": {
            "run_id": run_id,
            "activity_id": activity_id,
            "start_utc": start,
            "local_tz": tz,
            "sport": sport,
            "cluster": comp["cluster"],
            # THE GARMIN FILE ID, NOT THE PATH. `source_file` is
            # `data/raw/garmin/<id>_ACTIVITY.fit` -- a path into the one
            # directory that holds raw GPS recordings, handed to the
            # chat on every report. The id is the provenance a reader
            # actually needs (it is also how they reference a run); the
            # directory it lives in is not.
            "garmin_file_id": _garmin_id(src_file),
        },
        "provenance": {
            "sampling_regime": regime,
            "coverage_fraction": cov,
            "weather_source": wx[5],
            "weather_is_final": wx[6],
        },
        "measured": {
            "distance_m": dist,
            "elapsed_s": dur,
            "moving_s": moving,
            "stopped_s": stopped,
            # Pace is DISPLAY ONLY and is never stored (src/trail_factors/units.py).
            # Both are given because §4.3 forbids reporting one alone.
            "elapsed_pace_per_mile": pace_over(dur, dist),
            "moving_pace_per_mile": pace_over(moving, dist),
        },
        "comparison": comp,
        # §6.0: the run's own description. These are ANCHORS -- average HR
        # reads nearly the same on every easy run -- and they are here because
        # every §6.1-§6.9 number is a deviation from them. A chat that cannot
        # see the level cannot size the deviation.
        "basic": _section("basic", lambda: B.basic_metrics(con, activity_id)),
        # ENFORCED, NOT TRUSTED. See enforce_status_contract.
        "primitives": enforce_status_contract(prims),
        "grade_table": _section(
            "grade_table", lambda: P.grade_binned_pace(con, activity_id)["bins"]
        ),
        "grade_table_filters": _section(
            "grade_table_filters",
            lambda: P.grade_binned_pace(con, activity_id)["filters"],
        ),
        "climb_signature": _section(
            "climb_signature", lambda: P.climb_signature(con, activity_id)
        ),
        "elevation_note": ELEVATION_NOTE,
        # §6.0g: the athlete's second focus metric. climb_signature describes
        # the terrain; this says how it was RUN.
        "hill_pace": _section(
            "hill_pace", lambda: B.effort_pace_by_direction(con, activity_id)
        ),
        # M3 (§17.2): CHOICE, not capacity -- how the run was structured.
        "run_walk_cycles": _section(
            "run_walk_cycles", lambda: CY.cycles(con, activity_id)
        ),
        # §5a: WHY the terrain primitives are absent, when they are. A run on
        # ground no loaded tile covers produced a report with every terrain
        # number missing and no explanation -- the refusal was correct and
        # unreadable.
        "terrain_coverage": _section(
            "terrain_coverage", lambda: _terrain_coverage(con, activity_id)
        ),
        # PROVENANCE BESIDE THE NUMBERS, not only in `provenance`: a reader
        # quoting a temperature must see in the same block that a forecast
        # value is provisional until the archive refetch replaces it.
        "conditions": {
            "temp_c": (wx[0], wx[1]),
            "dew_point_c": (wx[2], wx[3]),
            "precip_prev_24h_mm": wx[4],
            "estimated_wbgt_c": wbgt,
            "shortwave_wm2": shortwave,
            "weather_source": wx[5],
            "is_final": wx[6],
        },
        # FOSTER LOAD FROM GARMIN'S OWN RATING: garmin_rpe x timer minutes.
        # An unrated activity is `unavailable` with the reason, never 0;
        # feel is shown and never enters the load. The filters travel with
        # the number so the basis (timer, not moving, time) is visible.
        "session_load": _section(
            "session_load", lambda: _session_load(con, activity_id)
        ),
        # GARMIN'S POST-RUN RATING, as the watch recorded it.
        "athlete": {
            "garmin_self_evaluation": _garmin_selfeval(con, activity_id),
            "notes": notes,
            "notes_status": notes_projection["status"],
            "notes_projection": (
                None if notes_projection["status"] == "ok" else notes_projection
            ),
        },
        "flags": [{"check": c, "severity": s, "message": m} for c, s, m in flags],
        # §15.4 requires flagged measurements be EXCLUDED, not down-weighted.
        # What this grain can and cannot enforce is stated rather than implied.
        "quality_flags": qflags,
        "unenforced": unenforced,
        "calc_version": CALC_VERSION,
    }
    rep["notable"] = notable(rep)
    return rep


def _session_load(con, activity_id: str) -> dict:
    """The run's Foster load (trail_factors.analysis.session_load) with its filters."""
    from trail_factors.analysis import session_load as SL

    return {**SL.session_load(con, activity_id), "filters": SL.filters()}


def _num_or_dash(v, fmt: str) -> str:
    """A number in `fmt`, or a dash of the same width: a missing figure is
    not a measured 0 and must not print as one."""
    width = int("".join(ch for ch in fmt.split(".")[0] if ch.isdigit()))
    return format(v, fmt) if v is not None else f"{'-':>{width}}"


def _garmin_selfeval(con, activity_id: str) -> dict:
    """Garmin's post-run RPE and feel, with the scale that decodes them.

    Asked by the watch seconds after the run, on a single 1-10 effort scale
    and a 1-5 feel scale. The RPE prices session_load; feel is shown only,
    because the direction of its decoding is unverified.
    """
    row = con.execute(
        """
        SELECT garmin_rpe, garmin_feel, garmin_selfeval_scale
        FROM activities WHERE activity_id = ?""",
        [activity_id],
    ).fetchone()
    if row is None or (row[0] is None and row[1] is None):
        return {
            "status": "unavailable",
            "reason": SL_UNRATED_REASON,
            "garmin_rpe": None,
            "garmin_feel": None,
        }
    return {
        "status": "ok",
        "garmin_rpe": row[0],
        "garmin_feel": row[1],
        "scale": row[2],
    }


def _terrain_coverage(con, activity_id: str) -> dict:
    """Is this run inside the loaded DEM tiles, and if not, what would fix it?

    NEVER emits a coordinate: the answer is a 10 km GRID CELL, which is what
    fetching a tile needs and is two orders of magnitude coarser than a
    position (§5a's reasoning, the same as rounding weather coordinates).
    """
    rows = con.execute(
        """
        SELECT lat, lon FROM records
        WHERE activity_id = ? AND lat IS NOT NULL
    """,
        [activity_id],
    ).fetchall()
    if not rows:
        return {
            "status": "no_position",
            "reason": "this run has no GPS, so terrain analysis is not "
            "gated on a tile -- it has nothing to look up",
            "missing_tiles": [],
        }
    try:
        from trail_factors.transform.dem import TileSet

        cov = TileSet().coverage([r[0] for r in rows], [r[1] for r in rows])
    except Exception as exc:  # a missing tile file, say
        return {
            "status": "unknown",
            "reason": f"{type(exc).__name__}: {exc}",
            "missing_tiles": [],
        }
    if not cov["missing_tiles"]:
        return {"status": "covered", **cov}
    return {"status": "outside_loaded_tiles", **cov}


def _garmin_id(source_file: str | None) -> str | None:
    """The Garmin activity id embedded in a raw filename, without the path."""
    import re

    if not source_file:
        return None
    m = re.search(r"(\d+)_ACTIVITY", source_file)
    return m.group(1) if m else None


def _f(c):
    return None if c is None else c * 9 / 5 + 32


def _render_basic(b: dict) -> list[str]:
    """§6.0 anchors. Every pace is M:SS/mi (src/trail_factors/units.py);
    nothing is stored."""
    L = ["  BASIC METRICS  (anchors, not signals -- HR is near-flat across grade)"]

    hr = b["average_hr"]
    if hr["value"] is None:
        L.append(f"    {'average HR':<22}{'UNAVAILABLE':>9}   {hr['reason']}")
    else:
        parts = []
        for label, key in (("run", "avg_hr_running"), ("walk", "avg_hr_walking")):
            if hr[key] is not None:
                parts.append(f"{label} {hr[key]:.0f}")
        L.append(
            f"    {'average HR':<22}{hr['value']:>7.1f} bpm"
            f"   ({', '.join(parts) if parts else 'no split decomposition'})"
        )

    # THE CEILING, BOTH WAYS. The single sample is what the watch showed and is
    # a SPIKE CANDIDATE; the 30 s mean is the defensible figure. Printing only
    # one of them would either disagree with the device for no visible reason
    # or quote a number one bad sample can set.
    mh = b.get("max_hr") or {}
    rec, w30 = (mh.get("max_hr_recorded") or {}), (mh.get("max_hr_30s") or {})
    if rec.get("value") is not None:
        tail = (
            f"   {w30['value']:>5.1f} bpm best 30 s"
            if w30.get("value") is not None
            else f"   30 s window {w30.get('status_hint', 'unavailable')}"
        )
        L.append(f"    {'max HR':<22}{rec['value']:>7.1f} bpm single sample{tail}")
        if mh.get("session_agrees") is False:
            L.append(
                f"      watch session summary says {mh['session_max_hr']:.0f} bpm -- "
                f"the record stream is decimated and understates (§6.0)"
            )

    d = b["durations"]
    if d.get("run_s") is None:
        L.append(f"    {'run/walk/stand':<22}{'UNAVAILABLE':>9}   {d['reason']}")
    else:
        # 0 s is a REAL value here; n_splits is what distinguishes it from
        # missing, so it is printed rather than inferred.
        bits = []
        for key in ("run", "walk", "stand"):
            bits.append(f"{key} {d[f'{key}_s'] / 60:.1f}m({d[f'n_{key}_splits']})")
        L.append(
            f"    {'run/walk/stand':<22}{'  '.join(bits)}"
            f"   [splits.total_timer_time, regime-independent]"
        )

    for p in b["paces"]["paces"]:
        if p["status"] == "ok":
            L.append(
                f"    {p['name'] + ' pace':<22}{p['pace_per_mile']:>7}/mi"
                f"   ({p['seconds'] / 60:.1f} min, {p['meters'] / 1609.344:.2f} mi)"
            )
        else:
            L.append(
                f"    {p['name'] + ' pace':<22}{p['status'].upper():>9}   {p['reason']}"
            )

    w = b["windowed_bests"]
    L.append(
        f"    {'BEST WINDOWS':<22}(sliding, interpolated -- never an instantaneous max)"
    )
    for win in w["windows"]:
        if win["status"] != "ok":
            L.append(f"      {win['window']:<20}{'UNAVAILABLE':>9}   {win['reason']}")
            continue
        gait = ""
        if win.get("walk_time_fraction") is not None:
            gait = f"  walk {win['walk_time_fraction']:.0%}"
        flag = ""
        if win.get("flagged_record_fraction") is not None:
            flag = f"  flagged {win['flagged_record_fraction']:.0%}"
        span = (win["end_elapsed_s"] - win["start_elapsed_s"]) / 60
        # A 30 s window spans a third of a minute, so a whole-minute range
        # renders as "100-100". Short windows state their start; long ones
        # state the stretch they cover.
        when = (
            f"from {win['start_elapsed_s'] / 60:.1f} min"
            if span < 1
            else f"at {win['start_elapsed_s'] / 60:.0f}-"
            f"{win['end_elapsed_s'] / 60:.0f} min"
        )
        # A WINDOW WITH NO PACE IS AN ABSENCE, NOT A FORMAT ERROR. On the
        # indoor rides the odometer is a constant 0.0, so the window exists
        # and its pace does not -- and `{None:>7}` is a TypeError that used to
        # take the whole report with it. A sweep found every indoor ride
        # dying here.
        _pace = win.get("pace_per_mile")
        L.append(
            f"      {win['window']:<20}"
            + (f"{_pace:>7}/mi" if _pace is not None else f"{'no pace':>7}   ")
            + f"   {when}{gait}{flag}"
        )

    c = b["mean_running_cadence"]
    if c["value"] is None:
        L.append(f"    {'running cadence':<22}{'UNAVAILABLE':>9}   {c['reason']}")
    else:
        L.append(
            f"    {'running cadence':<22}{c['value']:>7.1f} spm"
            f"   (steps/min = (raw + fractional) x 2, §9 resolved)"
        )

    cg = b["cadence_by_grade"]
    if not cg["bins"]:
        L.append(f"    {'cadence by grade':<22}{'UNAVAILABLE':>9}   {cg['reason']}")
    else:
        L.append("    CADENCE BY GRADE  (never pooled -- run and walk differ)")
        L.append(f"      {'split':<10}{'grade':>10}{'spm':>8}{'time':>8}")
        for g in cg["bins"]:
            L.append(
                f"      {g['split_type']:<10}"
                f"{g['grade_bin']:>5.0f}..{g['grade_bin'] + 5:<4.0f}"
                f"{g['steps_per_min']:>8.1f}{g['weight_s']:>7.0f}s"
            )
    return L


def _render_cycles(c: dict) -> list[str]:
    """M3 run/walk cycle economics. CHOICE (§6.6) -- how the run was run."""
    L = ["", "  RUN/WALK CYCLES  (choice, not capacity -- does the walk buy anything?)"]
    if not c["cycles"]:
        L.append(f"    UNAVAILABLE   {c['reason']}")
        return L
    s = c["summary"]
    L.append(
        f"    {s['n_run_blocks']} run blocks, {s['n_complete_cycles']} "
        f"complete cycles   [{c['status_hint'].upper()}]"
    )
    if c["reason"]:
        L.append(f"    {c['reason']}")
    # WHAT GOVERNED THE WALKS, printed with the numbers it qualifies. A
    # by-feel purchase curve is the same arithmetic on a different quantity,
    # and the place that matters is right here, above the series.
    L.append(
        f"    walk protocol   {c['walk_protocol'] or c['walk_protocol_status']}"
        f"   poolable: {'yes' if c['poolable_with_other_runs'] else 'NO'}"
    )
    if not c["poolable_with_other_runs"]:
        L.append(f"      {c['pooling_note']}")

    # A run with no walk at all is a REAL state, not missing data -- some
    # runs never walk. Each field says "none" rather than formatting
    # a null or, worse, printing 0 s of walking as if it had been measured.
    def q(v, fmt):
        return "none" if v is None else format(v, fmt)

    L.append(
        f"    median run {q(s['median_run_duration_s'], '.0f')} s, "
        f"median walk {q(s['median_walk_duration_s'], '.0f')} s, "
        f"median HR recovered {q(s['median_hr_recovered_bpm'], '+.0f')} bpm"
    )
    if s["first_run_pace_per_mile"]:
        L.append(
            f"    first run block {s['first_run_pace_per_mile']}/mi -> "
            f"last {s['last_run_pace_per_mile']}/mi"
        )
    if s["n_run_blocks_below_pace_gate"]:
        L.append(
            f"    {s['n_run_blocks_below_pace_gate']} run blocks are "
            f"under the {CY.MIN_CLASS_TIME_S:.0f} s pace gate and report "
            f"duration but no pace"
        )
    if c["purchase"]:
        L.append(f"    {'walk_s':>8}{'recov':>7}{'-> next run':>13}{'at min':>8}")
        for p in c["purchase"]:
            L.append(
                f"    {p['walk_duration_s']:>8.0f}"
                f"{_num_or_dash(p['hr_recovered_bpm'], '+7.0f')}"
                f"{str(p['next_run_pace_per_mile']):>13}"
                f"{p['elapsed_at_next_run_s'] / 60:>8.1f}"
            )
    return L


def _render_hill_pace(h: dict) -> list[str]:
    """§6.0g hill pace by direction: how the terrain was RUN, not what it is."""
    L = [
        "",
        "  HILL PACE BY DIRECTION  (mean = whole effort summit to col; "
        "best 30 s = the part run fast)",
    ]
    if not h["efforts"]:
        L.append("    UNAVAILABLE   no segment efforts on this run")
        return L
    L.append(
        f"    {'segment':<14}{'dir':<9}{'len':>6}{'grade':>7}"
        f"{'steep':>7}{'mean':>8}{'best30':>8}{'HR':>5}"
    )
    for e in h["efforts"]:
        steep = e["steep_mean_grade_pct"]
        best = e["best_30s_pace_per_mile"] or (e["best_30s_status"] or "-")
        L.append(
            f"    {e['segment_id']:<14}{e['direction']:<9}"
            f"{e['length_m']:>6.0f}{e['mean_grade_pct']:>+7.1f}"
            f"{_num_or_dash(steep, '+7.1f')}"
            f"{str(e['mean_pace_per_mile'] or '-'):>8}{str(best):>8}"
            f"{_num_or_dash(e['avg_hr'], '5.0f')}"
        )
    for d in ("ascent", "descent"):
        v = h["by_direction"][d]
        if v["pace_per_mile"] is None:
            L.append(f"    {d.upper():<14}no efforts")
            continue
        L.append(
            f"    {d.upper():<14}n={v['n_efforts']:<3}{v['distance_m']:>6.0f} m"
            f"{v['mean_grade_pct']:>+7.1f}{'':>7}{v['pace_per_mile']:>8}"
            f"{'':>8}{_num_or_dash(v['avg_hr'], '5.0f')}"
        )
    ratio = h["comparable_ground"]["length_ratio"]
    if ratio is not None:
        L.append(
            f"      ascent/descent ground ratio {ratio:.2f} -- the two "
            f"directions do NOT cover the same ground (§14k), so part of "
            f"any difference is terrain"
        )
    return L


def render_safe(rep: dict) -> tuple[str, str | None]:
    """(text, render_error). FORMATTING MUST NOT COST A PAYLOAD.

    render() runs OUTSIDE the per-section isolation `build` uses, so a
    formatting bug threw away a payload that had already been computed
    correctly -- 19 sections built, every number present, and the whole thing
    lost to `abs(None)` in a two-line number formatter.

    The payload is the product; the text is a convenience over it. So a
    raising renderer returns the exception as `render_error` and the caller
    still gets everything it asked for.
    """
    try:
        return render(rep), None
    except Exception as exc:
        return ("", f"{type(exc).__name__}: {exc}")


def render(rep: dict) -> str:
    """Plain text. The chat reads this; keep the shape, not a summary."""
    r, m, pv = rep["run"], rep["measured"], rep["provenance"]
    L = []
    L.append("=" * 70)
    L.append(f"POST-RUN REPORT  {r['run_id']}   {r['cluster']}")
    L.append("=" * 70)
    L.append(f"  {'start (UTC)':<22}{r['start_utc']}   local UTC{r['local_tz']:+.0f}")
    mi = m["distance_m"] / METERS_PER_MILE
    L.append(f"  {'distance':<22}{m['distance_m']:,.0f} m  ({mi:.2f} mi)")
    # §4.3: never report one pace without the other.
    for label, secs in (("elapsed", m["elapsed_s"]), ("moving", m["moving_s"])):
        if secs:
            pace = pace_over(secs, m["distance_m"], placeholder="--:--")
            L.append(f"  {label + ' pace':<22}{pace}/mi   ({secs / 60:.1f} min)")
    L.append(
        f"  {'sampling regime':<22}{pv['sampling_regime']}"
        f"   coverage {pv['coverage_fraction']:.0%}"
    )

    c = rep["conditions"]
    if c["temp_c"][0] is not None:
        final = "final" if pv["weather_is_final"] else "PROVISIONAL"
        assumed = (
            " -- ASSUMED, not measured"
            if pv["weather_source"] == "assumed_indoor"
            else ""
        )
        L.append(
            f"  {'temperature':<22}{_f(c['temp_c'][0]):.0f}-{_f(c['temp_c'][1]):.0f} F"
        )
        L.append(
            f"  {'dew point':<22}{_f(c['dew_point_c'][0]):.0f}-"
            f"{_f(c['dew_point_c'][1]):.0f} F"
            f"   [{pv['weather_source']}, {final}{assumed}]"
        )
        L.append(f"  {'rain prev 24h':<22}{c['precip_prev_24h_mm']:.1f} mm")
        wb = c.get("estimated_wbgt_c") or {}
        if wb.get("min") is not None:
            L.append(
                f"  {'est. WBGT':<22}{_f(wb['min']):.0f}-"
                f"{_f(wb['max']):.0f} F   (BoM simplified from temp + "
                f"humidity; assumes sun -- upper bound under canopy)"
            )
        sw = c.get("shortwave_wm2") or {}
        if sw.get("value") is not None:
            L.append(
                f"  {'shortwave':<22}{sw['value']:.0f} W/m2   "
                f"(run-weighted, open sky -- an upper bound under "
                f"canopy)"
            )

    L.append("")
    L += _render_basic(rep["basic"])

    cov = rep.get("terrain_coverage") or {}
    if cov.get("status") == "outside_loaded_tiles":
        L.append("")
        L.append(
            f"  TERRAIN: NO DEM TILE  ({cov['fraction_covered']:.0%} of "
            f"{cov['n_points']} positions covered)"
        )
        for m in cov["missing_tiles"]:
            L.append(f"    need cell {m['usgs_cell']:<10}({m['n_points']:,} positions)")
            L.append(
                f"      -> {m['fetch_command']}"
                if m.get("fetch_command") is not None
                else f"      -> no fetch: {m.get('fetch_unavailable_reason')}"
            )
        if any(m.get("fetch_command") is not None for m in cov["missing_tiles"]):
            L.append("    the tile's real filename is resolved from the 3DEP index,")
            L.append("    not constructed; retile runs automatically afterwards")
        L.append("    every terrain primitive below is refused for THIS reason")

    L.append("")
    L.append("  NOTABLE  (where to look first -- presentation, not a verdict)")
    if rep["notable"]:
        for i in rep["notable"]:
            L.append(f"    - {i['text']}")
    else:
        L.append("    nothing outside p10-p90, no failed gates, no flags.")

    comp = rep["comparison"]
    L.append("")
    L.append(f"  COMPARISON SET: {comp['n']} runs, {comp['rule']}")
    if comp["date_range"]:
        L.append(f"    {comp['date_range'][0]} .. {comp['date_range'][1]}")
    if comp["n"] < MIN_SAME_TYPE:
        L.append("    TOO FEW to give a percentile any meaning.")
    for e in comp.get("excluded", []):
        L.append(f"    excluded {e['date']} {e['run_id']}: {e['reason']}")

    L.append("")
    L.append("  PRIMITIVES  (percentile is against the comparison set above;")
    L.append("               it is presentation, not a verdict)")
    for p in rep["primitives"]:
        if p["status"] == OK:
            h = p["history"]
            pct = f"p{p['percentile']:.0f}" if p["percentile"] is not None else "p-"
            # Enough significant figures for EF (hundredths) and drift (tens
            # of bpm/hour) to read on the same line without either collapsing.
            dp = 4 if abs(p["value"]) < 1 else 2
            hist = (
                (
                    f"median {h['median']:+.{dp}f}  p25 {h['p25']:+.{dp}f}  "
                    f"p75 {h['p75']:+.{dp}f}  n={h['n']}"
                )
                if h["n"]
                else "no history"
            )
            L.append(
                f"    {p['name']:<24}{p['value']:+10.{dp}f} {p['unit']:<14}"
                f"{pct:>5}   {hist}"
            )
            if "n_qualifying" in p:
                L.append(
                    f"        n={p['n_qualifying']} qualifying efforts "
                    f"(of {p['n_estimates']} tau estimates on this run)"
                )
            f = p.get("applied_filters")
            if f:
                bits = [
                    f"{k}={v}" for k, v in f.items() if k != "note" and v is not None
                ]
                L.append(f"        filters: {', '.join(bits)}")
        else:
            L.append(f"    {p['name']:<24}{p['status'].upper():>9}   {p['reason']}")

    L.append("")
    sig = rep["climb_signature"]
    L.append(
        f"  CLIMB SIGNATURE  {sig['n_ascents']} ascents / "
        f"{sig['n_descents']} descents, {sig['total_vertical_m']:.0f} m up"
    )
    L.append(f"    {ELEVATION_NOTE}")
    for d, ln, vert, grade in sig["largest"]:
        L.append(f"    {d:<9}{ln:6.0f} m  {vert:5.1f} m vert  {grade:+5.1f}%")

    L += _render_hill_pace(rep["hill_pace"])
    L += _render_cycles(rep["run_walk_cycles"])

    L.append("")
    L.append("  GRADE-BINNED PACE  (never pooled -- run and walk are different)")
    # The dynamics columns appear only when the device recorded them, so a
    # FR245 report is unchanged rather than gaining four empty columns.
    has_rd = any(
        g.get("vertical_oscillation_mm") is not None for g in rep["grade_table"]
    )
    head = f"    {'split':<10}{'grade':>10}{'n':>7}{'m/s':>7}{'pace/mi':>9}{'HR':>6}"
    if has_rd:
        head += f"{'VO mm':>8}{'VR %':>7}{'GCT ms':>8}{'SL mm':>8}"
    L.append(head)
    for g in rep["grade_table"]:
        pace = pace_per_mile(g["mps"], placeholder="--:--")
        line = (
            "    "
            f"{g['split_type']:<10}{g['grade_bin']:>5.0f}..{g['grade_bin'] + 5:<4.0f}"
            f"{g['n']:>7}{g['mps']:>7.2f}{pace:>9}"
            f"{g['hr']:>6.0f}"
        )
        if has_rd:
            for key, w, dp in (
                ("vertical_oscillation_mm", 8, 1),
                ("vertical_ratio_pct", 7, 2),
                ("stance_time_ms", 8, 0),
                ("step_length_mm", 8, 0),
            ):
                v = g.get(key)
                line += f"{'--':>{w}}" if v is None else f"{v:>{w}.{dp}f}"
        L.append(line)

    a = rep["athlete"]
    L.append("")
    gse = a.get("garmin_self_evaluation") or {}
    if gse.get("status") == "ok":
        rpe = gse["garmin_rpe"]
        feel = gse["garmin_feel"]
        L.append(
            f"  GARMIN SELF-EVALUATION  rpe "
            f"{'--' if rpe is None else f'{rpe:.0f}/10'}   feel "
            f"{'--' if feel is None else f'{feel}/5'}   (asked on the "
            f"watch, seconds after the run)"
        )
    else:
        L.append(f"  GARMIN SELF-EVALUATION  none   ({gse.get('reason')})")
    sl = rep.get("session_load") or {}
    if sl.get("status") == "ok":
        L.append(
            f"  SESSION LOAD  {sl['foster_load_au']:.0f} AU   (garmin rpe "
            f"{sl['garmin_rpe']:.0f} x {sl['duration_min']:.1f} timer "
            f"min; feel not used)"
        )
    else:
        L.append(f"  SESSION LOAD  unavailable   ({sl.get('reason')})")
    for n in a["notes"]:
        L.append(f"    note: {n}")

    if rep["flags"]:
        L.append("")
        L.append("  VALIDATION FLAGS")
        for f in rep["flags"]:
            L.append(f"    [{f['severity']}] {f['check']}: {f['message'][:80]}")

    q = rep["quality_flags"]
    L.append("")
    if q["n_flagged"]:
        L.append(
            f"  §15.4 QUALITY FLAGS  {q['n_flagged']} of {q['n_records']} "
            f"records excluded ({100 * q['n_flagged'] / max(q['n_records'], 1):.1f}%)"
        )
        for f, n in q["by_flag"].items():
            L.append(f"    {f:<22}{n:>5}")
    else:
        L.append(f"  §15.4 QUALITY FLAGS  none on {q['n_records']} records")

    L.append("")
    L.append("  NOT ENFORCED AT THIS GRAIN (§15.4)")
    for u in rep["unenforced"]:
        L.append(f"    {u}")
    L.append("")
    L.append("  This report PRESENTS. It does not eliminate causes -- §16.3 needs")
    L.append("  the weather baseline and the athlete's account weighed together,")
    L.append("  which the chat has and this function does not.")
    return "\n".join(L)
