"""ATHLETE-SUPPLIED STATE commands (CLAUDE.md).

Everything here writes to the athlete-state directory (TRAIL_FACTORS_ATHLETE_DIR,
default data/athlete/), outside the rebuild path. Ingest never writes to these
tables."""

from __future__ import annotations

from datetime import UTC
from pathlib import Path

import duckdb

from trail_factors.cli.common import resolve_activity_id
from trail_factors.ingest.load import DB_PATH
from trail_factors.paths import RAW_DIR, display_path
from trail_factors.units import pace_per_mile


def _window_report(con, activity_id, vs, ve, dur, dist, new_start, new_end):
    """Build the whole report BEFORE anything is written.

    Separated so the write can be the LAST thing that happens. Everything that
    can fail — queries, formatting — runs first; §4.3's guarantee is that a
    failure leaves the valid window untouched.
    """
    valid_s = (new_end - new_start).total_seconds()
    vdist = con.execute(
        "SELECT max(distance_m) - min(distance_m) FROM records "
        "WHERE activity_id = ? AND timestamp BETWEEN ? AND ?",
        [activity_id, new_start, new_end],
    ).fetchone()[0]
    vdist = vdist if vdist is not None else 0.0
    rp = dist / dur if dur else 0
    vp = vdist / valid_s if valid_s else 0
    lines = [
        f"  window  {vs} .. {ve}",
        f"       -> {new_start} .. {new_end}",
        "",
        f"  {'':<18}{'recorded':>12}{'valid':>12}",
        f"  {'duration_s':<18}{dur:>12,.0f}{valid_s:>12,.0f}",
        f"  {'distance_m':<18}{dist:>12,.0f}{vdist:>12,.0f}",
        f"  {'mean speed m/s':<18}{rp:>12.3f}{vp:>12.3f}",
    ]
    if rp and vp:
        lines.append(
            f"  {'pace /mi':<18}"
            f"{pace_per_mile(rp, '--:--'):>12}"
            f"{pace_per_mile(vp, '--:--'):>12}"
        )
    lines += [
        "",
        "  mid-run stops are NOT trimmed (§4.3); bronze rows untouched",
        "  valid = a PREVIEW over the stored records; the window takes "
        "effect at re-ingest",
    ]
    return lines


def confirm_window(activity_id: str) -> None:
    """Confirm the detector's suggested window for an INGESTED activity.

    A THIN WRAPPER OVER confirm_file. This used to UPDATE
    activities.valid_* and INSERT into window_confirmations directly, and said
    the change took effect "without a re-ingest". Since the window-trim commit
    that is a HALF-APPLIED window: the window is applied in build_rows, so
    records, splits and session totals stayed whole-file while two columns
    claimed otherwise -- the defect that commit fixed, re-created by the one
    command that was not updated. Now it writes the confirmation to the
    durable file through the same path as confirm-file, and the activity
    changes at the next ingest, which is the only place a window is applied.

    Detection re-runs from the raw file, so the suggestion is deterministic.
    Only LEADING and TRAILING junk narrows the window -- a mid-run stop is
    training data and is never trimmed (§4.3).
    """
    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        activity_id = resolve_activity_id(con, activity_id)
        source_file, file_hash, vs, ve, dur, dist = con.execute(
            "SELECT source_file, file_hash, valid_start_utc, valid_end_utc, "
            "duration_s, total_distance_m FROM activities "
            "WHERE activity_id = ?",
            [activity_id],
        ).fetchone()
        path = Path(source_file)
        s = suggest_window(path)
        new_end = (
            s["suggested_valid_end"] if s["suggested_valid_end"] is not None else ve
        )
        new_start = (
            s["suggested_valid_start"] if s["suggested_valid_start"] is not None else vs
        )
        if (new_end, new_start) == (ve, vs):
            print("no trailing or leading stop detected; window unchanged")
            return
        # THE FILE ON DISK IS THE FILE THAT WAS INGESTED. Raw files are
        # immutable (§3), so a mismatch is a finding, not a case to handle.
        if s["file_sha256"] != file_hash:
            raise SystemExit(
                f"{path.name} hashes to {s['file_sha256'][:12]} but activity "
                f"{activity_id} was ingested from {file_hash[:12]}. Raw files "
                f"are immutable; nothing was written."
            )
        # Report FIRST. The write is the last thing that happens, so a
        # failure here leaves the stored confirmation untouched (§4.3).
        lines = _window_report(con, activity_id, vs, ve, dur, dist, new_start, new_end)
    finally:
        con.close()

    # EVERY BOUND THAT DIFFERS FROM THE FILE'S OWN SPAN IS PASSED, not only
    # the side the detector suggested: a None would read as "no bound" and
    # silently drop a start the athlete confirmed earlier.
    out = _confirm_path(
        path,
        valid_end=new_end if new_end != s["file_end_utc"] else None,
        valid_start=new_start if new_start != s["file_start_utc"] else None,
        note="confirmed via confirm-window",
        source="confirm-window",
        suggestion=s,
    )
    print("\n".join(lines))
    print(
        f"  confirmed {out['file']} (revision {out['revision']}); "
        f"activities is UNCHANGED until re-ingest"
    )
    print(f"  next: {out['next']}")


