"""§6.3 climb-recovery: delta-HR and tau, always together (SPEC.md §12.5).

Every negative case has a positive twin, per the §12.3 review lesson: a suite
that only asserts things do NOT happen passes on code that does nothing.
"""

import unittest
from datetime import datetime, timedelta

from trail_factors.analysis import recovery as R
from tests.corpus import CorpusTestCase, one, use_config

T0 = datetime(2023, 6, 20, 7, 0)                  # synthetic


def tail(*hrs, step_s=5):
    return [(T0 + timedelta(seconds=step_s * i), h) for i, h in enumerate(hrs)]


class TestDeltaHR(unittest.TestCase):
    """delta-HR is the finding; tau is the diagnostic."""

    def test_the_excursion_is_peak_minus_opening_not_peak_minus_trough(self):
        self.assertEqual(R.delta_hr([140, 150, 155, 148]), 15.0)

    def test_a_flat_effort_has_a_delta_of_zero_not_none(self):
        """CLAUDE.md: 0 is a value. None would mean 'no HR', which is different."""
        self.assertEqual(R.delta_hr([145, 145, 145]), 0.0)

    def test_missing_hr_gives_none(self):
        self.assertIsNone(R.delta_hr([None, None]))
        self.assertIsNone(R.delta_hr([]))

    def test_nulls_are_skipped_not_treated_as_zero(self):
        self.assertEqual(R.delta_hr([140, None, 160]), 20.0)


class TestTauRefusals(unittest.TestCase):
    """§4.2: a metric that cannot be measured must not be reported."""

    def test_a_short_clear_window_declines_with_a_reason(self):
        out = R.tau_estimate(tail(160, 150, 140), clear_window_s=30.0)
        self.assertIsNone(out["tau_s"])
        self.assertIn("clear", out["reason"])

    def test_hr_that_does_not_fall_declines(self):
        out = R.tau_estimate(tail(150, 151, 152, 153, 154, 155), 300.0)
        self.assertIsNone(out["tau_s"])
        self.assertIn("did not fall", out["reason"])

    def test_too_few_samples_declines(self):
        out = R.tau_estimate(tail(160, 150), 300.0)
        self.assertIsNone(out["tau_s"])
        self.assertIn("too few", out["reason"])

    def test_positive_twin_a_clean_decay_yields_a_tau(self):
        """The refusals above are meaningless unless a good case succeeds."""
        out = R.tau_estimate(tail(170, 168, 164, 158, 152, 148, 145, 143), 300.0)
        self.assertIsNotNone(out["tau_s"])
        self.assertGreater(out["tau_s"], 0)
        self.assertIsNone(out["reason"])
        self.assertEqual(out["drop_bpm"], 27.0)

    def test_every_refusal_states_why(self):
        for args in ((tail(160, 150, 140), 30.0),
                     (tail(150, 151, 152, 153, 154, 155), 300.0),
                     (tail(160, 150), 300.0)):
            out = R.tau_estimate(*args)
            self.assertIsNotNone(out["reason"], out)


class TestPairIsNeverSplit(unittest.TestCase):
    """tau must never be returned without the delta-HR that produced it."""

    def test_a_tiny_excursion_suppresses_tau(self):
        """A 3 bpm excursion recovering fast is not good recovery."""
        good_tail = tail(150, 149, 147, 145, 144, 143, 142, 141)
        out = R.effort_recovery([145, 148, 147], good_tail, 300.0)
        self.assertEqual(out["delta_hr_bpm"], 3.0)
        self.assertIsNone(out["tau_s"])
        self.assertIn("floor", out["reason"])

    def test_positive_twin_a_real_excursion_keeps_tau(self):
        good_tail = tail(170, 166, 160, 154, 150, 147, 145, 143)
        out = R.effort_recovery([145, 165, 170], good_tail, 300.0)
        self.assertGreaterEqual(out["delta_hr_bpm"], R.MIN_DELTA_HR_BPM)
        self.assertIsNotNone(out["tau_s"])

    def test_no_hr_during_the_effort_suppresses_tau(self):
        good_tail = tail(170, 166, 160, 154, 150, 147, 145, 143)
        out = R.effort_recovery([None], good_tail, 300.0)
        self.assertIsNone(out["delta_hr_bpm"])
        self.assertIsNone(out["tau_s"])

    def test_the_result_always_carries_delta_hr(self):
        out = R.effort_recovery([145, 160], tail(160, 150), 300.0)
        self.assertIn("delta_hr_bpm", out)


