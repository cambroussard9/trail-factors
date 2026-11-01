"""Shared CLI plumbing: schema bootstrap and id resolution."""

from __future__ import annotations

from pathlib import Path

from trail_factors.ingest.load import DB_PATH


TABLES = ("ingest_ledger", "runs", "activities", "records", "laps", "splits")

def ensure_schema(db_path: Path = DB_PATH) -> None:
    """Apply the DDL before ANY command touches the database.

    Schema changes used to reach the database only through ingest, so an
    existing database silently predated new objects and failed at use time
    (records_valid did exactly this). CREATE TABLE IF NOT EXISTS and
    CREATE OR REPLACE VIEW are both idempotent, so this is cheap to run
    every time. It does NOT backfill new columns on existing rows — a
    re-ingest still does that — but it guarantees the objects exist.
    """
    import duckdb

    from trail_factors.ingest.schema import DDL

    from trail_factors.ingest.athlete_state import restore_annotations, restore_into

    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path))
    try:
        con.execute(DDL)
        # Athlete decisions live in a file outside the database, so a deleted
        # or rebuilt database recovers them automatically (CLAUDE.md).
        restore_into(con)
        restore_annotations(con)
        # ONLY PROJECTIONS WHOSE WRITERS ALSO RUN THIS ARE RESTORED HERE.
        # This function runs only on WRITE connections, so anything it alone
        # refreshes is invisible to every read-only reader until a CLI
        # command happens to run. An athlete-state file written from a
        # read-only path must be read directly, never projected here.
    finally:
        con.close()

def resolve_activity_id(con, arg: str) -> str:
    """Accept a Garmin activity id from the filename, or an internal id.

    Garmin filenames embed the activity id (§11), so that is the identifier
    to hand. Requiring a SQL lookup for the internal SHA-256 prefix is
    friction on every command.
    """
    hit = con.execute(
        "SELECT activity_id FROM activities WHERE activity_id = ?", [arg]).fetchone()
    if hit:
        return hit[0]
    rows = con.execute(
        "SELECT activity_id, source_file FROM activities WHERE source_file LIKE ?",
        [f"%{arg}%"]).fetchall()
    if len(rows) == 1:
        return rows[0][0]
    if not rows:
        raise SystemExit(
            f"no activity matches {arg!r} (tried activity_id and Garmin id in filename)")
    raise SystemExit(
        f"{arg!r} is ambiguous: " + ", ".join(f"{a} ({Path(s).name})" for a, s in rows))
