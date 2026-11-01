"""Run selection for parameterised primitives (SPEC.md §12.6).

THE ARCHITECTURE: THE COACH INTERPRETS, THE SYSTEM COMPUTES. A coaching chat
never assembles numbers itself. It names a quantity and a selection; this layer
resolves the selection, the primitive computes, §15.4's gates apply, and the
coach reads the result through a coaching lens.

THE COMPUTATION IS FIXED AND GATED; THE SELECTION IS FREE. That is what lets
"HR drift on long runs over the last five sessions" be answered without a
generic query tool. It is matched_terrain_drift with a filter, not a new
computation and not arbitrary SQL.

WHAT A CHAT CANNOT ASK FOR -- documented here so the boundary is read rather
than discovered:

  * NO arbitrary SQL. There is no run_sql tool and there will not be one.
  * NO arbitrary aggregation. Group-by, custom expressions and derived columns
    are not exposed. A quantity either exists as a §6 primitive or it does not
    exist yet.
  * NO BRONZE ACCESS. `records`, `laps`, `splits` and the raw FIT files are not
    reachable. Bronze is the file as recorded (§3); reading it directly bypasses
    every gate that makes a number trustworthy.
  * NO NEW COMPUTATIONS. A chat cannot define a metric. If a question needs one,
    that is a spec change and a build, not a tool call.
  * NO WRITES to anything the system computed.

EXCLUSIONS ARE ALWAYS STATED. "Drift over five long runs" means something
different if two were dropped for coverage, so every result carries what was
included, what was excluded, and why.

RUN_TYPE IS IN THE VOCABULARY AND HAS NO SOURCE IN THIS BUILD. The parameter
stays on every tool so a later version can re-enable it through user-defined
run types without changing any signature; until then a NON-EMPTY run_type is
REJECTED BY NAME (FilterUnavailable) rather than matching nothing -- a filter
that silently selects zero runs reads as "you have no such runs". Every row
reports `run_type: null` with `run_type_status: "not collected"`.
"""

from __future__ import annotations

import datetime

from trail_factors.ingest.answers import NO_RUN_TYPE_SOURCE, NOT_COLLECTED

CALC_VERSION = 1

# The filter vocabulary, designed once and applied uniformly by every
# parameterised primitive. Anything not on this list is not askable.
FILTER_KEYS = (
    "run_type",
    "start_date",
    "end_date",
    "cluster",
    "last_n",
    "segment_id",
    "direction",
)

DIRECTIONS = ("ascent", "descent")

# WHICH SPORT A SELECTION DRAWS FROM. Every primitive compares runs with runs
# (§4.1), so the default is running and is unchanged for every caller. A DOSE
# is different: a weekly load sum that silently leaves out a rated ride
# understates the week with one sign, so a dose may ask for SPORT_ANY -- every
# sport the watch recorded. It is an argument to select_runs, not a filter
# key: a chat cannot point a running primitive at a ride through it.
DEFAULT_SPORT = "running"
SPORT_ANY = "any"

# A primitive that reads terrain needs a positioned trace. Below this the run is
# excluded WITH A REASON rather than contributing a number computed from a
# fraction of the ground (§14c is per-portion; this is the run-level gate for
# whether the run belongs in a cross-run comparison at all).
MIN_COVERAGE_FOR_TERRAIN = 0.5

# NOTHING IS EXCLUDED FROM A TREND ON THE ATHLETE'S SAY-SO IN THIS BUILD.
# A per-run "this session was not representative" statement has no source, so
# the only exclusions are measured ones (coverage). Stated on every selection
# rather than left for the reader to assume a check ran.
TREND_EXCLUSION_NOTE = (
    "no run is excluded from a trend on the athlete's "
    "say-so: per-run exclusion is not collected in this "
    "build, so the only exclusions are measured ones"
)


class FilterUnavailable(ValueError):
    """A filter that is in the vocabulary but has no source in this build.

    A ValueError so every existing `except ValueError` still catches it; its
    own class so a caller can tell "you asked for something malformed" from
    "you asked for something this build cannot answer yet" -- the second is
    fixed by omitting the filter, not by correcting it.
    """


def check_available(filt: dict) -> None:
    """Raise FilterUnavailable for a non-empty filter with no source.

    Empty string and None are "not asked", the same as omitting the key, so a
    caller that forwards every parameter with its default is not rejected.
    """
    rt = filt.get("run_type")
    if rt is not None and rt != "":
        raise FilterUnavailable(
            f"run_type={rt!r}: {NO_RUN_TYPE_SOURCE}. Omit run_type; a later "
            f"version re-enables it through user-defined run types."
        )


def validate_filter(filt: dict) -> list[str]:
    """Problems with a requested selection. Empty means it is answerable.

    A filter that is well-formed but UNAVAILABLE in this build (run_type) is
    not a problem in the list: it raises FilterUnavailable, because the fix is
    different -- omit it, rather than correct it.
    """
    check_available(filt)
    problems = []
    for key in filt:
        if key not in FILTER_KEYS:
            problems.append(
                f"unknown filter {key!r}. Allowed: {', '.join(FILTER_KEYS)}. "
                f"There is no arbitrary query interface by design."
            )
    d = filt.get("direction")
    if d is not None and d not in DIRECTIONS:
        problems.append(f"direction={d!r} must be one of {DIRECTIONS}")
    n = filt.get("last_n")
    if n is not None and (not isinstance(n, int) or isinstance(n, bool) or n < 1):
        problems.append(f"last_n={n!r} must be a positive integer")
    for key in ("start_date", "end_date"):
        v = filt.get(key)
        if v is None:
            continue
        # LENGTH IS NOT A FORMAT CHECK. '09/01/2026' is ten characters, so a
        # length test passed it, the comparison then matched nothing to narrow,
        # and the caller got the WHOLE CORPUS back labelled as September. A
        # malformed date must be rejected, never quietly ignored.
        if not isinstance(v, str):
            problems.append(f"{key}={v!r} must be YYYY-MM-DD")
            continue
        try:
            datetime.date.fromisoformat(v)
        except ValueError:
            problems.append(
                f"{key}={v!r} must be YYYY-MM-DD (ISO), not a "
                f"locale format -- it was not parsed, and a date "
                f"that silently fails to narrow returns the whole "
                f"corpus"
            )
    return problems


