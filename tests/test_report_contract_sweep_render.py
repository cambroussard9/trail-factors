"""Every activity in the corpus builds and renders.

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


class TestTheVerifiedCrasherRenderSweep(CorpusTestCase):
    """The sweep that found render deaths nobody had reported."""

    def test_every_activity_in_the_corpus_builds_and_renders(self):
        """One crash had been reported; this sweep found many more, all in
        formatting code that runs outside a section's isolation."""
        acts = [
            r[0]
            for r in self.con.execute("SELECT activity_id FROM activities").fetchall()
        ]
        self.assertTrue(acts, "the corpus has no activities")
        bad = []
        for aid in acts:
            try:
                rep = R.build(self.con, aid)
            except Exception as exc:
                bad.append((aid, f"build {type(exc).__name__}: {exc}"))
                continue
            _, err = R.render_safe(rep)
            if err:
                bad.append((aid, f"render {err}"))
        self.assertEqual(bad, [], f"{len(bad)} of {len(acts)} failed")


if __name__ == "__main__":
    unittest.main()
