"""Athlete-supplied state, persisted outside the database (CLAUDE.md).

`window_confirmations` living only in DuckDB was not enough: deleting
data/ultra.duckdb destroys it, and that is a routine operation. Derived data
is rebuilt from data/raw/; athlete input cannot be, so its durable home is a
file that no rebuild touches.

The file holds SHA-256 hashes and timestamps only — no coordinates.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from trail_factors.paths import ATHLETE_DIR

STATE_DIR = ATHLETE_DIR  # repo-root anchored (src/trail_factors/paths.py)
CONFIRMATIONS = STATE_DIR / "window_confirmations.json"


def load_json(path: Path):
    """The file's contents, or {} when it does not exist yet.

    Never creates the file. An absent athlete-state file means "nothing has
    been logged", which is a different fact from an empty one and needs no
    write to express.
    """
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def atomic_write(path: Path, obj) -> None:
    """Write a temp file and os.replace it into position.

    THE STANDING RULE FOR THIS DIRECTORY, and it had not reached this module.
    `jobs.json` needed it first, then the other athlete-state logs -- and the
    files written HERE (window_confirmations, segment names, run notes) were
    still going out through a plain `write_text` long after. A plain write interrupted
    or read mid-flight leaves
    unparseable JSON and takes every entry with it, and `data/athlete/` is the
    one directory that cannot be rebuilt from anything.

    os.replace is atomic on POSIX, so a concurrent reader sees the old file or
    the new one and never half of one. The pid in the temp name keeps two
    processes off the same scratch file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(obj, indent=2))
    os.replace(tmp, path)


def revise(previous: dict | None, entry: dict) -> dict:
    """Stamp `entry` with its revision, shedding `previous` into its history.

    THE BOOKKEEPING EVERY REVISIONED WRITER SHARES. The supersede POLICY stays
    with each writer -- what counts as a correction is the writer's call -- but
    "keep the old one, count the new one" is the same everywhere, and a third
    copy of it is a third place for it to drift.

    THE SHED IS EXCLUSION-BASED ON PURPOSE: every key the previous entry
    carried survives into history except its own history. An explicit key list
    would silently drop each new field as it is added, and the history is
    evidence, not a summary of it.
    """
    entry["revision"] = 0
    if previous is not None:
        history = list(previous.get("superseded", []))
        shed = {k: v for k, v in previous.items() if k != "superseded"}
        entry["superseded"] = history + [shed]
        entry["revision"] = len(history) + 1
    return entry


def _iso(v):
    return v.isoformat() if isinstance(v, datetime) else v


def _dt(v):
    return datetime.fromisoformat(v) if isinstance(v, str) else v


def save_confirmation(
    file_sha256, valid_start, valid_end, confirmed_at, note="", source="cli"
):
    """Record one confirmed window, KEEPING any it replaces.

    IT USED TO REPLACE WITHOUT HISTORY. `rows = [r for r in rows if sha != ...]`
    then append: a corrected window destroyed the one before it, in the one
    directory that cannot be rebuilt from anything. A confirmed window IS an
    athlete statement about a file -- the same class of thing as a segment
    name -- so it revises like one, through the shared `revise()`.

    `source` and `confirmed_at` are stored because a window confirmed through
    the chat and one confirmed at the CLI are the same datum from different
    places, and a reader deciding whether to trust an old confirmation wants
    to know which.
    """
    rows = load_confirmations()
    previous = next((r for r in rows if r["file_sha256"] == file_sha256), None)
    entry = {
        "file_sha256": file_sha256,
        "valid_start_utc": _iso(valid_start),
        "valid_end_utc": _iso(valid_end),
        "confirmed_at": _iso(confirmed_at),
        "source": source,
        "note": note,
    }
    revise(previous, entry)
    rows = [r for r in rows if r["file_sha256"] != file_sha256] + [entry]
    rows.sort(key=lambda r: r["file_sha256"])
    atomic_write(CONFIRMATIONS, rows)
    return entry


def load_confirmations() -> list[dict]:
    if not CONFIRMATIONS.exists():
        return []
    return json.loads(CONFIRMATIONS.read_text())


SEGMENT_NAMES = STATE_DIR / "segment_names.json"


def load_segment_names() -> list[dict]:
    """Athlete-supplied segment names, each held by a RUN-ANCHORED anchor.

    NOT keyed on segment_id: that id is founder-derived, so backfilling an
    older run re-founds the segment and changes it. The anchor -- (run_id,
    direction, at_distance_m), a point inside one traversal on a run whose raw
    file never changes -- RESOLVES to whichever segment that traversal belongs
    to now (src/trail_factors/analysis/venues.resolve_anchor).

    COORDINATE-FREE (§19.3). The store this replaced kept the peak's lat/lon
    and projected place x/y in the athlete-state directory; it was removed
    before any name was ever saved. An entry
    carrying a position is refused on READ as well as on write.
    """
    from trail_factors.analysis.venues import validate_anchor

    if not SEGMENT_NAMES.exists():
        return []
    rows = json.loads(SEGMENT_NAMES.read_text())
    for r in rows:
        validate_anchor(r["anchor"])
    return rows


