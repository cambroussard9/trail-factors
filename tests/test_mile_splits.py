"""mile_splits on a hand-computable fixture (an in-memory database; nothing
reads data/).

THE FIXTURE, 1 Hz for an hour: running at 3.0 m/s with HR 150, except a WALK
at 1.4 m/s with HR 100 from t=200 s to t=300 s.
  distance(t) = 3t                    for t < 200
              = 600 + 1.4 (t - 200)   for 200 <= t < 300
              = 740 + 3 (t - 300)     for t >= 300
MILE 1 ends where distance = 1609.344 m, i.e. t = 300 + 869.344 / 3
= 589.781 s. Its running part is 600 + 869.344 = 1469.344 m in
200 + 289.781 = 489.781 s -> exactly 3.0 m/s; its walking is 100 s.
"""

import datetime as dt
import unittest

import duckdb

from trail_factors.analysis import miles as MS
from trail_factors.ingest.schema import DDL
from trail_factors.units import METERS_PER_MILE

T0 = dt.datetime(2025, 1, 15, 7, 0, 0)  # synthetic
END_S = 3600


def dist(t):
    if t < 200:
        return 3.0 * t
    if t < 300:
        return 600.0 + 1.4 * (t - 200)
    return 740.0 + 3.0 * (t - 300)


class _Fixture(unittest.TestCase):
    ELEVATION = False

    def setUp(self):
        self.con = duckdb.connect(":memory:")
        self.con.execute(DDL)
        self.con.execute(
            "INSERT INTO activities (activity_id, run_id, file_hash, source_file,"
            " start_time_utc, source, sampling_regime, pipeline_version, sport)"
            " VALUES ('a1','r1','h1','x.fit',?,'garmin_watch','1hz',1,'running')",
            [T0],
        )
        recs = [
            (
                "a1",
                T0 + dt.timedelta(seconds=t),
                dist(t),
                100 if 200 <= t < 300 else 150,
            )
            for t in range(END_S + 1)
        ]
        self.con.executemany(
            "INSERT INTO records (activity_id, timestamp, distance_m, heart_rate)"
            " VALUES (?,?,?,?)",
            recs,
        )
        for i, (kind, s, e) in enumerate(
            (("rwd_run", 0, 200), ("rwd_walk", 200, 300), ("rwd_run", 300, END_S))
        ):
            self.con.execute(
                "INSERT INTO splits (activity_id, split_index, split_type, "
                "start_time, end_time, total_timer_time) VALUES (?,?,?,?,?,?)",
                [
                    "a1",
                    i,
                    kind,
                    T0 + dt.timedelta(seconds=s),
                    T0 + dt.timedelta(seconds=e),
                    float(e - s),
                ],
            )
        if self.ELEVATION:  # rising 0.01 m per second, no descent
            self.con.executemany(
                "INSERT INTO records_enriched (activity_id, timestamp, "
                "altitude_smooth_m, terrain_usable, calc_version) "
                "VALUES (?,?,?,TRUE,1)",
                [
                    ("a1", T0 + dt.timedelta(seconds=t), 100.0 + 0.01 * t)
                    for t in range(0, END_S + 1, 5)
                ],
            )
        self.r = MS.mile_splits(self.con, "a1")

    def tearDown(self):
        self.con.close()


class TestBoundaries(_Fixture):
    def test_boundaries_fall_within_1_m_of_the_hand_computed_distance(self):
        units = self.r["units"]
        for u in units[:-1]:
            k = u["index"]
            self.assertAlmostEqual(u["end_distance_m"], k * METERS_PER_MILE, delta=1e-6)
            # ...and the TIME placed there puts the odometer within 1 m of it.
            self.assertLess(abs(dist(u["end_elapsed_s"]) - k * METERS_PER_MILE), 1.0)
        self.assertAlmostEqual(units[0]["end_elapsed_s"], 589.781, delta=0.01)

    def test_the_last_unit_is_a_partial_with_its_real_length(self):
        last = self.r["units"][-1]
        self.assertTrue(last["partial"])
        self.assertAlmostEqual(last["end_distance_m"], dist(END_S), delta=1e-6)
        self.assertFalse(any(u["partial"] for u in self.r["units"][:-1]))

    def test_quarters_on_request(self):
        q = MS.mile_splits(self.con, "a1", unit="quarter")
        self.assertAlmostEqual(
            q["units"][0]["end_distance_m"], METERS_PER_MILE / 4, delta=1e-6
        )
        self.assertEqual(q["unit"], "quarter")


class TestRunningOnly(_Fixture):
    def test_a_walk_break_is_out_of_pace_and_in_time_walking(self):
        m1 = self.r["units"][0]
        self.assertAlmostEqual(m1["running_mps"], 3.0, places=3)
        self.assertAlmostEqual(m1["time_walking_s"], 100.0, places=3)
        self.assertAlmostEqual(m1["time_running_s"], 489.781, delta=0.01)
        # The all-time pace would be slower; the running pace is not.
        self.assertLess(METERS_PER_MILE / m1["elapsed_s"], 3.0)

    def test_hr_running_is_the_running_overlap_and_all_gaits_is_beside_it(self):
        m1 = self.r["units"][0]
        self.assertAlmostEqual(m1["hr_running"], 150.0, places=6)
        expected_all = (150 * 489.781 + 100 * 100) / 589.781
        self.assertAlmostEqual(m1["hr_all_gaits"], expected_all, delta=0.05)
        f = self.r["filters"]
        self.assertTrue(f["hr_raw"])
        self.assertFalse(f["hr_lag_shifted"])
        self.assertIn("PRIMARY", f["hr_running"])

    def test_a_mile_with_no_walk_has_equal_hr_figures(self):
        m2 = self.r["units"][1]
        self.assertEqual(m2["time_walking_s"], 0.0)
        self.assertAlmostEqual(m2["hr_running"], m2["hr_all_gaits"], places=6)


class TestNoDem(_Fixture):
    def test_a_no_dem_run_returns_null_gain_with_a_reason(self):
        self.assertFalse(self.r["elevation_available"])
        self.assertTrue(self.r["elevation_reason"])
        for u in self.r["units"]:
            self.assertIsNone(u["gain_ft"])
            self.assertIsNone(u["loss_m"])
            self.assertTrue(u["elevation_reason"])


class TestWithDem(_Fixture):
    """THE TWIN: with a DEM profile rising 0.01 m/s, mile 1's gain is
    0.01 x 589.781 = 5.898 m and its loss 0."""

    ELEVATION = True

    def test_gain_and_loss_from_the_smoothed_profile(self):
        m1 = self.r["units"][0]
        self.assertAlmostEqual(m1["gain_m"], 0.01 * 589.781, delta=0.01)
        self.assertAlmostEqual(m1["loss_m"], 0.0, places=9)
        self.assertAlmostEqual(m1["gain_ft"], m1["gain_m"] / 0.3048, places=9)
        self.assertIsNone(m1["elevation_reason"])


class TestGarminLapsAreBesideNotInstead(_Fixture):
    def test_laps_are_returned_as_recorded_and_never_substituted(self):
        self.con.execute(
            "INSERT INTO laps (activity_id, lap_index, start_time, "
            "total_timer_time, distance_m, avg_hr, trigger) "
            "VALUES ('a1', 0, ?, 600.0, 1609.0, 140, 'distance')",
            [T0],
        )
        r = MS.mile_splits(self.con, "a1")
        self.assertEqual(r["garmin_laps"][0]["trigger"], "distance")
        self.assertAlmostEqual(r["units"][0]["running_mps"], 3.0, places=3)


if __name__ == "__main__":
    unittest.main()
