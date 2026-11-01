"""Corpus queries shared by the CLI and the MCP server (SPEC.md §12.6).

EVERY function here is called by BOTH paths. That is the point: two
implementations of "which runs are these" would drift, and §15.4's exclusion
rules must hold identically whichever path is used. Nothing in the MCP layer
may query the database directly.
"""

from __future__ import annotations

import re

CALC_VERSION = 1


def corpus_fingerprint(con) -> dict:
    """What the corpus looked like when this result was produced.

    Reads are LIVE, so a long chat can have the data move under it: `sync` in a
    terminal adds a run, and because the comparison set is the last 8 runs on a
    cluster, one new run is a 12.5% turnover of the population every percentile
    is measured against. The same run's drift can read p90 at minute 5 and p75
    at minute 40 with no code change and nothing visible to the reader.

    Attaching this to EVERY tool result makes that movement detectable instead
    of silent: two results with different fingerprints were computed against
    different corpora, and the chat can say so rather than inventing a reason
    the number changed.
    """
    n, latest = con.execute(
        "SELECT count(*), max(start_time_utc) FROM activities").fetchone()
    notes = con.execute("SELECT count(*) FROM annotations").fetchone()[0]
    return {"activities": n,
            "latest_start_utc": str(latest) if latest is not None else None,
            "notes": notes}


# THE SPORT FILTER (A3). "Runs" listings defaulted to EVERY sport, so rides
# and a hike sat in list_runs beside the runs.
# The default is running; ANY_SPORT widens to everything, and any other value
# names one sport. A misspelt sport returns NOTHING, never the whole corpus --
# the safe direction for a filter that fails to match.
DEFAULT_SPORT = "running"
ANY_SPORT = "any"


def sport_clause(sport: str | None, column: str) -> tuple[str, list]:
    """SQL fragment + params for the sport filter. Empty/None is the default."""
    s = sport if sport else DEFAULT_SPORT
    if s == ANY_SPORT:
        return "TRUE", []
    return f"{column} = ?", [s]


def local_date_of(start_utc, tz_hours):
    """The LOCAL calendar date of a naive-UTC start. THE one rule.

    Group by local date, never UTC date (CLAUDE.md): an evening run at a
    negative UTC offset (say UTC-3) is the next day in UTC. Every listing
    calls this, so there is one definition of "what day was this run".
    """
    import datetime
    tz = tz_hours if tz_hours is not None else 0.0
    return (start_utc + datetime.timedelta(hours=tz)).date()


def list_runs(con, start_date: str | None = None, end_date: str | None = None,
              run_type: str | None = None, cluster: str | None = None,
              limit: int = 50, sport: str | None = None) -> list[dict]:
    """Corpus listing. Filters are AND-ed; any of them may be omitted.

    `cluster` is resolved from the TRACE, not the start point (§14g), so it is
    computed per activity rather than filtered in SQL. `sport` defaults to
    running; "any" lists every sport.

    RUN_TYPE IS NOT COLLECTED IN THIS BUILD. Every row reports `run_type: null`
    beside `run_type_status: "not collected"`, so a null cannot be read as "an
    untyped run". A NON-EMPTY run_type filter is REJECTED BY NAME rather than
    matched against nothing: an empty listing would read as "no runs of that
    type", which is a claim about the athlete this build cannot make. A later
    version re-enables it through user-defined run types.
    """
    from trail_factors.analysis.report import cluster_of
    from trail_factors.analysis.select import check_available
    from trail_factors.ingest.answers import NOT_COLLECTED

    # Raises select.FilterUnavailable for a non-empty run_type: ONE wording of
    # the rejection, shared with every selection tool.
    check_available({"run_type": run_type})

    where_sport, sport_params = sport_clause(sport, "a.sport")
    rows = con.execute(f"""
        SELECT a.activity_id, a.run_id, a.start_time_utc, a.local_tz, a.sport,
               a.total_distance_m, a.duration_s, a.moving_time_s,
               a.sampling_regime, a.coverage_fraction
        FROM activities a
        WHERE a.source = 'garmin_watch' AND {where_sport}
          AND (? IS NULL OR a.start_time_utc >= CAST(? AS TIMESTAMP))
          AND (? IS NULL OR a.start_time_utc < CAST(? AS TIMESTAMP) + INTERVAL 1 DAY)
        ORDER BY a.start_time_utc DESC
    """, [*sport_params, start_date, start_date, end_date, end_date]).fetchall()

    out = []
    for (aid, rid, start, tz, sport_, dist, dur, moving, regime, cov) in rows:
        if len(out) >= limit:
            break
        cl = cluster_of(con, aid)
        if cluster and cl != cluster:
            continue
        out.append({
            "activity_id": aid, "run_id": rid, "start_utc": str(start),
            "local_date": str(local_date_of(start, tz)),
            "local_tz": tz, "sport": sport_, "cluster": cl,
            "run_type": None, "run_type_status": NOT_COLLECTED,
            "distance_m": dist, "elapsed_s": dur, "moving_s": moving,
            "sampling_regime": regime, "coverage_fraction": cov,
        })
    return out


