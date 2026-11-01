"""Small text fixes: a message must state the count beside it and what it gated.

  * run_walk_cycles: the reason said "3 complete run->walk cycles; 4 are
    needed" beside n_cycles 4. It now states n_cycles AND the complete
    count, naming which is which.
  * gps_coverage: "NOT usable for terrain-based analysis" beside full terrain
    results. The warning gates nothing by itself; the message now names what
    the one coverage gate (select_runs, cross-run only) does to this run.
  * climb_recovery_tau: `n` counted every estimate while the value is the
    median of the REPORTABLE ones. An accepted value now carries its own n.

The cycle and tau checks read the corpus named by TRAIL_FACTORS_DB, read-only
(tests/corpus.py), with athlete state sandboxed and no athlete config; the
coverage check is a pure function of its inputs.
"""

import contextlib
import unittest

from tests.corpus import CorpusTestCase, use_config
from tests.isolation import state_sandbox
from tests.test_checks import recs
from trail_factors.analysis import cycles as CY
from trail_factors.analysis import report as REP
from trail_factors.analysis.select import MIN_COVERAGE_FOR_TERRAIN
from trail_factors.ingest.validate import WARNING, silver_checks


def coverage_message(frac):
    f = silver_checks(
        recs(), {"coverage_fraction": frac, "contiguous": True, "islands": 1}
    )
    [msg] = [
        x.message for x in f if x.check == "gps_coverage" and x.severity == WARNING
    ]
    return msg


class TestCoverageNamesWhatItGated(unittest.TestCase):
    def test_below_the_cross_run_gate_it_names_the_exclusion(self):
        msg = coverage_message(MIN_COVERAGE_FOR_TERRAIN - 0.15)
        self.assertIn("EXCLUDED from cross-run terrain comparisons", msg)
        self.assertIn("select_runs", msg)

    def test_positive_twin_above_the_gate_it_says_nothing_is_gated(self):
        msg = coverage_message(0.9)
        self.assertIn("NOTHING is gated", msg)
        self.assertNotIn("EXCLUDED", msg)

    def test_it_never_claims_the_runs_own_terrain_is_unusable(self):
        for frac in (0.2, 0.9):
            msg = coverage_message(frac)
            self.assertNotIn("NOT usable", msg)
            self.assertIn("OWN terrain results", msg)


class TestCountsAgreeWithTheirText(CorpusTestCase):
    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(state_sandbox())
        stack.enter_context(use_config())

    def test_a_gated_cycles_reason_states_n_cycles(self):
        aids = [
            r[0]
            for r in self.con.execute("""
            SELECT DISTINCT activity_id FROM splits
            WHERE split_type = 'rwd_run'""").fetchall()
        ]
        gated = 0
        for aid in aids:
            r = CY.cycles(self.con, aid)
            if r["status_hint"] != "gated":
                continue
            gated += 1
            self.assertTrue(
                r["reason"].startswith(
                    f"{r['n_cycles']} run blocks (n_cycles), "
                    f"{r['n_complete_cycles']} of them complete"
                ),
                r["reason"],
            )
        if gated == 0:
            self.skipTest("this corpus has no run whose cycles are gated")

    def test_an_accepted_tau_carries_the_n_it_is_a_median_of(self):
        row = self.con.execute("""
            SELECT activity_id, count(*) FROM segment_recovery
            WHERE tau_reportable AND tau_s IS NOT NULL
            GROUP BY 1 ORDER BY 2 DESC LIMIT 1""").fetchone()
        if row is None:
            self.skipTest("no accepted tau in the corpus")
        aid, n_reportable = row
        [t] = [
            p
            for p in REP.build(self.con, aid)["primitives"]
            if p["name"] == "climb_recovery_tau"
        ]
        self.assertEqual(t["status"], REP.OK)
        self.assertEqual(t["n"], n_reportable)
        self.assertEqual(t["n_qualifying"], n_reportable)
        self.assertIn("qualifying", t["n_basis"])


if __name__ == "__main__":
    unittest.main()
