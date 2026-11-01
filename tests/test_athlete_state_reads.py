"""Athlete-state files are read DIRECTLY, and a save is visible to the next read.

THE BUG THESE EXIST FOR. An athlete-state writer (since removed) wrote its
JSON file under data/athlete/ -- the record -- while every read answered from
a table projected into DuckDB inside `ensure_schema()`. That function runs
only on WRITE connections, i.e. CLI commands, and the MCP server opens
read-only connections exclusively. So saves succeeded to the file and every
read returned EMPTY, BOTH TOOLS REPORTING SUCCESS, until a CLI command
happened to run. The file was intact the whole time.

The writer that remains, the ride-block store, has always read its file
directly. These tests pin that property for it, so a projection added later
cannot reintroduce the outage unnoticed.

WHY THESE TESTS SPAWN A SUBPROCESS. Two handles in ONE process can share cached
catalog state, and the read path can be primed by whatever the test did
earlier. THE ORIGINAL BUG WOULD SURVIVE A SAME-PROCESS TEST: the write path and
the read path have to be in different processes for "visible to the next read"
to mean what it says at the tool boundary.

Every negative has a positive twin.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import duckdb

from trail_factors.analysis import queries as Q
from trail_factors.ingest import ride_blocks as RB
from tests.corpus import CorpusTestCase, isolated, one, use_config

REPO = Path(__file__).resolve().parent.parent

# SYNTHETIC: one stationary ride, 50 minutes of timer time.
RIDE_RUN_ID = "2025-01-15T0700Z"
RIDE_AID = "a-ride-1"


def _scratch_db(directory: Path, with_ride: bool = False) -> Path:
    """A database with the schema, inside a TemporaryDirectory.

    The child processes below open a READ-ONLY connection, exactly as an MCP
    tool does, and DuckDB opens read-only only on a file that exists. The file
    lives and dies in the caller's temp dir: these tests never touch the
    corpus, and nothing here may create data/ultra.duckdb on a clone.
    `with_ride` adds one synthetic ride for a ride-block read to place.
    """
    from trail_factors.ingest.schema import DDL
    db = directory / "scratch.duckdb"
    con = duckdb.connect(str(db))
    try:
        con.execute(DDL)
        if with_ride:
            con.execute(
                "INSERT INTO runs (run_id, start_time_utc, local_tz, sport, "
                "created_at) VALUES (?, TIMESTAMP '2025-01-15 07:00:00', 0.0, "
                "'cycling', TIMESTAMP '2025-01-15 09:00:00')", [RIDE_RUN_ID])
            con.execute(
                "INSERT INTO activities (activity_id, run_id, file_hash, "
                "source_file, start_time_utc, local_tz, source, sport, "
                "timer_time_s, sampling_regime, pipeline_version) VALUES "
                "(?, ?, 'h1', 'x.fit', TIMESTAMP '2025-01-15 07:00:00', 0.0, "
                "'garmin_watch', 'cycling', 3000.0, '1hz', 1)",
                [RIDE_AID, RIDE_RUN_ID])
    finally:
        con.close()
    return db


def _payload(blocks=None) -> dict:
    return {"run_id": RIDE_RUN_ID, "raw_text": "synthetic fixture",
            "blocks": blocks if blocks is not None else [
                {"resistance": 7.0, "rpm": [90, 95], "end_min": 30},
                {"resistance": 9.25, "rpm": [80, 90], "end_min": 50}]}


def _read_in_a_fresh_process(script: str, *args: str) -> dict:
    """Run `script` in a new interpreter and parse the JSON it prints.

    Nothing is shared with this process: not the connection, not the module
    state, not DuckDB's catalog cache.
    """
    proc = subprocess.run([sys.executable, "-c", script, *args],
                          capture_output=True, text=True, cwd=REPO)
    if proc.returncode != 0:
        raise AssertionError(f"child failed:\n{proc.stdout}\n{proc.stderr}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


RIDE_CHILD = """
import json, sys, duckdb
from pathlib import Path
from trail_factors.ingest import ride_blocks as RB
# The child points at the SAME redirected file the parent wrote to. It opens
# its own read-only connection, exactly as an MCP tool does.
RB.RIDE_BLOCKS = Path(sys.argv[1])
RB.BIKE_CONFIG = Path(sys.argv[1]).parent / "bike_config.json"
from trail_factors.analysis import rides as RD
con = duckdb.connect(sys.argv[2], read_only=True)
try:
    t = RD.time_in_resistance(con)
    print(json.dumps({"n_weeks": t["n_weeks"],
                      "total_min": [w["total_min"] for w in t["weeks"]]}))