class AmbiguousRun(LookupError):
    """A reference that matches more than one activity.

    A LookupError subclass so existing handlers still catch it, but its own
    type so the MCP boundary can report `rejected` rather than `absent`: the
    activities EXIST, and saying otherwise is the §16.2 status collapse.
    """


def resolve_run_id(con, run_ref: str) -> tuple[str, str]:
    """(run_id, run_start_iso) from a run_id, activity_id or Garmin file id.

    Shared so the CLI and any other caller agree on WHICH RUN a reference
    means. Two implementations of that is the drift the thin-wrapper rule
    exists to prevent.
    """
    row = con.execute("SELECT run_id, start_time_utc FROM runs WHERE run_id = ?",
                      [run_ref]).fetchone()
    if row is None:
        row = con.execute("""
            SELECT r.run_id, r.start_time_utc FROM runs r
            JOIN activities a USING (run_id)
            WHERE a.activity_id = ? OR a.source_file LIKE ?
            LIMIT 1""", [run_ref, f"%{run_ref}%"]).fetchone()
    if row is None and re.fullmatch(r"\d{4}-\d{2}-\d{2}", run_ref or ""):
        # A DATE IS A REFERENCE A HUMAN ACTUALLY USES, and it resolved to
        # "no run matches" for every activity in the corpus -- `absent`
        # reported for something that plainly exists, which is the status
        # collapse §16.2 forbids.
        #
        # THE DATE IS LOCAL (CLAUDE.md): a 22:00 run is one UTC date and a
        # different local one, and "the run on the 17th" means the day the
        # athlete lived.
        hits = con.execute("""
            SELECT DISTINCT r.run_id, r.start_time_utc
            FROM runs r JOIN activities a USING (run_id)
            WHERE CAST(a.start_time_utc
                       + INTERVAL 1 HOUR * coalesce(a.local_tz, 0) AS DATE)
                  = CAST(? AS DATE)
            ORDER BY r.start_time_utc""", [run_ref]).fetchall()
        if len(hits) == 1:
            row = hits[0]
        elif len(hits) > 1:
            # NEVER GUESS between two activities on one day. Naming them is
            # what lets the caller pick; picking for them would make the
            # answer depend on an ordering nobody stated.
            raise AmbiguousRun(
                f"{run_ref} has {len(hits)} activities: "
                + ", ".join(r[0] for r in hits)
                + ". Name one of those run_ids.")
    if row is None:
        raise LookupError(f"no run matches {run_ref!r}")
    return row[0], row[1].isoformat()


def segment_library(con) -> list[dict]:
    """The slope library, WITHOUT coordinates.

    `segments` carries four positional columns (high_lat, high_lon, low_lat,
    low_lon) and this projection is the only thing standing between the tool
    payload and every segment's coordinate pairs. An explicit column list, in
    the analysis layer where the boundary is testable, rather than an ad-hoc
    SELECT in the server.
    """
    cols = ("segment_id", "vertical_m", "length_m", "mean_grade_pct",
            "n_efforts", "n_ascents", "n_descents", "low_elevation_m",
            "high_elevation_m")
    return [dict(zip(cols, row)) for row in con.execute("""
        SELECT segment_id, vertical_m, length_m, mean_grade_pct,
               n_efforts, n_ascents, n_descents, low_elevation_m,
               high_elevation_m
        FROM segments ORDER BY n_efforts DESC, segment_id
    """).fetchall()]


def activity_timing(con, activity_id: str) -> tuple:
    """(run_id, timer_time_minutes) for one activity.

    LIVES HERE BECAUSE THE SERVER HOLDS NO SQL. `save_ride_blocks` needs the
    activity's own duration to bound the blocks against it, and reaching for
    a SELECT inside the tool is the thin-wrapper violation
    tests/test_mcp.py::test_the_server_contains_no_sql exists to catch -- it
    caught exactly this once already.

    TIMER TIME, not elapsed: elapsed includes auto-paused time, and a block
    bound built on it would accept minutes the ride did not record.
    """
    row = con.execute(
        "SELECT run_id, timer_time_s FROM activities WHERE activity_id = ?",
        [activity_id]).fetchone()
    if row is None:
        raise LookupError(f"no activity {activity_id}")
    return row[0], (row[1] if row[1] is not None else 0.0) / 60.0


