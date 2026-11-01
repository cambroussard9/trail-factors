"""The bad-data loop, end to end: reject -> confirm a window -> re-sync ingests.

THE LOOP. A run whose recording continues into a car ride fails validation
(sustained impossible pace) and gets no activity row. What makes that
recoverable is three things working together, and each has failed alone
before:

  1. the REJECTION names its reason and the exact next command -- `sync` used
     to close every rejection with "Corrupt beyond use; nothing to do", which
     is false of exactly this file;
  2. `confirm-file` resolves the RAW file by its Garmin id (there is no
     activity row to resolve) and writes the window to the athlete-state
     directory, atomically, with a revision;
  3. the next `sync` re-ingests the file at the confirmed window, which is
     applied BEFORE validation, so the trimmed run passes.

THE SUBJECT IS A REAL FIT FILE, generated at test time by tests/fitgen.py at
an arbitrary synthetic location and date: ~30 minutes on foot, then a drive
at car speed. It is parsed by fitdecode exactly as a watch file is.

THE CLI RUNS IN A CHILD PROCESS with every TRAIL_FACTORS_* path pointed into a
TemporaryDirectory, so module-level path constants bind to the sandbox from
the start and nothing can reach the real corpus, raw tree or athlete state.
The child disables the Garmin download and ALL sockets: no network.
Independent of TRAIL_FACTORS_DB -- it runs identically with it set or unset.

Every negative has a positive twin.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import duckdb

from trail_factors.ingest import fit_file as ff
from trail_factors.ingest import validate as V
from trail_factors.paths import REPO_ROOT
from tests import fitgen
from tests.isolation import git_ignores

# SYNTHETIC: a Garmin-style id and a date that belong to no one.
GID = "90000000001"
START = dt.datetime(2024, 5, 4, 6, 0, 0)
CLI = "uv run trail-factors"

# The child: no download, no sockets, then the real CLI dispatcher.
DRIVER = """
import socket, sys
from unittest import mock

def _no_network(*a, **k):
    raise OSError("network disabled in the bad-data-loop test")

socket.socket.connect = _no_network
socket.create_connection = _no_network