finally:
    con.close()
"""


class _RideStore:
    """The ride-block store and bike config redirected into `directory`."""

    def __init__(self, directory: Path):
        self.path = directory / "ride_blocks.json"
        self._p = [mock.patch.object(RB, "RIDE_BLOCKS", self.path),
                   mock.patch.object(RB, "BIKE_CONFIG",
                                     directory / "bike_config.json")]

    def __enter__(self):
        for p in self._p:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._p):
            p.stop()
        return False


class TestASaveIsVisibleToTheNextREAD(unittest.TestCase):
    """The round trip the bug broke, across a process boundary."""

    def test_a_ride_block_save_is_visible_to_a_separate_process(self):
        with tempfile.TemporaryDirectory() as d:
            db = _scratch_db(Path(d), with_ride=True)
            with _RideStore(Path(d)) as store:
                RB.save_ride_blocks(_payload(), duration_min=50.0)
            seen = _read_in_a_fresh_process(RIDE_CHILD, str(store.path),
                                            str(db))
        self.assertEqual(seen["n_weeks"], 1)
        self.assertEqual(seen["total_min"], [50.0])

    def test_a_supersede_is_visible_to_a_separate_process(self):
        """The replacement, not the original, is what the next read sees."""
        with tempfile.TemporaryDirectory() as d:
            db = _scratch_db(Path(d), with_ride=True)
            with _RideStore(Path(d)) as store:
                RB.save_ride_blocks(_payload(), duration_min=50.0)
                RB.save_ride_blocks(
                    _payload([{"resistance": 7.0, "end_min": 40}]),
                    duration_min=50.0, supersede=True)
            seen = _read_in_a_fresh_process(RIDE_CHILD, str(store.path),
                                            str(db))
        self.assertEqual(seen["total_min"], [40.0])

    def test_an_EMPTY_store_reads_as_empty_in_a_separate_process(self):
        """THE TWIN. Without it, a child that always reported a week -- or
        that read the athlete's real file instead of the temp one -- would
        pass both tests above."""
        with tempfile.TemporaryDirectory() as d:
            db = _scratch_db(Path(d), with_ride=True)
            seen = _read_in_a_fresh_process(
                RIDE_CHILD, str(Path(d) / "ride_blocks.json"), str(db))
        self.assertEqual(seen["n_weeks"], 0)
        self.assertEqual(seen["total_min"], [])


class TestTheSaveToolAssertsItsOwnVisibility(unittest.TestCase):
    """A save that reports file state accurately but does not know whether a
    read will see it is what misled the chat in the original outage."""

    # The read-back opens a read-only connection to DB_FILE. Point it at a
    # scratch database holding one synthetic ride: the claim is about the
    # FILE being visible to a read, and needs no corpus -- and none may be
    # created on a clone.
    def setUp(self):
        from trail_factors import mcp_server as M
        self._tmp = tempfile.TemporaryDirectory()
        d = Path(self._tmp.name)
        self._patches = [mock.patch.object(M, "DB_FILE",
                                           _scratch_db(d, with_ride=True)),
                         _RideStore(d), use_config()]
        for p in self._patches:
            p.__enter__()

    def tearDown(self):
        for p in reversed(self._patches):
            p.__exit__(None, None, None)
        self._tmp.cleanup()

    def _save(self, payload, **kw):
        from trail_factors import mcp_server as M
        return json.loads(M.save_ride_blocks(payload=json.dumps(payload), **kw))

    def test_the_response_carries_the_read_back(self):
        out = self._save(_payload())
        self.assertEqual(out["outcome"], "result", out)
        seen = out["payload"]["visible_to_read"]
        self.assertTrue(seen["confirmed"])
        self.assertEqual(seen["revision"], 0)
        self.assertEqual(seen["n_blocks"], 2)

    def test_the_read_back_follows_a_SUPERSEDE_to_the_new_revision(self):
        """The twin that a hardcoded `revision: 0` would fail."""
        self._save(_payload())
        out = self._save(_payload([{"resistance": 7.0, "end_min": 40}]),
                         supersede=True)
        self.assertEqual(out["outcome"], "result", out)
        seen = out["payload"]["visible_to_read"]
        self.assertEqual(seen["revision"], 1)
        self.assertEqual(seen["n_blocks"], 1)

    def test_an_INVISIBLE_save_is_reported_as_broken_not_as_success(self):
        """THE NEGATIVE TWIN, with the read path stubbed to the empty answer
        the bug produced. It must not read as a rejection either: the payload
        was fine and the write happened, so re-sending it would be wrong."""
        with mock.patch("trail_factors.analysis.rides.ride_blocks",
                        return_value={"status": "unavailable",
                                      "revision": None}):
            out = self._save(_payload())
        self.assertEqual(out["outcome"], "broken")
        self.assertIn("WRITTEN", out["text"])
        self.assertIn("do NOT", out["remedy"])
        self.assertFalse(out["retryable"])
        self.assertTrue(RB.RIDE_BLOCKS.exists(), "the write did land")


class TestAStaleProjectionSaysSoRatherThanReturningEmpty(unittest.TestCase):
    """`annotations` and `window_confirmations` are still projections. Their
    write paths are CLI-only and run ensure_schema, so they are not exposed the
    way the original writer was -- but the mechanism is identical, and an
    empty result and an unrefreshed projection are different facts."""

    def _con(self):
        from trail_factors.ingest.schema import DDL
        con = duckdb.connect(":memory:")
        con.execute(DDL)
        return con

    def test_a_table_that_disagrees_with_its_file_reports_stale(self):
        con = self._con()
        try:
            with tempfile.TemporaryDirectory() as d:
                notes = Path(d) / "notes.json"
                notes.write_text(json.dumps(
                    [{"run_id": "r1", "note": "a", "noted_at": "2025-01-15"},
                     {"run_id": "r2", "note": "b", "noted_at": "2025-01-15"}]))
                with mock.patch("trail_factors.ingest.athlete_state.NOTES", notes):
                    status = Q.projection_status(con, "annotations")
            self.assertEqual(status["status"], "stale_projection")
            self.assertEqual((status["rows"], status["source_rows"]), (0, 2))
            self.assertIn("verify", status["rebuild"])
            self.assertIn("different facts", status["do_not"])
        finally:
            con.close()

    def test_a_table_that_agrees_reports_ok(self):
        """THE POSITIVE TWIN: a guard that always cried stale would be ignored
        within a week."""
        con = self._con()
        try:
            with tempfile.TemporaryDirectory() as d:
                notes = Path(d) / "notes.json"
                notes.write_text("[]")
                with mock.patch("trail_factors.ingest.athlete_state.NOTES", notes):
                    self.assertEqual(
                        Q.projection_status(con, "annotations")["status"], "ok")
        finally:
            con.close()

    def test_both_remaining_projections_are_covered(self):
        con = self._con()
        try:
            for table in ("annotations", "window_confirmations"):
                self.assertIn(Q.projection_status(con, table)["status"],
                              ("ok", "stale_projection"), table)
        finally:
            con.close()


class TestTheReportSurfacesTheProjectionState(CorpusTestCase):
    """The exposed reader: `report` is an MCP tool, so this is where a chat
    would otherwise be told 'no notes' about a run that has them.

    WHICH RUN does not matter -- the notes projection is corpus-wide -- so the
    subject is simply the latest watch activity. The notes FILE is synthetic
    and sized against the corpus's own `annotations` table, so the two cases
    are stale and agreeing on any athlete's corpus."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.aid = one(cls.con,
                      "SELECT activity_id FROM activities "
                      "WHERE source = 'garmin_watch' "
                      "ORDER BY start_time_utc DESC LIMIT 1",
                      what="garmin_watch activity")[0]
        cls.n_annotations = cls.con.execute(
            "SELECT count(*) FROM annotations").fetchone()[0]

    @staticmethod
    def _notes(n: int) -> str:
        return json.dumps([{"run_id": f"r{i}", "note": "synthetic",
                            "noted_at": "2025-01-15"} for i in range(n)])

    def test_the_report_surfaces_a_stale_notes_projection(self):
        from trail_factors.analysis.report import build
        with isolated() as d:
            # One MORE note than the table holds: stale on any corpus.
            (d / "notes.json").write_text(self._notes(self.n_annotations + 1))
            rep = build(self.con, self.aid)
        athlete = rep["athlete"]
        self.assertEqual(athlete["notes_status"], "stale_projection")
        self.assertIn("verify", athlete["notes_projection"]["rebuild"])

    def test_the_report_says_ok_when_the_projection_agrees(self):
        """THE TWIN."""
        from trail_factors.analysis.report import build
        with isolated() as d:
            (d / "notes.json").write_text(self._notes(self.n_annotations))
            rep = build(self.con, self.aid)
        self.assertEqual(rep["athlete"]["notes_status"], "ok")
        self.assertIsNone(rep["athlete"]["notes_projection"])


