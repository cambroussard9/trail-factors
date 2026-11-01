"""The confirmed window is applied BEFORE validation (§4.3).

THE BUG. `window_confirmations` was read in `ingest()` and used to set two
COLUMNS on the activity row. Records, splits and session totals all kept
whole-file values, so the window was recorded and never applied -- and because
validation runs inside `build_rows`, long BEFORE `ingest()` is reached, a file
whose tail was a car ride was rejected outright and the athlete's confirmation
could never rescue it.

THE SUBJECT IS SYNTHETIC. The FIT reader is mocked to return a run followed by
a drive: RUN_S of running, then CAR_S at car speed, which the watch has
segmented as an `rwd_run`. A walk split straddles the window's end by a few
seconds. The confirmed window ends where the run ends. Expected: the car's
`rwd_run` gone, the walk truncated rather than dropped, its distance
recomputed from the records rather than scaled, and the rwd_* splits tiling
the activity's timer exactly.

A second class checks the same invariants on every window-applied activity of
whatever corpus TRAIL_FACTORS_DB names.

Every negative has a positive twin.
"""

from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import duckdb

from tests.corpus import CorpusTestCase
from trail_factors.ingest import fit_file as ff
from trail_factors.ingest import load as L
from trail_factors.ingest import validate as V

SEMI = 2**31 / 180.0
T0 = dt.datetime(2025, 1, 15, 7, 0, 0, tzinfo=dt.UTC)
RUN_S, CAR_S = 1800, 120
WALK_START_S, WALK_END_S = 1000, 1810  # straddles the window by 10 s
CAR_MPS = 20.0  # far above any running pace
WINDOW_END = (T0 + dt.timedelta(seconds=RUN_S)).replace(tzinfo=None)


def _speed(i):
    return 2.8 + (i % 7) * 0.05 if i <= RUN_S else CAR_MPS


def synthetic_messages():
    """A FIT-shaped dict, 1 Hz, at an arbitrary point far from anywhere real.

    The odometer is the running sum of speed, so the distance between any two
    seconds is known exactly and a scaled figure cannot pass for it.
    """
    recs, d = [], 0.0
    for i in range(RUN_S + CAR_S + 1):
        d += _speed(i) if i else 0.0
        moving_on_foot = i <= RUN_S
        recs.append(
            {
                "timestamp": T0 + dt.timedelta(seconds=i),
                "distance": d,
                "enhanced_speed": _speed(i),
                "heart_rate": 120 + (i % 9) if moving_on_foot else 100,
                "cadence": 80 + (i % 3) if moving_on_foot else 0,
                "fractional_cadence": 0.0,
                "position_lat": int((46.5 + i * 1e-5) * SEMI),
                "position_long": int(7.5 * SEMI),
            }
        )
    odo = {i: r["distance"] for i, r in enumerate(recs)}
    splits = [
        {
            "split_type": "rwd_run",
            "start_time": T0,
            "total_timer_time": float(WALK_START_S),
            "total_distance": odo[WALK_START_S],
        },
        {
            "split_type": "rwd_walk",
            "start_time": T0 + dt.timedelta(seconds=WALK_START_S),
            "total_timer_time": float(WALK_END_S - WALK_START_S),
            "total_distance": odo[WALK_END_S] - odo[WALK_START_S],
        },
        # THE CAR, segmented by the watch as RUNNING.
        {
            "split_type": "rwd_run",
            "start_time": T0 + dt.timedelta(seconds=WALK_END_S),
            "total_timer_time": float(RUN_S + CAR_S - WALK_END_S),
            "total_distance": odo[RUN_S + CAR_S] - odo[WALK_END_S],
        },
    ]
    total = float(RUN_S + CAR_S)
    msgs = {
        "file_id": [{"manufacturer": "garmin", "garmin_product": "fr265"}],
        "session": [
            {
                "sport": "running",
                "start_time": T0,
                "total_elapsed_time": total,
                "total_timer_time": total,
                "total_distance": d,
            }
        ],
        "record": recs,
        "split": splits,
    }
    return msgs, odo


class _SyntheticFile:
    """Mocks the FIT reader for one dummy file inside a TemporaryDirectory."""

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.path, self.db = d / "synthetic.fit", d / "trim.duckdb"
        self.path.write_bytes(b"synthetic")
        self.msgs, self.odo = synthetic_messages()
        self._p = [
            mock.patch.object(ff, "read_messages", return_value=self.msgs),
            mock.patch.object(ff, "sha256", return_value="0" * 64),
        ]
        for p in self._p:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._p:
            p.stop()
        self.tmp.cleanup()
        return False


def _window(start=None, end=WINDOW_END):
    return mock.patch.object(
        L, "confirmed_window", mock.Mock(return_value=(start, end))
    )