def outstanding_confirmations(con) -> list:
    """Flags that need a human decision: a suggested window with no confirmation."""
    return con.execute("""
        SELECT v.activity_id, a.source_file, v.check_name, v.value_observed, v.message
        FROM validation_results v
        JOIN activities a USING (activity_id)
        LEFT JOIN window_confirmations w ON w.file_sha256 = a.file_hash
        WHERE v.check_name IN ('trailing_stop', 'leading_stop')
          AND w.file_sha256 IS NULL
        ORDER BY v.value_observed DESC
    """).fetchall()


def add_note(run_id: str, note: str) -> None:
    """Free-text note on a run (§4 side tables).

    FREE TEXT ONLY, deliberately: an observation in the athlete's own words.
    A structured per-run channel would be a second place to record the same
    fact, and two places can disagree.
    """
    import datetime

    import duckdb

    from trail_factors.ingest.athlete_state import save_note

    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        known = con.execute("SELECT 1 FROM runs WHERE run_id = ?", [run_id]).fetchone()
    finally:
        con.close()
    if known is None:
        raise SystemExit(f"no run {run_id}; `verify` lists them")
    save_note(run_id, note, datetime.datetime.now(datetime.UTC).replace(tzinfo=None))
    print(f"noted on {run_id}: {note!r}")


# --------------------------------------------------------------------------
# CONFIRM BY FILE, NOT BY ACTIVITY (§4.3)
#
# WHY A SECOND PATH. `confirm_window` resolves an activity_id, so it can only
# confirm a window for a file that ALREADY INGESTED. The files that need a
# window most are the ones that did not: a run whose tail is a car ride fails
# validation, gets no activity row, and was therefore unreachable by the only
# tool that could have rescued it. Several real files sat in that state.
#
# So this resolves a RAW FILE -- by Garmin activity id or by file sha256 --
# and writes the confirmation without needing the corpus to contain it.
# --------------------------------------------------------------------------

RAW_GARMIN = RAW_DIR / "garmin"

# How every instruction this module prints spells the CLI, so a reader can
# paste it. One constant, so the spelling cannot drift between messages.
CLI = "uv run trail-factors"


def resolve_raw_file(ref: str) -> Path:
    """A raw .fit from a Garmin activity id or a file sha256 (full or prefix).

    Raises LookupError naming what it tried, never returns a guess.
    """
    from trail_factors.ingest import fit_file as ff

    ref = (ref or "").strip()
    if not ref:
        raise LookupError("give a Garmin activity id or a file sha256")
    by_id = RAW_GARMIN / f"{ref}_ACTIVITY.fit"
    if by_id.exists():
        return by_id
    # THE SHA SEARCH COVERS THE WHOLE RAW TREE, not only garmin/: a rejected
    # file outside it has no Garmin id, so its rejection names it by sha, and
    # that command must resolve.
    hits = [
        p
        for p in sorted(RAW_DIR.rglob("*.fit"))
        if ff.sha256(p).startswith(ref.lower())
    ]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        raise LookupError(
            f"sha prefix {ref!r} matches {len(hits)} files: "
            f"{', '.join(p.name for p in hits)}. Give more characters."
        )
    raise LookupError(
        f"no raw file matches {ref!r}. Tried {by_id.name} and a sha256 prefix "
        f"across {len(list(RAW_DIR.rglob('*.fit')))} files in "
        f"{display_path(RAW_DIR)}."
    )


