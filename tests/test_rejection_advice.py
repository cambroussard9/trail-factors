"""A rejection's advice must not assert a cause it did not check.

When the window detector cannot read the file, nothing is known about where
the bad stretch sits. The advice must then give the confirm-file command with
the times left open -- not claim the stretch is mid-run and strand the run.
"""

import contextlib
import datetime as dt
import importlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import duckdb

from tests import fitgen
from trail_factors.cli import athlete as A
from trail_factors.ingest import fit_file as ff
from trail_factors.ingest import validate as V


class TestAnUnreadableFileGetsTheOpenCommand(unittest.TestCase):
    def advice(self, checks):
        # The real layout: a Garmin file lives in RAW_GARMIN and is named by
        # its id. The file is never written, so the detector cannot read it.
        with (
            tempfile.TemporaryDirectory() as d,
            mock.patch.object(A, "RAW_GARMIN", Path(d)),
        ):
            missing = Path(d) / "10000000001_ACTIVITY.fit"
            return A.rejection_advice(missing, "synthetic", checks)

    def test_a_vehicle_finding_with_no_suggestion_keeps_the_command(self):
        a = self.advice(["pace_sustained_impossible"])
        self.assertIsNotNone(a["suggest_error"])
        self.assertIn("confirm-file 10000000001 --end", a["command"])
        self.assertIsNone(a["fixable_by_window"])  # unknown, not False
        self.assertNotIn("MIDDLE", a["next_step"])
        self.assertIn("could not read", a["next_step"])

    def test_a_cause_no_window_can_fix_still_gets_no_command(self):
        """Twin: corrupt timestamps are unfixable whatever the detector says."""
        a = self.advice(["timestamp_integrity"])
        self.assertIsNone(a["command"])
        self.assertTrue(a["next_step"])

    def test_an_unreadable_file_outside_the_garmin_dir_still_gets_advice(self):
        """The advice must never hide the rejection -- not even when the file
        cannot be hashed to name it."""
        with tempfile.TemporaryDirectory() as d:
            gone = Path(d) / "elsewhere.fit"  # never written
            a = A.rejection_advice(gone, "synthetic", ["pace_sustained_impossible"])
        self.assertEqual(a["ref"], "elsewhere.fit")
        self.assertIn("confirm-file elsewhere.fit --end", a["command"])


# --------------------------------------------------------------------------
# REAL FILES: the detector reads bytes, not a mocked dict. Every fixture is a
# synthetic FIT from tests/fitgen.py, written into a TemporaryDirectory that
# stands in for the raw garmin directory, at an arbitrary place and date.
# --------------------------------------------------------------------------

GID = "10000000002"
START = dt.datetime(2024, 5, 4, 6, 0, 0)
CLI = A.CLI
OPEN_END = f"{CLI} confirm-file {GID} --end 'YYYY-MM-DD HH:MM:SS'"


def _fmt(t: dt.datetime) -> str:
    return t.strftime("%Y-%m-%d %H:%M:%S")


