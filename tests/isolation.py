"""Keep tests away from the REAL athlete state, raw layer and job file.

`data/athlete/` cannot be regenerated from anything, `data/raw/` is
immutable source, and `data/jobs.json` is a live server's job state. A test
that reads any of them as input is pinned to one person's history; a test
that writes one destroys something. So a test that drives the tools -- which
read notes, zone config and venues, and may write a rejected save --
runs inside `state_sandbox()`.

THE REDIRECT IS BY VALUE, NOT BY NAME. Every module under `trail_factors` that holds a
`Path` at or under one of those locations is patched to the same relative
place in a TemporaryDirectory, so a path constant added next month is
sandboxed without anyone remembering to list it. Every `trail_factors` module is
imported first, because a module imported lazily inside a tool call would
otherwise bind the real path after the patch was taken.
"""

from __future__ import annotations

import contextlib
import importlib
import pkgutil
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


def _import_all_src() -> None:
    import trail_factors

    for m in pkgutil.walk_packages(trail_factors.__path__, "trail_factors."):
        if m.name.rsplit(".", 1)[-1] == "__main__":
            continue
        importlib.import_module(m.name)


@contextlib.contextmanager
def state_sandbox():
    """Yield a temp root holding `athlete/`, `raw/`, `lookup/`, `jobs.json`.

    Inside the block, every `trail_factors` path constant that pointed at the real
    ATHLETE_DIR, RAW_DIR, LOOKUP_DIR or JOBS_FILE points into the temp root.
    The directories exist and are EMPTY; a test seeds what it needs.
    """
    from trail_factors import paths as P

    _import_all_src()
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    targets = [
        (P.JOBS_FILE, root / "jobs.json"),
        (P.ATHLETE_DIR, root / "athlete"),
        (P.RAW_DIR, root / "raw"),
        (P.LOOKUP_DIR, root / "lookup"),
    ]
    for _, new in targets[1:]:
        new.mkdir(parents=True)

    def redirect(v: Path) -> Path | None:
        for real, new in targets:
            if v == real:
                return new
            if v.is_relative_to(real):
                return new / v.relative_to(real)
        return None

    patches = []
    for name, mod in list(sys.modules.items()):
        if (
            not (name == "trail_factors" or name.startswith("trail_factors."))
            or mod is None
        ):
            continue
        for attr, v in list(vars(mod).items()):
            if isinstance(v, Path):
                new = redirect(v)
                if new is not None:
                    patches.append(mock.patch.object(mod, attr, new))
    try:
        for p in patches:
            p.start()
        yield root
    finally:
        for p in reversed(patches):
            p.stop()
        tmp.cleanup()


def fingerprint(root: Path) -> dict:
    """Every file under `root` with size and mtime -- metadata only."""
    return {
        str(f.relative_to(root)): (f.stat().st_size, f.stat().st_mtime_ns)
        for f in sorted(root.rglob("*"))
        if f.is_file()
    }


def git_ignores(relpath: str) -> bool:
    """Would git ignore `relpath` (repo-relative)? Skips without git.

    Asks git rather than grepping .gitignore for a literal: `data/` ignores
    `data/jobs.json` without naming it, and a literal search would call that
    a failure.
    """
    from trail_factors.paths import REPO_ROOT

    git = shutil.which("git")
    if git is None:
        raise unittest.SkipTest("git is not installed: cannot evaluate .gitignore")
    r = subprocess.run(
        [git, "check-ignore", "--no-index", "-q", relpath],
        cwd=REPO_ROOT,
        capture_output=True,
    )
    if r.returncode not in (0, 1):
        raise unittest.SkipTest(f"git check-ignore failed: {r.stderr.decode()[:200]}")
    return r.returncode == 0