def suggest_window(path: Path) -> dict:
    """What the detectors propose for one raw file. Never applied here.

    TWO DETECTORS, because a junk edge comes in two kinds. A forgotten stop
    is STILL (detect_movement_ceased); a car ride is FAST
    (suggest_vehicle_window), and the stop detector cannot see it -- which
    is how a run ending in a drive used to be rejected with nothing to
    confirm. Where both suggest a bound, the TIGHTER one wins (the earlier
    end, the later start): a window only ever removes, and the tighter bound
    is the one that removes both kinds of junk. `suggested_basis` says which
    detector produced each bound, so the reader can check it against what
    actually happened.

    A SUGGESTION IS VERIFIED BEFORE ANYONE IS TOLD TO CONFIRM IT.
    `suggested_window_remaining_blocks` re-runs the fatal check's own
    sustained-block rule on the records the suggested window keeps -- the
    same inclusive filter build_rows applies. An edge window over a file that
    also has a mid-run drive leaves that drive inside it, and confirming it
    would only earn the same rejection on the next sync. The vehicle `cut`
    (the bound that removes every block, at a stated on-foot cost) is
    verified the same way. None where there is no window to verify.
    """
    from trail_factors.ingest import fit_file as ff
    from trail_factors.ingest.validate import (
        detect_movement_ceased,
        suggest_vehicle_window,
        sustained_impossible_blocks,
    )

    msgs = ff.read_messages(path)
    recs = ff.coalesce_records(msgs.get("record", []))
    sport = (msgs.get("session") or [{}])[0].get("sport")
    stop = detect_movement_ceased(recs)
    car = suggest_vehicle_window(recs, sport)

    def pick(key, edge, tighter):
        cands = [
            (ff.to_utc_naive(m[key]), basis)
            for m, basis in ((stop, f"{edge} stop"), (car, f"{edge} vehicle speed"))
            if m[key] is not None
        ]
        return tighter(cands, key=lambda c: c[0]) if cands else (None, None)

    end, end_basis = pick("suggested_valid_end", "trailing", min)
    start, start_basis = pick("suggested_valid_start", "leading", max)

    def remaining(lo, hi):
        kept = [
            r
            for r in recs
            if (lo is None or ff.to_utc_naive(r["timestamp"]) >= lo)
            and (hi is None or ff.to_utc_naive(r["timestamp"]) <= hi)
        ]
        return len(sustained_impossible_blocks(kept, sport))

    cut = None
    if car["cut"] is not None:
        at = ff.to_utc_naive(car["cut"]["at"])
        cut = {
            "bound": car["cut"]["bound"],
            "at": at,
            "discards_on_foot_s": car["cut"]["discards_on_foot_s"],
            "remaining_blocks": (
                remaining(None, at)
                if car["cut"]["bound"] == "end"
                else remaining(at, None)
            ),
        }
    return {
        "file": path.name,
        "file_sha256": ff.sha256(path),
        "n_records": len(recs),
        "file_start_utc": ff.to_utc_naive(recs[0]["timestamp"]) if recs else None,
        "file_end_utc": ff.to_utc_naive(recs[-1]["timestamp"]) if recs else None,
        "suggested_valid_start": start,
        "suggested_valid_end": end,
        "suggested_basis": {"start": start_basis, "end": end_basis},
        "suggested_window_remaining_blocks": (
            remaining(start, end) if start is not None or end is not None else None
        ),
        "vehicle_blocks": car["n_blocks"],
        "vehicle_midrun_blocks": car["midrun_blocks"],
        "vehicle_on_foot_records": car["n_on_foot_records"],
        "vehicle_has_cadence": car["has_cadence"],
        "vehicle_cut": cut,
    }


def confirm_file(
    ref: str,
    valid_end: str = "",
    valid_start: str = "",
    note: str = "",
    source: str = "cli",
) -> dict:
    """Confirm a valid window for a RAW FILE, ingested or not.

    Each bound comes from, in order: the explicit argument, the file's
    CURRENT stored confirmation, then the detector's suggestion. An explicit
    bound always wins: the detector is a suggestion and the athlete is the
    authority (§4.3) -- the moment a car door closed is a fact the athlete
    knows and no record can carry. A stored bound outranks the detector for
    the same reason: it is something the athlete already said.
    """
    return _confirm_path(
        resolve_raw_file(ref),
        valid_end=valid_end,
        valid_start=valid_start,
        note=note or f"confirmed by file reference {ref!r}",
        source=source,
    )