class TestASyntheticFileIngestsAtTheConfirmedWindow(unittest.TestCase):
    """The regression fixture, ingested into a scratch database."""

    @classmethod
    def setUpClass(cls):
        cls.f = _SyntheticFile().__enter__()
        with _window():
            cls.aid = L.ingest(cls.f.path, db_path=cls.f.db)["activity_id"]
        cls.con = duckdb.connect(str(cls.f.db), read_only=True)

    @classmethod
    def tearDownClass(cls):
        cls.con.close()
        cls.f.__exit__(None, None, None)

    def _row(self, *cols):
        return self.con.execute(
            f"SELECT {', '.join(cols)} FROM activities WHERE activity_id = ?",
            [self.aid],
        ).fetchone()

    def _walk(self):
        return self.con.execute(
            """
            SELECT split_type, total_timer_time, total_distance_m FROM splits
            WHERE activity_id = ? AND start_time = ?""",
            [self.aid, (T0 + dt.timedelta(seconds=WALK_START_S)).replace(tzinfo=None)],
        ).fetchone()

    def test_it_ingested_at_all(self):
        """Without the window it is REJECTED -- see TestTheORDERIsLoadBearing."""
        self.assertIsNotNone(self._row("activity_id"))

    def test_distance_and_duration_are_the_confirmed_ones(self):
        dist, dur = self._row("total_distance_m", "duration_s")
        self.assertAlmostEqual(dist, self.f.odo[RUN_S] - self.f.odo[0], places=6)
        self.assertEqual(dur, float(RUN_S))

    def test_the_whole_file_figures_are_KEPT_not_discarded(self):
        """They are what the device said about the FILE, which is still true
        about the file. Nothing may read them as the activity's own."""
        applied, dist, d_whole, timer, t_whole = self._row(
            "window_applied",
            "total_distance_m",
            "session_distance_m_whole_file",
            "timer_time_s",
            "timer_time_s_whole_file",
        )
        self.assertTrue(applied)
        self.assertAlmostEqual(d_whole, self.f.odo[RUN_S + CAR_S], places=6)
        self.assertEqual(t_whole, float(RUN_S + CAR_S))
        # ...and the whole-file excess is exactly the drive: ONE SIGN.
        self.assertAlmostEqual(
            d_whole - dist, self.f.odo[RUN_S + CAR_S] - self.f.odo[RUN_S], places=6
        )
        self.assertLess(timer, t_whole)

    def test_no_split_ends_after_the_window(self):
        late = self.con.execute(
            """
            SELECT count(*) FROM splits WHERE activity_id = ?
              AND start_time + (INTERVAL 1 SECOND * total_timer_time)
                  > ? + INTERVAL 1 SECOND""",
            [self.aid, WINDOW_END],
        ).fetchone()[0]
        self.assertEqual(late, 0)

    def test_the_car_ride_rwd_run_IS_GONE(self):
        """The watch segmented the drive as rwd_run. Left in, it reads as
        running at an impossible pace."""
        rows = self.con.execute(
            """
            SELECT count(*) FROM splits WHERE activity_id = ?
              AND split_type = 'rwd_run' AND start_time > ?""",
            [self.aid, WINDOW_END - dt.timedelta(seconds=60)],
        ).fetchone()[0]
        self.assertEqual(rows, 0)

    def test_the_run_before_it_IS_KEPT(self):
        """THE TWIN: the drop rule removes the drive, not every rwd_run."""
        n = self.con.execute(
            "SELECT count(*) FROM splits WHERE activity_id = ? "
            "AND split_type = 'rwd_run'",
            [self.aid],
        ).fetchone()[0]
        self.assertEqual(n, 1)

    def test_the_rwd_partition_SUMS_TO_THE_ACTIVITY_TIMER(self):
        """THE TILING IDENTITY, and the sharpest check here. The rwd_* splits
        partition the activity, so their timer must equal its timer time --
        which on this never-paused run is also its duration."""
        total = self.con.execute(
            "SELECT sum(total_timer_time) FROM splits WHERE activity_id = ? "
            "AND split_type LIKE 'rwd%'",
            [self.aid],
        ).fetchone()[0]
        timer, dur = self._row("timer_time_s", "duration_s")
        self.assertAlmostEqual(total, timer, places=1)
        self.assertAlmostEqual(total, dur, places=1)

    def test_no_record_survives_past_the_window(self):
        after = self.con.execute(
            "SELECT count(*) FROM records WHERE activity_id = ? AND timestamp > ?",
            [self.aid, WINDOW_END],
        ).fetchone()[0]
        self.assertEqual(after, 0)
        self.assertEqual(
            self.con.execute(
                "SELECT count(*) FROM records WHERE activity_id = ?", [self.aid]
            ).fetchone()[0],
            RUN_S + 1,
        )

    def test_the_straddling_walk_survives_with_a_truncated_duration(self):
        """Dropping a walk to remove its last few seconds would delete nearly
        all of a real walk to remove a sliver of contamination."""
        row = self._walk()
        self.assertIsNotNone(row, "the straddling walk was DROPPED")
        self.assertEqual(row[0], "rwd_walk")
        self.assertAlmostEqual(row[1], float(RUN_S - WALK_START_S), places=1)

    def test_its_distance_is_RECOMPUTED_not_scaled(self):
        """Distance is not linear in time. The records give the exact ground
        covered inside the window; a proportional figure would be an
        invention that looks measured."""
        d = self._walk()[2]
        whole = self.f.odo[WALK_END_S] - self.f.odo[WALK_START_S]
        scaled = whole * (RUN_S - WALK_START_S) / (WALK_END_S - WALK_START_S)
        self.assertAlmostEqual(
            d, self.f.odo[RUN_S] - self.f.odo[WALK_START_S], places=6
        )
        self.assertNotAlmostEqual(d, whole, places=1)  # untruncated
        self.assertNotAlmostEqual(d, scaled, places=1)  # scaled

    def test_one_split_was_dropped_and_two_remain(self):
        n = self.con.execute(
            "SELECT count(*) FROM splits WHERE activity_id = ?", [self.aid]
        ).fetchone()[0]
        self.assertEqual(n, 2)


