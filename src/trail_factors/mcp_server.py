"""MCP server exposing the analysis layer (SPEC.md §12.6).

THIN WRAPPER. Every tool calls the SAME function the CLI calls. There is no
analysis in this file — no SQL, no thresholds, no interpretation. Two
implementations of "what is a report" would drift, and §15.4's exclusion rules
must hold identically whichever path is used. If a tool needs something the
shared layer does not expose, the fix is a function in src/trail_factors/analysis/, not code
here.

NOTHING HERE MAY MODIFY data/raw/. Not alter, not move, not rename, not
delete. §3 makes the raw layer immutable, but that was written when the CLI was
the only way in. The guarantee is STRUCTURAL rather than a rule: no tool in
this module opens a file for writing.

The one exception is APPEND-ONLY and worth stating precisely. `sync` downloads
NEW activity files into data/raw/garmin/, which is a write to that directory.
It is append-only: `garmin_download_all` skips any activity already present, so
no code path reachable from this server rewrites, truncates or removes an
existing raw file. Everything else reachable from a tool writes only to the
athlete-state directory (TRAIL_FACTORS_ATHLETE_DIR, default data/athlete/) --
`save_ride_blocks` and the job-state file.

THE BOUNDARY IS NOT "READ-ONLY" -- IT IS ATHLETE INPUT VS COMPUTED VALUES.

The chat may SUBMIT athlete input through a validated contract: it structures
what the athlete recalls (a stationary ride's resistance blocks) and hands it
over, and the SYSTEM validates and writes it. The chat parses, the code
validates and stores.

The chat may NEVER modify anything the system computed. No bronze, no silver,
no gold, no derived value, ever. `save_ride_blocks` is the ONLY tool that
writes athlete input, it writes only to the athlete-state directory, and it
cannot write an unvalidated or clobbering payload. Every connection is
read_only=True.

NOTHING HERE ASKS THE ATHLETE FOR ANSWERS. Per-run context such as run_type
has no source in this build (src/trail_factors/ingest/answers.py): every reader reports it
null and labelled "not collected", and a run_type FILTER is rejected by name
rather than silently matching nothing. A later version re-enables it through
user-defined run types.

READS ARE LIVE, per call, on a short-lived read-only connection — exactly what
`report_cmd` does. Measured: DuckDB takes an exclusive file lock, so a reader
CANNOT open the database while `sync` holds it for writing. That failure is
real and expected; it is reported as a legible `broken` envelope telling the
athlete to ask again in a moment, and is NEVER retried in a loop (CLAUDE.md's
Garmin-auth precedent).

Every result carries a CORPUS FINGERPRINT so a long conversation can see the
data move under it rather than silently reasoning on stale numbers.

TOOLS NEVER RAISE. An exception crossing the MCP boundary becomes "the tool is
broken" to a chat, which is a worse answer than any of the real outcomes. The
envelope's `outcome` vocabulary is deliberately DISJOINT from the report's
four per-primitive statuses: a tool-level failure and a measurement-level
absence are different things and must not be confused.
"""

from __future__ import annotations

import contextvars
import json
from typing import Any

import duckdb
# mcp 2.x: FastMCP was renamed MCPServer. Pinned in pyproject to mcp>=2.
from mcp.server.mcpserver import MCPServer

from trail_factors.analysis.versions import calc_version, calc_version_ref
from trail_factors.config import ConfigError
from trail_factors.ingest.ride_blocks import RideBlockError
from trail_factors.analysis.queries import AmbiguousRun
from trail_factors.analysis.select import FilterUnavailable, check_available
from trail_factors.ingest.answers import NOT_COLLECTED
from trail_factors.paths import DB_FILE, REPO_ROOT, display_path

# STDIO ONLY. `main()` calls `mcp.run()` with no transport argument, which is
# stdio: the server is a child process of the client that launched it and
# binds no host and no port. Do not add a network transport -- every tool here
# answers about one person's training, and a socket would offer that to
# anything that can reach it.
mcp = MCPServer("trail-factors")


# A READ THAT BLOCKS FOR MINUTES LOOKS EXACTLY LIKE A SERVER THAT NEVER
# STARTED, which is how a lock was once mis-diagnosed as a broken server. DuckDB
# permits one writer, so a `sync` job or a stray CLI command can hold the file;
# the connection attempt gets a few seconds and then NAMES the lock.
CONNECT_TIMEOUT_S = 5.0


class DatabaseBusy(RuntimeError):
    """Another process holds the database. Not a corpus problem."""


def _connect():
    """A read-only connection, or a NAMED failure within seconds."""
    import concurrent.futures as _fut

    def open_it():
        return duckdb.connect(str(DB_FILE), read_only=True)

    pool = _fut.ThreadPoolExecutor(max_workers=1)
    try:
        return pool.submit(open_it).result(timeout=CONNECT_TIMEOUT_S)
    except _fut.TimeoutError:
        raise DatabaseBusy(
            f"the database did not open within {CONNECT_TIMEOUT_S:.0f}s: "
            f"another process holds it. Check for a running CLI command "
            f"(`sync` holds the write lock for minutes) or a second "
            f"mcp_server, then try again.")
    except (duckdb.IOException, duckdb.ConnectionException) as exc:
        # TWO DIFFERENT CAUSES, BOTH "SOMETHING ELSE HOLDS IT", and both
        # verified: a lock held by ANOTHER PROCESS raises IOException
        # ("Could not set lock on file") in about 10 ms; a handle already open
        # in THIS process with a different configuration raises
        # ConnectionException. Naming both is the difference between a chat
        # retrying sensibly and a chat reporting the corpus as broken.
        raise DatabaseBusy(
            f"the database could not be opened read-only: {exc} -- another "
            f"process holds it, or this process already holds a handle with a "
            f"different configuration. Check for a running CLI command "
            f"(`sync` holds the write lock for minutes) or a second "
            f"mcp_server, then try again.")
    finally:
        # Never wait on the worker: if it is blocked on the lock, shutting the
        # pool down synchronously would reintroduce the hang this prevents.
        pool.shutdown(wait=False)


class ReadBackFailed(RuntimeError):
    """A save landed in the file but the read path cannot see it.

    Its own failure class, because it is neither a rejection (the payload was
    fine and the write happened) nor an ordinary breakage (nothing is lost).
    Reporting it as either is what let four invisible saves read as successes.
    """

class RunAndSelection(ValueError):
    """A single run AND selection filters were both named.

    Its own class because the generic ValueError branch answers "use only the
    documented filters" with retryable false -- and the caller DID use only
    documented filters. Dropping either half is a call that succeeds.
    """


RESULT, REJECTED, ABSENT, BROKEN = "result", "rejected", "absent", "broken"
# REFUSED: the reference resolved, the thing EXISTS, and this tool cannot serve
# it. §16.2 already uses the word for exactly this -- "the analysis class is
# invalid here, NOT a finding" -- and the envelope lacked it, so a hike that a
# running primitive could not handle came back `absent`: the status collapse
# §16.2 exists to prevent, reported about an activity sitting in the corpus.
REFUSED = "refused"


# WHAT `retryable` MEANS. It had no definition anywhere -- not here, not in
# SPEC.md, which only ever showed it in use for a database lock -- and that is
# precisely why it drifted: an omission cannot be spotted against a rule that
# does not exist. A rejection once shipped `retryable: false` while its own
# remedy said to call again with a flag, and the caller stopped.
#
# THE DEFINITION IS THE CALLER'S QUESTION, NOT THE SERVER'S: "is there a next
# call that can succeed?" True when the caller can act -- wait for a lock, add
# a flag, fix a field -- and call again, AND nothing was written. False when a
# further call cannot help (a missing corpus, a sport this tool cannot serve)
# or when the write ALREADY LANDED and resending would duplicate it, which is
# why ReadBackFailed is false and says DO NOT re-send.
#
# It is deliberately NOT "an identical retry may succeed". That reading would
# make every corrigible rejection false and leave the field carrying no
# information a caller could act on.
RETRYABLE_MEANS = ("a further call can succeed after the stated action "
                   "(waiting, adding a flag, fixing a field) and nothing was "
                   "written; false means no next call helps, or the write "
                   "already landed")


def _scrub_paths(s: str) -> str:
    """Replace every absolute location this process knows with a safe form.

    THE BACKSTOP, NOT THE FIX. Each message that names a file is built with
    `display_path` at its source; this catches what no source controls -- a
    DuckDB exception quoting the file it could not lock, a lock-probe basis, a
    traceback fragment. An absolute path carries the home directory, and the
    envelope is exactly the text that ends up pasted into a chat or an issue.

    Read at CALL time: tests patch DB_FILE on this module.
    """
    import os
    from pathlib import Path

    db = globals()["DB_FILE"]
    pairs = []
    for p in {Path(db), Path(db).resolve()}:
        pairs.append((str(p), display_path(p)))
    for root in {REPO_ROOT, REPO_ROOT.resolve()}:
        pairs.append((str(root) + os.sep, ""))
        pairs.append((str(root), "."))
    for home in {Path.home(), Path.home().resolve()}:
        pairs.append((str(home), "~"))
    # Longest first, so a file path is replaced before the directory that
    # contains it turns it into a half-relative string.
    for old, new in sorted(pairs, key=lambda t: len(t[0]), reverse=True):
        if old and old not in (os.sep, "."):
            s = s.replace(old, new)
    return s


def _envelope(tool: str, outcome: str, *, payload: Any = None, text: str = "",
              problems: list[str] | None = None, remedy: str = "",
              retryable: bool = False, fingerprint: dict | None = None) -> str:
    return _scrub_paths(json.dumps({
        "tool": tool, "outcome": outcome, "payload": payload, "text": text,
        "problems": problems if problems is not None else [],
        "remedy": remedy, "retryable": retryable,
        # NEVER A BARE NULL. Every other field in this envelope means one
        # thing; a null fingerprint meant three -- the corpus is missing, the
        # call failed before the fingerprint could be taken, or the fingerprint
        # itself failed -- which is the same status-collapse §16.2 forbids for
        # primitives, committed by the envelope that reports them. A failure
        # path still knows WHICH corpus it failed against, and that is exactly
        # what a chat needs to decide whether to trust a cached earlier answer.
        "corpus_fingerprint": fingerprint if fingerprint is not None else {
            "status": "not_taken",
            "reason": "this call failed before a fingerprint could be read; "
                      "the corpus was not identified, not found to be empty",
        },
        # The fingerprint says whether the DATA moved; this says whether the
        # CODE moved. Both are needed: a changed grade window shifts every
        # number with no new run, and a chat comparing weeks would otherwise
        # read a refactor as a fitness change.
        #
        # THE REFERENCE, NOT THE PARAMETERS. {pipeline_version, digest} is
        # enough to tell two codes apart; the 81 parameters behind a digest
        # are in spec_context's payload, which exists for exactly that.
        "calc_version": calc_version_ref(),
        # `repo_root` WAS HERE AND IS REMOVED. It existed to disambiguate
        # which corpus answered, a job `corpus_fingerprint` already does. What
        # it actually did was hand every chat the absolute path to the
        # repository in every single result -- and a chat that also has
        # filesystem access needs nothing else, because CLAUDE.md documents
        # the database as `data/ultra.duckdb`. Do not re-add it; the same goes
        # for any absolute path, which _scrub_paths strips as a backstop.
        #
        # This does not PREVENT a direct read; nothing in this file can (§14m).
        # It removes the invitation.
    }, indent=2, default=str))


