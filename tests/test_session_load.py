"""Foster session load from Garmin's own rating: garmin_rpe x timer minutes.

WHAT THESE PIN. The load is a session DOSE priced by the one RPE the watch
recorded, so the properties that matter are the ones that would let a missing
rating become a number: an unrated activity priced at 0, a missing timer
priced at anything, or the unverified feel scale leaking into the price.

The synthetic tests build an in-memory `activities` table with only the
columns the function reads; every figure in them is made up. The corpus test
reads TRAIL_FACTORS_DB (skipped when unset) and checks every activity against
the formula recomputed from its own stored columns -- no value is pinned.

Every negative has a positive twin.
"""

from __future__ import annotations

import unittest

import duckdb

from trail_factors.analysis import session_load as SL
from tests.corpus import CorpusTestCase


def table(*rows):
    """(activity_id, timer_time_s, garmin_rpe, garmin_feel) rows."""
    con = duckdb.connect(":memory:")
    con.execute("""CREATE TABLE activities (activity_id VARCHAR,
                   run_id VARCHAR, timer_time_s DOUBLE, garmin_rpe DOUBLE,
                   garmin_feel INTEGER)""")
    for aid, timer, rpe, feel in rows:
        con.execute("INSERT INTO activities VALUES (?, ?, ?, ?, ?)",
                    [aid, f"run-{aid}", timer, rpe, feel])
    return con


class TestTheFormula(unittest.TestCase):

    def setUp(self):
        self.con = table(("rated", 3000.0, 4.0, 3),
                         ("unrated", 3000.0, None, None),
                         ("feel_only", 3000.0, None, 4),
                         ("no_timer", None, 6.0, 2))

    def tearDown(self):
        self.con.close()

    def test_a_rated_activity_is_rpe_times_timer_minutes(self):
        r = SL.session_load(self.con, "rated")
        self.assertEqual(r["status"], "ok")
        self.assertIsNone(r["reason"])
        self.assertAlmostEqual(r["duration_min"], 50.0)
        self.assertAlmostEqual(r["foster_load_au"], 4.0 * 50.0)
        self.assertEqual(r["run_id"], "run-rated")
        self.assertEqual(r["garmin_rpe"], 4.0)
        self.assertEqual(r["garmin_feel"], 3)      # shown

    def test_an_unrated_activity_is_unavailable_never_zero(self):
        r = SL.session_load(self.con, "unrated")
        self.assertEqual(r["status"], "unavailable")
        self.assertEqual(r["reason"], SL.UNRATED_REASON)
        self.assertIsNone(r["foster_load_au"])
        self.assertIsNotNone(r["duration_min"])    # the timer is still shown

    def test_feel_alone_does_not_price_a_session(self):
        """A feel with no RPE is still unrated: feel never stands in."""
        r = SL.session_load(self.con, "feel_only")
        self.assertEqual(r["status"], "unavailable")
        self.assertEqual(r["reason"], SL.UNRATED_REASON)
        self.assertIsNone(r["foster_load_au"])
        self.assertEqual(r["garmin_feel"], 4)

    def test_no_timer_is_unavailable_with_its_own_reason(self):
        r = SL.session_load(self.con, "no_timer")
        self.assertEqual(r["status"], "unavailable")
        self.assertIsNone(r["foster_load_au"])
        self.assertIsNone(r["duration_min"])
        self.assertIn("timer", r["reason"])
        self.assertNotEqual(r["reason"], SL.UNRATED_REASON)

    def test_an_unknown_activity_raises_rather_than_reporting_unrated(self):
        with self.assertRaises(LookupError):
            SL.session_load(self.con, "nope")


