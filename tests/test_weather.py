"""Milestone 3: weather ingest (SPEC.md §8).

Pure-function tests only -- nothing here touches the network. The fetch itself
is exercised by running `weather`, and its result is cached under
data/raw/weather/ so a re-run reproduces what was actually used.
"""

import unittest
from datetime import datetime, timedelta

from tests.corpus import CorpusTestCase, one
from trail_factors.ingest import weather as W

T0 = datetime(2025, 1, 15, 14, 0)


def hourly(*vals, start=T0):
    """(temp, dew, wind_dir) per hour from `start`."""
    return [
        {
            "timestamp": start + timedelta(hours=i),
            "temp_c": t,
            "dew_point_c": d,
            "humidity_pct": 50.0,
            "wind_mps": 3.0,
            "wind_dir_deg": wd,
            "precip_mm": 0.0,
            "cloud_cover_pct": 10.0,
        }
        for i, (t, d, wd) in enumerate(vals)
    ]


class TestCoordinateCoarsening(unittest.TestCase):
    """The athlete's privacy constraint, enforced in code (§5a.1)."""

    def test_a_coordinate_is_coarsened_before_it_leaves_the_machine(self):
        # Arbitrary synthetic point: not anywhere the athlete has been.
        self.assertEqual(W.round_coord(46.512345), 46.51)
        self.assertEqual(W.round_coord(7.498765), 7.5)
        self.assertEqual(W.round_coord(-33.926789), -33.93)

    def test_the_coarsening_is_about_a_kilometre(self):
        """Fine enough for any Open-Meteo model, coarse enough not to be an
        address. If this ever tightens, the privacy claim in the module
        docstring stops being true."""
        self.assertEqual(W.COORD_PRECISION, 2)
        worst = 0.5 * 10**-W.COORD_PRECISION * 111_320  # metres of lat
        self.assertLess(worst, 600)
        self.assertGreater(worst, 100)


class TestArchiveVsForecast(unittest.TestCase):
    """§8: the archive lags several days; recent runs use forecast past_days."""

    def test_an_old_run_uses_the_archive(self):
        today = datetime(2025, 6, 1)
        self.assertTrue(W.use_archive(datetime(2025, 1, 1), today))

    def test_a_recent_run_does_not(self):
        today = datetime(2025, 6, 1)
        self.assertFalse(W.use_archive(datetime(2025, 5, 31), today))

    def test_the_boundary_is_where_it_is_documented(self):
        today = datetime(2025, 6, 20)
        edge = today - timedelta(days=W.ARCHIVE_LAG_DAYS)
        self.assertFalse(W.use_archive(edge, today))
        self.assertTrue(W.use_archive(edge - timedelta(days=1), today))


class TestPerRecordInterpolation(unittest.TestCase):
    """§8: weather is interpolated PER RECORD, never a per-activity scalar.

    A 3-4 hour long run on a warm day can span 10-15 F within one activity;
    a run-level average destroys exactly what is being measured.
    """

    def test_temperature_is_interpolated_between_observations(self):
        rows = hourly((20.0, 10.0, 90.0), (24.0, 12.0, 90.0))
        mid = T0 + timedelta(minutes=30)
        out = W.interpolate(rows, [mid])
        self.assertAlmostEqual(out[0]["temp_c"], 22.0, places=6)
        self.assertAlmostEqual(out[0]["dew_point_c"], 11.0, places=6)

    def test_a_long_run_does_not_get_one_flat_value(self):
        rows = hourly((20.0, 10.0, 90.0), (24.0, 12.0, 90.0), (28.0, 14.0, 90.0))
        stamps = [T0 + timedelta(minutes=20 * i) for i in range(7)]
        temps = [r["temp_c"] for r in W.interpolate(rows, stamps)]
        self.assertGreater(max(temps) - min(temps), 7.0)
        self.assertEqual(temps, sorted(temps))

    def test_a_stamp_outside_the_series_clamps_rather_than_extrapolates(self):
        rows = hourly((20.0, 10.0, 90.0), (24.0, 12.0, 90.0))
        before = W.interpolate(rows, [T0 - timedelta(hours=4)])[0]
        after = W.interpolate(rows, [T0 + timedelta(hours=6)])[0]
        self.assertEqual(before["temp_c"], 20.0)
        self.assertEqual(after["temp_c"], 24.0)


class TestWindDirectionIsCircular(unittest.TestCase):
    """§8: averaging a compass bearing linearly sends 350 -> 10 through 180,
    a wind from the opposite quarter. Direction is carried by nearest."""

    def test_direction_is_carried_by_nearest_not_averaged_through_south(self):
        """350 deg to 10 deg must never pass through 180."""
        rows = hourly((20.0, 10.0, 350.0), (20.0, 10.0, 10.0))
        out = W.interpolate(
            rows, [T0 + timedelta(minutes=10), T0 + timedelta(minutes=50)]
        )
        self.assertEqual([r["wind_dir_deg"] for r in out], [350.0, 10.0])
        for r in out:
            self.assertNotAlmostEqual(r["wind_dir_deg"], 180.0, delta=90.0)

    def test_a_calm_zero_does_not_drag_its_neighbours(self):
        rows = hourly((20.0, 10.0, 270.0), (20.0, 10.0, 0.0))
        out = W.interpolate(rows, [T0 + timedelta(minutes=5)])
        self.assertEqual(out[0]["wind_dir_deg"], 270.0)


