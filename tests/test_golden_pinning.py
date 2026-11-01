"""The golden queries must be pinned, and the pinning must actually bite.

WHY THIS TEST EXISTS. The golden files are corpus-wide aggregates, so before
pinning every new run changed them and they had to be re-captured. A guard that
is routinely re-baselined has stopped guarding: the next real regression
arrives looking exactly like the last six false alarms, and gets re-captured
with them.

So these tests check the property that makes the guard worth having -- data
arrival cannot trip it, only code can -- rather than just running it.

THE GOLDEN FILES ARE THE CORPUS OWNER'S, NOT THE REPOSITORY'S. They live in
`$TRAIL_FACTORS_GOLDEN_DIR` (default `data/golden/`, gitignored), captured by
`tests/golden/capture.py` against that owner's own corpus. With no corpus, or
no pinned list, the corpus classes skip and say which is missing. The scoping
and change-detection tests below need neither.
"""

import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import duckdb

from tests.corpus import connect, require_corpus

sys.path.insert(0, str(Path(__file__).parent / "golden"))
import capture  # noqa: E402

MARKERS = ("/* PIN */", "/* PIN_E */", "/* PIN_A */")


def _main(**kw) -> int:
    """capture.main with its console report swallowed; the code is the result."""
    with contextlib.redirect_stdout(io.StringIO()):
        return capture.main(**kw)


def _require_pinned() -> list[str]:
    """The pinned ids, or SkipTest naming the missing golden directory."""
    f = capture.pinned_file()
    if not f.exists():
        raise unittest.SkipTest(
            f"no {capture.PINNED_NAME} in ${capture.GOLDEN_ENV} (default "
            f"data/golden/): the golden files are captured per corpus and are "
            f"not committed. Run `python tests/golden/capture.py --pin` and "
            f"then `capture.py` against your corpus to create them.")
    return capture.pinned_activities()