class TestReportability(unittest.TestCase):
    """tau is shown only if repeat traversals of ONE slope agree."""

    def test_noisy_repeats_are_not_reportable(self):
        v = R.tau_is_reportable({("s", "ascent"): [20.0, 120.0, 240.0]})
        self.assertFalse(v["reportable"])
        self.assertIsNotNone(v["reason"])

    def test_positive_twin_tight_repeats_are_reportable(self):
        v = R.tau_is_reportable({("s", "ascent"): [90.0, 92.0, 88.0],
                                 ("s", "descent"): [80.0, 82.0, 78.0]})
        self.assertTrue(v["reportable"])
        self.assertLess(v["cv"], R.MAX_TAU_CV)

    def test_too_few_repeats_is_not_reportable(self):
        v = R.tau_is_reportable({("s", "ascent"): [90.0, 91.0]})
        self.assertFalse(v["reportable"])
        self.assertEqual(v["n_groups"], 0)

    # THE RULE, ON FIXTURES. These replace a pinned corpus COUNT (3 groups),
    # which was a fact about one week's data and expired when a fourth group
    # reached 3 estimates. The rule does not drift; the count always will.

    def test_a_group_counts_only_at_three_estimates(self):
        """n_groups is the number of (segment, direction) groups with 3+."""
        tight = [90.0, 92.0, 88.0]
        v = R.tau_is_reportable({("a", "ascent"): tight,
                                 ("b", "ascent"): tight,
                                 ("c", "ascent"): [90.0, 91.0]})
        self.assertEqual(v["n_groups"], 2)

    def test_a_noisy_group_below_three_does_not_vote(self):
        """A two-estimate group is not evidence either way: it can neither
        open the gate nor hold it shut."""
        tight = {("a", "ascent"): [90.0, 92.0, 88.0]}
        noisy_pair = {("z", "descent"): [10.0, 400.0]}
        self.assertEqual(R.tau_is_reportable(tight)["cv"],
                         R.tau_is_reportable({**tight, **noisy_pair})["cv"])
        self.assertTrue(R.tau_is_reportable({**tight, **noisy_pair})["reportable"])

    def test_the_cv_is_median_within_group_sd_over_median_tau(self):
        v = R.tau_is_reportable({("a", "ascent"): [40.0, 50.0, 60.0],
                                 ("b", "ascent"): [90.0, 100.0, 110.0]})
        import statistics
        sd = statistics.pstdev([40.0, 50.0, 60.0])        # both groups: 8.165
        self.assertAlmostEqual(v["median_within_group_sd_s"], sd)
        self.assertAlmostEqual(v["median_tau_s"], 75.0)   # median of 50, 100
        self.assertAlmostEqual(v["cv"], sd / 75.0)

    def test_the_threshold_sits_at_MAX_TAU_CV(self):
        """Either side of 0.5: spread +-a around 100 gives CV a*sqrt(2/3)/100."""
        self.assertEqual(R.MAX_TAU_CV, 0.5, "the threshold must not be loosened")
        below = R.tau_is_reportable({("a", "ascent"): [40.0, 100.0, 160.0]})
        above = R.tau_is_reportable({("a", "ascent"): [38.0, 100.0, 162.0]})
        self.assertLess(below["cv"], R.MAX_TAU_CV)
        self.assertTrue(below["reportable"])
        self.assertGreater(above["cv"], R.MAX_TAU_CV)
        self.assertFalse(above["reportable"])


