"""A best distance window starts at the LAST time at its start distance and
ends at the FIRST time at its end distance.

THE FIXTURE: 0 m at t=0, 100 m at t=10, STANDING at 100 m until t=30,
200 m at t=40.
  * best 100 m = 10 s, from either side of the stand -- neither may include
    the 20 s standing at 100 m;
  * best 150 m = 35 s -- every 150 m window must cross the stand.
The old code kept the FIRST time at each distance and interpolated
first-to-first, which spread the stand across the rise and reported the
150 m window as 25 s: faster than the athlete actually covered it.
"""

import unittest

import numpy as np

from trail_factors.analysis.basic import _best_distance_window

T = np.array([0.0, 10.0, 20.0, 30.0, 40.0])
D = np.array([0.0, 100.0, 100.0, 100.0, 200.0])


class TestBestWindowBounds(unittest.TestCase):
    def best(self, target):
        return _best_distance_window(T, D, [(0, len(T))], target)

    def test_a_window_never_includes_standing_it_can_avoid(self):
        w = self.best(100.0)
        self.assertAlmostEqual(w["seconds"], 10.0, places=9)

    def test_a_window_that_must_cross_a_stand_counts_it(self):
        w = self.best(150.0)
        self.assertAlmostEqual(w["seconds"], 35.0, places=9)
        self.assertNotAlmostEqual(
            w["seconds"], 25.0, places=3, msg="the first-to-first interpolation"
        )

    def test_a_start_on_the_stand_leaves_at_its_LAST_time(self):
        """100 m starting AT 100 m departs at t=30, not t=10."""
        t = np.array([0.0, 10.0, 30.0, 40.0])
        d = np.array([0.0, 100.0, 100.0, 200.0])
        w = _best_distance_window(t, d, [(0, 4)], 100.0)
        self.assertAlmostEqual(w["seconds"], 10.0, places=9)
        self.assertIn(w["start_elapsed_s"], (0.0, 30.0))

    def test_positive_twin_no_standing_is_unchanged(self):
        """Constant 5 m/s: every window is exactly target / 5."""
        t = np.arange(0.0, 101.0)
        d = 5.0 * t
        w = _best_distance_window(t, d, [(0, len(t))], 402.336)
        self.assertAlmostEqual(w["seconds"], 402.336 / 5.0, places=9)


if __name__ == "__main__":
    unittest.main()
