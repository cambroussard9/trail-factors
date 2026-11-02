"""`trail-factors demo`: build data/demo.duckdb from the committed demo/ data.

NO NETWORK, NO RAW FILES, NO ATHLETE STATE. The demo is a set of Parquet
files (demo/data/<table>.parquet) exported from a real corpus by
scripts/export_demo.py: eleven runs with their bronze, silver and gold rows,
the segments they traverse, and their weather. Building applies the schema
DDL to a fresh database and loads each file into the table of the same name,
so every tool runs against it exactly as against an ingested corpus:

    uv run trail-factors demo
    TRAIL_FACTORS_DB=data/demo.duckdb uv run trail-factors report

The database is built in a temporary file and moved into place only when it
is complete, so an interrupted build never leaves a half-loaded demo behind.
It is derived and rebuildable: running the command again replaces it.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import duckdb

from trail_factors.ingest.schema import DDL
from trail_factors.paths import DATA_DIR, REPO_ROOT, display_path

DEMO_DIR = REPO_ROOT / "demo" / "data"
DEMO_DB = DATA_DIR / "demo.duckdb"


def demo_build(src: Path | None = None, dest: Path | None = None) -> Path:
    """Load every demo/data/<table>.parquet into a fresh database at `dest`."""
    src = DEMO_DIR if src is None else Path(src)
    dest = DEMO_DB if dest is None else Path(dest)
    files = sorted(src.glob("*.parquet"))
    if not files:
        raise SystemExit(f"no demo data in {display_path(src)}")
    t0 = time.monotonic()
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".building")
    for stale in (tmp, Path(f"{tmp}.wal")):
        stale.unlink(missing_ok=True)
    con = duckdb.connect(str(tmp))
    try:
        con.execute(DDL)
        tables = {
            r[0]
            for r in con.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_type = 'BASE TABLE'"
            ).fetchall()
        }
        loaded = []
        for f in files:
            table = f.stem
            # A file the schema has no table for is a stale or foreign file,
            # not something to create a table from: refuse it by name.
            if table not in tables:
                raise SystemExit(
                    f"{f.name}: the schema has no table {table!r}; nothing was built"
                )
            con.execute(
                f"INSERT INTO {table} BY NAME SELECT * FROM read_parquet(?)",
                [str(f)],
            )
            n = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            loaded.append((table, n))
        con.execute("CHECKPOINT")
    except BaseException:
        con.close()
        tmp.unlink(missing_ok=True)
        raise
    con.close()
    os.replace(tmp, dest)
    for table, n in loaded:
        print(f"  {table:<20} {n:>7,} rows")
    print(
        f"demo database built at {display_path(dest)} in "
        f"{time.monotonic() - t0:.1f} s, from {len(files)} files and no "
        f"network.\nnext: TRAIL_FACTORS_DB={display_path(dest)} "
        f"uv run trail-factors report"
    )
    return dest