class TestAgainstTheCorpus(CorpusTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        one(cls.con, "SELECT 1 FROM segment_efforts LIMIT 1",
            what="segment efforts (run `match` and `recovery`)")

    def test_every_effort_has_a_row(self):
        efforts, rows = self.con.execute(
            "SELECT (SELECT count(*) FROM segment_efforts), "
            "(SELECT count(*) FROM segment_recovery)").fetchone()
        self.assertEqual(efforts, rows)
        self.assertGreater(rows, 0)

    def test_tau_is_never_stored_without_delta_hr(self):
        bad = self.con.execute(
            "SELECT count(*) FROM segment_recovery "
            "WHERE tau_s IS NOT NULL AND delta_hr_bpm IS NULL").fetchone()[0]
        self.assertEqual(bad, 0)

    def test_tau_is_never_stored_below_the_delta_hr_floor(self):
        bad = self.con.execute(
            "SELECT count(*) FROM segment_recovery WHERE tau_s IS NOT NULL "
            "AND delta_hr_bpm < ?", [R.MIN_DELTA_HR_BPM]).fetchone()[0]
        self.assertEqual(bad, 0)

    def test_a_declined_tau_always_records_why(self):
        bad = self.con.execute(
            "SELECT count(*) FROM segment_recovery "
            "WHERE tau_s IS NULL AND reason IS NULL").fetchone()[0]
        self.assertEqual(bad, 0)

    def test_the_stored_gate_agrees_with_the_rule_on_the_live_corpus(self):
        """WHAT THIS USED TO PIN, AND WHY IT NO LONGER DOES. It pinned the
        corpus COUNT behind the gate, so that a moving basis went back to the
        athlete. It did: the count grew as data arrived, and the athlete
        ruled the premise expired. The RULE is now pinned on fixtures in
        TestReportability, and the report carries the live basis beside the
        figure (gate_basis.n_groups_supporting), so the reader sees it move.

        What stays here cannot drift: the flag `recovery` stored must be the
        rule's verdict over the stored estimates, and the report's count of
        supporting groups must be the gate's own count.
        """
        from trail_factors.analysis.recovery import tau_is_reportable
        by = {}
        for sid, d, tau in self.con.execute(
                "SELECT segment_id, direction, tau_s FROM segment_recovery "
                "WHERE tau_s IS NOT NULL").fetchall():
            by.setdefault((sid, d), []).append(tau)
        v = tau_is_reportable(by)
        stored = {r[0] for r in self.con.execute(
            "SELECT DISTINCT tau_reportable FROM segment_recovery").fetchall()}
        self.assertEqual(stored, {v["reportable"]})
        # report.build counts supporting groups with its OWN SQL; the two
        # definitions of "a group that votes" must not diverge. Read from a
        # real report, not a copy of its query.
        if not v["reportable"]:
            return
        from trail_factors.analysis import report as REP
        aid = self.con.execute(
            "SELECT activity_id FROM segment_recovery WHERE tau_reportable "
            "AND tau_s IS NOT NULL LIMIT 1").fetchone()[0]
        with use_config():
            rep = REP.build(self.con, aid)
        [t] = [p for p in rep["primitives"]
               if p["name"] == "climb_recovery_tau"]
        self.assertEqual(t["gate_basis"]["n_groups_supporting"], v["n_groups"])

    def test_the_delta_hr_confound_is_present_and_directional(self):
        asc, desc = self.con.execute(
            "SELECT median(delta_hr_bpm) FILTER (WHERE direction = 'ascent'), "
            "median(delta_hr_bpm) FILTER (WHERE direction = 'descent') "
            "FROM segment_recovery").fetchone()
        self.assertGreater(asc, desc,
                           "climbing should cost more HR than descending")


if __name__ == "__main__":
    unittest.main()