import trail_factors.cli.report as R
R.garmin_download_all = lambda days=None, dest=None: print("(download disabled)")
from trail_factors.cli.main import main
sys.argv = ["trail_factors.cli"] + sys.argv[1:]
main()
"""


def _fmt(t: dt.datetime) -> str:
    return t.strftime("%Y-%m-%d %H:%M:%S")


class TestTheBadDataLoopEndToEnd(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.root = root
        cls.athlete = root / "athlete"
        cls.db = root / "db" / "loop.duckdb"
        cls.fit = root / "raw" / "garmin" / f"{GID}_ACTIVITY.fit"
        cls.facts = fitgen.write_run_then_drive(cls.fit, START)
        cls.sha = ff.sha256(cls.fit)
        cls.real_athlete_existed = (REPO_ROOT / "data" / "athlete").exists()

        env = {k: v for k, v in os.environ.items()
               if not k.startswith("TRAIL_FACTORS_")}
        env.update({
            "TRAIL_FACTORS_DATA_DIR": str(root),
            "TRAIL_FACTORS_DB": str(cls.db),
            "TRAIL_FACTORS_RAW_DIR": str(root / "raw"),
            "TRAIL_FACTORS_DEM_DIR": str(root / "dem"),
            "TRAIL_FACTORS_ATHLETE_DIR": str(cls.athlete),
            "TRAIL_FACTORS_LOOKUP_DIR": str(root / "lookup"),
            "TRAIL_FACTORS_JOBS_FILE": str(root / "jobs.json"),
            "TRAIL_FACTORS_CONFIG": str(root / "no-config.yaml"),
            "TRAIL_FACTORS_GARMIN_TOKENSTORE": str(root / "garmin-tokens"),
            "TRAIL_FACTORS_GARMIN_AUTH_STATE": str(root / "garmin-auth.json"),
        })
        cls.env = env

        cls.first = cls._cli("sync")
        cls.listed = cls._cli("rejected-files")
        cls.db_after_first = cls._snapshot()
        m = re.search(r"^\s*run:\s+(.+confirm-file.+)$", cls.first.stdout,
                      re.MULTILINE)
        cls.command = m.group(1).strip() if m else None
        cls.confirm = None
        if cls.command is not None:
            parts = shlex.split(cls.command)
            cls.confirm = cls._cli(*parts[len(shlex.split(CLI)):])
        cls.second = cls._cli("sync")
        cls.listed_after = cls._cli("rejected-files")
        cls.third = cls._cli("sync")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @classmethod
    def _cli(cls, *args) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-c", DRIVER, *args],
                              cwd=REPO_ROOT, env=cls.env, capture_output=True,
                              text=True, timeout=300)

    @classmethod
    def _snapshot(cls) -> dict:
        con = duckdb.connect(str(cls.db), read_only=True)
        try:
            return {
                "n_activities": con.execute(
                    "SELECT count(*) FROM activities").fetchone()[0],
                "rejections": con.execute(
                    "SELECT check_name FROM validation_results WHERE "
                    "file_sha256 = ? AND activity_id IS NULL "
                    "AND severity = 'fatal'", [cls.sha]).fetchall()}
        finally:
            con.close()

    def _ok(self, proc):
        self.assertEqual(proc.returncode, 0,
                         f"stdout:\n{proc.stdout[-3000:]}\n"
                         f"stderr:\n{proc.stderr[-3000:]}")

    # --- 1. the first sync rejects, with a reason and the exact command ----

    def test_1a_the_first_sync_REJECTS_the_file(self):
        self._ok(self.first)
        self.assertEqual(self.db_after_first["n_activities"], 0)
        self.assertIn(("pace_sustained_impossible",),
                      self.db_after_first["rejections"])
        self.assertIn("1 file(s) REJECTED and not ingested", self.first.stdout)

    def test_1b_the_rejection_names_the_REASON_in_plain_words(self):
        reason = re.search(r"reason:\s+(.+)$", self.first.stdout, re.MULTILINE)
        self.assertIsNotNone(reason, self.first.stdout[-2000:])
        self.assertIn("faster than any running pace", reason.group(1))
        self.assertIn("vehicle", reason.group(1))

    def test_1c_the_next_step_is_the_EXACT_command_with_the_suggested_end(self):
        """The suggested end is the last ON-FOOT record: the stand and the
        acceleration before the impossible speed are part of the drive."""
        want = (f"{CLI} confirm-file {GID} "
                f"--end '{_fmt(self.facts['last_on_foot_utc'])}'")
        self.assertEqual(self.command, want, self.first.stdout[-2000:])
        self.assertIn(f"then:      {CLI} sync", self.first.stdout)

    def test_1d_it_points_at_rejected_files(self):
        self.assertIn(f"Everything still outstanding: {CLI} rejected-files",
                      self.first.stdout)

    def test_1e_the_old_GIVE_UP_line_is_gone(self):
        self.assertNotIn("Corrupt beyond use", self.first.stdout)
        self.assertNotIn("nothing to do", self.first.stdout)

    def test_1f_sync_asks_for_no_per_run_answers(self):
        """The only thing sync asks the athlete for is a WINDOW decision."""
        for out in (self.first.stdout, self.second.stdout):
            asks = [ln for ln in out.splitlines() if ln.startswith("NEEDS ")]
            self.assertTrue(all(ln.startswith("NEEDS YOUR CONFIRMATION")
                                for ln in asks), asks)
            self.assertNotIn("answer", out.lower())
            self.assertNotIn("trail_factors.cli log ", out)

    def test_1g_rejected_files_lists_it_with_the_same_command(self):
        self._ok(self.listed)
        self.assertIn(GID, self.listed.stdout)
        self.assertIn(self.command, self.listed.stdout)

    # --- 2. confirming the window writes per-user state --------------------

    def test_2a_confirm_file_succeeds(self):
        self.assertIsNotNone(self.confirm, "no command was printed to run")
        self._ok(self.confirm)
        self.assertIn(f"confirmed {self.fit.name} (revision 0)",
                      self.confirm.stdout)
        self.assertIn(f"{CLI} sync", self.confirm.stdout)

    def test_2b_the_window_is_stored_in_the_ATHLETE_DIR(self):
        path = self.athlete / "window_confirmations.json"
        self.assertTrue(path.exists())
        rows = json.loads(path.read_text())
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["file_sha256"], self.sha)
        self.assertEqual(row["valid_end_utc"],
                         self.facts["last_on_foot_utc"].isoformat())
        self.assertIsNone(row["valid_start_utc"])
        self.assertEqual(row["revision"], 0)
        self.assertEqual(row["source"], "cli")
        # Atomic write: no scratch file left beside it.
        self.assertEqual(list(self.athlete.glob("*.tmp")), [])

    def test_2c_the_REAL_athlete_dir_was_never_touched(self):
        self.assertEqual((REPO_ROOT / "data" / "athlete").exists(),
                         self.real_athlete_existed)

    def test_2d_window_confirmations_are_gitignored(self):
        """Per-user state: never committed."""
        self.assertTrue(git_ignores("data/athlete/window_confirmations.json"))

    # --- 3. the second sync ingests the TRIMMED run ------------------------

    def _con(self):
        con = duckdb.connect(str(self.db), read_only=True)
        self.addCleanup(con.close)
        return con

    def test_3a_the_second_sync_ingests_it(self):
        self._ok(self.second)
        self.assertNotIn("REJECTED", self.second.stdout)
        n = self._con().execute(
            "SELECT count(*) FROM activities WHERE file_hash = ?",
            [self.sha]).fetchone()[0]
        self.assertEqual(n, 1)

    def test_3b_the_window_is_the_confirmed_one(self):
        applied, ve, dur, timer = self._con().execute(
            "SELECT window_applied, valid_end_utc, duration_s, timer_time_s "
            "FROM activities WHERE file_hash = ?", [self.sha]).fetchone()
        self.assertTrue(applied)
        self.assertEqual(ve, self.facts["last_on_foot_utc"])
        self.assertAlmostEqual(dur, self.facts["last_on_foot_s"], places=3)
        self.assertAlmostEqual(timer, self.facts["last_on_foot_s"], places=3)

    def test_3c_no_record_after_the_window_end(self):
        n, last = self._con().execute(
            "SELECT count(*), max(r.timestamp) FROM records r "
            "JOIN activities a USING (activity_id) WHERE a.file_hash = ?",
            [self.sha]).fetchone()
        self.assertEqual(n, self.facts["last_on_foot_s"] + 1)
        self.assertLessEqual(last, self.facts["last_on_foot_utc"])

    def test_3d_no_car_speed_record_survives(self):
        fastest = self._con().execute(
            "SELECT max(r.speed_mps) FROM records r JOIN activities a "
            "USING (activity_id) WHERE a.file_hash = ?", [self.sha]).fetchone()[0]
        self.assertLessEqual(fastest, self.facts["max_on_foot_mps"] + 1e-6)
        self.assertLess(fastest, V.PACE_FATAL_MPS)

    def test_3e_distance_is_the_trimmed_distance_and_the_whole_file_is_kept(self):
        dist, whole = self._con().execute(
            "SELECT total_distance_m, session_distance_m_whole_file "
            "FROM activities WHERE file_hash = ?", [self.sha]).fetchone()
        self.assertAlmostEqual(dist, self.facts["odometer_at_last_on_foot_m"],
                               places=1)
        self.assertAlmostEqual(whole, self.facts["odometer_total_m"], places=1)
        self.assertLess(dist, whole)

    def test_3f_the_car_split_is_gone_and_the_partition_tiles(self):
        con = self._con()
        rows = con.execute(
            "SELECT s.split_type, s.start_time, s.total_timer_time "
            "FROM splits s JOIN activities a USING (activity_id) "
            "WHERE a.file_hash = ? ORDER BY s.start_time", [self.sha]).fetchall()
        self.assertEqual([r[0] for r in rows], ["rwd_run", "rwd_walk"])
        self.assertTrue(all(r[1] < self.facts["last_on_foot_utc"] for r in rows))
        self.assertAlmostEqual(sum(r[2] for r in rows),
                               self.facts["last_on_foot_s"], places=3)

    # --- 3h. the rescue is ANNOUNCED, once --------------------------------

    def test_3h_the_second_sync_ANNOUNCES_the_rescued_run(self):
        """The re-sync downloads nothing, so the new-run lines stay empty;
        the rescued run used to arrive in silence. It is named, with its
        trimmed figures beside the whole file's."""
        out = self.second.stdout
        self.assertIn("1 previously rejected file(s) INGESTED", out)
        self.assertIn(f"{GID}  now ingested at the confirmed window", out)
        self.assertIn(f"RESCUED  {self.fit.name}", out)
        km = self.facts["odometer_at_last_on_foot_m"] / 1000.0
        whole = self.facts["odometer_total_m"] / 1000.0
        self.assertIn(f"{km:.2f} km in "
                      f"{self.facts['last_on_foot_s'] / 60.0:.0f} min", out)
        self.assertIn(f"(the whole file was {whole:.2f} km", out)

    def test_3i_the_rescued_run_enters_the_report_loop(self):
        """Its §16.2 report is attempted like a new run's: rendered, or a
        named failure -- never skipped."""
        tail = self.second.stdout.split("WHAT THIS MEANS", 1)[1]
        # RENDERED, not merely attempted: on a one-activity corpus the report
        # used to fail inside drift_onset (an insufficient curve lacked
        # duration_min), and "report failed" would have passed this test.
        self.assertIn("POST-RUN REPORT", tail)
        self.assertNotIn(f"report failed for {GID}", tail)

    def test_3j_twin_nothing_is_announced_before_or_after(self):
        """Announced ONCE: not by the sync that rejected it, and not by a
        later sync that re-ingests it at the same window."""
        self._ok(self.third)
        for out in (self.first.stdout, self.third.stdout):
            self.assertNotIn("previously rejected", out)
            self.assertNotIn("RESCUED", out)
        tail = self.third.stdout.split("WHAT THIS MEANS", 1)[1]
        self.assertNotRegex(tail, rf"POST-RUN REPORT|report failed for {GID}")

    def test_3g_the_rejection_is_cleared(self):
        self._ok(self.listed_after)
        self.assertIn("0 file(s)", self.listed_after.stdout)
        self.assertNotIn(GID, self.listed_after.stdout)
        n = self._con().execute(
            "SELECT count(*) FROM validation_results WHERE file_sha256 = ? "
            "AND activity_id IS NULL", [self.sha]).fetchone()[0]
        self.assertEqual(n, 0)


