"""Test package. Importing it arms one structural guard.

NO TEST MAY CREATE A DATABASE FILE IN THE REPO OR AT $TRAIL_FACTORS_DB. A write
connection to a missing path creates an empty database silently, and an empty
corpus reads as "no activities" rather than as an error -- so the guard is
structural, not a convention: the repo and the configured database path are
snapshotted when the suite starts, and the process exits non-zero if a new
database file has appeared by the end. Tests that need a scratch database use
`:memory:` or a TemporaryDirectory of their own.
"""

import atexit
import os
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_SKIP_DIRS = {".git", ".venv", "__pycache__"}


def _db_files() -> set[Path]:
    found = set()
    for root, dirs, files in os.walk(_REPO):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        found |= {Path(root) / f for f in files if ".duckdb" in f}
    raw = os.environ.get("TRAIL_FACTORS_DB", "").strip()
    if raw:
        p = Path(raw).expanduser()
        p = p if p.is_absolute() else _REPO / p
        found |= {q for q in (p, Path(f"{p}.wal")) if q.exists()}
    return found


_BEFORE = _db_files()


def _check_no_database_created() -> None:
    new = sorted(_db_files() - _BEFORE)
    if new:
        names = ", ".join(
            str(p.relative_to(_REPO)) if p.is_relative_to(_REPO) else p.name
            for p in new
        )
        sys.stderr.write(
            f"\nFAIL: the test run created database file(s): "
            f"{names}. No test may create a database (see "
            f"tests/__init__.py).\n"
        )
        sys.stderr.flush()
        os._exit(1)


atexit.register(_check_no_database_created)