class _Fit(unittest.TestCase):
    """One synthetic file per test, named by its Garmin id in RAW_GARMIN."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        p = mock.patch.object(A, "RAW_GARMIN", self.dir)
        p.start()
        self.addCleanup(p.stop)

    def advice(self, checks=("pace_sustained_impossible",), **kw):
        fit = self.dir / f"{GID}_ACTIVITY.fit"
        self.facts = fitgen.write_run_then_drive(fit, START, **kw)
        self.fit = fit
        return A.rejection_advice(fit, "synthetic", list(checks))


class TestNoCadenceChannelIsNotCalledMidRun(_Fit):
    """(1) With no cadence channel no record reads as on foot, so the
    detector places nothing. It must SAY that -- not claim the drive is in
    the middle with running on both sides, which nobody checked."""

    def test_the_advice_says_it_could_not_place_it_and_gives_the_open_command(self):
        a = self.advice(cadence=False)
        s = a["suggestion"]
        self.assertEqual(s["vehicle_midrun_blocks"], 0)
        self.assertIs(s["vehicle_has_cadence"], False)
        self.assertNotIn("MIDDLE", a["next_step"])
        self.assertNotIn("running on both sides", a["next_step"])
        self.assertIn("could not place", a["next_step"])
        self.assertIn("no cadence channel", a["next_step"])
        self.assertEqual(a["command"], OPEN_END)
        self.assertIsNone(a["fixable_by_window"])  # unknown, not False

    def test_twin_the_same_file_WITH_cadence_gets_the_filled_command(self):
        a = self.advice()
        self.assertTrue(a["fixable_by_window"])
        self.assertEqual(
            a["command"],
            f"{CLI} confirm-file {GID} --end '{_fmt(self.facts['last_on_foot_utc'])}'",
        )


class TestAWalkAfterTheDriveIsACutNotADeadEnd(_Fit):
    """(2) Six minutes of walking from the parked car puts the drive past
    VEHICLE_EDGE_FOOT_MAX_S, so no window is PROPOSED -- but `--end` before
    the drive still removes it, at the cost of that walk, and says so."""

    WALK_S = 360

    def test_the_cut_is_offered_with_its_cost(self):
        self.assertGreater(self.WALK_S, V.VEHICLE_EDGE_FOOT_MAX_S)
        a = self.advice(tail=(("walk", self.WALK_S),))
        s = a["suggestion"]
        self.assertIsNone(s["suggested_valid_end"])  # not PROPOSED
        self.assertEqual(
            a["command"],
            f"{CLI} confirm-file {GID} --end '{_fmt(self.facts['last_on_foot_utc'])}'",
        )
        self.assertFalse(a["fixable_by_window"])  # not junk-only
        self.assertAlmostEqual(a["cut_discards_on_foot_s"], self.WALK_S, delta=2)
        self.assertIn(f"DISCARDING {a['cut_discards_on_foot_s']:.0f} s", a["next_step"])
        self.assertNotIn("running on both sides", a["next_step"])

    def test_the_cut_verifies_clean(self):
        a = self.advice(tail=(("walk", self.WALK_S),))
        self.assertEqual(a["suggestion"]["vehicle_cut"]["remaining_blocks"], 0)

    def test_twin_a_short_walk_is_still_PROPOSED_as_junk_removal(self):
        a = self.advice(tail=(("walk", 240),))
        self.assertTrue(a["fixable_by_window"])
        self.assertIsNone(a["cut_discards_on_foot_s"])
        self.assertIn(_fmt(self.facts["last_on_foot_utc"]), a["command"])


class TestAnEdgeWindowOverAMidRunDriveIsNotTheFix(_Fit):
    """(3) Run, drive, run, drive. The detector proposes an end before the
    SECOND drive, which leaves the first inside the window: confirming it
    would earn the same rejection on the next sync, forever."""

    TAIL = (("run", 600), ("ramp", 10), ("drive", 300))

    def test_it_is_not_marked_fixable_and_says_a_block_remains(self):
        a = self.advice(tail=self.TAIL)
        s = a["suggestion"]
        self.assertIsNotNone(s["suggested_valid_end"])
        self.assertEqual(s["suggested_window_remaining_blocks"], 1)
        self.assertIsNot(a["fixable_by_window"], True)
        self.assertIn("1 impossible block(s) remain", a["next_step"])
        self.assertNotIn(_fmt(s["suggested_valid_end"]), a["command"])

    def test_the_offered_command_actually_clears_the_file(self):
        """THE PROOF: apply the printed window to the records and run the
        fatal check itself. The edge window fails it; the cut passes."""
        from trail_factors.ingest import fit_file as ff

        a = self.advice(tail=self.TAIL)
        recs = ff.coalesce_records(ff.read_messages(self.fit)["record"])

        def fatal(end):
            kept = [r for r in recs if ff.to_utc_naive(r["timestamp"]) <= end]
            return [
                f.check
                for f in V.silver_checks(
                    kept, {"coverage_fraction": 1.0, "contiguous": True}
                )
                if f.is_fatal
            ]

        edge = a["suggestion"]["suggested_valid_end"]
        self.assertEqual(fatal(edge), ["pace_sustained_impossible"])
        self.assertEqual(
            a["command"],
            f"{CLI} confirm-file {GID} --end '{_fmt(self.facts['last_on_foot_utc'])}'",
        )
        self.assertEqual(fatal(self.facts["last_on_foot_utc"]), [])
        self.assertAlmostEqual(a["cut_discards_on_foot_s"], 600, delta=2)

    def test_twin_one_trailing_drive_is_verified_and_fixable(self):
        a = self.advice()
        self.assertEqual(a["suggestion"]["suggested_window_remaining_blocks"], 0)
        self.assertTrue(a["fixable_by_window"])


class TestNoPlaceholderRef(_Fit):
    """(5) The ref is known, so no command says `<id>`, and the two flags are
    spelled separately: `--start/--end` is not a flag that exists."""

    def assert_real(self, text):
        self.assertNotIn("<id>", text)
        self.assertNotIn("<UTC>", text)
        self.assertNotIn("--start/--end", text)
        self.assertNotIn("--end/--start", text)
        self.assertNotIn("{ref}", text)

    def test_timestamp_integrity_alone_names_the_file(self):
        a = self.advice(checks=["timestamp_integrity"])
        self.assert_real(a["next_step"])
        self.assertIn(f"{CLI} confirm-file {GID} --end", a["next_step"])
        self.assertIsNone(a["command"])

    def test_the_generic_fallback_names_the_file(self):
        a = self.advice(checks=["some_future_check"], drive_s=0, ramp_s=0)
        self.assert_real(a["next_step"])
        self.assertIn(f"{CLI} confirm-file {GID} --end", a["next_step"])

    def test_a_vehicle_suggestion_survives_a_timestamp_finding(self):
        """Both fatal: the drive's window is still shown, labelled as a fix
        only if the bad gap lies outside it."""
        a = self.advice(checks=["pace_sustained_impossible", "timestamp_integrity"])
        self.assertEqual(
            a["command"],
            f"{CLI} confirm-file {GID} --end '{_fmt(self.facts['last_on_foot_utc'])}'",
        )
        self.assertIsNone(a["fixable_by_window"])
        self.assertIn("ONLY IF the bad timestamp gap", a["next_step"])
        self.assert_real(a["next_step"])


class TestRejectedFilesCoversTheWholeRawTree(unittest.TestCase):
    """(6) A rejected file outside garmin/ is named by its sha prefix and
    gets its advice and command, as ingest_all's own rejection does."""

    def setUp(self):
        from trail_factors.ingest.schema import DDL

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        raw = Path(tmp.name) / "raw"
        for p in (
            mock.patch.object(A, "RAW_DIR", raw),
            mock.patch.object(A, "RAW_GARMIN", raw / "garmin"),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.phone = raw / "strava" / "phone-run.fit"
        self.watch = raw / "garmin" / f"{GID}_ACTIVITY.fit"
        fitgen.write_run_then_drive(self.phone, START)
        fitgen.write_run_then_drive(self.watch, START, run_s=1500)
        self.con = duckdb.connect(str(Path(tmp.name) / "r.duckdb"))
        self.addCleanup(self.con.close)
        self.con.execute(DDL)
        for p in (self.phone, self.watch):
            V.persist(
                self.con,
                ff.sha256(p),
                None,
                None,
                [
                    V.Finding(
                        "pace_sustained_impossible", V.FATAL, V.SILVER, "synthetic"
                    )
                ],
            )

    def rec(self, path):
        sha = ff.sha256(path)
        [r] = [
            r for r in A.rejected_files(self.con)["rejected"] if r["file_sha256"] == sha
        ]
        return r

    def test_the_phone_file_gets_advice_and_a_sha_named_command(self):
        r = self.rec(self.phone)
        ref = ff.sha256(self.phone)[:16]
        self.assertEqual(r["file"], self.phone.name)
        self.assertIsNone(r["garmin_id"])  # no guessed id
        self.assertEqual(r["ref"], ref)
        self.assertTrue(r["next_step"])
        self.assertIn(f"confirm-file {ref} --end", r["confirm_with"])

    def test_twin_the_watch_file_keeps_its_garmin_id(self):
        r = self.rec(self.watch)
        self.assertEqual(r["garmin_id"], GID)
        self.assertIn(f"confirm-file {GID} --end", r["confirm_with"])


class TestTheRejectedFilesListingPrintsNoBlankCommand(unittest.TestCase):
    """(6) A record with no raw file has no confirm_with; the listing used to
    print an empty indented line where the command goes."""

    def run_main(self, rec):
        # The MODULE, not the `main` function trail_factors.cli re-exports under the
        # same name: patch the module that owns the names.
        M = importlib.import_module("trail_factors.cli.main")
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "m.duckdb"
            duckdb.connect(str(db)).close()
            listing = {"rejected": [rec], "n_rejected": 1, "how_to_clear": "x"}
            with (
                mock.patch.object(M, "ensure_schema", lambda: None),
                mock.patch.object(M, "DB_PATH", db),
                mock.patch.object(M, "rejected_files", return_value=listing),
                mock.patch.object(sys, "argv", ["trail_factors.cli", "rejected-files"]),
                contextlib.redirect_stdout(out),
            ):
                M.main()
        return out.getvalue()

    BASE = {
        "file_sha256": "ab" * 32,
        "checks": "pace_sustained_impossible",
        "messages": ["synthetic"],
        "garmin_id": None,
    }

    def test_a_record_without_a_file_says_so(self):
        text = self.run_main(dict(self.BASE, file=None, ref=None))
        self.assertEqual([ln for ln in text.splitlines() if ln and not ln.strip()], [])
        self.assertIn("no advice can be built", text)

    def test_twin_a_record_with_a_command_prints_it(self):
        text = self.run_main(
            dict(
                self.BASE,
                file="x.fit",
                ref="abab",
                next_step="do the thing",
                confirm_with="CMD-HERE",
            )
        )
        self.assertIn("run: CMD-HERE", text)
        self.assertIn("next step: do the thing", text)
        self.assertIn("abab", text)


if __name__ == "__main__":
    unittest.main()
