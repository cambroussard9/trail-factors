"""confirm_window is a THIN WRAPPER over confirm_file (§4.3).

THE BUG. confirm_window applied a window by UPDATE-ing two activities columns
and INSERT-ing into window_confirmations, and said no re-ingest was needed.
Since the window-trim commit the window is applied in build_rows, so that was
a HALF-APPLIED window: records, splits and session totals stayed whole-file
while valid_* claimed otherwise. Now it writes the durable confirmation
through confirm_file's path and the activity changes at re-ingest.

THE OLD SUCCESS TEST PINNED THE BUG -- it asserted the activity's window
changed immediately. It is replaced by its opposite, and by the real proof:
ingest a file, confirm, re-ingest, and check the TILING IDENTITY (the rwd_*
splits' timer sum equals the activity's timer time).

The file is SYNTHETIC: the FIT reader is mocked to return a run followed by a
trailing stop, so the whole path -- suggest_window, confirm_window,
build_rows, ingest -- runs without any real recording.

Every path is a TemporaryDirectory: nothing writes data/athlete/ or reads the
real database.
"""

import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

import duckdb

# confirm_window lives in trail_factors.cli.athlete since the package split; patching
# must target the module that OWNS the name, not the re-export.
import trail_factors.cli.athlete as cli
from trail_factors.ingest import athlete_state
from trail_factors.ingest import fit_file as ff
from trail_factors.ingest.schema import DDL

# Synthetic span: a 3,000 s file whose detector suggests ending at 2,800 s.
BASE = datetime(2025, 1, 15, 7, 0, 0)
END = BASE + timedelta(seconds=3000)
TRIMMED = BASE + timedelta(seconds=2800)

SEMI = 2**31 / 180.0
RUN_S, STOP_S, GAP_S = 1800, 300, 2


def synthetic_messages():
    """A FIT-shaped dict: RUN_S of running, then STOP_S standing still.

    Position is an arbitrary point far from anywhere real. The trailing
    block has zero speed and zero cadence, which is what the movement
    detector reads as "the recording outlived the run".
    """
    t0 = BASE.replace(tzinfo=UTC)
    recs, d = [], 0.0
    for i in range(0, RUN_S + STOP_S + 1, GAP_S):
        moving = i <= RUN_S
        v = 2.8 + (i % 7) * 0.05 if moving else 0.0
        d += v * GAP_S if moving and i else 0.0
        recs.append(
            {
                "timestamp": t0 + timedelta(seconds=i),
                "distance": d,
                "enhanced_speed": v,
                "heart_rate": 120 + (i % 9) if moving else 110,
                "cadence": 80 + (i % 3) if moving else 0,
                "fractional_cadence": 0.0,
                "position_lat": int((46.5 + i * 1e-5) * SEMI),
                "position_long": int(7.5 * SEMI),
            }
        )
    splits = [
        {
            "split_type": "rwd_run",
            "start_time": t0,
            "total_timer_time": float(RUN_S),
            "total_distance": d,
        },
        {
            "split_type": "rwd_stand",
            "start_time": t0 + timedelta(seconds=RUN_S),
            "total_timer_time": float(STOP_S),
            "total_distance": 0.0,
        },
    ]
    total = float(RUN_S + STOP_S)
    return {
        "file_id": [{"manufacturer": "garmin", "garmin_product": "fr265"}],
        "session": [
            {
                "sport": "running",
                "start_time": t0,
                "total_elapsed_time": total,
                "total_timer_time": total,
                "total_distance": d,
            }
        ],
        "record": recs,
        "split": splits,
    }


def suggestion(start=None, end=TRIMMED, sha="h1"):
    return {
        "file": "x.fit",
        "file_sha256": sha,
        "n_records": 100,
        "file_start_utc": BASE,
        "file_end_utc": END,
        "suggested_valid_start": start,
        "suggested_valid_end": end,
    }


