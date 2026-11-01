"""Tests for the validation check registry (SPEC.md §3, §15.4).

Each check exists because we hit the bug it catches; each test pins that bug.
"""

import unittest
from datetime import datetime, timedelta

from trail_factors.ingest.validate import FATAL, WARNING, bronze_checks, silver_checks

BASE = datetime(2025, 1, 15, 7, 0, 0)
SEMI = 2**31 / 180.0
# Synthetic, arbitrary position: not anywhere the athlete has been.
LAT, LON = 46.5, 7.5


def recs(n=200, gap=5.0, **over):
    out = []
    for i in range(n):
        # HR and speed must VARY: a constant column is itself a finding, and
        # a fixture that holds them fixed trips constant_column legitimately.
        r = {
            "timestamp": BASE + timedelta(seconds=i * gap),
            "distance": i * 10.0,
            "enhanced_speed": 2.5 + (i % 7) * 0.1,
            "heart_rate": 145 + (i % 11),
            "position_lat": int(LAT * SEMI),
            "position_long": int(LON * SEMI),
        }
        r.update({k: (v(i) if callable(v) else v) for k, v in over.items()})
        out.append(r)
    return out


SESSION = {"total_elapsed_time": 1000.0, "total_distance": 2000.0}


def names(findings, severity=None):
    return {f.check for f in findings if severity is None or f.severity == severity}


class BronzeCheckTest(unittest.TestCase):
    def test_clean_file_is_quiet(self):
        self.assertEqual(names(bronze_checks(recs(), SESSION, {})), set())

    def test_broken_semicircle_conversion_is_fatal(self):
        """Raw semicircles left unconverted land far outside degree range."""
        bad = recs(position_lat=int(LAT * SEMI) * 1000)
        f = bronze_checks(bad, SESSION, {})
        self.assertIn("coordinate_range", names(f, FATAL))

    def test_constant_column_is_flagged(self):
        """cycle_length16: non-null on every record and zero on all of them."""
        f = bronze_checks(recs(cycle_length16=0.0), SESSION, {})
        self.assertIn("constant_column", names(f, WARNING))

    def test_distance_going_backwards_is_fatal(self):
        r = recs()
        r[100]["distance"] = 5.0
        self.assertIn("distance_monotonic", names(bronze_checks(r, SESSION, {}), FATAL))

    def test_duplicate_timestamps_flagged(self):
        r = recs()
        r[50]["timestamp"] = r[49]["timestamp"]
        self.assertIn(
            "duplicate_timestamps", names(bronze_checks(r, SESSION, {}), WARNING)
        )

    def test_record_count_wildly_off_is_flagged(self):
        """Un-coalesced Strava records look exactly like this: ~2x expected."""
        f = bronze_checks(recs(n=400, gap=5.0), {"total_elapsed_time": 500.0}, {})
        self.assertIn("record_count_plausible", names(f, WARNING))

    def test_lap_distance_inconsistency_flagged(self):
        """Strava writes two duplicate lap messages: laps sum to 2x session."""
        msgs = {"lap": [{"total_distance": 2000.0}, {"total_distance": 2000.0}]}
        f = bronze_checks(recs(), SESSION, msgs)
        self.assertIn("lap_distance_consistency", names(f, WARNING))


class SilverCheckTest(unittest.TestCase):
    FULL = {"coverage_fraction": 1.0, "contiguous": True, "islands": 1}

    def test_single_impossible_sample_warns_not_rejects(self):
        """One GPS re-fix sample far above any running pace must not reject
        a file."""
        r = recs()
        r[100]["enhanced_speed"] = 19.669
        f = silver_checks(r, self.FULL)
        self.assertIn("pace_spike", names(f, WARNING))
        self.assertEqual(names(f, FATAL), set())

    def test_sustained_impossible_pace_is_fatal(self):
        """A sustained impossibility means a unit error, not a GPS glitch."""
        r = recs(enhanced_speed=lambda i: 19.0 if 10 <= i <= 30 else 2.5)
        self.assertIn(
            "pace_sustained_impossible", names(silver_checks(r, self.FULL), FATAL)
        )

    def test_partial_coverage_is_flagged_for_15_4_exclusion(self):
        f = silver_checks(
            recs(), {"coverage_fraction": 0.35, "contiguous": True, "islands": 1}
        )
        self.assertIn("gps_coverage", names(f, WARNING))

    def test_scattered_coverage_flagged_separately(self):
        f = silver_checks(
            recs(), {"coverage_fraction": 0.25, "contiguous": False, "islands": 5}
        )
        self.assertIn("gps_scattered", names(f, WARNING))

    def test_impossible_heart_rate_flagged(self):
        r = recs()
        r[10]["heart_rate"] = 300
        self.assertIn("heart_rate_range", names(silver_checks(r, self.FULL), WARNING))


if __name__ == "__main__":
    unittest.main()