class TestTheClipRuleItself(unittest.TestCase):
    """Unit-level, so the rule is pinned without a database."""

    def _split(self, start_s, dur, kind="rwd_walk"):
        return {
            "split_type": kind,
            "start_time": dt.datetime(2026, 1, 1, 0, 0, 0)
            + dt.timedelta(seconds=start_s),
            "total_timer_time": float(dur),
            "total_distance": 100.0,
        }

    def test_wholly_inside_is_untouched(self):
        s = self._split(0, 60)
        out, stats = L._clip_splits([s], None, dt.datetime(2026, 1, 1, 0, 5))
        self.assertEqual(stats["kept"], 1)
        self.assertEqual(out[0]["total_timer_time"], 60.0)
        self.assertNotIn("truncated", out[0])

    def test_wholly_outside_is_dropped(self):
        s = self._split(600, 60)
        out, stats = L._clip_splits([s], None, dt.datetime(2026, 1, 1, 0, 5))
        self.assertEqual(stats["dropped"], 1)
        self.assertEqual(out, [])

    def test_straddling_is_truncated_and_FLAGGED(self):
        s = self._split(0, 600)
        out, stats = L._clip_splits([s], None, dt.datetime(2026, 1, 1, 0, 5))
        self.assertEqual(stats["truncated"], 1)
        self.assertEqual(out[0]["total_timer_time"], 300.0)
        self.assertTrue(out[0]["truncated"])
        self.assertEqual(out[0]["untruncated_timer_time"], 600.0)

    def test_a_leading_window_clips_the_START(self):
        """THE TWIN for valid_start, which the subject file does not exercise."""
        s = self._split(0, 600)
        out, stats = L._clip_splits([s], dt.datetime(2026, 1, 1, 0, 5), None)
        self.assertEqual(stats["truncated"], 1)
        self.assertEqual(out[0]["total_timer_time"], 300.0)
        self.assertEqual(out[0]["start_time"], dt.datetime(2026, 1, 1, 0, 5))

    def test_a_clipped_distance_is_read_from_the_odometer(self):
        """`_recompute_clipped_distance` writes the FIT's own key, which is
        the one the row insert reads."""
        s = self._split(0, 600)
        out, _ = L._clip_splits([s], None, dt.datetime(2026, 1, 1, 0, 5))
        recs = [
            {
                "timestamp": dt.datetime(2026, 1, 1, 0, 0) + dt.timedelta(seconds=k),
                "distance": float(k) ** 1.5,
            }
            for k in range(0, 601, 10)
        ]
        L._recompute_clipped_distance(out, recs)
        self.assertAlmostEqual(out[0]["total_distance"], 300.0**1.5, places=6)
        self.assertEqual(out[0]["distance_basis"], "recomputed from trimmed records")

    def test_an_unreadable_odometer_is_absent_not_scaled(self):
        """THE TWIN: no odometer in the window -> None, never a guess."""
        s = self._split(0, 600)
        out, _ = L._clip_splits([s], None, dt.datetime(2026, 1, 1, 0, 5))
        recs = [
            {
                "timestamp": dt.datetime(2026, 1, 1, 0, 0) + dt.timedelta(seconds=k),
                "distance": None,
            }
            for k in range(0, 601, 10)
        ]
        L._recompute_clipped_distance(out, recs)
        self.assertIsNone(out[0]["total_distance"])