def _confirm_path(
    path: Path,
    valid_end=None,
    valid_start=None,
    note: str = "",
    source: str = "cli",
    suggestion: dict | None = None,
) -> dict:
    """The ONE write path for a window confirmation: validate, then store.

    Shared by confirm-file (resolves a raw file) and confirm-window (an
    ingested activity's own source_file), so the two cannot disagree about
    what a valid window is. A bound may be a string or a datetime; an empty
    or None bound falls back to the file's CURRENT stored confirmation, and
    only then to the detector's suggestion.

    THE STORED BOUND COMES BEFORE THE DETECTOR. Confirming `--end` and later
    `--start` alone used to fill the end from the detector, replacing the
    athlete's own end -- or dropping it to None where the detector suggests
    no end, which ingests the tail on the next sync. `bound_source` says
    where each bound came from, so the reader can see which were kept.
    """
    from datetime import datetime

    from trail_factors.ingest.athlete_state import (
        _dt,
        load_confirmations,
        save_confirmation,
    )

    s = suggestion if suggestion is not None else suggest_window(path)
    stored = next(
        (r for r in load_confirmations() if r.get("file_sha256") == s["file_sha256"]),
        None,
    )

    def resolve(given, key):
        if given not in (None, ""):
            return _parse_utc(given), "argument"
        if stored is not None and stored.get(f"valid_{key}_utc") is not None:
            rev = stored.get("revision")
            return (
                _dt(stored[f"valid_{key}_utc"]),
                "stored confirmation"
                + (f" (revision {rev})" if rev is not None else ""),
            )
        v = s[f"suggested_valid_{key}"]
        if v is None:
            return None, None
        basis_map = s.get("suggested_basis")
        basis = basis_map.get(key) if basis_map is not None else None
        return v, "detector" + (f" ({basis})" if basis else "")

    end, end_src = resolve(valid_end, "end")
    start, start_src = resolve(valid_start, "start")
    if end is None and start is None:
        raise ValueError(
            f"{path.name}: no window to confirm -- the detector suggests "
            f"neither a start nor an end, and none was given. Pass "
            f"valid_end (or valid_start) explicitly."
        )
    for label, v in (("valid_start", start), ("valid_end", end)):
        if v is not None and not (s["file_start_utc"] <= v <= s["file_end_utc"]):
            raise ValueError(
                f"{label} {v} is outside the file's own span "
                f"{s['file_start_utc']} .. {s['file_end_utc']}. A window must "
                f"select from the file, not extend it."
            )
    if start is not None and end is not None and start >= end:
        raise ValueError(f"valid_start {start} is not before valid_end {end}")
    entry = save_confirmation(
        s["file_sha256"],
        start,
        end,
        datetime.now(UTC).replace(tzinfo=None),
        note=note,
        source=source,
    )
    return {
        **s,
        "confirmed_valid_start": start,
        "confirmed_valid_end": end,
        "bound_source": {"start": start_src, "end": end_src},
        "revision": entry["revision"],
        "next": (
            f"re-ingest: run `{CLI} sync` -- it re-ingests every "
            f"raw file, so this one is ingested at the confirmed "
            f"window (or "
            f"`{CLI} ingest {display_path(path)}` for this file "
            f"alone; the terrain steps dem -> silver -> match -> "
            f"recovery follow in the MCP sync job or as their own "
            f"commands)"
        ),
    }


def _parse_utc(v):
    from datetime import datetime

    if v in (None, ""):
        return None
    if isinstance(v, datetime):
        return v
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M",
    ):
        try:
            return datetime.strptime(str(v), fmt)
        except ValueError:
            continue
    raise ValueError(f"{v!r} is not a UTC timestamp like '2025-01-15 08:30:00'")