class _PinnedCorpus(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        require_corpus()
        cls.ids = _require_pinned()
        cls.con = connect()

    @classmethod
    def tearDownClass(cls):
        con = getattr(cls, "con", None)
        if con is not None:
            con.close()

    def _corpus_has_grown(self) -> bool:
        n = self.con.execute("SELECT count(*) FROM activities").fetchone()[0]
        return n > len(self.ids)


class TestPinnedSet(_PinnedCorpus):

    def test_the_pinned_set_is_read_from_the_file_that_documents_it(self):
        self.assertTrue(capture.pinned_file().exists())
        self.assertGreater(len(self.ids), 0)
        self.assertEqual(len(self.ids), len(set(self.ids)))

    def test_every_pinned_activity_is_still_in_the_corpus(self):
        """A rebuild that dropped a file would otherwise shrink the comparison,
        still pass, and take the guarantee with it."""
        have = {r[0] for r in self.con.execute(
            "SELECT activity_id FROM activities").fetchall()}
        self.assertEqual([a for a in self.ids if a not in have], [])

    def test_the_corpus_has_grown_past_the_pinned_set(self):
        """THE POSITIVE TWIN, and the whole point. If the pinned set were the
        entire corpus, scoping would be a no-op and these tests would prove
        nothing. It must be a strict subset for the guard to be doing work.

        A corpus pinned today has not grown yet; that is a state, not a
        failure, so it skips and says so."""
        if not self._corpus_has_grown():
            self.skipTest("the corpus has not grown since pinning, so this "
                          "test cannot yet demonstrate insensitivity")


class TestScopingActuallyRestricts(unittest.TestCase):
    """Pure SQL-text properties: no corpus, no golden files."""

    IDS = ["act-000000001", "act-000000002"]

    def test_every_query_carries_a_pin_marker(self):
        """A query with no marker would silently run corpus-wide."""
        for name, sql in capture.QUERIES.items():
            self.assertTrue(any(m in sql for m in MARKERS),
                            f"{name} is not pinned")

    def test_scoping_changes_the_sql(self):
        for name, sql in capture.QUERIES.items():
            self.assertNotEqual(capture.scope(sql, self.IDS), sql, name)

    def test_no_pin_marker_survives_scoping(self):
        """An unreplaced marker would leave a comment where a predicate should
        be -- valid SQL that quietly does nothing."""
        for name, sql in capture.QUERIES.items():
            scoped = capture.scope(sql, self.IDS)
            for marker in MARKERS:
                self.assertNotIn(marker, scoped, name)
            for a in self.IDS:
                self.assertIn(f"'{a}'", scoped, name)


class TestThePinBitesOnTheCorpus(_PinnedCorpus):

    def test_the_scoped_answer_differs_from_the_corpus_wide_one(self):
        """THE MEASUREMENT THAT PROVES THE PIN BITES. If the two agreed, the
        new runs would not have been contributing and the pin would be
        untested."""
        if not self._corpus_has_grown():
            self.skipTest("the corpus has not grown since pinning, so the "
                          "scoped and corpus-wide answers must agree")
        differing = 0
        for name, sql in capture.QUERIES.items():
            wide = sql
            for m in MARKERS:
                wide = wide.replace(m, "")
            if (capture.render(self.con, wide)
                    != capture.render(self.con, capture.scope(sql, self.ids))):
                differing += 1
        self.assertGreater(differing, 0,
                           "scoping changed no answer, so the pin is untested")


class TestTheGoldenFilesStillMatch(_PinnedCorpus):

    def test_the_check_passes(self):
        """The guard itself. A failure here means the CODE changed an answer
        for runs whose answer was already known -- never that a new run
        arrived, which is what pinning removed."""
        self.assertEqual(_main(check=True, db=require_corpus()), 0)

    def test_a_changed_answer_is_DETECTED(self):
        """THE NEGATIVE TWIN. A check that always returned 0 would pass the
        test above. Copy the golden files to a scratch directory, alter one
        captured answer, and the check must fail -- the real files are never
        touched."""
        src = capture.golden_dir()
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            for f in src.iterdir():
                if f.is_file():
                    shutil.copy(f, d / f.name)
            self.assertEqual(_main(check=True, db=require_corpus(),
                                          directory=d), 0,
                             "the unaltered copy must pass, or the twin "
                             "below proves nothing")
            victim = d / f"{next(iter(capture.QUERIES))}.tsv"
            victim.write_text(victim.read_text() + "altered\t0\n")
            self.assertEqual(_main(check=True, db=require_corpus(),
                                          directory=d), 1)


class TestTheGoldenDirectoryIsConfigurable(unittest.TestCase):
    """No corpus needed: where the files live, and what happens with none."""

    def test_the_default_is_under_data_and_out_of_the_repository(self):
        from trail_factors.paths import DATA_DIR
        with mock.patch.dict(os.environ, {capture.GOLDEN_ENV: ""}):
            self.assertEqual(capture.golden_dir(), DATA_DIR / "golden")

    def test_the_environment_variable_redirects_it(self):
        from trail_factors.paths import REPO_ROOT
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.dict(os.environ, {capture.GOLDEN_ENV: d}):
                self.assertEqual(capture.golden_dir(), Path(d))
        with mock.patch.dict(os.environ, {capture.GOLDEN_ENV: "some/rel"}):
            self.assertEqual(capture.golden_dir(), REPO_ROOT / "some/rel")

    def test_no_pinned_list_is_a_refusal_not_a_pass(self):
        """An empty golden directory must not read as 'all queries matched'."""
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(_main(check=True, directory=Path(d)), 1)

    def test_a_missing_corpus_is_refused_and_never_created(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            capture.pinned_file(d).write_text("act-000000001\n")
            db = d / "absent.duckdb"
            self.assertEqual(_main(check=True, db=db, directory=d), 1)
            self.assertFalse(db.exists())

    def test_a_missing_pinned_activity_is_a_hard_failure(self):
        """A rebuild that dropped a file must not shrink the comparison."""
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            db = d / "tiny.duckdb"
            con = duckdb.connect(str(db))
            con.execute("CREATE TABLE activities (activity_id VARCHAR)")
            con.execute("INSERT INTO activities VALUES ('act-000000001')")
            con.close()
            capture.pinned_file(d).write_text(
                "# synthetic\nact-000000001\nact-000000002\n")
            self.assertEqual(_main(check=True, db=db, directory=d), 1)


if __name__ == "__main__":
    unittest.main()
