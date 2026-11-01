"""§16.2's ok-requires-a-value contract, over the whole corpus.

SPLIT OUT OF tests/test_report_contract.py so no test module runs for too
long: each corpus sweep rebuilds every report, and three of them in one
module were the slowest module in the suite. The test body is unchanged
apart from reading the corpus through tests.corpus (skipped when
TRAIL_FACTORS_DB is unset).
"""

from __future__ import annotations

import unittest

from tests.corpus import CorpusTestCase
from trail_factors.analysis import report as R


class TestTheVerifiedCrasherOkNullSweep(CorpusTestCase):
    """Item 1's contract, checked on every activity."""

    def test_no_primitive_reports_ok_with_a_null_value(self):
        """THE CONTRACT ITSELF, over the whole corpus rather than one run."""
        acts = [
            r[0]
            for r in self.con.execute("SELECT activity_id FROM activities").fetchall()
        ]
        self.assertTrue(acts, "the corpus has no activities")
        offenders = []
        for aid in acts:
            for p in R.build(self.con, aid)["primitives"]:
                if p["status"] == R.OK and p.get("value") is None:
                    offenders.append((aid, p["name"]))
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
