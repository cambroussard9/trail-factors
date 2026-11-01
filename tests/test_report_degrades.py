"""A crash inside ONE report section must not take the report down (§16.2).

THE BUG. `durations()` did `float(sum(total_distance_m))` and the FR265 writes
a zero-duration `rwd_stand` split with NO distance field, so the sum was NULL.
`float(None)` raised TypeError, the MCP boundary caught it, and `report` came
back outcome "broken" with payload null -- for a run whose other eighteen
sections were all computable. That is a fifth status: §16.2 has four, and
"broken" for the whole report says the corpus could not answer, when what
happened is that one number could not be rendered.

THE NONE IS NOT THE BUG. A standing split of zero duration has no distance to
record; NULL is the honest value and an ingest gate would reject a legitimate
file. The crash is the bug.

Every negative has a positive twin.

Corpus tests read the database named by TRAIL_FACTORS_DB (skipped when unset).
The subject is found by its PROPERTY -- a watch activity with a rwd_stand
split that carries no distance, the exact shape that crashed -- so the test
holds on any corpus that has one and skips, naming it, on one that does not.
"""

from __future__ import annotations

import unittest
from unittest import mock

from trail_factors.analysis import basic as B
from trail_factors.analysis import report as R
from tests.corpus import CorpusTestCase, one

# The shape that crashed: a rwd_stand split with no distance, on an activity
# whose rwd_walk class is only PARTLY measured (some walk splits have a
# distance and some do not), so both the None and the lower-bound cases
# are present on one run.
CRASHER = """
    SELECT a.activity_id FROM activities a
    WHERE a.source = 'garmin_watch'
      AND EXISTS (SELECT 1 FROM splits s WHERE s.activity_id = a.activity_id
                    AND s.split_type = 'rwd_stand')
      AND NOT EXISTS (SELECT 1 FROM splits s
                      WHERE s.activity_id = a.activity_id
                        AND s.split_type = 'rwd_stand'
                        AND s.total_distance_m IS NOT NULL)
      AND EXISTS (SELECT 1 FROM splits s WHERE s.activity_id = a.activity_id
                    AND s.split_type = 'rwd_walk'
                    AND s.total_distance_m IS NULL)
      AND EXISTS (SELECT 1 FROM splits s WHERE s.activity_id = a.activity_id
                    AND s.split_type = 'rwd_walk'
                    AND s.total_distance_m IS NOT NULL)
    ORDER BY a.start_time_utc LIMIT 1"""


class TestTheReportSurvivesTheRunThatBrokeIt(CorpusTestCase):

    @property
    def aid(self):
        return one(self.con, CRASHER,
                   what="watch activity whose rwd_stand splits carry no "
                        "distance and whose rwd_walk class is partly "
                        "measured")[0]

    def test_1_the_report_returns_a_payload(self):
        rep = R.build(self.con, self.aid)
        self.assertIsInstance(rep, dict)
        self.assertGreater(len(rep), 10)
        self.assertIn("basic", rep)
        self.assertIn("measured", rep)

    def test_the_null_distance_is_None_and_NOT_zero(self):
        """The whole point of the fix: an unmeasured distance must not enter
        the record as a measured zero. That is the `value or default` bug."""
        d = B.durations(self.con, self.aid)
        self.assertIsNone(d["stand_distance_m"])
        self.assertIn("stand_distance_m", d["unavailable_fields"])
        self.assertIn("NOT a zero", d["unavailable_fields"]["stand_distance_m"])
        # ...and the class is PRESENT, which is what makes it not a zero.
        self.assertGreater(d["n_stand_splits"], 0)

    def test_an_ABSENT_class_is_still_a_real_zero(self):
        """THE TWIN. Absent and present-but-unmeasured are different facts and
        the fix must not collapse them the other way."""
        rows = self.con.execute("""
            SELECT a.activity_id FROM activities a
            WHERE a.source = 'garmin_watch' AND NOT EXISTS (
                SELECT 1 FROM splits s WHERE s.activity_id = a.activity_id
                  AND s.split_type = 'rwd_walk')
              AND EXISTS (SELECT 1 FROM splits s2
                          WHERE s2.activity_id = a.activity_id
                            AND s2.split_type LIKE 'rwd_%')""").fetchall()
        if not rows:
            self.skipTest("every activity has a rwd_walk split")
        d = B.durations(self.con, rows[0][0])
        self.assertEqual(d["walk_s"], 0.0)
        self.assertEqual(d["walk_distance_m"], 0.0)
        self.assertEqual(d["n_walk_splits"], 0)
        self.assertNotIn("walk_s", d["unavailable_fields"])

    def test_a_partial_sum_says_it_is_a_lower_bound(self):
        """sum() SKIPS nulls, so a class where only some splits carry a
        distance reports the subset. Understated with ONE sign."""
        d = B.durations(self.con, self.aid)
        self.assertIn("walk_distance_m", d["partial_sums"])
        self.assertIn("LOWER BOUND", d["partial_sums"]["walk_distance_m"])


