"""No tool output carries an absolute path or the home directory.

An absolute path names the user (`/home/<name>/...` and its equivalents),
and the envelope is exactly the text that ends up pasted into a chat or an
issue. `ping` and the no-corpus message are the two outputs every client sees
FIRST -- before any corpus exists -- so they are pinned here against both a
database path inside the repo and one outside it.

Every negative has a positive twin: the detector must flag a string that does
carry the home directory, and each message must still NAME the file it is
about, so a scrub that blanked everything would not pass.
"""

import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import duckdb

from trail_factors import mcp_server as M
from trail_factors.paths import REPO_ROOT
from tests.isolation import state_sandbox

ABSOLUTE = re.compile(r"(?<![\w.~<])/(?:Users|home|private|tmp|var|root|Volumes)/")


def leaks(text: str, *extra: Path) -> list[str]:
    """Every absolute location found in `text`."""
    found = [m.group(0) for m in ABSOLUTE.finditer(text)]
    for p in (Path.home(), Path.home().resolve(), REPO_ROOT, *extra):
        for form in {str(p), str(Path(p).resolve())}:
            if len(form) > 1 and form in text:
                found.append(form)
    return found


class TestTheDetectorCanFire(unittest.TestCase):

    def test_it_flags_the_home_directory_and_the_repo_root(self):
        self.assertTrue(leaks(f"no corpus at {Path.home() / 'x.duckdb'}"))
        self.assertTrue(leaks(f"see {REPO_ROOT / 'data'}"))
        self.assertTrue(leaks("failed at /home/someone/project/x"))

    def test_it_passes_a_relative_and_an_outside_form(self):
        self.assertEqual(leaks("no corpus at data/example.duckdb"), [])
        self.assertEqual(leaks("no corpus at <outside repo>/x.duckdb"), [])


class _Sandboxed(unittest.TestCase):
    def setUp(self):
        sandbox = state_sandbox()
        sandbox.__enter__()
        self.addCleanup(sandbox.__exit__, None, None, None)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def with_db(self, path: Path):
        p = mock.patch.object(M, "DB_FILE", path)
        p.start()
        self.addCleanup(p.stop)


class TestTheNoCorpusMessage(_Sandboxed):

    def _no_corpus(self):
        r = json.loads(M.list_runs())
        self.assertEqual(r["outcome"], M.BROKEN)
        self.assertIn("no corpus at", r["text"])
        return r

    def test_a_database_path_inside_the_repo_is_shown_repo_relative(self):
        db = REPO_ROOT / "data" / "no-such-corpus-for-a-test.duckdb"
        self.assertFalse(db.exists())
        self.with_db(db)
        r = self._no_corpus()
        blob = json.dumps(r)
        self.assertEqual(leaks(blob), [], blob)
        self.assertIn("data/no-such-corpus-for-a-test.duckdb", r["text"])
        self.assertIn("TRAIL_FACTORS_DB", r["text"])
        self.assertFalse(db.exists(), "reporting a missing corpus created it")

    def test_a_database_path_outside_the_repo_shows_only_its_name(self):
        db = Path(self.tmp.name) / "missing.duckdb"
        self.with_db(db)
        r = self._no_corpus()
        blob = json.dumps(r)
        self.assertEqual(leaks(blob, Path(self.tmp.name)), [], blob)
        self.assertIn("missing.duckdb", r["text"])


class TestPing(_Sandboxed):

    def _ping(self, *extra):
        r = json.loads(M.ping())
        self.assertEqual(r["outcome"], M.RESULT, r)
        blob = json.dumps(r)
        self.assertEqual(leaks(blob, *extra), [], blob)
        return r

    def test_ping_with_no_database_names_no_absolute_path(self):
        self.with_db(Path(self.tmp.name) / "absent.duckdb")
        self._ping(Path(self.tmp.name))

    def test_ping_with_a_database_outside_the_repo(self):
        db = Path(self.tmp.name) / "scratch.duckdb"
        duckdb.connect(str(db)).close()
        self.with_db(db)
        r = self._ping(Path(self.tmp.name))
        self.assertIs(r["payload"]["database"]["write_locked"], False)

    def test_ping_with_the_default_path_inside_the_repo(self):
        db = REPO_ROOT / "data" / "no-such-corpus-for-a-test.duckdb"
        self.with_db(db)
        self._ping()
        self.assertFalse(db.exists(), "ping created a database file")


if __name__ == "__main__":
    unittest.main()