def _fingerprint_or_reason(con) -> dict | None:
    """The corpus fingerprint on a FAILURE path, or a stated reason.

    Taken inside its own try: a fingerprint that raises must not replace the
    error being reported with a different one. Returning None lets _envelope
    state "not_taken" rather than emitting a bare null.
    """
    if con is None:
        return None
    try:
        from trail_factors.analysis.queries import corpus_fingerprint
        return corpus_fingerprint(con)
    except Exception:
        return None


# A JOB-STATE READ NEVER OPENS THE DATABASE. sync_status went through the
# same wrapper as every corpus tool, which opened DuckDB for the fingerprint --
# and a sync holds the write lock for its whole run, so the one tool that
# exists to watch a sync returned `broken` for exactly as long as there was a
# sync to watch. The fingerprint is not taken, and says so in its own words.
NO_DB_FINGERPRINT = {"status": "not_taken",
                     "reason": "job-state read; no corpus access"}


def _call_without_db(tool: str, fn) -> str:
    """`_call` for a tool that must answer while a sync holds the lock.

    `fn` receives None in place of a connection. Nothing here imports the
    query layer or touches DB_FILE through DuckDB.
    """
    try:
        payload, text = fn(None)
        return _envelope(tool, RESULT, payload=payload, text=text,
                         fingerprint=NO_DB_FINGERPRINT)
    except LookupError as exc:
        # An unknown job id is ABSENT, as it was through _call.
        return _envelope(tool, ABSENT, text=str(exc),
                         remedy="call sync_status with no job_id for the latest",
                         retryable=True, fingerprint=NO_DB_FINGERPRINT)
    except Exception as exc:                      # never cross the boundary
        return _envelope(tool, BROKEN, text=f"{type(exc).__name__}: {exc}",
                         problems=[str(exc)[:400]], retryable=False,
                         fingerprint=NO_DB_FINGERPRINT)


def _call(tool: str, fn, *, database: bool = True) -> str:
    """Open the shared layer, run one function, and never let it raise.

    `fn` receives a READ-ONLY connection and returns (payload, text).
    `database=False` is the no-database mode: see _call_without_db.

    EVERY connection is read_only=True, INCLUDING save_ride_blocks'. The
    save path only READS the database (to resolve which run a reference means);
    the write goes to a JSON file in the athlete-state directory. So no tool
    reachable from a chat can write to the corpus AT ALL -- that is
    structural, not a rule someone has to remember.
    """
    if not database:
        return _call_without_db(tool, fn)
    from trail_factors.analysis.queries import corpus_fingerprint

    con = None
    try:
        if not DB_FILE.exists():
            return _envelope(
                tool, BROKEN,
                # REPO-RELATIVE, never absolute: the absolute form carries
                # the home directory into every chat that hits this.
                text=(f"no corpus at {display_path(DB_FILE)} (the path comes "
                      f"from TRAIL_FACTORS_DB, or the default data/ultra.duckdb)"),
                remedy="run `uv run trail-factors ingest-all` in the repo",
                retryable=False)
        con = _connect()
        payload, text = fn(con)
        return _envelope(tool, RESULT, payload=payload, text=text,
                         fingerprint=corpus_fingerprint(con))
    except DatabaseBusy as exc:
        return _envelope(
            tool, BROKEN, text=str(exc), problems=[str(exc)],
            remedy="wait for the CLI command to finish and call again; "
                   "nothing was computed and nothing was written. This is a "
                   "LOCK, not a corpus problem -- do not re-ingest.",
            retryable=True)
    except ReadBackFailed as exc:
        return _envelope(
            tool, BROKEN, text=str(exc), problems=[str(exc)],
            remedy="the entry is safe in its file in the athlete-state "
                   "directory -- do NOT re-send it. Reads are not coming from "
                   "the file: check for a reintroduced table or view in the "
                   "database that shadows it.",
            retryable=False)
    except RideBlockError as exc:
        return _envelope(
            tool, REJECTED, text=str(exc),
            problems=(str(exc).split("\n  - ")[1:]
                      if "\n  - " in str(exc) else [str(exc)]),
            remedy="fix the listed entries and resend; nothing was written. "
                   "Ambiguity is refused rather than resolved -- resolve it "
                   "with the athlete, who is present, and resend structured "
                   "blocks.",
            retryable=True, fingerprint=_fingerprint_or_reason(con))
    except json.JSONDecodeError as exc:
        return _envelope(tool, REJECTED, text=f"payload is not valid JSON: {exc}",
                         remedy="send the payload as a JSON object string")
    except FilterUnavailable as exc:
        # A DOCUMENTED FILTER WITH NO DATA BEHIND IT IN THIS BUILD -- not a
        # typo and not an unknown filter. It is a ValueError, so it must be
        # caught before the generic branch below, which would tell the caller
        # to stick to the documented filters when it already had. The filter
        # stays in every signature so a later version can re-enable it
        # without changing the contract; until then matching nothing would
        # hand back an empty selection that reads as a finding.
        return _envelope(
            tool, REJECTED, text=str(exc), problems=[str(exc)],
            remedy="omit run_type; a later version re-enables it through "
                   "user-defined run types. Nothing was computed or written.",
            retryable=True, fingerprint=_fingerprint_or_reason(con))
    except RunAndSelection as exc:
        # BOTH HALVES WERE DOCUMENTED; only the combination is not. A
        # ValueError, so it must come before the generic branch below.
        return _envelope(
            tool, REJECTED, text=str(exc), problems=[str(exc)],
            remedy="pass either run or the selection filters, not both; "
                   "nothing was computed",
            retryable=True, fingerprint=_fingerprint_or_reason(con))
    except ConfigError as exc:
        # A MISSING SETTING, NOT A BAD FILTER. ConfigError is a ValueError, so
        # without this branch it landed below and told the caller to fix
        # arguments that were fine. The exception's own text is not copied:
        # it can name the UTM zone the athlete's runs sit in, which helps at a
        # terminal and is not this envelope's business. The field is enough.
        field = str(exc).split(" ", 1)[0]
        return _envelope(
            tool, REFUSED,
            text=f"the athlete configuration has no {field}, which this "
                 f"tool needs; the arguments were fine",
            problems=[f"{field} is not set"],
            remedy=f"the athlete sets {field} in config/athlete.yaml "
                   f"(config/athlete.example.yaml documents it) and restarts "
                   f"the MCP server; nothing was computed or written",
            retryable=False, fingerprint=_fingerprint_or_reason(con))
    except ValueError as exc:
        return _envelope(
            tool, REJECTED, text=str(exc), problems=[str(exc)],
            remedy="use only the documented filters; there is no arbitrary "
                   "query interface by design")
    except AmbiguousRun as exc:
        # THE ACTIVITIES EXIST; the reference does not pick one. `absent` would
        # say the opposite of what is true.
        return _envelope(
            tool, REJECTED, text=str(exc), problems=[str(exc)],
            remedy="name one of the run_ids listed; a date with more than one "
                   "activity is not a reference this tool will guess at")
    except KeyError as exc:
        # KeyError IS A LookupError IN PYTHON, so every crash inside a
        # primitive was being caught below and reported as `absent` -- "no such
        # run" for a run that is right there. Measured on a hike: run_walk_cycles
        # and report both came back absent with a bare `'n_complete_cycles'` /
        # `'duration_min'`.
        sport = _sport_of(con, tool)
        sport_name = sport if sport is not None else "non-running"
        return _envelope(
            tool, REFUSED,
            text=(f"this tool could not serve a {sport_name} "
                  f"activity: missing {exc}. The activity EXISTS and is "
                  f"ingested; what it cannot do is answer a running question."),
            problems=[f"KeyError: {exc}"],
            remedy="§4.1 puts every sport in bronze and leaves strictness to "
                   "the derived layers; which tools serve which sport is not "
                   "decided yet. Use a run for running primitives.",
            retryable=False)
    except LookupError as exc:
        return _envelope(tool, ABSENT, text=str(exc),
                         remedy="check the run reference with list_runs")
    except duckdb.IOException as exc:
        # The measured, expected case: `sync` holds the write lock.
        return _envelope(
            tool, BROKEN,
            text="the database is locked, which almost always means a `sync` "
                 "is writing right now",
            problems=[str(exc)[:200]],
            remedy="wait for the sync to finish and ask again; do not retry "
                   "in a loop",
            retryable=True)
    except Exception as exc:                      # never cross the boundary
        return _envelope(tool, BROKEN, text=f"{type(exc).__name__}: {exc}",
                         problems=[str(exc)[:400]], retryable=False)
    finally:
        if con is not None:
            con.close()


# --------------------------------------------------------------------------
# READ TOOLS
# --------------------------------------------------------------------------

@mcp.tool()
def spec_context() -> str:
    """The standing brief: how to read results, and the measured findings that
    make a number interpretable. Load this ONCE at the start of a conversation.

    Stable across runs — refetch only if told the spec changed.
    """
    from trail_factors.analysis.context import spec_context as brief
    # THE ONE PLACE THE PARAMETERS TRAVEL. Every other envelope carries only
    # the digest; this payload says what the digest is a digest OF.
    return _envelope("spec_context", RESULT, text=brief(),
                     payload={"calc_version": calc_version()})


# WHICH ACTIVITY THIS CALL RESOLVED TO, so a refusal can name the sport it is
# refusing. A ContextVar rather than a module global because tool calls may
# overlap, and a refusal that named the wrong activity's sport would be worse
# than one that named none.
_RESOLVED: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "resolved_activity", default=None)


