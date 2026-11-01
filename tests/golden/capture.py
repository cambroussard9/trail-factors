"""Capture three representative queries verbatim, as a refactoring guard.

These are queries the athlete would actually ask. They are captured BEFORE a
refactor and re-run after: a non-empty diff fails the tidy. The point is that a
pure code reorganisation cannot change an answer.

THE CAPTURED FILES ARE NOT COMMITTED. They are one athlete's real outputs over
one athlete's real activity list, so they live under the golden directory --
`$TRAIL_FACTORS_GOLDEN_DIR`, default `data/golden/` (gitignored). Only this
script is in the repository. A fresh checkout has no golden files, and the
check skips until its owner captures them against their own corpus.

PINNED TO A FIXED ACTIVITY SET. The queries are corpus-wide aggregates, so
before pinning EVERY NEW RUN changed them and the files had to be re-captured.
That makes re-capture a reflex, and a guard that is routinely re-baselined has
stopped guarding -- the next real regression arrives looking exactly like the
last six false alarms.

So every query is scoped to `pinned_activities.txt`: the activities present
when the files were first captured. Data arrival cannot trip them; only a code
change can. DATA ARRIVAL IS NOT A CODE REORGANISATION.

A MISSING PINNED ACTIVITY IS A HARD FAILURE, not a smaller comparison. A
rebuild that silently dropped a file would otherwise shrink the set, still
"pass", and take the guarantee with it.

Usage (the database is `$TRAIL_FACTORS_DB`, default `data/ultra.duckdb`):

    uv run python tests/golden/capture.py --pin      # write pinned_activities.txt
                                                     # from the corpus as it is
    uv run python tests/golden/capture.py            # capture the .tsv files
    uv run python tests/golden/capture.py --check    # compare; non-zero on diff
"""

import os
import sys
from pathlib import Path

# Run as a script, this directory is on sys.path and the repo root is not.
_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

GOLDEN_ENV = "TRAIL_FACTORS_GOLDEN_DIR"
PINNED_NAME = "pinned_activities.txt"


def golden_dir() -> Path:
    """Where the captured files live. Read at CALL time so tests can redirect.

    `$TRAIL_FACTORS_GOLDEN_DIR`, relative values resolved against the repo root;
    default `data/golden/`, which .gitignore covers with the rest of data/.
    """
    from trail_factors.paths import DATA_DIR, REPO_ROOT
    raw = os.environ.get(GOLDEN_ENV, "").strip()
    if not raw:
        return DATA_DIR / "golden"
    p = Path(raw).expanduser()
    return p if p.is_absolute() else REPO_ROOT / p


def pinned_file(directory: Path | None = None) -> Path:
    return (directory if directory is not None else golden_dir()) / PINNED_NAME


def pinned_activities(directory: Path | None = None) -> list[str]:
    """The fixed corpus, read from the file that documents it."""
    out = []
    for line in pinned_file(directory).read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line.split()[0])
    return out


def scope(sql: str, activity_ids: list[str]) -> str:
    """Restrict a query to the pinned set.

    Every one of these queries filters or groups on activity_id already, so a
    predicate on the outermost source is enough -- the joins are all
    activity-internal (a.activity_id = b.activity_id, splits to records by
    activity and timestamp).
    """
    ids = ", ".join(f"'{a}'" for a in activity_ids)
    return sql.replace("/* PIN */", f"AND activity_id IN ({ids})") \
              .replace("/* PIN_E */", f"AND e.activity_id IN ({ids})") \
              .replace("/* PIN_A */", f"AND a.activity_id IN ({ids})")

QUERIES = {
    # §6.2's deliverable: grade-binned pace, split by split_type. Never pooled.
    "grade_binned_pace": """
        WITH d AS (
          SELECT e.activity_id, e.timestamp, e.grade_pct,
                 e.path_distance_m - lag(e.path_distance_m) OVER w AS dx,
                 date_diff('second', lag(e.timestamp) OVER w, e.timestamp) AS dt
          FROM records_enriched e
          WHERE e.terrain_usable AND e.grade_pct IS NOT NULL
            /* PIN_E */
          WINDOW w AS (PARTITION BY e.activity_id ORDER BY e.timestamp)
        )
        SELECT s.split_type, floor(d.grade_pct/5)*5 AS bin, count(*) AS n,
               round(median(d.dx/d.dt), 3) AS mps
        FROM d JOIN splits s ON s.activity_id = d.activity_id
             AND d.timestamp BETWEEN s.start_time AND s.end_time
        WHERE d.dt BETWEEN 1 AND 20 AND d.dx >= 0
          AND s.split_type IN ('rwd_run','rwd_walk')
        GROUP BY 1,2 HAVING count(*) > 50 ORDER BY 1,2""",

    # §6.3: delta-HR and tau together, per slope per direction.
    "recovery_by_slope": """
        SELECT segment_id, direction, count(*) AS n,
               round(median(delta_hr_bpm), 1) AS median_delta_hr,
               count(tau_s) AS n_with_tau, bool_or(tau_reportable) AS reportable
        FROM segment_recovery WHERE 1=1 /* PIN */
        GROUP BY 1,2 HAVING count(*) >= 3
        ORDER BY count(*) DESC, 1, 2""",

    # §6.7: matched-terrain drift within one run, on the same slope.
    "matched_terrain_drift": """
        SELECT a.activity_id, a.segment_id, a.direction,
               date_diff('minute', a.start_utc, b.start_utc) AS gap_min,
               round(b.avg_hr - a.avg_hr, 1) AS d_hr
        FROM segment_efforts a JOIN segment_efforts b
          ON a.segment_id = b.segment_id AND a.activity_id = b.activity_id
         AND a.direction = b.direction AND a.start_utc < b.start_utc
         AND date_diff('minute', a.start_utc, b.start_utc) >= 10
        WHERE a.avg_hr IS NOT NULL AND b.avg_hr IS NOT NULL
          /* PIN_A */
        ORDER BY 1,2,3,4""",
}