def rejected_files(con) -> dict:
    """Files with validation findings and NO activity row (§15.4).

    A SEPARATE READ FROM sync_status, deliberately: a rejection PERSISTS
    across syncs -- a rejected file survives every sync until it is dealt
    with -- while sync_status describes one job and disappears with it. A rejection
    that only showed up in the job that produced it is a rejection nobody sees
    again.
    """
    from trail_factors.ingest import fit_file as ff

    rows = con.execute("""
        SELECT file_sha256, run_id,
               string_agg(DISTINCT check_name, ', ') AS checks,
               string_agg(DISTINCT severity, ', ') AS severities,
               count(*) AS n_findings, max(checked_at) AS last_checked
        FROM validation_results
        WHERE activity_id IS NULL
        GROUP BY 1, 2 ORDER BY 6 DESC
    """).fetchall()
    # ONE pass over the raw files, not one per rejection. setdefault keeps
    # the first path in sorted order, which is what the old per-row scan
    # returned for a sha that appears twice.
    #
    # THE WHOLE RAW TREE, as resolve_raw_file and ingest_all walk it. This
    # scanned only garmin/, so a rejected file elsewhere (the phone file)
    # was listed with no reason, no next step and no command -- in the one
    # list the athlete is told to read.
    path_by_sha: dict[str, Path] = {}
    if rows:
        for p in sorted(RAW_DIR.rglob("*.fit")):
            path_by_sha.setdefault(ff.sha256(p), p)
    out = []
    for sha, run_id, checks, sevs, n, last in rows:
        path = path_by_sha.get(sha)
        # A Garmin id exists only for garmin/<id>_ACTIVITY.fit; anything else
        # is named by its sha prefix (raw_ref), never by a guessed id.
        gid = (
            path.name.split("_")[0]
            if path is not None
            and path.parent == RAW_GARMIN
            and path.name.endswith("_ACTIVITY.fit")
            else None
        )
        rec = {
            "file_sha256": sha,
            "run_id": run_id,
            "checks": checks,
            "severities": sevs,
            "n_findings": n,
            "last_checked": str(last),
            "file": path.name if path else None,
            "garmin_id": gid,
            "ref": raw_ref(path) if path is not None else None,
            "messages": [
                r[0]
                for r in con.execute(
                    "SELECT message FROM validation_results WHERE "
                    "file_sha256 = ? AND activity_id IS NULL",
                    [sha],
                ).fetchall()
            ],
        }
        if path is not None:
            check_names = [c.strip() for c in (checks or "").split(",") if c.strip()]
            advice = rejection_advice(path, "; ".join(rec["messages"]), check_names)
            rec.update(
                reason=advice["reason"],
                next_step=advice["next_step"],
                fixable_by_window=advice["fixable_by_window"],
                cut_discards_on_foot_s=advice["cut_discards_on_foot_s"],
            )
            if advice.get("suggest_error"):
                rec["suggested_window"] = None
                rec["suggest_error"] = advice["suggest_error"]
            else:
                s = advice["suggestion"]
                rec["suggested_window"] = {
                    "valid_start": _ts(s["suggested_valid_start"]),
                    "valid_end": _ts(s["suggested_valid_end"]),
                    "basis": s["suggested_basis"],
                    "file_span": [_ts(s["file_start_utc"]), _ts(s["file_end_utc"])],
                    "n_records": s["n_records"],
                }
            # The EXACT command, with the suggested bound filled in -- or no
            # command at all where a window cannot fix the cause. A
            # placeholder such as '<UTC timestamp>' is an instruction the
            # reader must complete by guessing.
            rec["confirm_with"] = advice["command"]
        out.append(rec)
    return {
        "rejected": out,
        "n_rejected": len(out),
        "note": (
            "these files are in the raw directory and have validation "
            "findings but NO activity row -- they did not ingest. A "
            "rejection persists across syncs; it is not a job event."
        ),
        "how_to_clear": (
            "confirm a window with `confirm-file` where the "
            "problem is a leading or trailing block (a car "
            "ride, a forgotten stop), then re-run `sync`. "
            "Where the file is genuinely corrupt, or the "
            "impossible stretch is mid-run, there is no "
            "masking mechanism and it stays here."
        ),
    }


# --------------------------------------------------------------------------
# A REJECTION NAMES ITS REASON AND ITS NEXT STEP (§15.4)
#
# THE DEFECT. `sync` printed every rejected file under one line, "Corrupt
# beyond use; nothing to do unless you want them re-exported" -- true of a
# file with corrupt timestamps, and FALSE of a run whose tail is a car ride,
# which one confirmed window rescues. The reader was told to give up on
# exactly the files the window machinery exists for.
#
# So each rejection is classified by the CHECK that fired (carried on the
# ValidationError, never parsed back out of its text), and gets a plain
# reason, the next step, and -- where a window can fix it -- the exact command
# with the suggested bound filled in.
# --------------------------------------------------------------------------