def _sport_of(con, _tool: str) -> str | None:
    """The sport of the activity THIS call resolved to, for a legible refusal.

    Never raises and never guesses: with nothing resolved it returns None and
    the refusal says "non-running" rather than inventing a sport.
    """
    from trail_factors.analysis.queries import sport_of
    return sport_of(con, _RESOLVED.get())


def _activity_for(con, run: str) -> str:
    """One garmin activity id for a run reference. Thin wrapper, no SQL."""
    from trail_factors.analysis.queries import activity_for_run
    aid = activity_for_run(con, run)
    _RESOLVED.set(aid)
    return aid


@mcp.tool()
def basic_metrics(run: str = "", run_type: str = "", start_date: str = "",
                  end_date: str = "", cluster: str = "", last_n: int = 0,
                  segment_id: str = "", direction: str = "") -> str:
    """§6.0 the run's own description: average HR, three paces, windowed bests,
    run/walk/stand time, and cadence.

    Args:
        run: run_id, activity_id or Garmin file id. Empty means the latest run.

    THESE ARE ANCHORS, NOT SIGNALS. For a run/walk athlete holding an HR cap,
    average HR barely moves from run to run because HR is held nearly flat
    across grade — quote it as the level that drift and efficiency are
    deviations FROM, not as a finding.

    Paces are never pooled: `overall` is Garmin's own basis (distance over
    timer time), `running` and `walking` come from the rwd_* splits and are
    separate quantities. A class below the minimum time is `gated` with its
    time stated, not silently absent.

    The bests are SLIDING WINDOWS, never an instantaneous maximum, and they are
    NOT comparable across sampling regimes. Read each window's
    `walk_time_fraction` and `flagged_record_fraction` before quoting it: a
    maximum selects for positive measurement error in a way a mean does not.

    Cadence is steps per minute. It is running-only and non-zero-only, so it
    does NOT equal Garmin's own averageRunningCadence, which includes standing.
    """
    def go(con):
        from trail_factors.analysis.basic import basic_metrics as calc
        _selection("basic_metrics", run_type=run_type, start_date=start_date,
                   end_date=end_date, cluster=cluster, last_n=last_n,
                   segment_id=segment_id, direction=direction)
        aid = _activity_for(con, run)
        r = calc(con, aid)
        hr = r["average_hr"]["value"]
        cad = r["mean_running_cadence"]["value"]
        return r, (f"avg HR {hr:.1f} bpm, cadence {cad:.1f} spm"
                   if hr is not None and cad is not None else "basic metrics")
    return _call("basic_metrics", go)


@mcp.tool()
def max_hr_over(run_type: str = "", start_date: str = "", end_date: str = "",
                cluster: str = "", last_n: int = 0, segment_id: str = "",
                direction: str = "") -> str:
    """The highest heart rate in a selection, and the run that produced it (§6.0).

    TWO CEILINGS, and the difference between them is the point.
    `max_hr_recorded` is the single highest sample -- what the watch displayed,
    and a SPIKE CANDIDATE, because a maximum selects for positive measurement
    error. `max_hr_30s` is the highest 30 s rolling mean with §15.4-flagged
    records excluded, and it is the defensible figure. When the two come from
    DIFFERENT runs, the corpus's highest spike and its highest sustained effort
    are not the same session, and `same_run` says so.

    EACH WINNER CARRIES ITS CIRCUMSTANCES: date, run_id and dew point. **A
    maximum found on a hot run is a different fact from one found on a hard
    effort** -- neither disqualifies a peak, and both change what anchoring a
    zone scale to that peak would mean. run_type is null and labelled "not
    collected": this build has no source for it.

    `declared_ceiling` compares the corpus peak against the max HR the athlete's
    zones are actually anchored to (§14e). It is REPORTED, never corrected:
    re-anchoring zones is an athlete decision with a coach behind it.

    THE COMPARISON IS GATED ON THE SELECTION ITSELF. It is reported only when
    a run IN THIS SELECTION reached within the ceiling band of the
    demonstrated 30 s ceiling; otherwise it is REFUSED, naming the
    selection's best 30 s value and the band. Over easy runs the gap measures
    the selection, not the heart.
    """
    def go(con):
        from trail_factors.analysis.over import max_hr_over as q
        r = q(con, **_selection("max_hr_over", run_type=run_type,
                                start_date=start_date, end_date=end_date,
                                cluster=cluster, last_n=last_n,
                                segment_id=segment_id, direction=direction))
        top = r["max_hr_recorded"]
        # A SELECTION CAN HAVE A PEAK AND NO 30 s WINDOW (a 20 s activity,
        # records gapped wider than the window). Formatting must not cost the
        # payload that was already computed.
        w30 = r["max_hr_30s"]
        w30_value = w30.get("value") if w30 is not None else None
        sustained = (f"{w30_value:.0f} sustained over 30 s"
                     if w30_value is not None
                     else "no 30 s window could be filled")
        return r, ("no heart rate in this selection" if top is None else
                   f"peak {top['value']:.0f} bpm on {top['date']} "
                   f"({sustained})")
    return _call("max_hr_over", go)


@mcp.tool()
def hr_above(threshold: float = 0.0, run_type: str = "", start_date: str = "",
             end_date: str = "", cluster: str = "", last_n: int = 0,
             segment_id: str = "", direction: str = "") -> str:
    """Every contiguous excursion at or above a heart-rate threshold (§14e-ter).

    THE UNIT IS THE EXCURSION, NOT THE RECORD. A sustained bout would otherwise
    arrive as hundreds of rows and a single spike as one, with nothing in the
    shape to tell them apart — which is the whole question when deciding
    whether a ceiling is demonstrated or merely recorded.

    Each excursion carries the run and date, start offset, span, peak with its
    timestamp, mean, the 30 s window CENTRED on the peak, record count,
    §15.4 flag count, sampling regime, and pace / grade / cadence. **The same
    peak reached while climbing at a walking pace and while running fast on
    the flat are different facts about the same heart.**

    RAW heart rate, never lag-shifted. FLAGS ARE REPORTED, NOT APPLIED: at the
    top of the range a flag may mark a real artifact or a real spike and cannot
    settle which. **A run whose peak has unresolved provenance (§14e-bis;
    `unresolved_peak_run` in config/athlete.yaml, when set) is MARKED and never
    dropped, and every total is reported a second time without it** under
    `without_unresolved_peak`.

    Args:
        threshold: bpm. 0 (the default) means the configured threshold,
            `hr_above_default_bpm` in config/athlete.yaml -- set it near the
            top of your own HR distribution. `threshold_bpm` in the result
            says which value was used.
    """
    def go(con):
        from trail_factors.analysis.over import hr_above as q
        sel = _selection("hr_above", run_type=run_type, start_date=start_date,
                         end_date=end_date, cluster=cluster, last_n=last_n,
                         segment_id=segment_id, direction=direction)
        if threshold < 0:
            raise ValueError(f"threshold={threshold!r}: a heart rate cannot be "
                             f"negative. Pass a bpm, or 0 for the configured "
                             f"default.")
        # 0 IS THE NOT-SUPPLIED SENTINEL, and the threshold is then left to
        # over.hr_above, which owns the configured default. Reading config
        # here as well would be two places to decide one number.
        r = (q(con, **sel) if threshold == 0
             else q(con, float(threshold), **sel))
        return r, (f"{r['n_excursions']} excursions at or above "
                   f"{r['threshold_bpm']:.0f} bpm across {r['n_runs_with_any']} runs")
    return _call("hr_above", go)


@mcp.tool()
def time_above_hr(run_type: str = "", start_date: str = "", end_date: str = "",
                  cluster: str = "", last_n: int = 0, segment_id: str = "",
                  direction: str = "") -> str:
    """Time at or above each rung of the configured HR ladder
    (`time_above_ladder_bpm` in config/athlete.yaml), per run and corpus-wide.
    `thresholds_bpm` in the result lists the rungs used.

    THE QUESTION THE EXCURSION LIST CANNOT ANSWER: is the top of the range a
    place this athlete has been REPEATEDLY, or twice? One run contributing six
    excursions and six runs contributing one each look identical at the top of
    a list sorted by peak.

    Timer-time weighting, capped per record: time at a heart rate is a DOSE,
    not a rate, so stopped time is not subtracted — the heart does not stop at
    a road crossing. Totals are also reported without the run whose provenance
    is open (§14e-bis).
    """
    def go(con):
        from trail_factors.analysis.over import time_above_hr as q
        r = q(con, **_selection("time_above_hr", run_type=run_type,
                                start_date=start_date, end_date=end_date,
                                cluster=cluster, last_n=last_n,
                                segment_id=segment_id, direction=direction))
        c = r["corpus"]
        return r, ("; ".join(f"{int(t)}+: {c[t]['seconds']/60:.1f} min over "
                             f"{c[t]['n_runs']} runs" for t in r["thresholds_bpm"]))
    return _call("time_above_hr", go)


@mcp.tool()
def zone_by_gait(run_type: str = "", start_date: str = "", end_date: str = "",
                 cluster: str = "", last_n: int = 0, segment_id: str = "",
                 direction: str = "") -> str:
    """M7 time in HR zone split by GAIT — running vs walking vs standing.

    Zone-2 minutes are the headline, per run and rolled up per LOCAL ISO week.

    WHY THE SPLIT MATTERS: a pooled zone-2 total mixes a walk up a climb
    with running on the flat, and those are different training. On a hilly
    run/walk session the walking share of zone 2 can exceed the running share
    — invisible in the pooled figure, and the whole point of the run/walk
    structure.

    Boundaries come from EACH ACTIVITY'S OWN FILE where present (§14e); a
    corpus spanning zone changes holds several distinct sets, and
    `boundary_set` says which ruler a
    run was measured with, and a week spanning two is reported as SPLIT rather
    than summed into a number that is on no scale.

    Do not compare this against Garmin's hrTimeInZone: ours excludes stops and
    flagged records and applies the 40 s HR lag, so it reads lower by design.
    """
    def go(con):
        from trail_factors.analysis.over import zone_by_gait as q
        r = q(con, **_selection("zone_by_gait", run_type=run_type,
                                start_date=start_date, end_date=end_date,
                                cluster=cluster, last_n=last_n,
                                segment_id=segment_id, direction=direction))
        return r, (f"{len(r['per_run'])} runs over {len(r['weekly'])} weeks")
    return _call("zone_by_gait", go)


