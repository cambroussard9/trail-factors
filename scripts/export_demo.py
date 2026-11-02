"""Export the demo dataset (demo/data/*.parquet) from a private corpus.

A MAINTAINER TOOL: it reads an ingested database and writes the files that
`trail-factors demo` loads. It is not part of the package.

    TRAIL_FACTORS_DB=data/test-copy.duckdb uv run python scripts/export_demo.py

WHAT LEAVES THE CORPUS, AND WHAT DOES NOT:
- Runs only (sport = running, watch recordings), the DEMO_RUNS below, and
  only their rows: bronze (activities, runs, records, splits, laps, stops),
  silver (dem_elevations, records_enriched, slope and hill traversals,
  weather) and gold (segment efforts, recovery, HR zone time, validation),
  plus the segments those runs traverse.
- A run with a confirmed or applied recording window is REFUSED, so a file
  whose tail was a drive never enters the demo.
- ingest_ledger is NOT exported: it holds the watch serial and absolute file
  paths. Window confirmations and notes are not exported either.
- Garmin activity ids are replaced by synthetic ones (90000000001, ...) in
  every text column, so a demo file cannot be looked up on Garmin Connect.
  DEM tile file names (a named region) become dem_tile_1, dem_tile_2, ...
  Dates, times, positions and every measurement are the real ones.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import duckdb

from trail_factors.paths import DB_FILE, REPO_ROOT

# Chosen for coverage of the analysis surface, not for flattering numbers:
# flat urban runs, rolling trail runs (both sampling regimes), long hilly runs,
# repeated segments, and FR265 runs that carry Garmin's own effort rating.
DEMO_RUNS = (
    # flat urban
    "2026-01-15T2248Z",
    "2026-03-12T1533Z",
    "2026-04-07T1851Z",
    # rolling trail
    "2026-03-24T2155Z",
    "2026-09-06T2202Z",
    "2026-09-12T1640Z",
    "2026-10-27T2143Z",
    "2026-10-28T1429Z",
    # long and hilly
    "2026-09-26T1422Z",
    "2026-10-03T1840Z",
    "2026-10-11T1635Z",
)

# Tables keyed by activity_id, exported row for row.
BY_ACTIVITY = (
    "activities",
    "records",
    "splits",
    "laps",
    "stops",
    "dem_elevations",
    "records_enriched",
    "slope_traversals",
    "hill_traversals",
    "weather_records",
    "weather_hourly",
    "segment_efforts",
    "segment_recovery",
    "activity_hr_zones",
)


def _selected(con) -> list[tuple[str, str, str]]:
    """(activity_id, run_id, garmin_id) for the demo runs; refuses bad picks."""
    rows = con.execute(
        """
        SELECT a.activity_id, a.run_id, a.source_file, a.sport,
               coalesce(a.window_applied, false),
               a.file_hash IN (SELECT file_sha256 FROM window_confirmations)
        FROM activities a WHERE a.run_id IN (SELECT unnest(?))
          AND a.source = 'garmin_watch'
        ORDER BY a.start_time_utc""",
        [list(DEMO_RUNS)],
    ).fetchall()
    found = {r[1] for r in rows}
    missing = sorted(set(DEMO_RUNS) - found)
    if missing:
        raise SystemExit(f"not in this corpus: {', '.join(missing)}")
    out = []
    for aid, run_id, src, sport, applied, confirmed in rows:
        if sport != "running":
            raise SystemExit(f"{run_id} is {sport!r}, not a run")
        if applied or confirmed:
            raise SystemExit(f"{run_id} has a confirmed recording window")
        out.append((aid, run_id, Path(src).name.split("_")[0]))
    return out


def _scrubbed_select(
    con, table: str, ids: dict[str, str], replace: dict[str, str] | None = None
) -> str:
    """SELECT * with every Garmin id replaced in every text column.

    `replace` maps a column to the expression that stands in for it.
    """
    replace = replace if replace is not None else {}
    cols = con.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_name = ? ORDER BY ordinal_position",
        [table],
    ).fetchall()
    parts = []
    for name, dtype in cols:
        expr = f'"{name}"'
        if name in replace:
            expr = f'{replace[name]} AS "{name}"'
        elif dtype == "VARCHAR":
            for real, fake in ids.items():
                expr = f"replace({expr}, '{real}', '{fake}')"
            expr = f'{expr} AS "{name}"'
        parts.append(expr)
    return ", ".join(parts)


def export(source: Path, out_dir: Path) -> None:
    con = duckdb.connect(str(source), read_only=True)
    try:
        picked = _selected(con)
        aids = [p[0] for p in picked]
        runs = [p[1] for p in picked]
        ids = {g: str(90000000000 + i) for i, (_, _, g) in enumerate(picked, 1)}
        # DEM tile file names carry a USGS acquisition project and the 10 km
        # grid cell, i.e. a named region. They are provenance, not data the
        # demo needs, so each becomes a neutral label in every text column.
        tiles = [
            r[0]
            for r in con.execute(
                "SELECT DISTINCT dem_tile FROM dem_elevations "
                "WHERE activity_id IN (SELECT unnest(?)) AND dem_tile IS NOT NULL "
                "ORDER BY 1",
                [aids],
            ).fetchall()
        ]
        ids |= {name: f"dem_tile_{k}" for k, name in enumerate(tiles, 1)}
        out_dir.mkdir(parents=True, exist_ok=True)
        for old in out_dir.glob("*.parquet"):
            old.unlink()

        def write(
            table: str, where: str, params: list, replace: dict | None = None
        ) -> int:
            sel = _scrubbed_select(con, table, ids, replace)
            dest = out_dir / f"{table}.parquet"
            con.execute(
                f"COPY (SELECT {sel} FROM {table} WHERE {where} ORDER BY ALL) "
                f"TO '{dest}' (FORMAT parquet, COMPRESSION zstd)",
                params,
            )
            return con.execute(
                f"SELECT count(*) FROM read_parquet('{dest}')"
            ).fetchone()[0]

        counts = {}
        for t in BY_ACTIVITY:
            counts[t] = write(t, "activity_id IN (SELECT unnest(?))", [aids])
        counts["runs"] = write("runs", "run_id IN (SELECT unnest(?))", [runs])

        # THE EFFORT COUNTS ARE RECOUNTED OVER THE EXPORTED RUNS. The stored
        # ones count the whole corpus, so the demo's library claimed efforts
        # its segment_efforts does not hold (19 ascents listed, 6 present).
        def recount(direction: str | None) -> str:
            only = "" if direction is None else f" AND e.direction = '{direction}'"
            return (
                "(SELECT count(*) FROM segment_efforts e "
                "WHERE e.segment_id = segments.segment_id "
                f"AND e.activity_id IN (SELECT unnest(?)){only})::INTEGER"
            )

        counts["segments"] = write(
            "segments",
            "segment_id IN (SELECT segment_id FROM segment_efforts "
            "WHERE activity_id IN (SELECT unnest(?)))",
            # One aids per recount, in column order, then the WHERE's.
            [aids, aids, aids, aids],
            replace={
                "n_efforts": recount(None),
                "n_ascents": recount("ascent"),
                "n_descents": recount("descent"),
            },
        )
        counts["validation_results"] = write(
            "validation_results",
            "activity_id IN (SELECT unnest(?)) OR run_id IN (SELECT unnest(?))",
            [aids, runs],
        )
    finally:
        con.close()
    for t, n in sorted(counts.items()):
        print(f"  {t:<20} {n:>7,} rows")
    print(f"{len(picked)} runs exported to {out_dir.relative_to(REPO_ROOT)}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--source",
        type=Path,
        default=DB_FILE,
        help="the corpus to export from (default: TRAIL_FACTORS_DB)",
    )
    ap.add_argument("--out", type=Path, default=REPO_ROOT / "demo" / "data")
    a = ap.parse_args()
    export(a.source, a.out)


if __name__ == "__main__":
    main()
