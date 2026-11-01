"""The one guard for tests that read an ingested corpus, and a config helper.

A CORPUS TEST READS `$TRAIL_FACTORS_DB` AND NOTHING ELSE. Unset, it SKIPS with a
reason that says how to run it. Set to a path that does not exist, it FAILS:
a typo would otherwise skip the whole corpus suite and read as a pass. It is
never allowed to create the file -- every connection here is read-only, which
DuckDB refuses to open on a missing path.

Corpus tests are written against ANY athlete's corpus: they find their subject
by a property ("the first 1 Hz watch run", "a run with running dynamics") and
assert invariants, never one person's run ids or measured values. When a
corpus has no run with the property, the test skips and names it.
"""

from __future__ import annotations

import contextlib
import dataclasses
import importlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import duckdb

ENV = "TRAIL_FACTORS_DB"
SKIP_REASON = (
    f"{ENV} is not set: this test reads an ingested corpus. Set "
    f"{ENV} to a database built by `trail-factors ingest-all`."
)


def corpus_path() -> Path | None:
    """The corpus database, None when unset. Raises if set but missing."""
    from trail_factors.paths import REPO_ROOT

    raw = os.environ.get(ENV, "").strip()
    if not raw:
        return None
    p = Path(raw).expanduser()
    p = p if p.is_absolute() else REPO_ROOT / p
    if not p.exists():
        raise RuntimeError(
            f"{ENV}={raw} does not exist (resolved to a path under "
            f"{'the repo' if p.is_relative_to(REPO_ROOT) else 'elsewhere'}). "
            f"Tests never create a database; fix the path or unset {ENV}."
        )
    return p


def require_corpus() -> Path:
    """The corpus path, or SkipTest when TRAIL_FACTORS_DB is unset."""
    p = corpus_path()
    if p is None:
        raise unittest.SkipTest(SKIP_REASON)
    return p


def connect():
    """A READ-ONLY connection to the corpus (skips when unset)."""
    return duckdb.connect(str(require_corpus()), read_only=True)


# Decorator for a class or method: skipped unless TRAIL_FACTORS_DB is set.
needs_corpus = unittest.skipUnless(os.environ.get(ENV, "").strip(), SKIP_REASON)


class CorpusTestCase(unittest.TestCase):
    """Base class: `cls.con` is a read-only corpus connection."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.con = connect()

    @classmethod
    def tearDownClass(cls):
        con = getattr(cls, "con", None)
        if con is not None:
            con.close()
        super().tearDownClass()


def one(con, sql: str, params=(), what: str = "a qualifying run"):
    """First row of `sql`, or SkipTest naming what this corpus lacks."""
    row = con.execute(sql, list(params)).fetchone()
    if row is None:
        raise unittest.SkipTest(f"this corpus has no {what}")
    return row


@contextlib.contextmanager
def use_config(**fields):
    """Run with an athlete config built from `fields`, never the real file.

    Production code reads `trail_factors.config.get()`; this patches it, so a test sees
    exactly the values it names and the defaults for everything else --
    whatever config/athlete.yaml happens to hold on the machine.
    """
    from trail_factors import config as C

    clusters = tuple(
        c if isinstance(c, C.Cluster) else C.Cluster(**c)
        for c in fields.pop("clusters", ())
    )
    cfg = dataclasses.replace(C.AthleteConfig(clusters=clusters), **fields)
    with mock.patch.object(C, "get", lambda: cfg):
        yield cfg


# Every module that holds a path under ATHLETE_DIR as a module attribute.
_STATE_MODULES = (
    "trail_factors.ingest.athlete_state",
    "trail_factors.ingest.ride_blocks",
    "trail_factors.analysis.zones",
)


# MODULE-SCOPED USE: enter it in setUpModule and close it in tearDownModule
# (a module-level contextlib.ExitStack), NOT with unittest.enterModuleContext.
# That registers the exit as a unittest MODULE CLEANUP, which pytest never
# runs: under pytest the patched athlete-state paths stayed patched for every
# later test file, and the isolation guards there failed. setUpModule and
# tearDownModule run under both runners.
@contextlib.contextmanager
def isolated(**config):
    """An EMPTY athlete-state directory and exactly `config`. Every
    module-level path under ATHLETE_DIR is redirected to a temporary
    directory, so a read finds nothing and a stray write lands there."""
    from trail_factors.paths import ATHLETE_DIR

    with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
        for name in _STATE_MODULES:
            mod = importlib.import_module(name)
            for attr, val in list(vars(mod).items()):
                if isinstance(val, Path) and val.is_relative_to(ATHLETE_DIR):
                    stack.enter_context(
                        mock.patch.object(
                            mod, attr, Path(tmp) / val.relative_to(ATHLETE_DIR)
                        )
                    )
        stack.enter_context(use_config(**config))
        yield Path(tmp)


def corpus_projection() -> str | None:
    """The WGS84 UTM zone this corpus's runs centre on -- derived from the
    corpus itself, so no test reads the owner's config for it."""
    from trail_factors.transform.dem import utm_zone_at

    con = connect()
    try:
        lat, lon = con.execute("""
            SELECT median(r.lat), median(r.lon) FROM records r
            JOIN activities a USING (activity_id)
            WHERE a.source = 'garmin_watch' AND a.sport = 'running'
              AND r.lat IS NOT NULL""").fetchone()
    finally:
        con.close()
    if lat is None:
        return None
    zone, north = utm_zone_at(lat, lon)
    return f"EPSG:{(32600 if north else 32700) + zone}"