@mcp.tool()
def run_walk_cycles(run: str = "", run_type: str = "", start_date: str = "",
                    end_date: str = "", cluster: str = "", last_n: int = 0,
                    segment_id: str = "", direction: str = "") -> str:
    """M3 run/walk cycle economics — does the walk buy anything? (§17.2)

    Args:
        run: run_id, activity_id or Garmin file id. Empty means the latest run.

    CHOICE, NOT CAPACITY (§6.6). This is how the athlete elected to structure
    the run; it says nothing about what they could have done.

    `purchase` pairs each walk with the NEXT run block's pace; `decay` is run
    pace by cycle index. Both are PAIRED SERIES, never a fitted slope — time
    into the run confounds both, and a coefficient over four points is wider
    than itself. Read `elapsed_at_*` before concluding the walk caused anything.

    Run blocks under the 120 s pace gate report duration but no pace.

    WHAT GOVERNED THE WALKS DECIDES WHAT THIS MEASURES, and this build does
    not know. If a rule (an HR cap, a clock) decided them, purchase and decay
    measure what the STRUCTURE delivered; if the athlete walked when they felt
    like it, identical arithmetic measures WILLINGNESS. `walk_protocol` is
    "not collected" here, so `poolable_with_other_runs` is false on every run:
    structure that has not been established cannot be assumed. Read each run
    on its own.
    """
    def go(con):
        from trail_factors.analysis.cycles import cycles
        _selection("run_walk_cycles", run_type=run_type, start_date=start_date,
                   end_date=end_date, cluster=cluster, last_n=last_n,
                   segment_id=segment_id, direction=direction)
        r = cycles(con, _activity_for(con, run))
        return r, (f"{r['n_cycles']} run blocks, {r['n_complete_cycles']} "
                   f"complete cycles [{r['status_hint']}]")
    return _call("run_walk_cycles", go)


@mcp.tool()
def session_series(run: str = "", run_type: str = "", start_date: str = "",
                   end_date: str = "", cluster: str = "", last_n: int = 0,
                   segment_id: str = "", direction: str = "",
                   include_dynamics: bool = False) -> str:
    """§17.1b the session as a 30 s binned series — for "over the run" reasoning.

    Args:
        run: run_id, activity_id or Garmin file id. Empty means the latest run.
        include_dynamics: add the per-bin running-dynamics block (below).
            Default false: it is the bulk of the payload, and when false it is
            absent from EVERY bin, with `dynamics_included: false` saying so.
            `dynamics_available` is reported either way.

    NULL-DISTANCE BINS (D4). A bin with fewer than two records that carry a
    distance -- a GPS gap, an indoor stretch -- reports only the DISTANCE-
    DERIVED fields null (distance_start_m/end_m, mps, pace_per_mile), with
    `distance_reason` saying how many distance records it had. avg_hr,
    steps_per_min, elevation_m, n_records and coverage are still reported
    from the records the bin does hold. A bin with NO records at all (past
    the last record, inside a long gap) is null throughout with n_records 0.
    (A stationary ride is the opposite case: its odometer is a constant 0.0,
    not null, so its bins report mps 0.0 -- a known defect, CLAUDE.md §18.)

    Pace, HR, elevation, cadence and run/walk/stand state per bin, indexed by
    BOTH elapsed seconds and path distance, so "at minute 40" and "at mile 4"
    are both answerable from one payload.

    STATE IS SECONDS, NOT A LABEL. Each bin carries `run_s`, `walk_s` and
    `stand_s`. Many run/walk walks are shorter than a 30 s bin, so a majority
    label would erase much of the walking — and on a run/walk session a large
    share of bins contain both running and walking.

    Elevation is DEM-gated and absent on more than half the corpus; read
    `elevation_available`. No position is ever emitted.

    RUNNING DYNAMICS PER BIN (§17.1b-bis): stance_time_ms, step_length_mm,
    vertical_oscillation_mm and vertical_ratio_pct, each {value, n, unit}.
    Garmin's units, unchanged -- millimetres, milliseconds, percent -- and
    vertical_ratio is the DEVICE'S OWN field, never recomputed from the other
    two.

    rwd_run RECORDS ONLY, which is a DIFFERENT RECORD SET from the one behind
    `steps_per_min` in the same bin (all records with cadence > 0, running and
    walking pooled). `filters.gait_conventions` states each field's, because
    reading them as one gait is the mistake the two conventions invite.

    n IS PER FIELD. The device drops the four independently -- one field can
    sit on a handful of records where another sits on thousands -- so a
    per-bin n would misstate the evidence behind a field by orders of
    magnitude.

    THREE ABSENCES, NEVER 0.0, all distinguishable: `dynamics_reason`
    "no_device_channel" (this activity carries none -- FR245, phone file, or a
    non-running sport), "no_running_in_bin" (the device records them, this bin
    has no running), or null with n 0 (running present, device wrote nothing
    for that field here).

    `flags_available` says whether §15.4 flags could be applied. Where
    records_enriched covers the run, flagged records are excluded; where it
    does not, every record is included -- the same figure with and without
    flags is NOT the same quantity, so the payload says which it is rather
    than refusing the run.
    """
    def go(con):
        from trail_factors.analysis.series import binned_series
        _selection("session_series", run_type=run_type, start_date=start_date,
                   end_date=end_date, cluster=cluster, last_n=last_n,
                   segment_id=segment_id, direction=direction)
        r = binned_series(con, _activity_for(con, run),
                          include_dynamics=include_dynamics)
        if not r["elevation_available"]:
            elev = ", no elevation (no DEM tile)"
        elif not r.get("elevation_emitted", True):
            elev = (", elevation withheld (route_fingerprint_accepted is "
                    "false in the athlete config)")
        else:
            elev = ""
        return r, f"{r['n_bins']} bins of {r['bin_s']:.0f} s{elev}"
    return _call("session_series", go)


@mcp.tool()
def mile_splits(run: str = "", unit: str = "mile") -> str:
    """Per mile (or `unit="quarter"`): running-only pace, HR, DEM gain/loss
    and time by gait, with Garmin's own laps beside it for cross-check.

    Args:
        run: run_id, activity_id or Garmin file id. Empty means the latest run.
        unit: "mile" (default) or "quarter".

    BOUNDARIES from the device odometer, interpolated exactly at each mile
    (the windowed-best rule), never from 30 s bins; across a standing stretch
    the LAST instant at that distance. The final unit is a PARTIAL with its
    real length. A unit crossing a recording gap over 20 s is FLAGGED
    (`contains_gap` / `boundary_in_gap`), not refused.

    RUNNING-ONLY PACE: rwd_run splits intersected with the unit, stopped time
    removed. A walk break inside a mile is excluded from pace and counted in
    `time_walking_s`. `hr_running` (PRIMARY) is time-weighted over the same
    running overlap; `hr_all_gaits` covers the whole unit. Both RAW, no lag.

    Gain/loss from the smoothed DEM profile, in m and ft; with no DEM they are
    null and `elevation_reason` says why. `garmin_laps` are Garmin's laps AS
    RECORDED -- only distance-triggered ones are miles, and their pace
    includes walking -- and are never substituted for ours.
    """
    def go(con):
        from trail_factors.analysis.miles import mile_splits as ms
        r = ms(con, _activity_for(con, run), unit=unit)
        return r, f"{r.get('n_units', 0)} {unit} units"
    return _call("mile_splits", go)


@mcp.tool()
def hill_pace(run: str = "", run_type: str = "", start_date: str = "",
              end_date: str = "", cluster: str = "", last_n: int = 0,
              segment_id: str = "", direction: str = "") -> str:
    """§6.0g pace on every slope effort of one run, split by direction.

    Args:
        run: run_id, activity_id or Garmin file id. Empty means the latest run.

    Each effort carries mean pace (whole effort, summit to col, moving time)
    AND best 30 s (the part actually run fast). They are different quantities —
    a slow mean beside a much faster best 30 s is a description of how the
    effort was run (walked in part, run in part), not a contradiction.

    READ `comparable_ground.length_ratio` BEFORE COMPARING THE DIRECTIONS.
    Ascents and descents need not cover the same ground — plateau absorption
    can glue flat ground onto one direction (§14k) — so part of
    any ascent-vs-descent difference is terrain rather than the athlete.
    """
    def go(con):
        from trail_factors.analysis.basic import effort_pace_by_direction
        _selection("hill_pace", run_type=run_type, start_date=start_date,
                   end_date=end_date, cluster=cluster, last_n=last_n,
                   segment_id=segment_id, direction=direction)
        r = effort_pace_by_direction(con, _activity_for(con, run))
        return r, (f"{len(r['efforts'])} efforts: "
                   f"{r['by_direction']['ascent']['n_efforts']} ascent, "
                   f"{r['by_direction']['descent']['n_efforts']} descent")
    return _call("hill_pace", go)


@mcp.tool()
def cadence_by_grade(run_type: str = "", start_date: str = "",
                     end_date: str = "", cluster: str = "",
                     last_n: int = 0, segment_id: str = "",
                     direction: str = "") -> str:
    """§6.0 cadence per 5% grade bin over a selection, split by split_type.

    Never pooled across run and walk — running and walking cadence sit tens of
    spm apart, so a pooled bin reports a number neither mode took.

    Needs a DEM tile for the grade, so it sees only DEM-covered runs;
    `n_runs_without_grade` says how many of the selection had none.
    """
    def go(con):
        from trail_factors.analysis.over import cadence_table
        r = cadence_table(con, **_selection("cadence_by_grade", run_type=run_type, start_date=start_date,
                        end_date=end_date, cluster=cluster, last_n=last_n,
                        segment_id=segment_id, direction=direction))
        return r, (f"{len(r['table'])} bins over "
                   f"{r['selection']['n_included']} runs")
    return _call("cadence_by_grade", go)


@mcp.tool()
def drift_onset(run: str = "", run_type: str = "", start_date: str = "",
                end_date: str = "", cluster: str = "", last_n: int = 0,
                segment_id: str = "", direction: str = "") -> str:
    """§6.9 WHEN in a run cardiac drift begins — a VIEW of §6.7, currently REFUSED.

    Args:
        run: run_id, activity_id or Garmin file id. Empty means the latest run.

    The result carries `gate`, which is computed from the live corpus. While
    `gate.reportable` is false the curve is a DIAGNOSTIC and must not be quoted
    as a finding — the same standing as tau (§6.3). Read `gate.reason`: it says
    exactly what would make it start reporting.

    Onset is confounded with run length and its earliest representable value is
    §6.7's own 600 s minimum pair separation. Dew point travels with every
    value because onset is uninterpretable without it.
    """
    def go(con):
        from trail_factors.analysis import drift_onset as DO
        _selection("drift_onset", run_type=run_type, start_date=start_date,
                   end_date=end_date, cluster=cluster, last_n=last_n,
                   segment_id=segment_id, direction=direction)
        aid = _activity_for(con, run)
        gate = DO.gate_status(con)
        c = DO.curve(con, aid)
        return {"gate": gate, "curve": c}, (
            f"drift onset is {'reportable' if gate['reportable'] else 'REFUSED'}; "
            f"this run: {c['onset_status']}")
    return _call("drift_onset", go)