def select_runs(
    con, filt: dict, needs_terrain: bool = True, sport: str = DEFAULT_SPORT
) -> dict:
    """Resolve a filter to activities, and say what was left out and why.

    `sport` is DEFAULT_SPORT for every primitive. SPORT_ANY selects every
    sport (for a dose, where leaving one out is an error with one sign); any
    other value names one sport, and a sport the watch never recorded is
    REFUSED rather than selecting nothing -- an empty selection would read as
    "you did none of that". Off the default, each row carries its `sport`
    and the rule names the choice.

    `last_n` is applied LAST, after every other filter and after exclusions, so
    "the last five long runs" means five usable long runs rather than five
    candidates of which two were dropped.

    A non-empty run_type raises FilterUnavailable here as well as in
    validate_filter, so a caller that skips validation still cannot get an
    empty selection back for a filter this build cannot apply.
    """
    from trail_factors.analysis.report import cluster_of

    check_available(filt)
    if not isinstance(sport, str) or not sport:
        raise ValueError(f"sport={sport!r} must be a sport name or {SPORT_ANY!r}")
    if sport not in (DEFAULT_SPORT, SPORT_ANY):
        known = [
            s
            for (s,) in con.execute(
                "SELECT DISTINCT sport FROM activities "
                "WHERE source = 'garmin_watch' AND sport IS NOT NULL "
                "ORDER BY 1"
            ).fetchall()
        ]
        # The default is never checked: a corpus with no runs yet is an
        # empty selection, as it always was.
        if sport not in known:
            raise ValueError(
                f"sport={sport!r}: no garmin_watch activity has that sport. "
                f"Recorded: {', '.join(known) or 'none'}; or {SPORT_ANY!r} "
                f"for every sport"
            )
    # ONE FILE PER SESSION: garmin_watch only, so a dually recorded session
    # is never counted twice, whatever the sport.
    rows = con.execute(
        """
        SELECT a.activity_id, a.run_id, a.start_time_utc, a.coverage_fraction,
               a.sampling_regime, a.sport
        FROM activities a WHERE a.source = 'garmin_watch'
          AND (? OR a.sport = ?)
        ORDER BY a.start_time_utc DESC
    """,
        [sport == SPORT_ANY, sport],
    ).fetchall()

    included, excluded = [], []
    for aid, rid, start, cov, regime, row_sport in rows:
        day = str(start)[:10]
        if filt.get("start_date") and day < filt["start_date"]:
            continue  # outside the window: not "excluded"
        if filt.get("end_date") and day > filt["end_date"]:
            continue
        cl = cluster_of(con, aid)
        if filt.get("cluster") and cl != filt["cluster"]:
            continue
        # Past this point the run MATCHED the request, so anything dropped is a
        # real exclusion and must be reported.
        if needs_terrain and (cov is None or cov < MIN_COVERAGE_FOR_TERRAIN):
            # An UNMEASURED coverage is named as such, never formatted as a
            # number it is not (and never allowed to crash the selection).
            excluded.append(
                {
                    "activity_id": aid,
                    "run_id": rid,
                    "date": day,
                    "kind": "coverage",
                    "reason": (
                        f"GPS coverage {cov:.0%} is below "
                        f"{MIN_COVERAGE_FOR_TERRAIN:.0%}"
                        if cov is not None
                        else "GPS coverage was not measured for this activity"
                    )
                    + "; terrain-derived quantities would come from "
                    "part of the ground",
                }
            )
            continue
        row = {
            "activity_id": aid,
            "run_id": rid,
            "date": day,
            "run_type": None,
            "run_type_status": NOT_COLLECTED,
            "cluster": cl,
            "sampling_regime": regime,
            "coverage_fraction": cov,
        }
        if sport != DEFAULT_SPORT:
            row["sport"] = row_sport
        included.append(row)

    truncated = 0
    if filt.get("last_n") and len(included) > filt["last_n"]:
        truncated = len(included) - filt["last_n"]
        included = included[: filt["last_n"]]

    parts = []
    if sport == SPORT_ANY:
        parts.append("every sport (sport=any)")
    elif sport != DEFAULT_SPORT:
        parts.append(f"sport={sport}")
    for key in ("cluster", "start_date", "end_date", "last_n"):
        if filt.get(key) is not None:
            parts.append(f"{key}={filt[key]}")
    return {
        "rule": ", ".join(parts) if parts else "all garmin_watch runs",
        "included": included,
        "excluded": excluded,
        "n_included": len(included),
        "n_excluded": len(excluded),
        "n_beyond_last_n": truncated,
        "trend_exclusion": TREND_EXCLUSION_NOTE,
        "date_range": (
            (included[-1]["date"], included[0]["date"]) if included else None
        ),
    }