class TestPrecipProxy(unittest.TestCase):
    def test_only_the_24h_before_the_run_counts(self):
        start = T0
        rows = [
            {"timestamp": start - timedelta(hours=h), "precip_mm": 1.0}
            for h in range(1, 40)
        ]
        self.assertAlmostEqual(W.precip_prev_24h(rows, start), 24.0, places=6)

    def test_rain_during_the_run_is_not_counted_as_prior_wetness(self):
        start = T0
        rows = [{"timestamp": start + timedelta(hours=1), "precip_mm": 9.0}]
        self.assertEqual(W.precip_prev_24h(rows, start), 0.0)

    def test_zero_rain_is_a_value_not_a_missing_reading(self):
        """CLAUDE.md: 0.0 is legitimate and must not become a default."""
        start = T0
        rows = [{"timestamp": start - timedelta(hours=2), "precip_mm": 0.0}]
        self.assertEqual(W.precip_prev_24h(rows, start), 0.0)


class TestIndoorAssumption(unittest.TestCase):
    """§8: an assumed value that is LABELLED is usable; an unlabelled one is a
    lie. NULL would be indistinguishable from a fetch failure."""

    def test_indoor_rows_are_produced_for_every_stamp(self):
        stamps = [T0 + timedelta(minutes=i) for i in range(5)]
        rows = W.indoor_rows(stamps)
        self.assertEqual(len(rows), 5)
        for r in rows:
            self.assertEqual(r["temp_c"], W.INDOOR_TEMP_C)
            self.assertIsNotNone(r["dew_point_c"])

    def test_the_assumption_is_a_plausible_indoor_temperature(self):
        """Conditioned indoor air, roughly 18-26 C -- not a placeholder 0."""
        self.assertGreater(W.INDOOR_TEMP_C, 18.0)
        self.assertLess(W.INDOOR_TEMP_C, 26.0)
        self.assertLess(W.INDOOR_DEW_POINT_C, W.INDOOR_TEMP_C)

    def test_the_source_label_exists_so_it_can_be_excluded(self):
        self.assertEqual(W.ASSUMED_INDOOR, "assumed_indoor")


class TestStoredWeather(CorpusTestCase):
    """Milestone 3's done-when: every run has dew point attached.

    Read-only against whatever corpus TRAIL_FACTORS_DB names. It assumes
    `weather` has been run on that corpus, which `sync` does.
    """

    def test_every_outdoor_activity_has_per_record_weather(self):
        n_weather = self.con.execute("SELECT count(*) FROM weather_records").fetchone()[
            0
        ]
        if n_weather == 0:
            self.skipTest("this corpus has no weather_records (run `weather`)")
        missing = self.con.execute("""
            SELECT count(*) FROM activities a
            WHERE a.coverage_fraction > 0
              AND NOT EXISTS (SELECT 1 FROM weather_records w
                              WHERE w.activity_id = a.activity_id)
        """).fetchone()[0]
        no_dew = self.con.execute(
            "SELECT count(*) FILTER (WHERE dew_point_c IS NULL) FROM weather_records"
        ).fetchone()[0]
        self.assertEqual(missing, 0, "§8: a gap is a fetch failure, not a gap")
        self.assertEqual(no_dew, 0, "dew point is the point (§8)")

    def test_weather_is_per_record_not_per_activity(self):
        """THE POSITIVE TWIN: an outdoor activity carries one weather row per
        record, not one scalar for the run."""
        _, n_rec = one(
            self.con,
            """
            SELECT w.activity_id, count(*) FROM weather_records w
            WHERE w.weather_source <> 'assumed_indoor'
            GROUP BY 1 ORDER BY 2 DESC LIMIT 1""",
            what="outdoor activity with weather_records",
        )
        self.assertGreater(n_rec, 1)

    def test_weather_varies_within_a_single_activity(self):
        """If it did not, the per-record design would be pointless."""
        spread = self.con.execute("""
            SELECT max(max_t - min_t) FROM (
                SELECT activity_id, min(temp_c) min_t, max(temp_c) max_t
                FROM weather_records WHERE weather_source <> 'assumed_indoor'
                GROUP BY 1)
        """).fetchone()[0]
        if spread is None:
            self.skipTest("this corpus has no outdoor weather_records")
        self.assertGreater(
            spread, 0.0, "within-run temperature change is what §8 captures"
        )


if __name__ == "__main__":
    unittest.main()