@mcp.tool()
def report(run: str = "", run_type: str = "", start_date: str = "",
           end_date: str = "", cluster: str = "", last_n: int = 0,
           segment_id: str = "", direction: str = "") -> str:
    """The §16.2 post-run report for one run, against its recent history.

    Args:
        run: run_id, activity_id or Garmin file id. Empty means the latest run.

    Presents only — it does not judge. Eliminating causes is your work.
    Read `notable` first, then `unenforced`.
    """
    def go(con):
        from trail_factors.analysis.report import build, render_safe
        _selection("report", run_type=run_type, start_date=start_date,
                   end_date=end_date, cluster=cluster, last_n=last_n,
                   segment_id=segment_id, direction=direction)
        aid = _activity_for(con, run)
        rep = build(con, aid)
        # THE PAYLOAD SURVIVES A BROKEN RENDERER. It is the product; the
        # text is a convenience over it, and losing 19 computed sections to a
        # number formatter is the failure this exists to stop.
        text, render_error = render_safe(rep)
        if render_error:
            rep = {**rep, "render_error": render_error}
            text = (f"the payload is complete; its TEXT rendering failed with "
                    f"{render_error}. Read the payload -- this is a "
                    f"formatting defect, not a missing result.")
        return rep, text
    return _call("report", go)


def load_summary(rows: list[dict]) -> dict:
    """Sum the priced sessions, and say exactly what the sum leaves out.

    TWO WAYS NOT TO BE PRICED, COUNTED APART. `n_unrated` is the rows with no
    Garmin rating at all; a row that IS rated but still could not be priced
    (no timer time) is `n_rated_unpriced`, each with its own reason. Counting
    the second kind as unrated would assert a cause nobody checked.

    THE SUM IS A LOWER BOUND ONLY WHEN SOMETHING WAS LEFT OUT. With every
    session priced it is the selection's load, and labelling it a lower bound
    would understate a complete figure.
    """
    priced = [r for r in rows if r["status"] == "ok"]
    unrated = [r for r in rows if r["garmin_rpe"] is None]
    rated_unpriced = [r for r in rows
                      if r["garmin_rpe"] is not None and r["status"] != "ok"]
    lower_bound = bool(unrated or rated_unpriced)
    if not rows:
        note = ("the selection is empty: null is no session selected, not a "
                "load of zero")
    elif lower_bound:
        note = (f"{len(unrated)} unrated and {len(rated_unpriced)} rated but "
                f"unpriced session(s) are left out rather than counted as "
                f"zero, so the sum is a LOWER BOUND on the selection's load; "
                f"null means no session was priced")
    else:
        note = "every session in the selection is priced; the sum is its load"
    return {
        "n": len(rows),
        "n_priced": len(priced),
        "n_rated": len(rows) - len(unrated),
        "n_unrated": len(unrated),
        "n_rated_unpriced": len(rated_unpriced),
        "rated_unpriced": [{"activity_id": r["activity_id"],
                            "run_id": r["run_id"], "reason": r["reason"]}
                           for r in rated_unpriced],
        "sum_foster_load_au_priced": (
            sum(r["foster_load_au"] for r in priced) if priced else None),
        "is_lower_bound": lower_bound,
        "note": note,
    }


@mcp.tool()
def session_load(run: str = "", run_type: str = "", start_date: str = "",
                 end_date: str = "", cluster: str = "", last_n: int = 0,
                 segment_id: str = "", direction: str = "") -> str:
    """Foster session load: Garmin's own post-run RPE x timer minutes, in AU.

    Args:
        run: run_id, activity_id or Garmin file id -- ONE activity. Empty,
            with no selection filter, means the latest run.
        start_date, end_date, cluster, last_n: a SELECTION instead of one
            activity -- of EVERY SPORT, since load is a dose and a rated
            ride is load the legs carried (`summary.n_by_sport`). Name a run
            OR a selection, never both.
        run_type: REJECTED when non-empty -- this build has no run_type source.

    THE RPE IS GARMIN'S. Some watches ask "how hard was that?" when an
    activity is saved; an activity where the device did not ask, or the
    rating was declined, is `unavailable` with that reason -- NEVER priced at
    0 and never filled from another activity. `garmin_feel` is SHOWN and
    never used: it is a different question, and its 1-5 direction is
    unverified.

    TIMER TIME, NOT MOVING TIME: load is a session DOSE, not a rate, so
    standing time counts. Read `filters` for the formula and its basis.

    OVER A SELECTION, `summary.sum_foster_load_au_priced` sums the priced
    sessions. When any session is unpriced -- unrated (`n_unrated`), or rated
    with no timer time (`n_rated_unpriced`, each with its reason) -- it is a
    LOWER BOUND and `is_lower_bound` says so; unpriced sessions are left out,
    never counted as zero. No GPS coverage gate applies -- load is volume,
    not a terrain comparison.
    """
    def go(con):
        from trail_factors.analysis import session_load as SL
        sel = _selection("session_load", run_type=run_type,
                         start_date=start_date, end_date=end_date,
                         cluster=cluster, last_n=last_n,
                         segment_id=segment_id, direction=direction)
        if sel and run != "":
            raise RunAndSelection(
                f"session_load: name a run OR a selection, not both "
                f"(run={run!r} with {', '.join(sorted(sel))}). Nothing was "
                f"computed.")
        if sel:
            from trail_factors.analysis.select import (SPORT_ANY, select_runs,
                                             validate_filter)
            problems = validate_filter(sel)
            if problems:
                raise ValueError("; ".join(problems))
            # A DOSE, NOT A TERRAIN READ: no coverage gate. A run with a
            # patchy GPS trace is still load the athlete carried. And EVERY
            # SPORT: a rated ride is load the legs carried, and a running-only
            # sum understated every week with cross-training in it, with one
            # sign and nothing saying so.
            chosen = select_runs(con, sel, needs_terrain=False,
                                 sport=SPORT_ANY)
            rows = [{**SL.session_load(con, r["activity_id"]),
                     "sport": r["sport"]} for r in chosen["included"]]
            rule = chosen["rule"]
        else:
            rows = [SL.session_load(con, _activity_for(con, run))]
            rule = "one activity"
        summary = load_summary(rows)
        if sel:
            by_sport: dict = {}
            for r in rows:
                by_sport[r["sport"]] = by_sport.get(r["sport"], 0) + 1
            summary["n_by_sport"] = by_sport
            summary["sports"] = ("every sport the watch recorded: load is a "
                                 "session dose, so a ride or a swim counts "
                                 "as much as a run")
        out = {"selection_rule": rule, "rows": rows, "summary": summary,
               "unrated_reason": SL.UNRATED_REASON, "filters": SL.filters()}
        if len(rows) == 1:
            r = rows[0]
            text = (f"{r['run_id']}: {r['foster_load_au']:.0f} AU "
                    f"(RPE {r['garmin_rpe']:g} x {r['duration_min']:.1f} min)"
                    if r["status"] == "ok" else
                    f"{r['run_id']}: unavailable -- {r['reason']}")
        else:
            total = summary["sum_foster_load_au_priced"]
            unpriced = summary["n"] - summary["n_priced"]
            text = (f"{summary['n_priced']} of {summary['n']} sessions priced"
                    + (f", {total:.0f} AU" if total is not None else "")
                    + (f" (a lower bound: {unpriced} unpriced)"
                       if summary["is_lower_bound"] else ""))
        return out, text
    return _call("session_load", go)


@mcp.tool()
def list_runs(start_date: str = "", end_date: str = "", run_type: str = "",
              cluster: str = "", limit: int = 50, sport: str = "") -> str:
    """Corpus listing, newest first. All filters optional and AND-ed.

    Args:
        start_date: inclusive YYYY-MM-DD.
        end_date: inclusive YYYY-MM-DD.
        run_type: REJECTED when non-empty -- this build has no run_type
            source. Each row reports run_type null, labelled "not collected";
            a later version re-enables it through user-defined run types.
        cluster: a cluster name from config/athlete.yaml, or NO_GPS / OTHER.
        limit: maximum rows.
        sport: empty means running. "any" lists every sport; otherwise one
            sport by name (cycling, hiking, ...).

    Each row carries `local_date`, the day the athlete would call it.
    """
    def go(con):
        from trail_factors.analysis.queries import list_runs as q
        _reject_run_type("list_runs", run_type)
        # BY KEYWORD, and run_type never passed: it was rejected above if
        # given, so the shared layer's own default applies.
        rows = q(con, start_date=_opt(start_date), end_date=_opt(end_date),
                 cluster=_opt(cluster), limit=limit, sport=_opt(sport))
        shown = sport if sport != "" else "running"
        return rows, f"{len(rows)} activities ({shown})"
    return _call("list_runs", go)


# --------------------------------------------------------------------------
# PARAMETERISED PRIMITIVES
#
# THE COACH INTERPRETS, THE SYSTEM COMPUTES. The computation is fixed and
# gated; only the SELECTION is free. That is what answers "HR drift on long
# runs over the last five sessions" without a generic query tool.
#
# Filter vocabulary, uniform across all of them:
#   run_type, start_date, end_date, cluster, last_n, segment_id, direction
# run_type is in every signature and REJECTED BY NAME when non-empty: this
# build has no source for it (see _reject_run_type).
#
# NOT available, by design: arbitrary SQL, arbitrary aggregation, bronze
# access, chat-defined metrics. See src/trail_factors/analysis/select.py.
# --------------------------------------------------------------------------

# The filter vocabulary spec_context advertises. Not every tool can honour
# every one of these, and the ones it cannot are REJECTED BY NAME rather than
# ignored -- see _selection().
SELECTION_FILTERS = ("run_type", "start_date", "end_date", "cluster", "last_n",
                     "segment_id", "direction")

