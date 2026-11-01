"""Tests for file-level validation (SPEC.md §9).

Synthetic records, so these do not depend on data/raw/ contents.
"""

import unittest
from datetime import datetime, timedelta

from trail_factors.ingest.validate import BRONZE, FATAL, bronze_checks, check_timestamps

BASE = datetime(2025, 1, 15, 7, 0, 0)
# A corrupt timestamp of several weeks between two consecutive records: the
# failure mode these checks exist for. Synthetic, ~46 days.
WEEKS_GAP_S = 4_000_000.0


def records(gaps_s):
    """Records starting at BASE, separated by the given gaps."""
    out, t = [{"timestamp": BASE}], BASE
    for g in gaps_s:
        t = t + timedelta(seconds=g)
        out.append({"timestamp": t})
    return out


class TimestampValidationTest(unittest.TestCase):
    def test_clean_file_passes(self):
        recs = records([5.0] * 200)
        fatal, warn = check_timestamps(recs, {"total_elapsed_time": 1000.0})
        self.assertEqual(fatal, [])
        self.assertEqual(warn, [])

    def test_a_gap_of_weeks_is_fatal(self):
        """The motivating failure mode: one absurd gap among normal ones."""
        recs = records([5.0] * 100 + [WEEKS_GAP_S] + [5.0] * 100)
        fatal, _ = check_timestamps(recs, {"total_elapsed_time": 2484.0})
        self.assertEqual(len(fatal), 2, "both span and gap checks should fire")
        self.assertTrue(any("span" in f for f in fatal))
        self.assertTrue(any("gap" in f for f in fatal))

    def test_span_exceeding_session_is_fatal_even_without_one_big_gap(self):
        """Corruption spread across many gaps still trips the span check."""
        recs = records([600.0] * 50)  # 30000 s of span
        fatal, _ = check_timestamps(recs, {"total_elapsed_time": 2000.0})
        self.assertTrue(any("span" in f for f in fatal))

    def test_median_is_robust_so_regime_detection_cannot_catch_this(self):
        """Why this check exists at all: the median ignores the outlier."""
        import statistics

        recs = records([5.0] * 100 + [WEEKS_GAP_S] + [5.0] * 100)
        stamps = [r["timestamp"] for r in recs]
        gaps = [
            (b - a).total_seconds() for a, b in zip(stamps, stamps[1:], strict=False)
        ]
        self.assertEqual(statistics.median(gaps), 5.0)  # regime looks fine
        self.assertTrue(check_timestamps(recs, {"total_elapsed_time": 2484.0})[0])

    def test_large_but_survivable_gap_warns_rather_than_rejects(self):
        recs = records([5.0] * 100 + [400.0] + [5.0] * 100)
        fatal, warn = check_timestamps(recs, {"total_elapsed_time": 1500.0})
        self.assertEqual(fatal, [])
        self.assertEqual(len(warn), 1)

    def test_missing_session_elapsed_still_checks_gaps(self):
        recs = records([5.0] * 50 + [7200.0] + [5.0] * 50)
        fatal, _ = check_timestamps(recs, {})
        self.assertTrue(any("gap" in f for f in fatal))

    def test_too_few_records_is_fatal(self):
        fatal, _ = check_timestamps([{"timestamp": BASE}], {"total_elapsed_time": 10.0})
        self.assertTrue(fatal)

    def test_bronze_checks_carry_a_fatal_timestamp_as_a_finding(self):
        """The ingest path reaches check_timestamps through bronze_checks: a
        fatal timestamp problem must arrive as a FATAL `timestamp_integrity`
        Finding, which is what makes ingest reject the file."""
        found = bronze_checks(
            records([5.0] * 10 + [WEEKS_GAP_S]), {"total_elapsed_time": 100.0}, {}
        )
        ts = [f for f in found if f.check == "timestamp_integrity"]
        self.assertTrue(ts)
        for f in ts:
            self.assertEqual(f.severity, FATAL)
            self.assertEqual(f.layer, BRONZE)
            self.assertTrue(f.is_fatal)

    def test_a_clean_file_carries_no_timestamp_finding(self):
        """THE TWIN: the check must not fire on normal spacing."""
        found = bronze_checks(records([5.0] * 50), {"total_elapsed_time": 300.0}, {})
        self.assertFalse([f for f in found if f.check.startswith("timestamp")])

    def test_a_survivable_gap_is_a_stored_warning_not_a_rejection(self):
        found = bronze_checks(
            records([5.0] * 100 + [400.0] + [5.0] * 100),
            {"total_elapsed_time": 1500.0},
            {},
        )
        gap = [f for f in found if f.check == "timestamp_gap"]
        self.assertEqual(len(gap), 1)
        self.assertFalse(gap[0].is_fatal)
        self.assertFalse([f for f in found if f.check == "timestamp_integrity"])


if __name__ == "__main__":
    unittest.main()
