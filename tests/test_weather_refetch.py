"""Provisional weather is refetched inside `weather` (and so inside `sync`)
once the run is older than the archive lag, and is_final flips on success.

Scratch database and scratch cache, both inside a TemporaryDirectory that is
removed afterwards; the fetch is mocked -- nothing touches the network or
data/. The database is a FILE rather than :memory: because `weather_build`
opens its own connection by path, and the test reads back what it wrote.
"""

import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

import duckdb

import trail_factors.cli.weather as CW
from tests.corpus import CorpusTestCase
from trail_factors.ingest import weather as W
from trail_factors.ingest.schema import DDL

NOW = datetime.now(UTC).replace(tzinfo=None).replace(microsecond=0)


def payload(start, final, temp=20.0):
    hours = [start - timedelta(hours=25) + timedelta(hours=i) for i in range(30)]
    return {
        "hourly": {
            "time": [h.isoformat() for h in hours],
            "temperature_2m": [temp] * 30,
            "dew_point_2m": [10.0] * 30,
            "relative_humidity_2m": [50.0] * 30,
            "wind_speed_10m": [2.0] * 30,
            "wind_direction_10m": [90.0] * 30,
            "precipitation": [0.0] * 30,
            "cloud_cover": [20.0] * 30,
        },
        "_source": W.SOURCE_ERA5 if final else W.SOURCE_FORECAST,
        "_is_final": final,
    }


class TestRefetchInsideTheWeatherStep(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.db, self.cache = d / "w.duckdb", d / "weather"
        con = duckdb.connect(str(self.db))
        con.execute(DDL)
        # OLD is past the lag, NEW is inside it; both start out provisional.
        self.starts = {
            "old": NOW - timedelta(days=W.ARCHIVE_LAG_DAYS + 2),
            "new": NOW - timedelta(days=W.ARCHIVE_LAG_DAYS - 3),
        }
        for aid, start in self.starts.items():
            con.execute(
                "INSERT INTO activities (activity_id, run_id, file_hash, "
                "source_file, start_time_utc, source, sampling_regime, "
                "pipeline_version) VALUES (?,?,?,?,?,'garmin_watch','1hz',1)",
                [aid, f"r-{aid}", f"h-{aid}", f"{aid}.fit", start],
            )
            for i in range(3):
                con.execute(
                    "INSERT INTO records (activity_id, timestamp, lat, lon) "
                    # Synthetic, arbitrary, already at the 2 dp the
                    # request is rounded to.
                    "VALUES (?,?,46.5,7.5)",
                    [aid, start + timedelta(minutes=10 * i)],
                )
        con.close()
        self.cache.mkdir()
        for aid, start in self.starts.items():
            (self.cache / f"{aid}.json").write_text(
                json.dumps(payload(start, final=False))
            )
        self.patches = [
            mock.patch.object(CW, "DB_PATH", self.db),
            mock.patch.object(W, "CACHE_DIR", self.cache),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def stored(self, aid):
        con = duckdb.connect(str(self.db), read_only=True)
        try:
            return con.execute(
                "SELECT any_value(weather_source), bool_and(is_final) "
                "FROM weather_records WHERE activity_id = ?",
                [aid],
            ).fetchone()
        finally:
            con.close()

    def run_weather(self):
        fetched = []

        def fake_fetch(lat, lon, start, end):
            fetched.append(start)
            return payload(start, final=True, temp=25.0)

        with mock.patch.object(W, "fetch", side_effect=fake_fetch):
            CW.weather_build()
        return fetched

    def test_a_provisional_run_older_than_the_lag_is_refetched(self):
        fetched = self.run_weather()
        self.assertIn(self.starts["old"], fetched)
        self.assertEqual(
            self.stored("old"),
            (W.SOURCE_ERA5, True),
            "is_final flips on a successful refetch",
        )
        self.assertTrue(
            json.loads((self.cache / "old.json").read_text())["_is_final"],
            "and the cache is the final payload",
        )

    def test_a_newer_provisional_run_is_NOT_refetched(self):
        fetched = self.run_weather()
        self.assertNotIn(self.starts["new"], fetched)
        self.assertEqual(self.stored("new"), (W.SOURCE_FORECAST, False))

    def test_the_lag_is_seven_days_compared_as_a_duration(self):
        self.assertEqual(W.ARCHIVE_LAG_DAYS, 7)
        today = datetime(2025, 6, 15, 12, 0)
        self.assertFalse(W.use_archive(today - timedelta(days=7), today))
        self.assertTrue(W.use_archive(today - timedelta(days=7, minutes=1), today))


class TestConditionsCarryFinality(CorpusTestCase):
    """A provisional row carries is_final false INTO conditions until it is
    refetched. Read-only against the corpus: whichever runs are provisional
    today, their reports must say so in the conditions block -- and a final
    one must say final."""

    def _rows(self):
        return self.con.execute("""
            SELECT activity_id, bool_and(is_final) FROM weather_records
            GROUP BY 1 ORDER BY bool_and(is_final), max(timestamp) DESC
        """).fetchall()

    def _check(self, rows):
        from trail_factors.analysis import report as REP

        for aid, flag in rows:
            c = REP.build(self.con, aid)["conditions"]
            self.assertIs(c["is_final"], flag, aid)
            self.assertIn("weather_source", c)

    def test_a_provisional_run_says_provisional(self):
        provisional = [r for r in self._rows() if r[1] is False][:1]
        if not provisional:
            self.skipTest("nothing is provisional in this corpus today")
        self._check(provisional)

    def test_a_final_run_says_final(self):
        """THE TWIN: runs on its own, so it is not hostage to whether
        anything happens to be provisional today."""
        final = [r for r in self._rows() if r[1] is True][:1]
        if not final:
            self.skipTest("this corpus has no final weather")
        self._check(final)


if __name__ == "__main__":
    unittest.main()
