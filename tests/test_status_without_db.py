"""sync_status and ping answer while a WRITER holds the database.

THE BUG: sync_status went through the shared envelope wrapper, which opened
DuckDB for the corpus fingerprint -- so for the whole of a sync, the one tool
that exists to watch a sync came back `broken`. Both tools now run in the
wrapper's no-database mode.

THE LOCK IS HELD BY A CHILD PROCESS, deliberately: a same-process DuckDB
handle conflicts by a different route (ConnectionException), and POSIX record
locks are invisible to the process that holds them -- a same-process lock
would test neither the bug nor the probe. Everything points at a scratch
database and a scratch jobs file; nothing reads data/.
"""

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import duckdb

from trail_factors import mcp_server as M
from trail_factors.analysis import jobs

HOLD = r"""
import sys, time, duckdb
con = duckdb.connect(sys.argv[1])          # a WRITE connection: the sync's lock
print("held", flush=True)
time.sleep(60)
"""


class TestNoDatabaseMode(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "locked.duckdb"
        duckdb.connect(str(self.db)).close()
        self.patches = [mock.patch.object(M, "DB_FILE", self.db),
                        mock.patch.object(jobs, "JOBS_FILE",
                                          Path(self.tmp.name) / "jobs.json")]
        for p in self.patches:
            p.start()
        self.holder = None

    def tearDown(self):
        if self.holder is not None:
            self.holder.kill()
            self.holder.wait()
            self.holder.stdout.close()
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def hold_write_lock(self):
        self.holder = subprocess.Popen([sys.executable, "-c", HOLD, str(self.db)],
                                       stdout=subprocess.PIPE, text=True)
        self.assertEqual(self.holder.stdout.readline().strip(), "held")

    def test_the_lock_is_real(self):
        """Positive control: a corpus tool DOES fail against this lock, so the
        two passes below are not passing because nothing was locked."""
        self.hold_write_lock()
        r = json.loads(M.list_runs())
        self.assertEqual(r["outcome"], M.BROKEN)

    def test_sync_status_is_ok_while_write_locked(self):
        self.hold_write_lock()
        r = json.loads(M.sync_status())
        self.assertEqual(r["outcome"], M.RESULT, r)
        self.assertEqual(r["corpus_fingerprint"],
                         {"status": "not_taken",
                          "reason": "job-state read; no corpus access"})

    def test_ping_is_ok_while_write_locked_and_says_so(self):
        self.hold_write_lock()
        t0 = time.monotonic()
        r = json.loads(M.ping())
        self.assertLess(time.monotonic() - t0, M.CONNECT_TIMEOUT_S,
                        "ping must not wait on the lock")
        self.assertEqual(r["outcome"], M.RESULT, r)
        db = r["payload"]["database"]
        self.assertIs(db["write_locked"], True, db)
        self.assertEqual(db["holder_pid"], self.holder.pid)
        self.assertEqual(r["corpus_fingerprint"]["status"], "not_taken")

    def test_positive_twin_ping_says_free_when_nothing_holds_it(self):
        r = json.loads(M.ping())
        self.assertEqual(r["outcome"], M.RESULT)
        self.assertIs(r["payload"]["database"]["write_locked"], False)

    def test_a_reader_is_not_a_lock(self):
        """A read-only handle elsewhere does not stop a read; reporting it as
        locked would send a chat to wait for nothing."""
        self.holder = subprocess.Popen(
            [sys.executable, "-c",
             HOLD.replace("duckdb.connect(sys.argv[1])",
                          "duckdb.connect(sys.argv[1], read_only=True)"),
             str(self.db)], stdout=subprocess.PIPE, text=True)
        self.assertEqual(self.holder.stdout.readline().strip(), "held")
        self.assertIs(json.loads(M.ping())["payload"]["database"]["write_locked"],
                      False)

    def test_ping_carries_server_time_and_the_pipeline_digest(self):
        from trail_factors.analysis import versions
        p = json.loads(M.ping())["payload"]
        self.assertEqual(p["pipeline"], {"pipeline_version": versions.PIPELINE_VERSION,
                                         "digest": versions.digest()})
        self.assertIn("T", p["server_time_utc"])

    def test_neither_tool_opens_a_connection(self):
        with mock.patch.object(M, "_connect",
                               side_effect=AssertionError("opened the database")), \
             mock.patch.object(duckdb, "connect",
                               side_effect=AssertionError("opened the database")):
            for tool in (M.sync_status, M.ping):
                self.assertEqual(json.loads(tool())["outcome"], M.RESULT,
                                 tool.__name__)


if __name__ == "__main__":
    unittest.main()