class TestFeelNeverChangesTheLoad(unittest.TestCase):
    """The feel scale's direction is unverified, so every feel value -- and
    none -- gives the same load for the same RPE and timer."""

    def test_every_feel_gives_the_same_load(self):
        rows = [(f"f{v}", 2700.0, 7.0, v) for v in (None, 1, 2, 3, 4, 5)]
        con = table(*rows)
        try:
            loads = {SL.session_load(con, aid)["foster_load_au"]
                     for aid, *_ in rows}
        finally:
            con.close()
        self.assertEqual(loads, {7.0 * 45.0})

    def test_positive_twin_the_rpe_does_change_it(self):
        con = table(("a", 2700.0, 7.0, 3), ("b", 2700.0, 8.0, 3))
        try:
            a, b = (SL.session_load(con, x)["foster_load_au"] for x in "ab")
        finally:
            con.close()
        self.assertNotEqual(a, b)

    def test_the_filters_say_feel_is_not_used(self):
        f = SL.filters()
        self.assertFalse(f["feel_used"])
        self.assertIn("never enters", f["feel_note"])
        self.assertIn("timer_time_s", f["duration_basis"])
        self.assertIn("garmin_rpe", f["rpe_source"])


class TestTheSelectionSummary(unittest.TestCase):
    """Over a selection the sum leaves unpriced sessions out. Two kinds of
    unpriced, counted apart; "lower bound" only when something WAS left out."""

    def summary(self, *rows):
        from trail_factors.mcp_server import load_summary
        con = table(*rows)
        try:
            return load_summary([SL.session_load(con, r[0]) for r in rows])
        finally:
            con.close()

    def test_a_rated_session_with_no_timer_is_not_counted_unrated(self):
        s = self.summary(("rated", 3000.0, 4.0, 3),
                         ("unrated", 3000.0, None, None),
                         ("no_timer", None, 6.0, 2))
        self.assertEqual((s["n"], s["n_priced"], s["n_rated"]), (3, 1, 2))
        self.assertEqual(s["n_unrated"], 1)
        self.assertEqual(s["n_rated_unpriced"], 1)
        [u] = s["rated_unpriced"]
        self.assertEqual(u["activity_id"], "no_timer")
        self.assertIn("timer", u["reason"])
        self.assertNotEqual(u["reason"], SL.UNRATED_REASON)
        self.assertAlmostEqual(s["sum_foster_load_au_priced"], 4.0 * 50.0)
        self.assertIs(s["is_lower_bound"], True)
        self.assertIn("LOWER BOUND", s["note"])

    def test_positive_twin_every_session_priced_is_not_a_lower_bound(self):
        s = self.summary(("a", 3000.0, 4.0, 3), ("b", 1800.0, 6.0, None))
        self.assertEqual((s["n_unrated"], s["n_rated_unpriced"]), (0, 0))
        self.assertAlmostEqual(s["sum_foster_load_au_priced"],
                               4.0 * 50.0 + 6.0 * 30.0)
        self.assertIs(s["is_lower_bound"], False)
        self.assertNotIn("LOWER BOUND", s["note"])

    def test_an_empty_selection_is_null_not_zero(self):
        from trail_factors.mcp_server import load_summary
        s = load_summary([])
        self.assertIsNone(s["sum_foster_load_au_priced"])
        self.assertIs(s["is_lower_bound"], False)
        self.assertIn("not a load of zero", s["note"])


class TestEveryActivityInTheCorpus(CorpusTestCase):
    """Every activity, rated or not, against its own stored columns."""

    def test_rated_loads_are_the_formula_and_unrated_ones_are_unavailable(self):
        rows = self.con.execute("""
            SELECT activity_id, garmin_rpe, timer_time_s
            FROM activities ORDER BY activity_id""").fetchall()
        if not rows:
            self.skipTest("this corpus has no activities")
        n_ok = 0
        for aid, rpe, timer_s in rows:
            r = SL.session_load(self.con, aid)
            if rpe is not None and timer_s is not None:
                self.assertEqual(r["status"], "ok", aid)
                self.assertAlmostEqual(r["foster_load_au"],
                                       rpe * timer_s / 60.0, places=9)
                n_ok += 1
            else:
                self.assertEqual(r["status"], "unavailable", aid)
                self.assertIsNone(r["foster_load_au"], aid)
                self.assertTrue(r["reason"], aid)
                if rpe is None:
                    self.assertEqual(r["reason"], SL.UNRATED_REASON, aid)
        if n_ok == 0:
            self.skipTest("no activity in this corpus carries a Garmin RPE, "
                          "so only the unavailable path ran")


if __name__ == "__main__":
    unittest.main()
