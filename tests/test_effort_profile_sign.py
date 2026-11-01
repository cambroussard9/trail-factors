"""effort_profile's steep mean keeps the effort's sign, and its label states
the threshold the code uses.

WHAT WAS REPORTED, AND WHAT IT WAS. A descent's steep_mean_grade_pct read
shallower than the effort's own mean_grade_pct, which looked like a sign or
selection bug. Worked by hand it was neither: every sample in the steep set
had the descent's sign, and the shallower figure came from comparing two
DIFFERENT ESTIMATORS on a short effort with few records -- a distance-weighted
mean of short-window (25 m) record grades against the end-to-end rise over
run. These tests pin the sign so the report cannot come back as a bug, and
pin the sparse shape so the explanation stays reproducible.

THE ONE REAL DEFECT WAS A LABEL: the filters said the steep set was ">= 10%"
while the code selects >= 5% (GRADE_BAND_MODERATE_PCT). The text is now built
from the constant, and a test reads it back against the threshold.

Every fixture is synthetic: an in-memory records_enriched with only the
columns the function reads. No corpus is needed.
"""

from __future__ import annotations

import random
import unittest

import duckdb

from trail_factors.analysis import basic as B

AID = "synthetic"


def profile(records, direction, lo=0.0, hi=None):
    """effort_profile over (path_distance_m, grade_pct) records."""
    con = duckdb.connect(":memory:")
    try:
        con.execute("CREATE TABLE records_enriched (activity_id VARCHAR, "
                    "path_distance_m DOUBLE, grade_pct DOUBLE)")
        for d, g in records:
            con.execute("INSERT INTO records_enriched VALUES (?, ?, ?)",
                        [AID, d, g])
        hi = hi if hi is not None else records[-1][0]
        return B.effort_profile(con, AID, lo, hi, direction)
    finally:
        con.close()


THRESHOLD = B.GRADE_BAND_MODERATE_PCT


class TestTheSteepMeanKeepsTheEffortsSign(unittest.TestCase):

    def test_a_descents_steep_mean_is_at_most_minus_the_threshold(self):
        p = profile([(0, -2.0), (10, -8.0), (20, -15.0), (30, -6.0),
                     (40, -1.0)], "descent")
        self.assertIsNotNone(p["steep_mean_grade_pct"])
        self.assertLessEqual(p["steep_mean_grade_pct"], -THRESHOLD)

    def test_an_ascents_steep_mean_is_at_least_the_threshold(self):
        """The mirror image."""
        p = profile([(0, 2.0), (10, 8.0), (20, 15.0), (30, 6.0),
                     (40, 1.0)], "ascent")
        self.assertIsNotNone(p["steep_mean_grade_pct"])
        self.assertGreaterEqual(p["steep_mean_grade_pct"], THRESHOLD)

    def test_it_holds_on_any_profile(self):
        """Every sample in the set is >= the threshold in the effort's
        direction, so their weighted mean must be too -- on irregular spacing
        and mixed-sign grades alike."""
        rng = random.Random(20250101)              # fixed seed: reproducible
        n_with_mean = 0
        for _ in range(80):
            n = rng.randint(3, 25)
            ds = sorted(rng.uniform(0, 400) for _ in range(n))
            recs = [(d, rng.uniform(-30, 30)) for d in ds]
            for direction, sign in (("ascent", 1.0), ("descent", -1.0)):
                m = profile(recs, direction)["steep_mean_grade_pct"]
                if m is None:
                    continue
                n_with_mean += 1
                self.assertGreaterEqual(sign * m, THRESHOLD - 1e-9,
                                        (direction, recs))
        self.assertGreater(n_with_mean, 40)        # the property was exercised


class TestAWrongSignSampleNeverEntersTheSet(unittest.TestCase):

    def test_a_steep_climb_inside_a_descent_is_left_out(self):
        """A +20% record inside a descent is wrong-sign: counted in
        wrong_sign_m, never averaged into the descent's steep mean."""
        with_it = profile([(0, -8.0), (10, 20.0), (20, -8.0), (30, -8.0)],
                          "descent")
        self.assertAlmostEqual(with_it["steep_mean_grade_pct"], -8.0)
        self.assertGreater(with_it["wrong_sign_m"], 0)
        self.assertEqual(with_it["len_steep_m"], 0.0)

    def test_a_steep_drop_inside_an_ascent_is_left_out(self):
        with_it = profile([(0, 8.0), (10, -20.0), (20, 8.0), (30, 8.0)],
                          "ascent")
        self.assertAlmostEqual(with_it["steep_mean_grade_pct"], 8.0)
        self.assertGreater(with_it["wrong_sign_m"], 0)

    def test_positive_twin_the_same_sample_with_the_right_sign_enters(self):
        """So the exclusion above is about SIGN, not about the value being
        extreme: flip the record to the descent's sign and it is averaged."""
        p = profile([(0, -8.0), (10, -20.0), (20, -8.0), (30, -8.0)],
                    "descent")
        self.assertLess(p["steep_mean_grade_pct"], -8.0)
        self.assertEqual(p["wrong_sign_m"], 0.0)
        self.assertGreater(p["len_steep_m"], 0)

    def test_a_sample_just_under_the_threshold_stays_out(self):
        """The set is >= THRESHOLD in the effort's direction, edge
        inclusive."""
        p = profile([(0, -THRESHOLD + 0.01), (10, -THRESHOLD),
                     (20, -THRESHOLD + 0.01)], "descent")
        self.assertAlmostEqual(p["steep_mean_grade_pct"], -THRESHOLD)