# Which tools honour which. Stated once, here, so the standing brief and the
# signatures cannot drift apart again.
SELECTION_SUPPORT = {
    "matched_terrain_drift": ("run_type", "start_date", "end_date", "cluster", "last_n"),
    "segment_effort_drift": ("run_type", "start_date", "end_date", "cluster",
                             "last_n", "direction"),
    "efficiency_factor": ("run_type", "start_date", "end_date", "cluster", "last_n"),
    "grade_binned_pace": ("run_type", "start_date", "end_date", "cluster", "last_n"),
    "cadence_by_grade": ("run_type", "start_date", "end_date", "cluster", "last_n"),
    "delta_hr": SELECTION_FILTERS,
    "segment_history": SELECTION_FILTERS,
    # Per-run tools. They take `run` and nothing else; a selection filter here
    # is a category error, not a narrowing.
    "report": (),
    "basic_metrics": (),
    "drift_onset": (),
    "hill_pace": (),
    "session_series": (),
    "run_walk_cycles": (),
    # A DOSE, so a selection is a set of sessions to price -- not a trend.
    "session_load": ("run_type", "start_date", "end_date", "cluster", "last_n"),
    "zone_by_gait": ("run_type", "start_date", "end_date", "cluster", "last_n"),
    "max_hr_over": ("run_type", "start_date", "end_date", "cluster", "last_n"),
    "hr_above": ("run_type", "start_date", "end_date", "cluster", "last_n"),
    "time_above_hr": ("run_type", "start_date", "end_date", "cluster", "last_n"),
}

# Where to send a caller whose filter this tool cannot honour.
_ALTERNATIVE = {
    "segment_id": "segment_history(segment_id=...) or delta_hr(segment_id=...) "
                  "-- this primitive is not computed per segment",
    "direction": "segment_history(direction=...) or delta_hr(direction=...) "
                 "-- this primitive has no per-direction form",
}


def _is_supplied(v) -> bool:
    """Was this argument actually given?

    The MCP signatures use "" and 0 as their not-supplied sentinels, so this
    tests them EXPLICITLY rather than leaning on falsiness -- 0 is a legitimate
    value elsewhere in this codebase and the shortcut has inverted results
    twice (CLAUDE.md).
    """
    if v is None:
        return False
    if isinstance(v, str):
        return v != ""
    if isinstance(v, int) and not isinstance(v, bool):
        return v != 0
    return True


def _opt(v: str) -> str | None:
    """An MCP string argument as the shared layer wants it: "" -> None.

    Spelled out rather than `v or None`: the falsy shortcut is banned in this
    codebase (CLAUDE.md), and the sentinel here is "" and nothing else.
    """
    return None if v is None or v == "" else v


def _selection(tool: str, **given) -> dict:
    """Build a filter dict, REJECTING any filter this tool cannot honour.

    THE BUG THIS EXISTS TO PREVENT. The MCP layer silently DROPS arguments a
    tool's signature does not declare, and spec_context tells the chat that
    every primitive takes the same seven filters. So
    `grade_binned_pace(segment_id=..., direction='descent')` returned the whole
    corpus -- byte-identical to the unfiltered call -- with no error at
    all. The chat was following the documented contract and being handed a
    number for a different question. That is the same failure class as the
    falsy-default and the end_time-minus-start_time bugs: every individual
    value looks plausible.

    Silently ignoring a filter is the one outcome that must never happen. A
    rejection is recoverable; a plausible wrong answer is not.

    run_type IS CHECKED FIRST, FOR EVERY TOOL: it is a documented filter with
    no source in this build, and that is a different fact from "this tool
    cannot honour it" -- see _reject_run_type.
    """
    _reject_run_type(tool, given.get("run_type"))
    supported = SELECTION_SUPPORT[tool]
    supplied = {k: v for k, v in given.items() if _is_supplied(v)}
    unsupported = [k for k in SELECTION_FILTERS if k in supplied and k not in supported]
    if unsupported:
        bits = []
        for k in unsupported:
            bits.append(f"{k}={supplied[k]!r}: use "
                        f"{_ALTERNATIVE.get(k, 'a selection-level tool')}")
        allowed = (", ".join(supported) if supported
                   else "none -- this tool reports on ONE run, named by `run`")
        raise ValueError(
            f"{tool} cannot honour {', '.join(unsupported)}. "
            f"It accepts: {allowed}. " + " ".join(bits) +
            " Nothing was computed: a filter this tool ignores would have "
            "returned a number for a different question.")
    return supplied


def _reject_run_type(tool: str, run_type) -> None:
    """Reject a non-empty run_type BY NAME: this build has no source for it.

    THE PARAMETER STAYS on every selection tool, so a later version can
    re-enable it through user-defined run types without changing any
    signature. Until then a run_type filter could only ever match nothing,
    and an empty selection reads as "no such runs" -- a finding about the
    athlete, when the truth is that the value was never collected.
    """
    # THE WORDING IS select.check_available's, so the MCP rejection and the
    # shared layer's cannot drift apart; this adds only the tool's name and
    # what the caller will see on every row instead.
    try:
        check_available({"run_type": run_type if _is_supplied(run_type)
                         else None})
    except FilterUnavailable as exc:
        raise FilterUnavailable(
            f"{tool}: {exc} Every run's run_type is reported null and "
            f"labelled {NOT_COLLECTED!r}.") from None


def _filters(run_type, start_date, end_date, cluster, last_n):
    """The five run-selection filters, omitting the ones not supplied."""
    given = (("run_type", run_type), ("start_date", start_date),
             ("end_date", end_date), ("cluster", cluster), ("last_n", last_n))
    return {k: v for k, v in given if _is_supplied(v)}


@mcp.tool()
def matched_terrain_drift(run_type: str = "", start_date: str = "",
                          end_date: str = "", cluster: str = "",
                          last_n: int = 0, segment_id: str = "",
                          direction: str = "") -> str:
    """§6.7 HR drift on the SAME ground, bpm/hour, over a selection of runs.

    Pairs RECORDS within 20 m of each other, at least 10 min apart, BOTH
    rwd_run, with speed matched to 0.35 m/s. Walking is not paired. Read
    `applied_filters` — those tolerances are what the number means.

    Effort-matched by construction, so it is the substitute when §6.4's
    steady-effort gate fails. Read `excluded` too — a drift figure over "five
    long runs" means something different if two were dropped for coverage.

    `segment_effort_drift` is a DIFFERENT quantity, usually several times
    larger. Neither is "the" drift.
    """
    def go(con):
        from trail_factors.analysis.over import drift
        r = drift(con, **_selection("matched_terrain_drift", run_type=run_type, start_date=start_date,
                        end_date=end_date, cluster=cluster, last_n=last_n,
                        segment_id=segment_id, direction=direction))
        return r, (f"{r['primitive']}: median "
                   f"{r['median'] if r['median'] is None else round(r['median'], 2)} "
                   f"{r['unit']} over {r['n_with_value']} of "
                   f"{r['selection']['n_included']} runs")
    return _call("matched_terrain_drift", go)


@mcp.tool()
def segment_effort_drift(run_type: str = "", start_date: str = "",
                         end_date: str = "", cluster: str = "",
                         last_n: int = 0, segment_id: str = "",
                         direction: str = "") -> str:
    """Effort-level HR drift on a matched slope. NOT §6.7 — a DIFFERENT quantity.

    Pairs whole effort averages with no speed match and no split_type filter,
    and normalises by the gap between effort starts, so a short gap amplifies:
    +10 bpm over 10 min reads as +60 bpm/hour. On the same run it can read
    several times larger than matched_terrain_drift.

    Use `matched_terrain_drift` unless you specifically want the slope-matched
    measure. NEVER present either as "the" drift. Read `applied_filters`.
    """
    def go(con):
        from trail_factors.analysis.over import segment_effort_drift as q
        r = q(con, **_selection("segment_effort_drift", run_type=run_type,
                        start_date=start_date, end_date=end_date,
                        cluster=cluster, last_n=last_n,
                        segment_id=segment_id, direction=direction))
        return r, f"{r['n_with_value']} of {r['selection']['n_included']} runs"
    return _call("segment_effort_drift", go)


@mcp.tool()
def efficiency_factor(run_type: str = "", start_date: str = "",
                      end_date: str = "", cluster: str = "",
                      last_n: int = 0, segment_id: str = "",
                      direction: str = "") -> str:
    """§6.8 speed per heartbeat on segment-matched terrain, over a selection.

    Matched by segment identity, NOT by GAP — GAP is unvalidated (§12.2).
    """
    def go(con):
        from trail_factors.analysis.over import efficiency
        r = efficiency(con, **_selection("efficiency_factor", run_type=run_type, start_date=start_date,
                        end_date=end_date, cluster=cluster, last_n=last_n,
                        segment_id=segment_id, direction=direction))
        return r, f"{r['n_with_value']} of {r['selection']['n_included']} runs"
    return _call("efficiency_factor", go)


@mcp.tool()
def delta_hr(direction: str = "", segment_id: str = "", run_type: str = "",
             start_date: str = "", end_date: str = "", cluster: str = "",
             last_n: int = 0) -> str:
    """§6.3 HR excursion across an effort, by direction, over a selection.

    Ascent and descent are NOT comparable on this metric: the excursion
    differs systematically by direction. Tau is never returned by this tool;
    whether it is reportable at all is §6.3's corpus-level gate.
    """
    def go(con):
        from trail_factors.analysis.over import delta_hr as q
        r = q(con, **_selection(
            "delta_hr", run_type=run_type, start_date=start_date,
            end_date=end_date, cluster=cluster, last_n=last_n,
            segment_id=segment_id, direction=direction))
        return r, f"{r['n_with_value']} of {r['selection']['n_included']} runs"
    return _call("delta_hr", go)


@mcp.tool()
def grade_binned_pace(run_type: str = "", start_date: str = "",
                      end_date: str = "", cluster: str = "",
                      last_n: int = 0, segment_id: str = "",
                      direction: str = "") -> str:
    """§6.2 pace by grade bin, split by split_type, over a selection.

    Run and walk are never pooled: pooling reads as "pace does not vary with
    grade" purely because each bin mixes the two modes.
    """
    def go(con):
        from trail_factors.analysis.over import grade_table
        r = grade_table(con, **_selection("grade_binned_pace", run_type=run_type, start_date=start_date,
                        end_date=end_date, cluster=cluster, last_n=last_n,
                        segment_id=segment_id, direction=direction))
        return r, f"{len(r['table'])} bins over {r['selection']['n_included']} runs"
    return _call("grade_binned_pace", go)