# Plain words for each FATAL check: what the file shows, not what the code did.
REJECTION_REASONS = {
    "pace_sustained_impossible": (
        "it moves faster than any running pace (4:00/mi) for a sustained "
        "stretch -- almost always the watch still recording in a vehicle"
    ),
    "timestamp_integrity": (
        "its timestamps are corrupt: a gap or a total span no single activity can have"
    ),
    "coordinate_range": (
        "its positions fall outside the valid degree range -- a file or "
        "conversion fault, not the run itself"
    ),
    "distance_monotonic": "its distance channel runs backwards",
    "confirmed_window_empty": (
        "the window confirmed for it selects none of its records"
    ),
}

# What to check where NO window can fix the cause. TEMPLATES: `{ref}` is
# filled with the file's own reference when the advice is built, so the
# printed command names the file rather than a placeholder to guess at.
NO_WINDOW_STEP = {
    "timestamp_integrity": (
        "check the activity in Garmin Connect. If the bad gap is at the very "
        f"start or end of the recording, `{CLI} confirm-file {{ref}} --end "
        "'YYYY-MM-DD HH:MM:SS'` (a UTC time before a gap at the end) or "
        "`--start 'YYYY-MM-DD HH:MM:SS'` (after a gap at the start) can "
        "exclude it; a gap or span in the middle cannot be repaired by a "
        "window, and the file stays rejected"
    ),
    "coordinate_range": (
        "a window cannot repair this. Check the activity in Garmin Connect; "
        "if it is wrong there too, the file stays rejected. The raw file is "
        "never edited in place"
    ),
    "distance_monotonic": (
        "a window cannot repair this. Check the activity in Garmin Connect; "
        "if it is wrong there too, the file stays rejected. The raw file is "
        "never edited in place"
    ),
}


def _ts(v) -> str | None:
    """A timestamp exactly as confirm-file parses it, or None."""
    return v.strftime("%Y-%m-%d %H:%M:%S") if v is not None else None


def raw_ref(path: Path) -> str:
    """How a command names a raw file: its Garmin id where the file is
    garmin/<id>_ACTIVITY.fit, else a sha256 prefix (resolve_raw_file takes
    either)."""
    name = Path(path).name
    if Path(path).parent == RAW_GARMIN and name.endswith("_ACTIVITY.fit"):
        return name.split("_")[0]
    from trail_factors.ingest import fit_file as ff

    try:
        return ff.sha256(Path(path))[:16]
    except OSError:
        # An UNREADABLE file still gets advice -- the advice must never hide
        # the rejection. Its name is the only handle left; resolve_raw_file
        # will say plainly if it cannot find it.
        return name