class TestALockedDatabaseFailsFast(unittest.TestCase):
    """A read that blocked for minutes was indistinguishable from a server that
    never started, which is how this bug was first mis-diagnosed.

    EVERYTHING POINTS AT A SCRATCH DATABASE in a TemporaryDirectory, never at
    data/ultra.duckdb: the lock holder opens a WRITE connection, and on a
    clone that would CREATE the corpus file. The call reads no athlete state
    and an empty config."""

    def setUp(self):
        from trail_factors import mcp_server as M
        self._tmp = tempfile.TemporaryDirectory()
        self.db = _scratch_db(Path(self._tmp.name))
        self._patches = [mock.patch.object(M, "DB_FILE", self.db),
                         use_config()]
        for p in self._patches:
            p.__enter__()

    def tearDown(self):
        for p in reversed(self._patches):
            p.__exit__(None, None, None)
        self._tmp.cleanup()

    def test_a_held_write_lock_returns_a_named_error_in_seconds(self):
        import time

        from trail_factors import mcp_server as M
        holder = subprocess.Popen(
            [sys.executable, "-c",
             "import sys, duckdb, time; c = duckdb.connect(sys.argv[1]); "
             "print('held', flush=True); time.sleep(30)", str(self.db)],
            cwd=REPO, stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "held")
            started = time.monotonic()
            out = json.loads(M.list_runs())
            elapsed = time.monotonic() - started
        finally:
            holder.kill()
            holder.wait()
            holder.stdout.close()
        self.assertEqual(out["outcome"], "broken")
        self.assertLess(elapsed, M.CONNECT_TIMEOUT_S + 2.0)
        # IT NAMES THE LOCK. "broken" alone would send a chat looking for a
        # corrupt corpus, and the remedy says not to re-ingest.
        self.assertIn("another process holds it", out["text"])
        self.assertTrue(out["retryable"])
        self.assertIn("do not re-ingest", out["remedy"])

    def test_the_same_call_succeeds_with_no_lock_held(self):
        """THE POSITIVE TWIN: without it, a server that always reported a lock
        would pass the test above."""
        from trail_factors import mcp_server as M
        out = json.loads(M.list_runs())
        self.assertEqual(out["outcome"], "result", out)


if __name__ == "__main__":
    unittest.main()
