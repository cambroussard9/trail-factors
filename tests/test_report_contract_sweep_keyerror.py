"""The reported KeyError `duration_min` did not reproduce.

SPLIT OUT OF tests/test_report_contract.py so no test module runs for too
long: each corpus sweep rebuilds every report, and three of them in one
module were the slowest module in the suite. The test body is unchanged
apart from reading the corpus through tests.corpus (skipped when
TRAIL_FACTORS_DB is unset).
"""

from __future__ import annotations

import unittest

from trail_factors.analysis import report as R
from tests.corpus import CorpusTestCase


class TestTheKeyErrorDidNotReproduce(CorpusTestCase):
    """Item 6, recorded as a finding rather than fixed blind."""

    def test_no_activity_raises_KeyError_duration_min(self):
        con = self.con
        acts = [r[0] for r in con.execute(
            "SELECT activity_id FROM activities").fetchall()]
        splitless = {r[0] for r in con.execute("""
            SELECT a.activity_id FROM activities a WHERE NOT EXISTS (
                SELECT 1 FROM splits s WHERE s.activity_id = a.activity_id
                  AND s.split_type LIKE 'rwd\\_%' ESCAPE '\\')""").fetchall()}
        if not splitless:
            self.skipTest("this corpus has no activity without rwd_* splits, "
                          "which is the shape the KeyError was reported on")
        for aid in acts:
            try:
                rep = R.build(con, aid)
            except KeyError as exc:      # the reported shape
                self.fail(f"{aid}: KeyError {exc}")
            _, err = R.render_safe(rep)
            self.assertNotIn("KeyError", err if err is not None else "")


if __name__ == "__main__":
    unittest.main()
