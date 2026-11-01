"""Milestone 6: slope boundary rules, tested on SYNTHETIC profiles.

These are synthetic on purpose. A rolling trail with no long flats between
features CANNOT exercise the boundary rule at all -- on such terrain a corpus
test is a regression, not a validation. The first real validation of slope
boundaries is the first run on a course with climbs that top out onto flats
(§5).

Every negative case has a positive twin, per the §12.3 review lesson: a suite
that only asserts things do NOT happen passes on code that does nothing.
"""

import unittest

import numpy as np

from trail_factors.transform.silver import detect_slopes, profile_grade


def profile(*legs, step=10.0):
    """Build (elev, cum) from (length_m, grade_pct) legs."""
    cum, elev, d, e = [0.0], [0.0], 0.0, 0.0
    for length, grade in legs:
        end = d + length
        while d + step <= end + 1e-9:
            d += step
            e += step * grade / 100.0
            cum.append(d)
            elev.append(e)
    return np.array(elev), np.array(cum)


class TestFlatsBelongToNeither(unittest.TestCase):
    """The case a rolling trail cannot test and a course with long flats
    between climbs will hit."""

    def test_three_km_flat_gives_two_segments_with_the_flat_in_neither(self):
        elev, cum = profile((500, 6.0), (3000, 0.0), (500, -6.0))
        slopes = detect_slopes(elev, cum)
        self.assertEqual(len(slopes), 2, [s["direction"] for s in slopes])
        asc, desc = slopes
        self.assertEqual(asc["direction"], "ascent")
        self.assertEqual(desc["direction"], "descent")
        # the flat runs 500..3500; neither segment may reach into it
        self.assertLess(asc["end_distance_m"], 600)
        self.assertGreater(desc["start_distance_m"], 3400)

    def test_positive_twin_without_the_flat_it_is_one_hill_crossing(self):
        """Same terrain, no flat: still two slopes, but adjacent."""
        elev, cum = profile((500, 6.0), (500, -6.0))
        slopes = detect_slopes(elev, cum)
        self.assertEqual(len(slopes), 2)
        self.assertLess(
            slopes[1]["start_distance_m"] - slopes[0]["end_distance_m"],
            100,
            "with no flat the two slopes should be adjacent",
        )

    def test_climb_to_plateau_then_a_different_descent(self):
        """A climb that tops out onto a plateau. There is NO col to key on."""
        elev, cum = profile((2000, 8.0), (1500, 0.2), (2500, -5.0))
        slopes = detect_slopes(elev, cum)
        self.assertEqual(len(slopes), 2)
        self.assertEqual([s["direction"] for s in slopes], ["ascent", "descent"])
        self.assertGreater(slopes[0]["vertical_m"], 100)
        self.assertGreater(slopes[1]["vertical_m"], 100)


class TestPeakKeyingDoesNotReintroduceUpThenDown(unittest.TestCase):
    """Identity is (place, side), but the UNIT is still a slope (§5.4).

    Inheriting identity from the peak is what makes the library stable -- §5.3
    measured peaks at SD 0.54 m against cols at 2.20 m. The worry is that it
    smuggles the up-then-down assumption back in. It does not, for a structural
    reason: hysteresis never ENTERS on the flat side, so a summit followed by a
    flat yields one slope, not a matched pair.
    """

    def test_climb_flat_descent_gives_two_slopes_with_different_high_ends(self):
        """The two slopes are anchored at opposite ends of the flat, 3 km
        apart, so they can never be keyed to the same place."""
        elev, cum = profile((500, 6.0), (3000, 0.0), (500, -6.0))
        slopes = detect_slopes(elev, cum)
        self.assertEqual(len(slopes), 2)
        asc, desc = slopes
        # high end of the ascent vs high end of the descent, along the path
        asc_high = asc["end_distance_m"]
        desc_high = desc["start_distance_m"]
        self.assertGreater(
            abs(desc_high - asc_high),
            2500,
            "the two high ends must be separated by the flat",
        )

    def test_a_summit_followed_by_a_flat_yields_one_slope(self):
        """A climb onto a plateau. Hysteresis never enters on the flat side,
        so there is no descent to pair with -- and that is correct, not a gap."""
        elev, cum = profile((1500, 7.0), (2500, 0.1))
        slopes = detect_slopes(elev, cum)
        self.assertEqual(len(slopes), 1)
        self.assertEqual(slopes[0]["direction"], "ascent")

    def test_positive_twin_a_true_roller_still_pairs(self):
        """With no flat between them, the two flanks share a summit -- which is
        what peak keying is FOR."""
        elev, cum = profile((500, 6.0), (500, -6.0))
        slopes = detect_slopes(elev, cum)
        self.assertEqual(len(slopes), 2)
        self.assertLess(
            abs(slopes[1]["start_distance_m"] - slopes[0]["end_distance_m"]),
            100,
            "the two flanks must meet at a shared summit",
        )