class TestReintroducingTheNoneStillReturns(CorpusTestCase):
    """Test 2 and 3 from the brief: the crash is the bug, the None is a
    legitimate state, and a section that cannot compute degrades alone."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.aid = one(cls.con, "SELECT activity_id FROM activities "
                               "WHERE sport = 'running' AND source = "
                               "'garmin_watch' ORDER BY start_time_utc LIMIT 1",
                      what="watch running activity")[0]

    def test_2_a_section_that_raises_degrades_and_the_report_still_returns(self):
        boom = mock.Mock(side_effect=TypeError(
            "float() argument must be a string or a real number, not 'NoneType'"))
        with mock.patch.object(B, "basic_metrics", boom):
            rep = R.build(self.con, self.aid)
        self.assertIsInstance(rep, dict)
        self.assertIn("measured", rep)                  # the rest survived
        self.assertIn("primitives", rep)
        self.assertEqual(rep["basic"]["status"], R.UNAVAILABLE)
        self.assertIn("TypeError", rep["basic"]["error"])
        # ...and it does NOT pretend the input was missing.
        self.assertIn("DEFECT", rep["basic"]["defect"])

    def test_the_degraded_status_is_one_of_the_FOUR(self):
        """No fifth status. `broken` for a whole report because one section
        failed is exactly the collapse §16.2 forbids."""
        with mock.patch.object(B, "basic_metrics",
                               mock.Mock(side_effect=ValueError("x"))):
            rep = R.build(self.con, self.aid)
        self.assertIn(rep["basic"]["status"],
                      (R.OK, R.GATED, R.REFUSED, R.UNAVAILABLE))

    def test_3_a_missing_field_entirely_gives_the_SAME_degraded_section(self):
        """Not a different crash: KeyError is the shape a missing field takes,
        and it must land in the same place as the TypeError."""
        with mock.patch.object(B, "basic_metrics",
                               mock.Mock(side_effect=KeyError("stand_distance_m"))):
            rep = R.build(self.con, self.aid)
        self.assertEqual(rep["basic"]["status"], R.UNAVAILABLE)
        self.assertIn("KeyError", rep["basic"]["error"])
        self.assertEqual(rep["basic"]["section"], "basic")

    def test_a_HEALTHY_section_is_not_wrapped_in_a_status(self):
        """THE TWIN: an isolator that degraded everything would pass above."""
        rep = R.build(self.con, self.aid)
        self.assertNotIn("defect", rep["basic"])


class TestTheEnvelopeNeverEmitsABareNullFingerprint(unittest.TestCase):

    def test_a_broken_envelope_says_why_there_is_no_fingerprint(self):
        import json

        import trail_factors.mcp_server as M
        env = json.loads(M._envelope("t", M.BROKEN, text="boom"))
        fp = env["corpus_fingerprint"]
        self.assertIsNotNone(fp)
        self.assertEqual(fp["status"], "not_taken")
        self.assertIn("not found to be empty", fp["reason"])

    def test_a_real_fingerprint_still_passes_through(self):
        """THE TWIN."""
        import json

        import trail_factors.mcp_server as M
        env = json.loads(M._envelope("t", M.RESULT, fingerprint={"n_runs": 3}))
        self.assertEqual(env["corpus_fingerprint"], {"n_runs": 3})


if __name__ == "__main__":
    unittest.main()