def rejection_advice(path: Path, message: str, checks=()) -> dict:
    """Reason, next step and exact command for one rejected file.

    `checks` are the FATAL check names (ValidationError.checks). Returns
    {ref, file, checks, reason, fixable_by_window, next_step, command,
    then, list_command, suggestion, suggest_error, cut_discards_on_foot_s}.

    `fixable_by_window` is True only where the suggested window removes junk
    alone AND was verified to leave no impossible block inside it; None where
    it cannot be known from the file (unreadable, unplaceable, or a timestamp
    gap whose position decides it). A `command` beside a False is a CUT: it
    clears the file at the on-foot cost in `cut_discards_on_foot_s`, which
    the next step states. `command` is None where no window can fix the
    cause, and `next_step` then says what to check instead.
    """
    from trail_factors.ingest.validate import VEHICLE_EDGE_FOOT_MAX_S

    path = Path(path)
    checks = [c for c in checks if c]
    ref = raw_ref(path)
    reasons = [REJECTION_REASONS[c] for c in checks if c in REJECTION_REASONS]
    reason = (
        "; ".join(reasons)
        if reasons
        else (
            f"validation rejected it: {message}"
            if message
            else "validation rejected it"
        )
    )
    out = {
        "ref": ref,
        "file": path.name,
        "checks": checks,
        "reason": reason,
        "fixable_by_window": False,
        "next_step": "",
        "command": None,
        "then": f"{CLI} sync",
        "list_command": f"{CLI} rejected-files",
        "suggestion": None,
        "suggest_error": None,
        "cut_discards_on_foot_s": None,
    }
    try:
        s = suggest_window(path)
        out["suggestion"] = s
    except Exception as exc:  # advice must never hide the rejection
        s = None
        out["suggest_error"] = f"{type(exc).__name__}: {exc}"

    pace = "pace_sustained_impossible" in checks
    unfixable = [c for c in checks if c in NO_WINDOW_STEP]
    bounds, where = [], ""
    if s is not None:
        if s["suggested_valid_end"] is not None:
            bounds.append(f"--end '{_ts(s['suggested_valid_end'])}'")
        if s["suggested_valid_start"] is not None:
            bounds.append(f"--start '{_ts(s['suggested_valid_start'])}'")
        basis = s["suggested_basis"]
        where = ", ".join(
            f"{k} {_ts(s[f'suggested_valid_{k}'])} UTC ({basis[k]})"
            for k in ("start", "end")
            if s[f"suggested_valid_{k}"] is not None
        )
    # Blocks the suggested window would KEEP (suggest_window re-ran the fatal
    # check's own rule on its records). Only a 0 clears the file.
    remaining = s["suggested_window_remaining_blocks"] if bounds else None
    suggested_cmd = f"{CLI} confirm-file {ref} {' '.join(bounds)}"
    # A cut is offered only once it, too, verified clean.
    cut = s["vehicle_cut"] if s is not None else None
    if cut is not None and cut["remaining_blocks"] != 0:
        cut = None

    def offer_cut(lead: str) -> None:
        side = "after" if cut["bound"] == "end" else "before"
        out.update(
            fixable_by_window=False,
            cut_discards_on_foot_s=cut["discards_on_foot_s"],
            command=(f"{CLI} confirm-file {ref} --{cut['bound']} '{_ts(cut['at'])}'"),
            next_step=(
                f"{lead}. The window `--{cut['bound']} '{_ts(cut['at'])}'` "
                f"(UTC) removes every impossible stretch "
                f"({s['vehicle_blocks']} block(s)) and everything {side} it, "
                f"DISCARDING {cut['discards_on_foot_s']:.0f} s of on-foot "
                f"movement recorded {side} that time. Confirm it only if that "
                f"movement is not part of the run (a walk from a parked car "
                f"is not); otherwise there is no mid-run masking and the file "
                f"stays rejected. Then run `{CLI} sync`"
            ),
        )

    if unfixable:
        steps = "; ".join(NO_WINDOW_STEP[c].format(ref=ref) for c in unfixable)
        if unfixable == ["timestamp_integrity"] and pace and remaining == 0:
            # THE VEHICLE SUGGESTION IS NOT HIDDEN BEHIND THE TIMESTAMP CHECK.
            # A drive at the end can also be what breaks the timestamps, and
            # then the one window fixes both -- but only the person who can
            # see where the bad gap sits can say so.
            out.update(
                fixable_by_window=None,
                command=suggested_cmd,
                next_step=(
                    f"{steps}. The impossible pace has a suggested window "
                    f"({where}) that removes it; confirming it fixes the file "
                    f"ONLY IF the bad timestamp gap also lies outside that "
                    f"window -- check where the gap is first, then run "
                    f"`{CLI} sync`"
                ),
            )
        else:
            out["next_step"] = steps
    elif bounds and remaining == 0:
        out.update(
            fixable_by_window=True,
            command=suggested_cmd,
            next_step=(
                f"confirm a recording window that excludes it -- suggested "
                f"{where}. Check it against when you actually stopped (or "
                f"started); if it is wrong, pass your own --end/--start in "
                f"UTC. Then run `{CLI} sync` to ingest the trimmed run"
            ),
        )
    elif bounds:
        # AN EDGE WINDOW OVER A FILE WITH A MID-RUN BLOCK. Printing it as the
        # fix made a loop: confirm, sync, the same rejection, the same
        # command, revision after revision.
        lead = (
            f"the suggested window ({where}) removes the stretch at the "
            f"edge of the recording, but {remaining} impossible block(s) "
            f"remain inside it, so confirming it alone would NOT clear "
            f"the file"
        )
        if cut is not None:
            offer_cut(lead)
        else:
            out["next_step"] = (
                f"{lead}. No single start/end window removes them all, and "
                f"there is no mid-run masking: the file stays rejected"
            )
    elif s is None:
        # THE DETECTOR COULD NOT READ THE FILE, so nothing is known about
        # WHERE the bad stretch sits. Saying "it is mid-run, nothing to do"
        # would assert a cause that was never checked and strand a run a
        # window could rescue; the honest step is the command with the times
        # left for the person who knows them.
        out.update(
            fixable_by_window=None,
            command=(f"{CLI} confirm-file {ref} --end 'YYYY-MM-DD HH:MM:SS'"),
            next_step=(
                "no window could be suggested (the detector could not read "
                "the file). If the bad stretch is at the start or end of the "
                "recording, confirm the window that IS the run yourself, in "
                "UTC (--start trims the front); a stretch in the middle "
                "cannot be removed by a window"
            ),
        )
    elif pace and cut is not None:
        n_mid = s["vehicle_midrun_blocks"]
        offer_cut(
            f"the impossible stretch sits in the MIDDLE of the recording by "
            f"the detector's rule ({n_mid} block(s) with more than "
            f"{VEHICLE_EDGE_FOOT_MAX_S:.0f} s of on-foot movement on each "
            f"side), so no window was proposed automatically"
            if n_mid
            else "no window was proposed automatically"
        )
    elif pace:
        # THE DETECTOR COULD NOT PLACE THE STRETCH. Every cause below is
        # measured on this file; "mid-run" is not one of them, because with
        # nothing on foot to anchor a bound nobody has looked at where it is.
        raw_cut = s["vehicle_cut"]
        if s["vehicle_blocks"] == 0:
            why = "it found no sustained impossible block in the file as a whole"
        elif s["vehicle_has_cadence"] is False:
            why = (
                "the file has no cadence channel, so no record can be told "
                "apart from a vehicle as being on foot"
            )
        elif s["vehicle_on_foot_records"] == 0:
            why = (
                "no record reads as on foot (steps at a speed between "
                "standing and the running bound)"
            )
        elif raw_cut is not None:
            why = (
                f"the window at {_ts(raw_cut['at'])} still kept "
                f"{raw_cut['remaining_blocks']} impossible block(s) when "
                f"checked"
            )
        else:
            why = (
                "no on-foot record lies before the first impossible "
                "stretch or after the last one"
            )
        out.update(
            fixable_by_window=None,
            command=(f"{CLI} confirm-file {ref} --end 'YYYY-MM-DD HH:MM:SS'"),
            next_step=(
                f"the detector could not place the impossible stretch: {why}. "
                f"If it is at the start or end of the recording, confirm the "
                f"window that IS the run yourself, in UTC (--end before a "
                f"drive at the end, --start after one at the beginning); a "
                f"stretch in the middle cannot be removed by a window"
            ),
        )
    elif "confirmed_window_empty" in checks:
        out["next_step"] = (
            f"confirm a corrected window: `{CLI} confirm-file {ref} --end "
            f"'YYYY-MM-DD HH:MM:SS'` with a UTC time inside the file's span "
            f"(a new confirmation revises the old one), then run `{CLI} sync`"
        )
    else:
        out["next_step"] = (
            "no window suggestion fits this cause; check the activity in "
            "Garmin Connect. If the junk is at the very end, "
            f"`{CLI} confirm-file {ref} --end 'YYYY-MM-DD HH:MM:SS'` can "
            "exclude it (or `--start 'YYYY-MM-DD HH:MM:SS'` at the very "
            "start, in UTC); otherwise it stays rejected"
        )
    return out


def advice_lines(a: dict, indent: str = "    ") -> list[str]:
    """The printed form of one rejection_advice, shared by ingest-all and
    sync so the two cannot word the same rejection differently."""
    lines = [
        f"{indent}reason:    {a['reason']}",
        f"{indent}next step: {a['next_step']}",
    ]
    if a["command"]:
        lines += [
            f"{indent}run:       {a['command']}",
            f"{indent}then:      {a['then']}",
        ]
    if a.get("suggest_error"):
        lines.append(f"{indent}(no window suggestion: {a['suggest_error']})")
    return lines
