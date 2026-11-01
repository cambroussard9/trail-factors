"""Pace is displayed as M:SS per mile, never as decimal minutes (src/trail_factors/units.py).

The failure this guards against is quiet: "12.45 min/mi" is a well-formed
number that a reader will take as 12:45 and that is really 12:27. It is wrong
by 18 s, always in the same direction, and nothing downstream can detect it —
so the formatter is pinned here rather than trusted.
"""

import unittest

from trail_factors.units import METERS_PER_MILE, miles, mmss, pace_over, pace_per_mile


class TestKnownPace(unittest.TestCase):
    """An illustrative decimal-minutes example, and round ones around it."""

    def test_the_reference_pace_renders_as_minutes_and_seconds(self):
        """12.45 decimal min/mi is 12:27 — NOT 12:45."""
        self.assertEqual(pace_over(12.45 * 60, METERS_PER_MILE), "12:27")

    def test_an_eight_minute_mile_from_speed(self):
        """480 s per mile is 3.3528 m/s; it must come back as 8:00."""
        self.assertEqual(pace_per_mile(METERS_PER_MILE / 480), "8:00")

    def test_a_whole_number_pace_keeps_its_zeroes(self):
        self.assertEqual(mmss(600), "10:00")

    def test_seconds_are_zero_padded(self):
        """9:05, not 9:5 — an unpadded second reads as a different pace."""
        self.assertEqual(mmss(545), "9:05")

    def test_a_slow_walk_is_still_minutes_and_seconds(self):
        """20.25 decimal min/mi, a walking pace, is 20:15."""
        self.assertEqual(mmss(20.25 * 60), "20:15")

    def test_a_pace_over_an_hour_does_not_wrap(self):
        """A 61-minute mile is 61:00, not 1:00. Nothing here is a clock time."""
        self.assertEqual(mmss(3660), "61:00")


class TestRounding(unittest.TestCase):

    def test_rounding_carries_into_the_minute(self):
        """13.999 min/mi must be 14:00. Splitting before rounding gives 13:60."""
        self.assertEqual(mmss(13.999 * 60), "14:00")

    def test_a_half_second_rounds_rather_than_truncates(self):
        """Truncating would bias every displayed pace fast."""
        self.assertEqual(mmss(599.6), "10:00")

    def test_sub_second_differences_do_not_change_the_display(self):
        self.assertEqual(mmss(600.2), mmss(600.0))


class TestNoPaceToShow(unittest.TestCase):
    """Zero is a legitimate stored speed (CLAUDE.md) and is NOT a fast pace.
    Every one of these must reach the placeholder rather than a number that
    looks real."""

    def test_a_missing_speed_gives_the_placeholder(self):
        self.assertEqual(pace_per_mile(None, "--:--"), "--:--")

    def test_a_zero_speed_gives_the_placeholder_not_a_division_error(self):
        self.assertEqual(pace_per_mile(0.0, "--:--"), "--:--")

    def test_a_negative_speed_gives_the_placeholder(self):
        self.assertEqual(pace_per_mile(-1.0, "--:--"), "--:--")

    def test_the_default_placeholder_is_none_for_a_json_payload(self):
        """A tool result carries null, not the string a terminal would print."""
        self.assertIsNone(pace_per_mile(None))
        self.assertIsNone(pace_over(0, 1000))
        self.assertIsNone(pace_over(600, 0))
        self.assertIsNone(pace_over(None, None))

    def test_a_real_speed_still_formats(self):
        """The positive twin: the guards above must not swallow real values."""
        self.assertEqual(pace_per_mile(2.0, "--:--"), "13:25")


class TestMiles(unittest.TestCase):

    def test_the_mile_is_the_international_mile(self):
        self.assertAlmostEqual(miles(1609.344), 1.0, places=9)

    def test_a_missing_distance_is_not_zero_miles(self):
        self.assertIsNone(miles(None))

    def test_zero_metres_is_zero_miles_not_missing(self):
        """0 is a real distance; the falsy shortcut would drop it (CLAUDE.md)."""
        self.assertEqual(miles(0.0), 0.0)


if __name__ == "__main__":
    unittest.main()