@mcp.tool()
def segment_history(segment_id: str, direction: str = "", run_type: str = "",
                    start_date: str = "", end_date: str = "",
                    cluster: str = "", last_n: int = 0) -> str:
    """§6.6 one slope, every traversal, over time — the trending primitive.

    Use `report` to find segment ids; they are unrecognisable on their own, so
    the report lists them in the order the reference run meets them.
    """
    def go(con):
        from trail_factors.analysis.over import segment_history as q
        _reject_run_type("segment_history", run_type)
        r = q(con, segment_id, _opt(direction),
              **_filters(run_type, start_date, end_date, cluster, last_n))
        return r, f"{r['n_efforts_in_selection']} efforts on {segment_id}"
    return _call("segment_history", go)


@mcp.tool()
def list_segments() -> str:
    """The slope library: every segment, with where the reference run meets it."""
    def go(con):
        from trail_factors.analysis.queries import segment_library
        rows = segment_library(con)
        return rows, f"{len(rows)} slopes"
    return _call("list_segments", go)


@mcp.tool()
def ride_blocks(run: str = "") -> str:
    """§18 the stationary ride's blocks, each with its HR summary.

    The watch records HEART RATE ONLY for these -- measured on the indoor
    cycling activities in the corpus: no cadence, no speed, no position, no power,
    and distance_m is a constant 0.0 rather than null. So resistance, RPM and
    the block boundaries are ATHLETE-RECALLED, and `provenance` says so on
    every payload. `machine_totals` is the console readout -- machine-reported,
    a different reliability, and the machine is the only source for it.

    A block containing an off-bike event (water bottle, restroom) is FLAGGED:
    the clock keeps running, so the break lands inside a block and its HR is
    recovery rather than work at that resistance.

    An unannotated ride returns `unavailable` WITH A REASON, never an empty
    list -- nothing logged is a different fact from a ride with no structure.
    """
    def go(con):
        from trail_factors.analysis.rides import ride_blocks as rb
        r = rb(con, _activity_for(con, run))
        if r["status"] != "ok":
            return r, r["reason"]
        return r, (f"{r['n_blocks']} blocks on {r['run_id']} "
                   f"(revision {r['revision']})")
    return _call("ride_blocks", go)


@mcp.tool()
def resistance_history(resistance: float = 0.0, match_rpm: bool = True) -> str:
    """§18.1 HR at matched resistance over CALENDAR TIME — the longitudinal cut.

    EFFECTIVE n IS THE NUMBER OF BLOCKS, NEVER THE NUMBER OF HR RECORDS. A
    block is one exposure to one resistance; its several hundred HR samples are
    one observation of it. This project has made the opposite mistake twice
    (§6.9, §2.6) and both are recorded as the same disease.

    A group with fewer than three blocks reports `trend_status: insufficient`
    and says how many it has, rather than drawing a line through two points.
    Blocks with no stated RPM are grouped SEPARATELY from blocks that have one:
    no stated cadence is not a cadence that happens to match.
    """
    def go(con):
        from trail_factors.analysis.rides import resistance_history as rh
        # 0.0 IS THE NOT-SUPPLIED SENTINEL (a closed knob is not a load).
        r = rh(con, resistance=(resistance if resistance != 0 else None),
               match_rpm=match_rpm)
        return r, (f"{r['n_groups']} resistance groups over "
                   f"{r['n_sessions']} logged rides")
    return _call("resistance_history", go)


@mcp.tool()
def time_in_resistance() -> str:
    """§18.2 minutes per resistance value and per 0.5-turn band, per LOCAL week.

    The same shape as time-in-zones, and the only ride read that rests on NO
    assumption beyond the blocks having been logged correctly. Pure
    aggregation of stored blocks.
    """
    def go(con):
        from trail_factors.analysis.rides import time_in_resistance as tir
        r = tir(con)
        return r, f"{r['n_weeks']} weeks with logged resistance blocks"
    return _call("time_in_resistance", go)


@mcp.tool()
def save_ride_blocks(payload: str, supersede: bool = False) -> str:
    """Store one stationary ride's block structure. THE ONE tool that writes
    athlete input.

    A writer is a deliberate act: the watch cannot record resistance or RPM,
    and nothing else in this system does either, so there is no existing
    channel this competes with. Perceived effort is NOT a field here --
    Garmin's own self-evaluation, where the device asked for one, is the
    effort channel (see session_load), and a second would be two places for
    one answer.

    Args:
        payload: a JSON object. Required: run_id (or activity_id), raw_text,
            blocks. Optional: events, machine_totals.
        supersede: replace an existing log for this ride, AS AN ARGUMENT
            BESIDE the payload and never a field inside it. The previous
            revision is KEPT.

    BLOCKS ARE END-ANCHORED: each entry is {resistance, end_min, rpm?}, and
    start_min is the previous entry's end (0 for the first). Supplying
    start_min is REJECTED -- one way to state a boundary means no way for two
    statements to disagree. Storage is explicit [start_min, end_min).

    resistance is DECIMAL knob-turns from closed (quarter turns such as 4.25,
    4.5, 4.75 are ordinary values), validated against [0, MAX]. That ceiling is DECLARED, not
    measured, until the athlete tests where the knob stops.

    rpm is a number or [low, high] and is stored as [low, high], NEVER
    collapsed to a midpoint -- a midpoint invents precision the recollection
    does not have. Absent is absent; a log with no RPM at all is complete.

    raw_text is REQUIRED and stored VERBATIM through every revision. The
    parsed blocks are an interpretation of it; the text is the record, for the
    same reason bronze keeps the raw file.

    AMBIGUITY IS REFUSED, NOT RESOLVED. This tool takes already-structured
    blocks; overlapping or contradictory entries are rejected naming both.
    Parsing prose is the chat's job, at save time, with the athlete present.

    Athlete-recalled average HR is NOT a field here -- the watch's HR is
    authoritative and already in the corpus. It survives in raw_text only.
    """
    def go(con):
        import json as _json

        from trail_factors.ingest.ride_blocks import save_ride_blocks as save
        body = _json.loads(payload) if isinstance(payload, str) else payload
        if not isinstance(body, dict):
            raise RideBlockError("payload must be a JSON object")
        from trail_factors.analysis.queries import activity_timing
        ref = next((v for v in (body.get("run_id"), body.get("activity_id"))
                    if v is not None and v != ""), "")
        aid = _activity_for(con, ref)
        run_id, duration_min = activity_timing(con, aid)
        body.setdefault("run_id", run_id)
        body["activity_id"] = aid
        entry = save(body, duration_min=duration_min, supersede=supersede)
        # VERIFIED THROUGH THE READ PATH: a write that only the writer can see
        # is the outage this project already had once, when saves landed in a
        # file while every read answered from a stale projection.
        from trail_factors.analysis.rides import ride_blocks as rb
        back = rb(con, aid)
        if back["status"] != "ok" or back["revision"] != entry["revision"]:
            raise ReadBackFailed(
                f"{run_id} was WRITTEN to data/athlete/ride_blocks.json "
                f"(revision {entry['revision']}) but the read path does not "
                f"return it. The file is the record and is intact; do not "
                f"re-send the payload.")
        entry["visible_to_read"] = {"confirmed": True,
                                    "revision": back["revision"],
                                    "n_blocks": back["n_blocks"]}
        return entry, (f"logged {run_id}: {len(entry['blocks'])} blocks, "
                       f"revision {entry['revision']}")
    return _call("save_ride_blocks", go)


# --------------------------------------------------------------------------
# SYNC — fire and forget, because it cannot be synchronous
# --------------------------------------------------------------------------

# THE NEXT STEP IS A COMMAND, NOT A HINT. A rejection that says "confirm a
# window" without saying how is a dead end for a chat that cannot see the
# CLI, and the files that need a window most are exactly the ones with no
# activity row for any MCP tool to resolve. The command itself comes from the
# CLI's rejection_advice (src/trail_factors/cli/athlete.py), so the two cannot drift.
REJECTED_FILES_CMD = "uv run trail-factors rejected-files"
# CONFIRMING A WINDOW IS CLI-ONLY. No MCP tool writes a window confirmation:
# it changes what the corpus INGESTS, which is a different act from the one
# athlete-input writer this server exposes, and it is followed by a re-ingest
# that needs the write lock. Said in every rejection so a chat does not go
# looking for a tool that does not exist.
CONFIRM_IS_CLI_ONLY = ("Confirming a window is CLI-only: there is no MCP tool "
                       "for it. Run the command in a terminal in the repo, "
                       "then sync again -- a confirmed file is re-ingested, "
                       "trimmed to the window, on the next sync.")


def sync_rejections(ingest: dict, new_ids, raw_dir) -> tuple[list[dict], list[str]]:
    """THIS sync's rejected and failed files, each with its reason and the
    exact next step. Returns (rejected rows, problem strings).

    SCOPED TO THIS SYNC'S FILES. ingest_all walks every raw file, so its
    `rejected` list carries the historical ones too -- and a report that
    re-raises old rejections on every sync is a report that gets ignored.
    Those live in `rejected-files`, which persists on purpose. With no new
    downloads (the re-sync after a confirmation) every file ingest_all
    reported is in scope, because then that IS what this sync did.

    Each rejected file carries the CLI's rejection_advice (ingest_all's
    `advice`, recomputed if absent): the reason, the next step, and the exact
    confirm-file command with the detector's suggested window filled in. It is
    a SUGGESTION -- the athlete knows when the car door closed -- and where no
    window can fix the cause there is no command, only what to check.
    """
    rejected, problems = [], []
    advice_by_file = {a["file"]: a for a in ingest.get("advice") or []}
    for kind in ("rejected", "failed"):
        for name, msg in ingest.get(kind, []):
            gid = str(name).split("_")[0]
            if new_ids and gid not in new_ids:
                continue
            row = {"garmin_id": gid, "file": name, "kind": kind,
                   "message": str(msg)}
            if kind == "rejected":
                # THE CLI'S OWN ADVICE, so MCP and the terminal cannot word
                # one rejection differently: the reason in plain words, and
                # the exact command with the detector's suggested window
                # filled in -- or no command at all where no window can fix
                # the cause (a mid-run drive, corrupt timestamps).
                a = advice_by_file.get(name)
                if a is None:
                    from trail_factors.cli.athlete import rejection_advice
                    a = rejection_advice(raw_dir / name, str(msg))
                s = a.get("suggestion") or {}
                row["reason"] = a["reason"]
                row["suggested_valid_end_utc"] = (
                    str(s["suggested_valid_end"])
                    if s.get("suggested_valid_end") is not None else None)
                row["suggested_valid_start_utc"] = (
                    str(s["suggested_valid_start"])
                    if s.get("suggested_valid_start") is not None else None)
                if a.get("suggest_error"):
                    row["suggest_error"] = a["suggest_error"]
                row["next_step"] = {
                    "what": a["next_step"],
                    "fixable_by_window": a["fixable_by_window"],
                    "confirm_command": a["command"],
                    "list_command": a["list_command"],
                    "then": a["then"] if a["command"] else None,
                    "reachable_from_mcp": False,
                }
                run = (f" Run `{a['command']}`, then `{a['then']}`."
                       if a["command"] else "")
                problems.append(
                    f"REJECTED: garmin {gid} did not ingest -- {a['reason']}. "
                    f"Next step: {a['next_step']}.{run} "
                    f"`{a['list_command']}` shows the file span and the "
                    f"detector's suggestion. {CONFIRM_IS_CLI_ONLY}")
            else:
                row["next_step"] = {
                    "what": "the file could not be read or processed; a "
                            "window will not fix it",
                    "list_command": REJECTED_FILES_CMD,
                    "reachable_from_mcp": False,
                }
                problems.append(
                    f"FAILED: garmin {gid} did not ingest -- {str(msg)[:200]}. "
                    f"This is an error reading or processing the file, not a "
                    f"validation finding, so confirming a window will not "
                    f"clear it.")
            rejected.append(row)
    if problems:
        problems.append(
            "a file that did not ingest is NOT in the corpus: nothing "
            "downstream will mention it again until it is dealt with. "
            f"`{REJECTED_FILES_CMD}` lists everything still outstanding.")
    return rejected, problems


