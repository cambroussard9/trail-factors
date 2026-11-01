"""One segment must not pool efforts on different ground (SPEC.md §5, §12.4).

THE DEFECT THIS GUARDS. A segment was found holding a cluster of short, steep
descents AND a cluster of "ascents" several times longer at barely 1%. It
looked like a fork -- a direct line and a shallow route between the same
endpoints -- and it is not. The long members are the SAME short pitch with a
stretch of plateau absorbed onto them: the merge veto in silver.py tests the
grade of the CUMULATIVE result, never of the piece being added, so near-flat
pieces each passed on vertical banked by an earlier legitimate merge.

WHY IT IS NOT SIMPLY FIXED. Adding the missing added-piece veto was
implemented and measured across every DEM-covered activity. It does not
remove the absorption, it MIRRORS it: the long, shallow members move from the
ascents onto the descents. The residual needs a threshold below the plateau's
own texture, where the project's significance constant (PROMINENCE_MIN_M =
8.0 m) sits above it -- and a rolling home trail cannot validate the slope
boundary rule, having already produced two corrected design errors that way.
So the rule is left alone, the composition is carried per effort instead
(basic.effort_profile), and these tests keep the pooling VISIBLE and
recognisable rather than pinning which segments of one corpus show it.
"""

import unittest

import duckdb

from tests.corpus import CorpusTestCase, use_config

# Ratio of max to min |mean_grade_pct| across one segment's efforts. On the
# development corpus the ratios split into two groups with an empty band
# between them, and 5.0 sat in it -- a property of that data, not of every
# corpus, so it is used here as the definition of "pooled", not re-derived.
MAX_SEGMENT_GRADE_RATIO = 5.0

# A pattern needs repeats: the subject segment must have this many efforts
# for both its short and its absorbed members to be represented.
MIN_EFFORTS_FOR_SUBJECT = 10

# THE STEEP-VS-MEAN PREMISE HOLDS ONLY WHERE THE TWO ESTIMATORS AGREE ON WHAT
# THEY MEASURE. steep_mean_grade_pct is a distance-weighted mean of short-window
# (25 m) record grades; mean_grade_pct is the effort's end-to-end rise over
# run. On an effort a few window-lengths long, with a handful of records, the
# window grades can read SHALLOWER than the end-to-end figure while keeping its
# sign (basic.profile_filters()['steep_mean_estimator']). That is a property of
# the estimators on sparse ground, not the absorption these tests guard, so the
# premise is asserted on efforts of four windows or more.
MIN_LENGTH_FOR_STEEP_PREMISE_M = 100.0


def _ratios(con):
    return {r[0]: (r[1], r[2]) for r in con.execute("""
        SELECT segment_id, count(*),
               max(abs(mean_grade_pct)) / nullif(min(abs(mean_grade_pct)), 0)
        FROM segment_efforts
        GROUP BY 1 HAVING count(*) >= 2 AND min(abs(mean_grade_pct)) > 0
    """).fetchall()}


def _pooled(ratios):
    return {s for s, (n, r) in ratios.items()
            if r is not None and r > MAX_SEGMENT_GRADE_RATIO}


def _history(con, segment_id):
    from trail_factors.analysis.over import segment_history
    with use_config():
        return segment_history(con, segment_id)


class TestTheRatioCheckCanFire(unittest.TestCase):
    """The detector itself, on a synthetic effort table: no corpus needed."""

    def setUp(self):
        self.con = duckdb.connect(":memory:")
        self.con.execute("CREATE TABLE segment_efforts "
                         "(segment_id VARCHAR, mean_grade_pct DOUBLE)")
        self.con.executemany("INSERT INTO segment_efforts VALUES (?, ?)", [
            # one slope, efforts at similar grades
            ("seg000000001", 6.0), ("seg000000001", -6.5), ("seg000000001", 7.0),
            # a short pitch pooled with plateau-absorbed long members
            ("seg000000002", -7.5), ("seg000000002", 1.2), ("seg000000002", -8.0),
            # a single effort has no spread to measure
            ("seg000000003", 4.0),
        ])

    def tearDown(self):
        self.con.close()

    def test_the_check_can_actually_fire(self):
        """THE PRESENCE TWIN. A threshold above every observed value would
        guard nothing (CLAUDE.md: a test that only asserts absence is
        vacuous)."""
        self.assertEqual(_pooled(_ratios(self.con)), {"seg000000002"})

    def test_a_consistent_segment_is_not_flagged(self):
        r = _ratios(self.con)
        self.assertLess(r["seg000000001"][1], MAX_SEGMENT_GRADE_RATIO)
        self.assertNotIn("seg000000003", r)