def _records(**kw) -> list[dict]:
    msgs, facts = fitgen.run_then_drive(START, **kw)
    return [f for name, f in msgs if name == "record"], facts


class TestTheVehicleSuggestion(unittest.TestCase):
    """The detector the rejection's command is built from."""

    def test_a_trailing_drive_suggests_the_last_on_foot_record(self):
        recs, facts = _records()
        s = V.suggest_vehicle_window(recs, "running")
        self.assertEqual(ff.to_utc_naive(s["suggested_valid_end"]),
                         facts["last_on_foot_utc"])
        self.assertIsNone(s["suggested_valid_start"])
        self.assertEqual(s["midrun_blocks"], 0)

    def test_the_check_and_the_suggestion_see_the_SAME_block(self):
        recs, _ = _records()
        fatal = [f for f in V.silver_checks(recs, {"coverage_fraction": 1.0,
                                                   "contiguous": True})
                 if f.is_fatal]
        self.assertEqual([f.check for f in fatal], ["pace_sustained_impossible"])
        span = max(b["span_s"] for b in V.impossible_speed_blocks(recs))
        self.assertAlmostEqual(fatal[0].observed, span)

    def test_a_run_with_NO_drive_suggests_nothing(self):
        """THE TWIN: the suggestion is about the drive, not about runs."""
        recs, _ = _records(drive_s=0, ramp_s=0)
        s = V.suggest_vehicle_window(recs, "running")
        self.assertIsNone(s["suggested_valid_end"])
        self.assertIsNone(s["suggested_valid_start"])

    def test_a_drive_shorter_than_the_sustained_bound_suggests_nothing(self):
        recs, _ = _records(drive_s=int(V.PACE_SUSTAINED_S) - 15, ramp_s=0)
        self.assertIsNone(
            V.suggest_vehicle_window(recs, "running")["suggested_valid_end"])

    def test_a_MIDRUN_drive_is_not_windowed_away(self):
        """Running on both sides: no start/end window can remove it, and a
        suggestion would discard half a run."""
        recs, _ = _records()
        more = []
        last = recs[-1]
        for i in range(1, 1201):
            more.append(dict(last, timestamp=last["timestamp"]
                             + dt.timedelta(seconds=i),
                             distance=last["distance"] + 2.9 * i,
                             enhanced_speed=2.9, cadence=85, heart_rate=140))
        s = V.suggest_vehicle_window(recs + more, "running")
        self.assertIsNone(s["suggested_valid_end"])
        self.assertIsNone(s["suggested_valid_start"])
        self.assertEqual(s["midrun_blocks"], 1)

    def test_a_LEADING_drive_suggests_a_start(self):
        recs, facts = _records()
        # Mirror the timeline: the drive comes first.
        t0, t1 = recs[0]["timestamp"], recs[-1]["timestamp"]
        mirrored = [dict(r, timestamp=t0 + (t1 - r["timestamp"]))
                    for r in recs]
        s = V.suggest_vehicle_window(mirrored, "running")
        self.assertIsNone(s["suggested_valid_end"])
        self.assertEqual(s["suggested_valid_start"],
                         t0 + (t1 - (START.replace(tzinfo=dt.timezone.utc)
                                     + dt.timedelta(seconds=facts["last_on_foot_s"]))))

    def test_a_clean_trailing_drive_cuts_at_the_suggestion_for_free(self):
        """THE CUT AGREES WITH THE PROPOSAL where both exist: same bound,
        no on-foot movement discarded."""
        recs, facts = _records()
        s = V.suggest_vehicle_window(recs, "running")
        self.assertEqual(s["cut"]["bound"], "end")
        self.assertEqual(s["cut"]["at"], s["suggested_valid_end"])
        self.assertEqual(s["cut"]["discards_on_foot_s"], 0.0)

    def test_a_walk_past_the_edge_bound_is_not_proposed_but_IS_cut(self):
        """The 300 s bound decides what is PROPOSED, never what is
        recoverable."""
        recs, facts = _records(tail=(("walk", 360),))
        s = V.suggest_vehicle_window(recs, "running")
        self.assertIsNone(s["suggested_valid_end"])
        self.assertEqual(s["midrun_blocks"], 1)
        self.assertEqual(ff.to_utc_naive(s["cut"]["at"]),
                         facts["last_on_foot_utc"])
        self.assertAlmostEqual(s["cut"]["discards_on_foot_s"], 360, delta=2)
        # The other end would cost the whole run, so it is not the one chosen.
        self.assertGreater(s["cut_start"]["discards_on_foot_s"],
                           s["cut_end"]["discards_on_foot_s"])

    def test_no_cadence_means_nothing_on_foot_and_no_cut(self):
        recs, _ = _records(cadence=False)
        s = V.suggest_vehicle_window(recs, "running")
        self.assertEqual(s["n_blocks"], 1)
        self.assertIs(s["has_cadence"], False)
        self.assertEqual(s["n_on_foot_records"], 0)
        self.assertIsNone(s["cut"])
        self.assertEqual(s["midrun_blocks"], 0)

    def test_a_non_running_sport_is_not_bound(self):
        """Pace bounds are a RUNNING bound (§4.1); a ride is never 'a car'."""
        recs, _ = _records()
        s = V.suggest_vehicle_window(recs, "cycling")
        self.assertIsNone(s["suggested_valid_end"])