# THE TERRAIN STEPS NEED A DEM TILE, AND A FRESH INSTALL HAS NONE. dem_populate
# raises when data/dem/ is empty, and the sync job used to die there -- one
# step AFTER ingest had rejected a file and built the advice for it -- so the
# rejection and its confirm-file command were never returned, and the
# re-sync after confirming died the same way. No tiles is a STATE, not a
# failure: the four steps are skipped and the job says so.
TERRAIN_STEPS = ("dem", "silver", "match", "recovery")
# Each terrain step reads what the one before it wrote, so a step whose input
# did not complete in THIS sync is skipped rather than run over stale input:
# recovery over last week's segment_efforts is the staleness the step order
# exists to prevent.
STEP_NEEDS = {"silver": "dem", "match": "silver", "recovery": "match"}


def no_tiles_note() -> str:
    from trail_factors.paths import DEM_DIR
    return (f"no DEM tiles in {display_path(DEM_DIR)}: dem, silver, match "
            f"and recovery were skipped, so this sync computed no grade, "
            f"slopes or segment efforts. That is the normal state of a fresh "
            f"install, not a failure; `uv run trail-factors fetch-tile "
            f"--missing` fetches the tiles the corpus needs.")


def run_sync_steps(steps, progress, tiles_present: bool) -> dict:
    """Run each (name, fn) in order; ONE STEP'S FAILURE COSTS ONLY ITSELF.

    A step that raises is recorded under `step_errors` and in `problems` with
    its name, and the steps after it still run -- except a terrain step whose
    input step did not complete here, which is skipped with that reason.
    Returns {"outputs", "steps", "skipped_steps", "step_errors", "problems"}.
    """
    outputs, status, skipped, errors, problems = {}, {}, [], [], []
    for step, fn in steps:
        need = STEP_NEEDS.get(step)
        if step in TERRAIN_STEPS and not tiles_present:
            reason = no_tiles_note()
        elif need is not None and status.get(need) != "done":
            reason = (f"{need} did not complete in this sync "
                      f"({status.get(need)}), and {step} reads what {need} "
                      f"writes; running it would build on stale input")
        else:
            reason = None
        if reason is not None:
            status[step] = "skipped"
            skipped.append({"step": step, "reason": reason})
            continue
        progress(step)
        try:
            outputs[step] = fn()
            status[step] = "done"
        except Exception as exc:
            msg = _scrub_paths(f"{type(exc).__name__}: {exc}")
            status[step] = "failed"
            errors.append({"step": step, "error": msg})
            problems.append(f"STEP {step} FAILED: {msg[:400]}")
    return {"outputs": outputs, "steps": status, "skipped_steps": skipped,
            "step_errors": errors, "problems": problems}


def sync_work(progress) -> dict:
    """The body of the sync job: download, then every step, then the report.

    THE DOWNLOAD STILL FAILS THE JOB: nothing has been ingested yet, so
    there is no rejection to lose, and a Garmin outage must read as one.
    Every step after it is caught per step (run_sync_steps), so the files
    ingest rejected are reported whatever a later step does.
    """
    from trail_factors.cli.garmin import already_downloaded, garmin_download_all
    from trail_factors.cli.ingest import ingest_all
    from trail_factors.cli.report import validate_cmd
    from trail_factors.cli.terrain import dem_populate, match_build, silver_build
    from trail_factors.cli.weather import weather_build
    from trail_factors.paths import RAW_DIR
    from trail_factors.transform.dem import loaded_tiles

    dest = RAW_DIR / "garmin"
    before = already_downloaded(dest)
    progress("download")
    garmin_download_all(400, dest)
    new_ids = sorted(already_downloaded(dest) - before)
    # recovery MUST follow match: it reads segment_efforts. Leaving it out
    # let segment_recovery go stale while segment_efforts was current, and
    # delta_hr then reported "no efforts on this run" about a run that had
    # several.
    from trail_factors.cli.analysis import recovery_build
    # KEEP ingest_all's RETURN VALUE. `fn()` discarded it, so a file that was
    # REJECTED during a sync was reported nowhere: the job said done, the new
    # activity was simply absent, and the only trace was a validation_results
    # row with activity_id NULL that nothing read. Several files accumulated
    # that way.
    ran = run_sync_steps(
        (("ingest", ingest_all), ("dem", dem_populate),
         ("silver", silver_build), ("weather", weather_build),
         ("match", match_build), ("recovery", recovery_build),
         ("validate", lambda: validate_cmd(verbose=False))),
        progress, tiles_present=bool(loaded_tiles()))

    # SCOPED TO THIS SYNC'S FILES -- see sync_rejections.
    ingest_result = ran["outputs"].get("ingest")
    ingest = ingest_result if ingest_result is not None else {}
    rejected, problems = sync_rejections(ingest, new_ids, RAW_DIR / "garmin")
    problems = ran["problems"] + problems
    return {"new_activities": new_ids, "n_new": len(new_ids),
            # None, not 0, when ingest itself failed: nothing was counted.
            "n_ingested": (len(ingest.get("ok", []))
                           if ingest_result is not None else None),
            "rejected": rejected, "n_rejected": len(rejected),
            # A FILE RESCUED BY A CONFIRMED WINDOW IS NEWS. The re-sync after
            # confirm-file downloads nothing, so without this the job would
            # say nothing about the run it just brought into the corpus.
            "rescued": ingest.get("rescued", []),
            "n_rescued": len(ingest.get("rescued", [])),
            "steps": ran["steps"], "skipped_steps": ran["skipped_steps"],
            "step_errors": ran["step_errors"],
            "problems": problems,
            # A SYNC WITH PROBLEMS IS NOT A CLEAN DONE. A skip for want of
            # tiles is not a problem; a skip because a step failed is
            # already counted through that step's own error.
            "clean": not problems}


@mcp.tool()
def sync() -> str:
    """Fetch new Garmin activities and process them. Returns a JOB ID at once.

    This takes minutes and holds the database's exclusive write lock while it
    runs, so it cannot be awaited: a synchronous version would make every other
    tool call fail for the duration. Poll `sync_status` instead.

    NEW FILES ONLY. Downloads APPEND to data/raw/; nothing in this path
    modifies, moves, renames or deletes an existing raw file (§3).

    A FILE THAT DID NOT INGEST IS REPORTED BY NAME. A finished job with
    `clean: false` lists each rejected file under `rejected` with the reason
    and a `next_step` carrying the exact command: a recording with a drive or
    a forgotten stop at one end is cleared by confirming the window that is
    the run (`confirm-file <garmin-id> --end '...'`) and syncing again.
    Confirming a window is CLI-ONLY -- no tool here does it -- so relay the
    command to the athlete rather than looking for one.

    A STEP THAT FAILS COSTS ONLY ITSELF. Each step's outcome is in `steps`
    (done / skipped / failed); a failure is in `step_errors` and `problems`
    by step name and makes the job `clean: false`, and the rejections are
    still reported. With no DEM tiles on disk the terrain steps (dem, silver,
    match, recovery) are SKIPPED with the reason in `skipped_steps` -- the
    normal state of a fresh install, not a failure.
    """
    def go(_con):
        from trail_factors.analysis.jobs import start

        def work(progress):
            # sync_work imports its steps inside the thread, so a job failure
            # cannot take the server down with it.
            return sync_work(progress)

        job_id = start(work, label="sync")
        return ({"job_id": job_id, "state": "queued"},
                f"sync started as {job_id}; poll sync_status(\"{job_id}\"). "
                f"Other tools will fail with outcome=broken while it writes.")
    return _call("sync", go)


@mcp.tool()
def sync_status(job_id: str = "") -> str:
    """Progress of a sync job. Empty job_id returns the most recent.

    States: queued, running (with the current step), done, failed (with which
    step failed -- a Garmin outage and a corrupt file are different problems).
    A job fails outright only when the DOWNLOAD fails; a later step that
    raises is reported inside a `done` job's result, by name, under
    `step_errors` with `clean: false`, so the rejections still arrive.
    """
    def go(_con):
        from trail_factors.analysis.jobs import status
        s = status(_opt(job_id))
        latest = s["latest"] if s.get("latest") is not None else s
        return s, f"{latest.get('job_id', '?')}: {latest.get('state', 'unknown')}"
    return _call("sync_status", go, database=False)


@mcp.tool()
def ping() -> str:
    """Is the server alive, which code is it running, is the database locked.

    NEVER TOUCHES THE DATABASE, so it answers during a sync. A `broken` from
    any other tool while `write_locked` is true is the lock, not the corpus.
    """
    def go(_con):
        import datetime as _dt

        from trail_factors.analysis.jobs import database_lock_state
        from trail_factors.analysis.versions import PIPELINE_VERSION, digest
        lock = database_lock_state(DB_FILE)
        payload = {
            "server_time_utc": _dt.datetime.now(_dt.timezone.utc)
                                  .replace(tzinfo=None).isoformat(),
            "pipeline": {"pipeline_version": PIPELINE_VERSION,
                         "digest": digest()},
            "database": lock,
        }
        state = {True: "write-locked", False: "free", None: "lock unknown"}
        return payload, f"alive; database {state[lock['write_locked']]}"
    return _call("ping", go, database=False)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
