"""Shortwave radiation (§8): a preceding-hour mean, duration-weighted per run.

Pure functions on synthetic hourly rows; nothing touches the network.
"""

import unittest
from datetime import datetime, timedelta
from unittest import mock

from trail_factors.ingest import weather as W

T = datetime(2025, 1, 15, 0, 0)


def h(hour, **kw):
    return {"timestamp": T + timedelta(hours=hour), **kw}


class TestDurationWeighting(unittest.TestCase):
    def test_matches_a_hand_computation(self):
        """Run 09:30-11:15. A preceding-hour mean stamped T covers [T-1h, T):
          10:00 = 100 W/m2 over 09:30-10:00  -> 1800 s
          11:00 = 400 W/m2 over 10:00-11:00  -> 3600 s
          12:00 = 600 W/m2 over 11:00-11:15  ->  900 s
          (100*1800 + 400*3600 + 600*900) / 6300 = 2,160,000 / 6300
        """
        rows = [h(9, sw=50.0), h(10, sw=100.0), h(11, sw=400.0),
                h(12, sw=600.0), h(13, sw=900.0)]
        out = W.duration_weighted(rows, T + timedelta(hours=9, minutes=30),
                                  T + timedelta(hours=11, minutes=15), "sw")
        self.assertAlmostEqual(out["value"], 2_160_000 / 6300)
        self.assertEqual(out["seconds_weighted"], 6300)
        self.assertEqual(out["n_hours"], 3)
        self.assertAlmostEqual(out["coverage"], 1.0)

    def test_an_instantaneous_channel_uses_the_centred_hour(self):
        """Temperature stamped T stands for [T-30min, T+30min).
        Run 10:00-11:00: 10:00 covers 10:00-10:30 (1800 s, 20 C),
        11:00 covers 10:30-11:00 (1800 s, 24 C) -> 22 C."""
        rows = [h(10, t=20.0), h(11, t=24.0)]
        out = W.duration_weighted(rows, T + timedelta(hours=10),
                                  T + timedelta(hours=11), "t",
                                  preceding_hour_mean=False)
        self.assertAlmostEqual(out["value"], 22.0)

    def test_a_missing_hour_carries_no_weight_and_shows_in_coverage(self):
        rows = [h(10, sw=100.0), h(11, sw=None)]
        out = W.duration_weighted(rows, T + timedelta(hours=9),
                                  T + timedelta(hours=11), "sw")
        self.assertAlmostEqual(out["value"], 100.0)
        self.assertAlmostEqual(out["coverage"], 0.5)


class TestPerRecordIsTheHourItFallsInside(unittest.TestCase):
    def test_a_record_takes_the_covering_mean_not_a_blend(self):
        rows = [{"timestamp": T + timedelta(hours=k), "temp_c": 20.0,
                 "dew_point_c": 10.0, "humidity_pct": 50.0, "wind_mps": 1.0,
                 "wind_dir_deg": 0.0, "precip_mm": 0.0, "cloud_cover_pct": 0.0,
                 "shortwave_wm2": sw}
                for k, sw in ((10, 100.0), (11, 500.0))]
        [rec] = W.interpolate(rows, [T + timedelta(hours=10, minutes=20)])
        self.assertEqual(rec["shortwave_wm2"], 500.0,
                         "10:20 lies in 10:00-11:00, the mean stamped 11:00")


class TestTheChannelIsFetched(unittest.TestCase):
    def test_the_variable_is_fetched_from_now_on(self):
        self.assertIn("shortwave_radiation", W.HOURLY_VARS)
        self.assertIn("shortwave_radiation", W.PRECEDING_HOUR_MEAN)

    def test_the_request_carries_only_the_rounded_coordinate(self):
        """Shortwave rides the same request as every other channel, so the
        same privacy rule covers it: what LEAVES THE MACHINE is the coarsened
        point (§5a.1). Synthetic point; `_get` is mocked, nothing is sent."""
        sent = []

        def fake_get(url, params):
            sent.append(params)
            return {"hourly": {"time": [T.isoformat()]}}

        with mock.patch.object(W, "_get", side_effect=fake_get):
            out = W.fetch(46.512345, 7.498765, T, T + timedelta(hours=1),
                          today=T + timedelta(days=30))
        [params] = sent
        self.assertEqual((params["latitude"], params["longitude"]), (46.51, 7.5))
        self.assertIn("shortwave_radiation", params["hourly"])
        self.assertEqual((out["_requested_lat"], out["_requested_lon"]),
                         (46.51, 7.5), "the audit trail records what was sent")


if __name__ == "__main__":
    unittest.main()