def render(con, sql):
    cur = con.execute(sql)
    cols = [d[0] for d in cur.description]
    lines = ["\t".join(cols)]
    for row in cur.fetchall():
        lines.append("\t".join("" if v is None else str(v) for v in row))
    return "\n".join(lines) + "\n"


def _database(db) -> Path:
    """The corpus to read: `db` if given, else `$TRAIL_FACTORS_DB` / default."""
    if db is not None:
        return Path(db)
    from trail_factors.paths import DB_FILE
    return DB_FILE


def pin(db=None, directory: Path | None = None) -> int:
    """Write pinned_activities.txt from the corpus as it stands now.

    REFUSES TO OVERWRITE. Growing the pinned set is a deliberate act with the
    .tsv files re-captured beside it -- never a way to make a diff go away.
    """
    import duckdb
    from trail_factors.paths import display_path
    out = pinned_file(directory)
    if out.exists():
        print(f"{display_path(out)} already exists; delete it deliberately "
              f"to re-pin (and re-capture in the same step).")
        return 1
    path = _database(db)
    if not path.exists():
        print(f"no corpus at {display_path(path)}; set TRAIL_FACTORS_DB.")
        return 1
    con = duckdb.connect(str(path), read_only=True)
    try:
        rows = con.execute("SELECT activity_id FROM activities "
                           "ORDER BY activity_id").fetchall()
    finally:
        con.close()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("# The fixed corpus the golden queries are scoped to.\n"
                   "# One activity_id per line. Never edit to make a diff "
                   "pass.\n" + "".join(f"{r[0]}\n" for r in rows))
    print(f"pinned {len(rows)} activities to {display_path(out)}")
    return 0


def main(check: bool = False, db=None, directory: Path | None = None) -> int:
    import duckdb
    from trail_factors.paths import display_path
    out_dir = directory if directory is not None else golden_dir()
    if not pinned_file(out_dir).exists():
        print(f"no {PINNED_NAME} in {display_path(out_dir)}: nothing is "
              f"pinned. Run with --pin first (see the module docstring).")
        return 1
    path = _database(db)
    if not path.exists():
        # read_only would refuse anyway; say why instead of a DuckDB error,
        # and never create the file.
        print(f"no corpus at {display_path(path)}; set TRAIL_FACTORS_DB.")
        return 1
    con = duckdb.connect(str(path), read_only=True)
    failed = []
    try:
        ids = pinned_activities(out_dir)
        have = {r[0] for r in con.execute(
            "SELECT activity_id FROM activities").fetchall()}
        missing = [a for a in ids if a not in have]
        if missing:
            print(f"PINNED ACTIVITIES MISSING FROM THE CORPUS: "
                  f"{', '.join(missing)}\n"
                  f"These queries are pinned to a fixed set (see "
                  f"{PINNED_NAME}). A missing one means the corpus lost data, "
                  f"not that the comparison should shrink.")
            return 1
        for name, sql in QUERIES.items():
            got = render(con, scope(sql, ids))
            path = out_dir / f"{name}.tsv"
            if check:
                want = path.read_text() if path.exists() else ""
                if got != want:
                    failed.append(name)
            else:
                path.write_text(got)
                print(f"captured {name}: {len(got.splitlines()) - 1} rows")
    finally:
        con.close()
    if failed:
        print("GOLDEN DIFF: " + ", ".join(failed))
        return 1
    if check:
        print(f"all {len(QUERIES)} golden queries byte-identical "
              f"({len(ids)} pinned activities)")
    return 0


if __name__ == "__main__":
    if "--pin" in sys.argv:
        sys.exit(pin())
    sys.exit(main(check="--check" in sys.argv))