def sport_of(con, activity_id: str | None) -> str | None:
    """The sport of one activity, or None. Never raises.

    Lives here rather than in the MCP server because the server holds NO SQL
    (§12.6, and a test enforces it) -- and because a refusal that fails while
    explaining itself is worse than the refusal.
    """
    if not activity_id:
        return None
    try:
        row = con.execute("SELECT sport FROM activities WHERE activity_id = ?",
                          [activity_id]).fetchone()
        return row[0] if row else None
    except Exception:
        return None


def activity_for_run(con, run: str = "") -> str:
    """One garmin activity id for a run reference, or the latest run.

    ONE ACTIVITY PER run_id (CLAUDE.md): every run is recorded twice and the
    watch copy is the one carrying heart rate and cadence.
    """
    if run:
        run_id, _ = resolve_run_id(con, run)
        row = con.execute(
            "SELECT activity_id FROM activities WHERE run_id = ? "
            "AND source = 'garmin_watch' LIMIT 1", [run_id]).fetchone()
        if row is None:
            raise LookupError(f"no garmin activity for run {run_id}")
        return row[0]
    # AN EMPTY REFERENCE STILL MEANS THE LATEST *RUN*, and that is a HOLD
    # rather than a rule. §4.1's broad download put every sport in the
    # corpus, and without this the meaning of every no-argument tool
    # call would have changed silently: `report` would have pointed at a hike
    # because it happened to be the most recent thing logged. Which sports a
    # default reference should span is part of the deferred decision.
    row = con.execute(
        "SELECT activity_id FROM activities WHERE source = 'garmin_watch' "
        "AND sport = 'running' ORDER BY start_time_utc DESC LIMIT 1").fetchone()
    if row is None:
        row = con.execute(
            "SELECT activity_id FROM activities WHERE source = 'garmin_watch' "
            "ORDER BY start_time_utc DESC LIMIT 1").fetchone()
    if row is None:
        raise LookupError("no activities in the corpus")
    return row[0]


# --- ATHLETE-STATE PROJECTIONS, AND HOW THEY ANNOUNCE BEING STALE -----------
#
# `annotations` and `window_confirmations` are tables rebuilt from files by
# `ensure_schema`, which runs only on WRITE connections. Their write paths are
# CLI-only and run ensure_schema themselves, but every MCP read is read-only:
# a file written by some other route would leave the table behind, and the way
# that reads from outside is an empty result that looks like an answer.
#
# AN EMPTY RESULT AND AN UNREFRESHED PROJECTION ARE DIFFERENT FACTS. So every
# read of these goes through `projection_status` first, and a mismatch is
# reported as `stale_projection` naming the command that fixes it -- never as
# "no notes".
#
# The count is a cheap detector, not a checksum: it catches the case that
# actually occurs (a file written while the table was not rebuilt) and misses
# an edit that preserves the row count. It is not presented as more than that.
PROJECTION_REBUILD_CMD = "uv run trail-factors verify"
# table -> (the athlete_state module constant holding its file, its loader).
# The file is looked up at CALL time, so TRAIL_FACTORS_ATHLETE_DIR (and a test
# that redirects the module's path) is honoured, and it is reported through
# display_path so no absolute path reaches a payload.
_PROJECTION_SOURCES = {
    "annotations": ("NOTES", "load_notes"),
    "window_confirmations": ("CONFIRMATIONS", "load_confirmations"),
}


def projection_status(con, table: str) -> dict:
    """Does this projection still agree with the file it was built from?"""
    from trail_factors.ingest import athlete_state
    from trail_factors.paths import display_path

    attr, loader = _PROJECTION_SOURCES[table]
    path = display_path(getattr(athlete_state, attr))
    expected = len(getattr(athlete_state, loader)())
    actual = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    if actual == expected:
        return {"status": "ok", "table": table, "rows": actual,
                "source_file": path}
    return {
        "status": "stale_projection",
        "table": table, "rows": actual, "source_file": path,
        "source_rows": expected,
        "reason": f"`{table}` holds {actual} row(s) and {path} holds "
                  f"{expected}. The file is the record; the table is a "
                  f"projection rebuilt only by a WRITE connection, so a "
                  f"read-only reader sees whatever the last CLI command left.",
        "rebuild": PROJECTION_REBUILD_CMD,
        "do_not": "do not read this as an absence -- an unrefreshed projection "
                  "and an empty file are different facts",
    }