class TestAWindowSelectingNothingIsRefused(unittest.TestCase):
    def test_it_raises_rather_than_ingesting_an_empty_activity(self):
        """A window that selects nothing is a WRONG WINDOW, not an empty
        activity. Ingesting it would write a row with no records and let every
        downstream division meet a zero."""
        impossible = (dt.datetime(2020, 1, 1, 0, 0), dt.datetime(2020, 1, 1, 1, 0))
        with _SyntheticFile() as f, _window(*impossible):
            with self.assertRaises(V.ValidationError) as ctx:
                L.build_rows(f.path)
        self.assertIn("contains none of this file's", str(ctx.exception))
        self.assertIn("wrong window", str(ctx.exception))

    def test_a_window_that_selects_records_builds(self):
        """THE TWIN: the refusal is about the empty selection, not windows."""
        with _SyntheticFile() as f, _window():
            rows = L.build_rows(f.path)
        self.assertTrue(rows["window"]["applied"])
        self.assertEqual(rows["window"]["n_records_trimmed"], RUN_S + 1)


class TestTheORDERIsLoadBearing(unittest.TestCase):
    """Reintroduce 'validate before trimming' and the subject file dies."""

    def test_validating_the_whole_file_rejects_the_run(self):
        """THE WHOLE POINT OF STEP 1. The car block raises a FATAL
        pace_sustained_impossible: validate first and no confirmation can ever
        rescue the file, because build_rows never reaches ingest()."""
        with _SyntheticFile() as f, _window(end=None):
            fatal_whole = [x for x in L.build_rows(f.path)["findings"] if x.is_fatal]
            with self.assertRaises(V.ValidationError):
                L.ingest(f.path, db_path=f.db)
        self.assertTrue(
            fatal_whole,
            "the untrimmed file must still be fatal, or this fixture proves nothing",
        )
        self.assertTrue(any("pace" in x.check for x in fatal_whole))

    def test_the_trimmed_activity_is_clean(self):
        """...and the TRIMMED activity is clean. Same file, same checks."""
        with _SyntheticFile() as f, _window():
            fatal_trimmed = [x for x in L.build_rows(f.path)["findings"] if x.is_fatal]
        self.assertEqual(
            fatal_trimmed,
            [],
            f"the trimmed activity must validate clean; got "
            f"{[x.message for x in fatal_trimmed]}",
        )


class TestEveryWindowAppliedActivityInTheCorpus(CorpusTestCase):
    """The same invariants on whatever window-applied activities the corpus
    holds. None of them depends on which run it is."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.rows = cls.con.execute("""
            SELECT activity_id, valid_start_utc, valid_end_utc,
                   total_distance_m, session_distance_m_whole_file,
                   timer_time_s, timer_time_s_whole_file
            FROM activities WHERE window_applied ORDER BY activity_id
        """).fetchall()

    def setUp(self):
        if not self.rows:
            self.skipTest("this corpus has no window-applied activity")

    def test_no_record_lies_outside_the_window(self):
        for aid, vs, ve, *_ in self.rows:
            n = self.con.execute(
                """
                SELECT count(*) FROM records WHERE activity_id = ?
                  AND (timestamp < coalesce(?, timestamp)
                       OR timestamp > coalesce(?, timestamp))""",
                [aid, vs, ve],
            ).fetchone()[0]
            self.assertEqual(n, 0, aid)

    def test_no_split_ends_after_the_window(self):
        for aid, _, ve, *_ in self.rows:
            if ve is None:
                continue
            late = self.con.execute(
                """
                SELECT count(*) FROM splits WHERE activity_id = ?
                  AND start_time + (INTERVAL 1 SECOND * total_timer_time)
                      > ? + INTERVAL 1 SECOND""",
                [aid, ve],
            ).fetchone()[0]
            self.assertEqual(late, 0, aid)

    def test_the_rwd_partition_sums_to_the_activity_timer(self):
        for aid, *_, timer, _ in self.rows:
            total = self.con.execute(
                "SELECT sum(total_timer_time) FROM splits WHERE "
                "activity_id = ? AND split_type LIKE 'rwd%'",
                [aid],
            ).fetchone()[0]
            if total is None:
                continue
            self.assertAlmostEqual(total, timer, places=1, msg=aid)

    def test_a_window_only_ever_removes(self):
        """The whole-file figure is never below the windowed one."""
        for aid, _, _, dist, d_whole, timer, t_whole in self.rows:
            if dist is not None and d_whole is not None:
                self.assertGreaterEqual(d_whole + 1e-6, dist, aid)
            if timer is not None and t_whole is not None:
                self.assertGreaterEqual(t_whole + 1e-6, timer, aid)


if __name__ == "__main__":
    unittest.main()