class TestTheSparseShortEffort(unittest.TestCase):
    """The shape that was reported: a 36 m descent with four records, whose
    end-to-end mean grade is -23.0% (8.28 m of drop) while the steep mean of
    its window grades reads about -19.4%. Shallower, SAME SIGN.

    Midpoint weights over [0, 36] for records at 2, 10, 20, 34 m are 6, 9, 12
    and 9 m. The 2 m record (-2%) is not moderate-or-steeper, so the set is
    (-30 x 9 + -14 x 12 + -16 x 9) / 30 = -582 / 30 = -19.4.
    """

    RECORDS = [(2.0, -2.0), (10.0, -30.0), (20.0, -14.0), (34.0, -16.0)]
    END_TO_END_PCT = -8.28 / 36.0 * 100          # -23.0, the stored figure

    def setUp(self):
        self.p = profile(self.RECORDS, "descent", lo=0.0, hi=36.0)

    def test_the_hand_computation(self):
        self.assertAlmostEqual(self.p["steep_mean_grade_pct"], -19.4,
                               places=9)
        self.assertAlmostEqual(self.END_TO_END_PCT, -23.0, places=9)

    def test_it_reads_shallower_than_end_to_end_with_the_right_sign(self):
        m = self.p["steep_mean_grade_pct"]
        self.assertLess(m, 0)
        self.assertLess(abs(m), abs(self.END_TO_END_PCT))
        self.assertLessEqual(m, -THRESHOLD)

    def test_the_weights_cover_the_effort(self):
        p = self.p
        total = (p["len_steep_m"] + p["len_moderate_m"] + p["len_gentle_m"]
                 + p["len_flat_m"])
        self.assertAlmostEqual(total, 36.0)
        self.assertAlmostEqual(p["grade_coverage_fraction"], 1.0)
        self.assertAlmostEqual(p["len_steep_m"], 30.0)    # 9 + 12 + 9
        self.assertAlmostEqual(p["len_gentle_m"], 6.0)    # the -2% record


class TestTheLabelStatesTheThresholdUsed(unittest.TestCase):
    """The filters once said the steep set was >= 10% while the code used 5%.
    The text must name the threshold the code applies."""

    def setUp(self):
        self.f = B.profile_filters()

    def test_the_threshold_field_is_the_constant_the_code_applies(self):
        self.assertEqual(self.f["steep_threshold_pct"],
                         B.GRADE_BAND_MODERATE_PCT)

    def test_the_definition_names_that_threshold_and_not_the_band(self):
        t = self.f["steep_threshold_definition"]
        pct = f"{self.f['steep_threshold_pct']:.0f}%"
        self.assertIn(f">= {pct}", t)
        self.assertIn(f"+{pct}", t)
        self.assertIn(f"-{pct}", t)
        self.assertIn("MODERATE-OR-STEEPER", t)
        self.assertIn("effort's own direction", t)
        # It mentions the 10% band only to say it is NOT the one used.
        self.assertIn(f"not the {B.GRADE_BAND_STEEP_PCT:.0f}% steep band", t)
        self.assertNotIn(f">= {B.GRADE_BAND_STEEP_PCT:.0f}%", t)

    def test_the_estimator_is_described(self):
        e = self.f["steep_mean_estimator"]
        self.assertIn("distance-weighted", e)
        self.assertIn("25 m", e)
        self.assertIn("SHALLOWER", e)
        self.assertIn("sign", e)

    def test_positive_twin_the_band_text_still_names_the_10_percent_band(self):
        """The bands DO cut at 10%, so their text must still say so."""
        self.assertIn(f"{B.GRADE_BAND_STEEP_PCT:.0f}%",
                      self.f["band_definition"])
        self.assertEqual(self.f["bands_pct"]["steep"],
                         f">= {B.GRADE_BAND_STEEP_PCT:.0f}")


if __name__ == "__main__":
    unittest.main()