class TestBenchTolerance(unittest.TestCase):
    """A brief flat inside a climb must not end the climb."""

    def test_a_100m_bench_leaves_one_ascent(self):
        elev, cum = profile((400, 6.0), (100, 0.0), (400, 6.0))
        slopes = detect_slopes(elev, cum)
        self.assertEqual(len(slopes), 1, [s["length_m"] for s in slopes])
        self.assertEqual(slopes[0]["direction"], "ascent")
        self.assertGreater(slopes[0]["length_m"], 800)

    def test_a_500m_flat_splits_the_climb(self):
        """The negative twin: past the tolerance it is two ascents."""
        elev, cum = profile((400, 6.0), (500, 0.0), (400, 6.0))
        slopes = detect_slopes(elev, cum)
        self.assertEqual(len(slopes), 2, [s["length_m"] for s in slopes])
        self.assertEqual([s["direction"] for s in slopes], ["ascent", "ascent"])

    def test_the_tolerance_is_the_thing_being_tested(self):
        """Both cases must flip when BENCH_MAX_M moves, or they test nothing."""
        elev, cum = profile((400, 6.0), (300, 0.0), (400, 6.0))
        self.assertEqual(len(detect_slopes(elev, cum, bench_max_m=400.0)), 1)
        self.assertEqual(len(detect_slopes(elev, cum, bench_max_m=100.0)), 2)


class TestSignificanceThreshold(unittest.TestCase):
    """Reversal significance reuses the 8 m prominence threshold."""

    def test_a_small_dip_mid_climb_is_absorbed(self):
        elev, cum = profile((400, 6.0), (60, -8.0), (400, 6.0))  # ~4.8 m dip
        slopes = detect_slopes(elev, cum)
        self.assertEqual(len(slopes), 1, [s["vertical_m"] for s in slopes])

    def test_a_significant_dip_mid_climb_splits_it(self):
        elev, cum = profile((400, 6.0), (200, -8.0), (400, 6.0))  # ~16 m dip
        slopes = detect_slopes(elev, cum)
        self.assertGreater(len(slopes), 1, "a 16 m reversal is a real feature")
        self.assertIn("descent", [s["direction"] for s in slopes])

    def test_a_slope_below_the_vertical_threshold_is_not_a_segment(self):
        elev, cum = profile((100, 5.0))  # 5 m of vertical
        self.assertEqual(detect_slopes(elev, cum), [])

    def test_positive_twin_just_above_the_threshold_is_a_segment(self):
        elev, cum = profile((200, 5.0))  # 10 m of vertical
        self.assertEqual(len(detect_slopes(elev, cum)), 1)


class TestMergeVeto(unittest.TestCase):
    """Chained benches must not produce a 'climb' that is mostly flat."""

    def test_chained_benches_do_not_make_a_flat_stretch_a_climb(self):
        """Three short rises joined by three 350 m benches: each merge is
        individually legal, and iterating them produced a 1,400 m 'ascent'
        averaging +0.9% on the real corpus. The result must stay a slope."""
        elev, cum = profile(
            (120, 5.0),
            (350, 0.0),
            (120, 5.0),
            (350, 0.0),
            (120, 5.0),
            (350, 0.0),
            (120, 5.0),
        )
        for s in detect_slopes(elev, cum):
            self.assertGreaterEqual(
                abs(s["mean_grade_pct"]),
                1.0,
                f"{s['length_m']:.0f} m at {s['mean_grade_pct']:+.1f}% is not a slope",
            )

    def test_positive_twin_a_real_climb_with_one_bench_still_merges(self):
        """The veto must not block a legitimate merge."""
        elev, cum = profile((400, 6.0), (200, 0.0), (400, 6.0))
        slopes = detect_slopes(elev, cum)
        self.assertEqual(len(slopes), 1)
        self.assertGreater(abs(slopes[0]["mean_grade_pct"]), 1.0)


class TestPartitionInvariants(unittest.TestCase):
    """Non-overlap is structural, not something a threshold has to protect."""

    def test_slopes_never_overlap(self):
        elev, cum = profile(
            (400, 6.0),
            (200, 0.0),
            (300, -7.0),
            (150, 0.0),
            (600, 5.0),
            (2000, 0.0),
            (400, -6.0),
        )
        slopes = detect_slopes(elev, cum)
        for a, b in zip(slopes, slopes[1:], strict=False):
            self.assertLessEqual(
                a["end_distance_m"],
                b["start_distance_m"],
                "ascent and descent must not overlap",
            )

    def test_endpoints_are_direction_normalised(self):
        """A slope is keyed low-to-high however it was run, so the ascent and
        the descent of one face are the SAME segment (§5, amended)."""
        up, cum_u = profile((500, 6.0))
        down, cum_d = profile((500, -6.0))
        a = detect_slopes(up, cum_u)[0]
        d = detect_slopes(down, cum_d)[0]
        self.assertEqual(a["direction"], "ascent")
        self.assertEqual(d["direction"], "descent")
        for s in (a, d):
            self.assertLess(s["low_elevation_m"], s["high_elevation_m"])
        self.assertAlmostEqual(a["vertical_m"], d["vertical_m"], places=6)

    def test_a_pure_flat_produces_nothing(self):
        elev, cum = profile((3000, 0.0))
        self.assertEqual(detect_slopes(elev, cum), [])

    def test_profile_grade_is_signed_and_scaled(self):
        elev, cum = profile((500, 6.0))
        g = profile_grade(elev, cum)
        mid = g[len(g) // 2]
        self.assertAlmostEqual(mid, 6.0, delta=0.5)
        elev2, cum2 = profile((500, -6.0))
        self.assertAlmostEqual(profile_grade(elev2, cum2)[len(g) // 2], -6.0, delta=0.5)


if __name__ == "__main__":
    unittest.main()
