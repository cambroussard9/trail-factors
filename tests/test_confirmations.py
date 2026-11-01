"""Athlete-supplied state must survive a rebuild (CLAUDE.md, SPEC.md §4.3).

A window confirmation is the athlete's statement about which part of a file
was the run. Derived tables are rebuilt from raw; athlete input cannot be, so
the confirmation lives in a file under the athlete-state directory that no
rebuild touches.
"""

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

import duckdb

from trail_factors.ingest import athlete_state as A
from trail_factors.ingest.load import ingest
from trail_factors.ingest.schema import DDL
from trail_factors.paths import RAW_DIR, REPO_ROOT

FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures"


def _candidate_fits() -> list[Path]:
    """Every FIT this checkout has, smallest first: a synthetic fixture if one
    exists, else the owner's raw watch files. Chosen by PROPERTY, never by a
    named activity, so the test runs on anyone's raw directory."""
    found = []
    for d in (FIXTURE_DIR, RAW_DIR / "garmin"):
        if d.is_dir():
            found += [p for p in d.iterdir() if p.suffix.lower() == ".fit"]
    return sorted(found, key=lambda p: p.stat().st_size)


class ConfirmationSurvivesRebuildTest(unittest.TestCase):
    """Runs against a real FIT file ingested into a SCRATCH database in a
    TemporaryDirectory. The confirmations file is redirected there too, so
    ingest reads no real athlete state and the test writes none."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls._tmp.name)
        cls.fit = None
        probe = cls.dir / "probe.duckdb"
        with mock.patch.object(
            A, "CONFIRMATIONS", cls.dir / "window_confirmations.json"
        ):
            # The SMALLEST file that ingests with a record stream: a file the
            # validator rejects, or one with no records, cannot carry a window.
            for path in _candidate_fits()[:10]:
                probe.unlink(missing_ok=True)
                try:
                    ingest(path, probe)
                except Exception:
                    continue
                con = duckdb.connect(str(probe), read_only=True)
                try:
                    n = con.execute("SELECT count(*) FROM records").fetchone()[0]
                finally:
                    con.close()
                if n > 0:
                    cls.fit = path
                    break
        probe.unlink(missing_ok=True)
        if cls.fit is None:
            cls._tmp.cleanup()
            raise unittest.SkipTest(
                "needs a FIT file that ingests with records (tests/fixtures/ "
                "or the raw garmin directory); none in this checkout"
            )

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def setUp(self):
        self.db = self.dir / "conf_test.duckdb"
        self.db.unlink(missing_ok=True)
        self._p = mock.patch.object(
            A, "CONFIRMATIONS", self.dir / "window_confirmations.json"
        )
        self._p.start()
        self.rows = ingest(self.fit, self.db)
        self.sha = self.rows["file_sha256"]

    def tearDown(self):
        self._p.stop()
        self.db.unlink(missing_ok=True)
        (self.dir / "window_confirmations.json").unlink(missing_ok=True)

    def window(self):
        con = duckdb.connect(str(self.db), read_only=True)
        try:
            return con.execute(
                "SELECT valid_start_utc, valid_end_utc FROM activities"
            ).fetchone()
        finally:
            con.close()

    def test_confirmation_survives_full_reingest(self):
        """THE SET-UP GOES THROUGH THE REAL WRITE PATH.

        This used to INSERT straight into the `window_confirmations` TABLE.
        That worked while `ingest()` read the table, and it stopped working
        when the window began being applied inside `build_rows` -- which has
        no database connection and reads the window_confirmations.json file
        in the athlete-state directory, the durable record the table is only
        a projection of.

        Writing into the projection was always testing a state the system
        never creates: `save_confirmation` writes the FILE, and
        `restore_into` rebuilds the table from it. So the test now makes a
        confirmation the way a confirmation is actually made, which is also
        what makes the assertion mean something -- it survives a rebuild
        because it was never in the database to begin with.
        """
        recorded_start, recorded_end = self.window()
        trimmed = recorded_end - timedelta(
            seconds=min(300, (recorded_end - recorded_start).total_seconds() / 2)
        )
        trimmed = trimmed.replace(microsecond=0)

        A.save_confirmation(
            self.sha,
            recorded_start,
            trimmed,
            datetime.now(UTC).replace(tzinfo=None),
            "test",
            source="test",
        )
        ingest(self.fit, self.db)  # the rebuild that used to wipe it
        self.assertEqual(
            self.window()[1],
            trimmed,
            "a rebuild must not discard athlete-supplied state",
        )

    def test_ingest_never_writes_to_window_confirmations(self):
        """Ingest reads confirmations; it must never create or delete them."""
        con = duckdb.connect(str(self.db), read_only=True)
        before = con.execute("SELECT count(*) FROM window_confirmations").fetchone()[0]
        con.close()
        self.assertEqual(before, 0)

        ingest(self.fit, self.db)

        con = duckdb.connect(str(self.db), read_only=True)
        after = con.execute("SELECT count(*) FROM window_confirmations").fetchone()[0]
        con.close()
        self.assertEqual(after, 0, "ingest must not write to athlete-owned tables")

    def test_recorded_window_is_used_when_no_confirmation_exists(self):
        start, end = self.window()
        con = duckdb.connect(str(self.db), read_only=True)
        rec = con.execute(
            "SELECT min(timestamp), max(timestamp) FROM records"
        ).fetchone()
        con.close()
        self.assertEqual((start, end), rec)


class ConfirmationSurvivesDatabaseDeletionTest(unittest.TestCase):
    """The database file is disposable; athlete decisions are not.

    window_confirmations living only in DuckDB was not enough -- deleting the
    database file is routine and destroyed a real confirmation. The durable
    record is a file that no rebuild touches.
    """

    def test_confirmation_file_round_trips(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            with mock.patch.object(A, "CONFIRMATIONS", d / "conf_state.json"):
                start = datetime(2025, 1, 15, 7, 0, 0)
                end = datetime(2025, 1, 15, 7, 42, 7)
                A.save_confirmation("deadbeef", start, end, datetime(2025, 1, 16), "t")

                db = d / "conf_test.duckdb"
                con = duckdb.connect(str(db))
                try:
                    con.execute(DDL)
                    n = A.restore_into(con)  # simulates a deleted database
                    got = con.execute(
                        "SELECT file_sha256, valid_start_utc, valid_end_utc "
                        "FROM window_confirmations"
                    ).fetchone()
                finally:
                    con.close()

        self.assertEqual(n, 1)
        self.assertEqual(got, ("deadbeef", start, end))


if __name__ == "__main__":
    unittest.main()