class TestRejectionAdvice(unittest.TestCase):
    """What a rejection says for each kind, without running a sync."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.fit = Path(cls.tmp.name) / f"{GID}_ACTIVITY.fit"
        cls.facts = fitgen.write_run_then_drive(cls.fit, START)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _advice(self, checks):
        from trail_factors.cli import athlete
        return athlete.rejection_advice(self.fit, "msg", checks)

    def test_a_vehicle_tail_gets_a_command(self):
        a = self._advice(["pace_sustained_impossible"])
        self.assertTrue(a["fixable_by_window"])
        self.assertIn("confirm-file", a["command"])
        self.assertIn(_fmt(self.facts["last_on_foot_utc"]), a["command"])

    def test_corrupt_timestamps_get_NO_command_and_say_what_to_check(self):
        a = self._advice(["timestamp_integrity"])
        self.assertFalse(a["fixable_by_window"])
        self.assertIsNone(a["command"])
        self.assertIn("Garmin Connect", a["next_step"])
        self.assertIn("timestamps are corrupt", a["reason"])

    def test_an_unknown_check_still_carries_the_message(self):
        a = self._advice(["some_future_check"])
        self.assertIn("msg", a["reason"])
        self.assertTrue(a["next_step"])


if __name__ == "__main__":
    unittest.main()
