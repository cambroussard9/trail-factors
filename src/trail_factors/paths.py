"""Repo-root-anchored paths, each overridable by an environment variable.

EVERY data path in this project was once relative to the working directory.
That is fine for a CLI run from the repo, and WRONG for anything launched by
something else -- an MCP server started by a desktop client inherits an
arbitrary cwd, and `ensure_schema()` would cheerfully CREATE a fresh empty
database wherever it landed. The first report would then say "no activities"
about a populated corpus: no error, no traceback, a plausible-looking empty
answer.

So every default is anchored on this file's location, and every override is
too: a RELATIVE value in an environment variable is resolved against the repo
root, never against the cwd, for the same reason. `.env.example` documents
each variable.
"""

from __future__ import annotations

import os
from pathlib import Path

# src/trail_factors/paths.py -> the repository root, two levels up. The
# project is meant to be run from a clone (`uv sync` installs it editable);
# every default below is anchored here, and each is overridable by its
# TRAIL_FACTORS_* environment variable.
REPO_ROOT = Path(__file__).resolve().parents[2]


def _env_path(name: str, default: Path) -> Path:
    """`$name` if set and non-empty, else `default`. Relative -> repo root."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    p = Path(raw).expanduser()
    return p if p.is_absolute() else (REPO_ROOT / p)


DATA_DIR = _env_path("TRAIL_FACTORS_DATA_DIR", REPO_ROOT / "data")
DB_FILE = _env_path("TRAIL_FACTORS_DB", DATA_DIR / "ultra.duckdb")
RAW_DIR = _env_path("TRAIL_FACTORS_RAW_DIR", DATA_DIR / "raw")
DEM_DIR = _env_path("TRAIL_FACTORS_DEM_DIR", DATA_DIR / "dem")
# Athlete-supplied state: confirmations, answers, names. It cannot be
# regenerated from any source, so back it up -- it is NOT in git (it sits
# under data/, which is ignored as a whole).
ATHLETE_DIR = _env_path("TRAIL_FACTORS_ATHLETE_DIR", DATA_DIR / "athlete")
# NOT CORPUS DATA, and the separate directory is the point (§10.1). Things
# under here are LOOKUPS -- fetched to decide what to fetch next -- and are
# never ingested, never joined to a run, and never read by anything in
# src/trail_factors/analysis or src/trail_factors/ingest. Deleting it loses nothing but a re-fetch.
LOOKUP_DIR = _env_path("TRAIL_FACTORS_LOOKUP_DIR", DATA_DIR / "lookup")
# SYNC-JOB STATE: operational and regenerable, safe to delete, and kept apart
# from ATHLETE_DIR so that a `rm` of one is never mistaken for the other.
JOBS_FILE = _env_path("TRAIL_FACTORS_JOBS_FILE", DATA_DIR / "jobs.json")
# The athlete configuration (src/trail_factors/config.py). Optional: absent means defaults.
CONFIG_FILE = _env_path("TRAIL_FACTORS_CONFIG",
                        REPO_ROOT / "config" / "athlete.yaml")

# GARMIN CREDENTIALS LIVE OUTSIDE THE REPO, structurally. The token store holds
# an OAuth token, and the auth-state file the 30-minute lockout after a failed
# login. Neither may sit inside the working tree, where one `git add -A` would
# publish it -- `garmin_api` refuses a path under REPO_ROOT rather than trusting
# .gitignore to catch it.
GARMIN_TOKENSTORE = _env_path("TRAIL_FACTORS_GARMIN_TOKENSTORE",
                              Path.home() / ".garminconnect")
GARMIN_AUTH_STATE = _env_path("TRAIL_FACTORS_GARMIN_AUTH_STATE",
                              Path.home() / ".garminconnect_auth_state.json")


def inside_repo(p: Path) -> bool:
    """True when `p` resolves to somewhere under the repo root."""
    try:
        Path(p).resolve().relative_to(REPO_ROOT)
        return True
    except ValueError:
        return False


def display_path(p: Path) -> str:
    """A path safe to put in OUTPUT: repo-relative, or just its name.

    An absolute path carries the user's home directory, and anything a tool
    emits can end up pasted into a chat or an issue. Inside the repo the
    relative path is all a reader needs; outside it, the file name is.
    """
    p = Path(p)
    try:
        return str(p.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return f"<outside repo>/{p.name}"