class TestPooledSegmentsShowTheKnownSignature(CorpusTestCase):
    """Every pooled segment in the corpus must be the DIAGNOSED kind: its
    shallowest member is mostly gentle or flat ground, i.e. a pitch with
    plateau absorbed. A segment pooled some OTHER way would fail here, which
    is the regression this guards -- without naming any corpus's segments."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.pooled = _pooled(_ratios(cls.con))
        if not cls.pooled:
            raise unittest.SkipTest("this corpus has no segment pooling "
                                    "across grades")

    def test_the_shallowest_member_is_mostly_not_steep(self):
        for sid in sorted(self.pooled):
            efforts = [e for e in _history(self.con, sid)["efforts"]
                       if e["len_gentle_m"] is not None and e["length_m"]]
            if not efforts:
                continue
            shallow = min(efforts, key=lambda e: abs(e["mean_grade_pct"]))
            gentle = shallow["len_gentle_m"] + shallow["len_flat_m"]
            with self.subTest(segment=sid):
                self.assertGreater(gentle / shallow["length_m"], 0.5)


class TestEffortProfileMakesPoolingVisible(CorpusTestCase):
    """The pooling is not repaired, so it must at least be VISIBLE wherever an
    effort is read. That is the whole justification for not moving the rule.

    Subject, by property: the most-pooled segment (highest grade ratio) with
    enough efforts to carry both kinds of member.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        candidates = sorted(
            ((r, s) for s, (n, r) in _ratios(cls.con).items()
             if r is not None and r > MAX_SEGMENT_GRADE_RATIO
             and n >= MIN_EFFORTS_FOR_SUBJECT), reverse=True)
        if not candidates:
            raise unittest.SkipTest(
                f"this corpus has no pooled segment with "
                f"{MIN_EFFORTS_FOR_SUBJECT}+ efforts")
        cls.hist = _history(cls.con, candidates[0][1])

    def test_every_effort_carries_its_grade_composition(self):
        for e in self.hist["efforts"]:
            for k in ("len_steep_m", "len_moderate_m", "len_gentle_m",
                      "len_flat_m", "steep_mean_grade_pct",
                      "tail_after_last_steep_m"):
                self.assertIn(k, e)

    def test_the_bands_sum_to_the_covered_length(self):
        """A band set that did not sum to the effort would be a record-count
        fraction wearing a distance's name."""
        n = 0
        for e in self.hist["efforts"]:
            if e["len_steep_m"] is None or e["grade_coverage_fraction"] is None:
                continue
            n += 1
            total = (e["len_steep_m"] + e["len_moderate_m"]
                     + e["len_gentle_m"] + e["len_flat_m"])
            self.assertAlmostEqual(
                total, e["length_m"] * e["grade_coverage_fraction"],
                delta=max(1.0, 0.02 * e["length_m"]), msg=e["date"])
        self.assertGreater(n, 0)

    def test_the_steep_part_is_steeper_than_the_stored_average(self):
        """The point of carrying the profile: on a plateau-absorbed effort the
        stored mean understates the pitch, and the reader must be able to see
        by how much. Efforts shorter than MIN_LENGTH_FOR_STEEP_PREMISE_M are
        left out: there the two estimators can disagree by construction."""
        gaps = [abs(e["steep_mean_grade_pct"]) - abs(e["mean_grade_pct"])
                for e in self.hist["efforts"]
                if e["steep_mean_grade_pct"] is not None
                and e["length_m"] is not None
                and e["length_m"] >= MIN_LENGTH_FOR_STEEP_PREMISE_M]
        if not gaps:
            self.skipTest(f"no effort of {MIN_LENGTH_FOR_STEEP_PREMISE_M:.0f} m "
                          f"or more on this segment carries a steep mean")
        for g in gaps:
            self.assertGreater(g, -0.5)
        # PRESENCE: on a pooled segment the understatement must be large, or
        # the measurement is not doing its job.
        self.assertGreater(max(gaps), 3.0)

    def test_the_shallowest_effort_is_mostly_not_steep(self):
        """The diagnosis: the shallow members are a short pitch plus a
        plateau, so most of their length is gentle or flat."""
        shallow = min((e for e in self.hist["efforts"]
                       if e["len_gentle_m"] is not None and e["length_m"]),
                      key=lambda e: abs(e["mean_grade_pct"]))
        gentle = shallow["len_gentle_m"] + shallow["len_flat_m"]
        self.assertGreater(gentle / shallow["length_m"], 0.5)

    def test_a_short_effort_is_mostly_steep(self):
        """The positive twin: the short members are real pitches, so the
        measurement must distinguish them rather than calling everything flat."""
        shortest = min((e for e in self.hist["efforts"] if e["len_steep_m"] is not None),
                       key=lambda e: e["length_m"])
        steep = shortest["len_steep_m"] + shortest["len_moderate_m"]
        self.assertGreater(steep / shortest["length_m"], 0.5)


if __name__ == "__main__":
    unittest.main()