class _Scratch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.state = d / "window_confirmations.json"
        self.db = d / "cw.duckdb"
        self.patches = [
            mock.patch.object(athlete_state, "CONFIRMATIONS", self.state),
            mock.patch.object(cli, "DB_PATH", self.db),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def stored(self):
        return json.loads(self.state.read_text()) if self.state.exists() else []


class TestConfirmWindowWritesOnlyTheConfirmation(_Scratch):
    def setUp(self):
        super().setUp()
        con = duckdb.connect(str(self.db))
        con.execute(DDL)
        con.execute(
            "INSERT INTO activities (activity_id, run_id, file_hash, source_file, "
            "start_time_utc, duration_s, total_distance_m, source, sampling_regime, "
            "valid_start_utc, valid_end_utc, pipeline_version) VALUES "
            "('a1','r1','h1','x.fit',?,3000.0,8000.0,'garmin_watch','smart_recording',?,?,1)",
            [BASE, BASE, END],
        )
        con.close()

    def window(self):
        con = duckdb.connect(str(self.db), read_only=True)
        try:
            return con.execute(
                "SELECT valid_start_utc, valid_end_utc FROM activities"
            ).fetchone()
        finally:
            con.close()

    def run_confirm(self, s):
        with mock.patch.object(cli, "suggest_window", return_value=s):
            cli.confirm_window("a1")

    def test_the_activity_is_UNCHANGED_until_re_ingest(self):
        self.run_confirm(suggestion())
        self.assertEqual(
            self.window(),
            (BASE, END),
            "valid_* must not move without the records moving",
        )
        con = duckdb.connect(str(self.db), read_only=True)
        try:
            self.assertEqual(
                con.execute("SELECT count(*) FROM window_confirmations").fetchone()[0],
                0,
                "the projection table is not written directly",
            )
        finally:
            con.close()

    def test_positive_twin_the_durable_confirmation_IS_written(self):
        self.run_confirm(suggestion())
        [row] = self.stored()
        self.assertEqual(row["file_sha256"], "h1")
        self.assertEqual(row["valid_end_utc"], TRIMMED.isoformat())
        self.assertIsNone(row["valid_start_utc"])
        self.assertEqual(row["source"], "confirm-window")

    def test_it_says_re_ingest(self):
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.run_confirm(suggestion())
        self.assertIn("next: re-ingest", buf.getvalue())
        self.assertIn("UNCHANGED until re-ingest", buf.getvalue())

    def test_report_failure_writes_nothing(self):
        """§4.3: a failure must leave the stored window untouched."""
        with (
            mock.patch.object(
                cli, "_window_report", side_effect=RuntimeError("report blew up")
            ),
            self.assertRaises(RuntimeError),
        ):
            self.run_confirm(suggestion())
        self.assertEqual(self.stored(), [])

    def test_an_end_only_suggestion_keeps_an_earlier_confirmed_start(self):
        """A None start reads as "no bound". Passing only the side the
        detector suggested would silently drop the athlete's earlier start."""
        start = BASE + timedelta(seconds=30)
        con = duckdb.connect(str(self.db))
        con.execute("UPDATE activities SET valid_start_utc = ?", [start])
        con.close()
        self.run_confirm(suggestion())
        self.assertEqual(self.stored()[0]["valid_start_utc"], start.isoformat())

    def test_a_file_that_does_not_match_its_ingest_writes_nothing(self):
        with self.assertRaises(SystemExit):
            self.run_confirm(suggestion(sha="not-h1"))
        self.assertEqual(self.stored(), [])

    def test_no_suggestion_writes_nothing(self):
        self.run_confirm(suggestion(end=END))
        self.assertEqual(self.stored(), [])


class TestOneBoundKeepsTheOtherStoredBound(_Scratch):
    """confirm-file with ONE bound keeps the OTHER from the file's current
    stored confirmation before falling back to the detector. It used to fill
    it from the detector, replacing the athlete's own end -- or dropping it
    to None where the detector suggests no end, which ingests the tail."""

    OWN_END = BASE + timedelta(seconds=1200)
    OWN_START = BASE + timedelta(seconds=60)

    def confirm(self, s, **kw):
        return cli._confirm_path(Path("x.fit"), suggestion=s, **kw)

    def test_a_later_start_keeps_the_earlier_confirmed_end(self):
        self.confirm(suggestion(), valid_end=self.OWN_END)
        out = self.confirm(suggestion(), valid_start=self.OWN_START)
        [row] = self.stored()
        self.assertEqual(row["valid_end_utc"], self.OWN_END.isoformat())
        self.assertEqual(row["valid_start_utc"], self.OWN_START.isoformat())
        self.assertEqual(row["revision"], 1)
        self.assertEqual(
            out["bound_source"],
            {"start": "argument", "end": "stored confirmation (revision 0)"},
        )

    def test_it_holds_when_the_detector_suggests_NO_end(self):
        self.confirm(suggestion(), valid_end=self.OWN_END)
        self.confirm(suggestion(end=None), valid_start=self.OWN_START)
        self.assertEqual(self.stored()[0]["valid_end_utc"], self.OWN_END.isoformat())

    def test_twin_with_nothing_stored_the_detector_fills_it_and_says_so(self):
        s = dict(suggestion(), suggested_basis={"start": None, "end": "trailing stop"})
        out = self.confirm(s, valid_start=self.OWN_START)
        self.assertEqual(self.stored()[0]["valid_end_utc"], TRIMMED.isoformat())
        self.assertEqual(out["bound_source"]["end"], "detector (trailing stop)")

    def test_an_explicit_bound_still_wins_over_the_stored_one(self):
        self.confirm(suggestion(), valid_end=self.OWN_END)
        later = self.OWN_END + timedelta(seconds=300)
        out = self.confirm(suggestion(), valid_end=later)
        self.assertEqual(self.stored()[0]["valid_end_utc"], later.isoformat())
        self.assertEqual(out["bound_source"]["end"], "argument")


class TestReIngestAppliesIt(_Scratch):
    """The real proof, on a synthetic file with a trailing stop. Ingested
    whole into a SCRATCH database, confirmed, re-ingested."""

    def setUp(self):
        super().setUp()
        self.file = Path(self.tmp.name) / "synthetic.fit"
        self.file.write_bytes(b"synthetic")
        self.fit = [
            mock.patch.object(ff, "read_messages", return_value=synthetic_messages()),
            mock.patch.object(ff, "sha256", return_value="0" * 64),
        ]
        for p in self.fit:
            p.start()

    def tearDown(self):
        for p in self.fit:
            p.stop()
        super().tearDown()

    def row(self, aid):
        con = duckdb.connect(str(self.db), read_only=True)
        try:
            return con.execute(
                "SELECT valid_end_utc, duration_s, timer_time_s FROM activities "
                "WHERE activity_id = ?",
                [aid],
            ).fetchone()
        finally:
            con.close()

    def rwd_timer(self, aid):
        con = duckdb.connect(str(self.db), read_only=True)
        try:
            return con.execute(
                "SELECT sum(total_timer_time) FROM splits WHERE activity_id = ? "
                "AND split_type LIKE 'rwd%'",
                [aid],
            ).fetchone()[0]
        finally:
            con.close()

    def test_window_lands_at_re_ingest_and_the_tiling_identity_holds(self):
        from trail_factors.ingest.load import ingest

        out = ingest(self.file, db_path=self.db)
        aid = out["activity_id"]
        whole_end, whole_dur, whole_timer = self.row(aid)
        s = cli.suggest_window(self.file)
        self.assertIsNotNone(s["suggested_valid_end"], "fixture has no suggestion")
        self.assertLess(s["suggested_valid_end"], whole_end)

        cli.confirm_window(aid)
        self.assertEqual(
            self.row(aid),
            (whole_end, whole_dur, whole_timer),
            "confirm_window must not touch the activity",
        )

        ingest(self.file, db_path=self.db)
        end, dur, timer = self.row(aid)
        self.assertEqual(end, s["suggested_valid_end"])
        self.assertLess(dur, whole_dur)
        self.assertLess(timer, whole_timer)
        # THE TILING IDENTITY: the rwd_* splits partition the activity's
        # TIMER. Against TIMER time, not duration_s: duration_s is ELAPSED and
        # includes auto-paused time, which no split covers; on a run that
        # never paused the two happen to agree.
        self.assertAlmostEqual(self.rwd_timer(aid), timer, places=1)
        self.assertAlmostEqual(timer, float(RUN_S), places=1)


if __name__ == "__main__":
    unittest.main()