def save_segment_name(
    name: str, anchor: dict, named_at, previous: dict | None = None
) -> dict:
    """Name the slope `anchor` resolves to. `previous` is the existing entry
    that resolves to the SAME segment, found by the caller (which holds the
    database); it is revised, not duplicated."""
    from trail_factors.analysis.venues import validate_anchor

    entry = {
        "name": name,
        "anchor": validate_anchor(anchor),
        "named_at": _iso(named_at),
    }
    revise(previous, entry)
    rows = [
        r
        for r in load_segment_names()
        if previous is None or r["anchor"] != previous["anchor"]
    ]
    rows.append(entry)
    rows.sort(key=lambda r: (r["anchor"]["run_id"], r["anchor"]["at_distance_m"]))
    atomic_write(SEGMENT_NAMES, rows)
    return entry


VENUES = STATE_DIR / "venues.json"


def load_venues() -> list[dict]:
    """§19 venues: name -> run-anchored anchors. Coordinate-free, like segment
    names, and validated on read."""
    from trail_factors.analysis.venues import validate_anchor

    if not VENUES.exists():
        return []
    rows = json.loads(VENUES.read_text())
    for v in rows:
        for a in v["anchors"]:
            validate_anchor(a)
    return rows


def save_venue_anchor(venue: str, anchor: dict, at) -> dict:
    """Add one anchor to a venue, creating the venue if new. Revisioned: the
    venue's previous anchor list is kept in its history."""
    from trail_factors.analysis.venues import validate_anchor

    anchor = validate_anchor(anchor)
    rows = load_venues()
    previous = next((v for v in rows if v["name"] == venue), None)
    anchors = list(previous["anchors"]) if previous else []
    if anchor in anchors:
        raise ValueError(f"{venue!r} already has this anchor")
    entry = {"name": venue, "anchors": anchors + [anchor], "updated_at": _iso(at)}
    revise(previous, entry)
    rows = [v for v in rows if v["name"] != venue] + [entry]
    rows.sort(key=lambda v: v["name"])
    atomic_write(VENUES, rows)
    return entry


NOTES = STATE_DIR / "notes.json"


def load_notes() -> list[dict]:
    """Free-text run notes. NOT a structured channel.

    Free text only: a structured per-run record here would be a second place
    to state the same fact, and two places can disagree.
    """
    if not NOTES.exists():
        return []
    return json.loads(NOTES.read_text())


def save_note(run_id: str, note: str, noted_at) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    rows = load_notes()
    rows.append({"run_id": run_id, "note": note, "noted_at": _iso(noted_at)})
    rows.sort(key=lambda r: (r["run_id"], r["noted_at"]))
    atomic_write(NOTES, rows)


def restore_annotations(con) -> int:
    """Load free-text notes into `annotations` (§4 side tables).

    ATHLETE-SUPPLIED STATE. The durable home is data/athlete/notes.json; this
    table is a queryable projection rebuilt on every schema ensure. A rebuild
    that silently discarded athlete input would be the records_valid failure
    class (CLAUDE.md).
    """
    rows = load_notes()
    con.execute("DELETE FROM annotations")
    for r in rows:
        con.execute(
            "INSERT INTO annotations (run_id, note, noted_at) VALUES (?,?,?)",
            [r["run_id"], r["note"], _dt(r["noted_at"])],
        )
    return len(rows)


def restore_into(con) -> int:
    """Load the durable file into the database. Called on every schema ensure,
    so a deleted database recovers the athlete's decisions automatically."""
    rows = load_confirmations()
    for r in rows:
        con.execute(
            "DELETE FROM window_confirmations WHERE file_sha256 = ?", [r["file_sha256"]]
        )
        con.execute(
            "INSERT INTO window_confirmations VALUES (?,?,?,?,?)",
            [
                r["file_sha256"],
                _dt(r["valid_start_utc"]),
                _dt(r["valid_end_utc"]),
                _dt(r["confirmed_at"]),
                r.get("note"),
            ],
        )
    return len(rows)


# A PROJECTION OF AN ATHLETE-STATE FILE IS REFRESHED ONLY HERE, from
# ensure_schema -- a WRITE path. So a file written from a READ-ONLY path (an
# MCP tool) must never be projected: every read-only reader would see whatever
# the last CLI command left behind. Anything that introduces such a projection
# must refresh it from the file's own WRITE path, and its reads must detect
# staleness rather than return empty.
