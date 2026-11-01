"""Estimated WBGT in report().conditions: named formula, stated limits,
display only.

The fixture values are worked by hand from the BoM simplified formula:
  Ta 30 C, RH 60 %:  es = 6.105 exp(17.27*30/267.7) = 42.2872 hPa
                     e  = 0.60 * 42.2872           = 25.3723 hPa
                     WBGT = 0.567*30 + 0.393*25.3723 + 3.94 = 30.9213 C
  (the BoM's published approximate-WBGT table gives ~31 for 30 C / 60 %)
  Ta 25 C, RH 80 %:  es = 31.5830, e = 25.2664, WBGT = 28.0447 C
"""

import tempfile
import unittest
from pathlib import Path

from tests.corpus import CorpusTestCase, one
from trail_factors.ingest import weather as W
from trail_factors.ingest.answers import NOT_COLLECTED
from trail_factors.paths import REPO_ROOT


class TestTheNamedFormula(unittest.TestCase):
    def test_matches_the_hand_computation(self):
        self.assertAlmostEqual(W.estimated_wbgt_c(30.0, 60.0), 30.9213, places=3)
        self.assertAlmostEqual(W.estimated_wbgt_c(25.0, 80.0), 28.0447, places=3)

    def test_a_missing_input_is_None_never_a_guess(self):
        self.assertIsNone(W.estimated_wbgt_c(None, 60.0))
        self.assertIsNone(W.estimated_wbgt_c(30.0, None))

    def test_the_approximation_is_named(self):
        self.assertIn("BoM", W.WBGT_APPROXIMATION)
        self.assertIn("0.567", W.WBGT_FORMULA)


class TestInTheReport(CorpusTestCase):
    """Read-only against the corpus: one outdoor activity and, where the
    corpus has one, one assumed-indoor activity."""

    def conditions(self, op, what):
        from trail_factors.analysis import report as REP

        (aid,) = one(
            self.con,
            "SELECT activity_id FROM weather_records "
            f"WHERE weather_source {op} 'assumed_indoor' "
            "GROUP BY 1 ORDER BY 1 LIMIT 1",
            what=what,
        )
        return REP.build(self.con, aid)["conditions"]

    def test_it_sits_beside_temperature_with_its_limits_stated(self):
        w = self.conditions("<>", "outdoor activity with weather")["estimated_wbgt_c"]
        self.assertEqual(w["status"], "ok")
        self.assertLessEqual(w["min"], w["max"])
        f = w["filters"]
        self.assertEqual(f["approximation"], W.WBGT_APPROXIMATION)
        self.assertIn("OPEN-SKY", f["canopy"])
        self.assertIn("UPPER BOUND", f["canopy"])
        from trail_factors.analysis.report import SUN_EXPOSURE_NOTE

        self.assertEqual(f["correction"], SUN_EXPOSURE_NOTE)
        self.assertIn("DISPLAY AND STORAGE ONLY", f["use"])

    def test_assumed_indoor_conditions_get_no_outdoor_wbgt(self):
        ind = self.conditions("=", "assumed-indoor activity")
        self.assertIsNone(ind["estimated_wbgt_c"]["min"])
        self.assertIn("ASSUMED", ind["estimated_wbgt_c"]["reason"])


class TestShadeIsNotCollected(unittest.TestCase):
    """Shade would correct the open-sky bound, and this build has no source
    for it -- so the note says the bound stands UNCORRECTED and why, rather
    than leaving the reader to wonder whether a correction was considered."""

    def test_the_state_is_labelled_not_collected(self):
        from trail_factors.analysis import report as REP

        s = REP._sun_exposure()
        self.assertEqual(s["status"], NOT_COLLECTED)
        self.assertIsNone(s["value"])
        self.assertEqual(s["note"], REP.SUN_EXPOSURE_NOTE)
        self.assertIn(NOT_COLLECTED, s["note"])
        self.assertIn("uncorrected", s["note"])


class TestTheCorrectionSaysShadeIsNotCollected(CorpusTestCase):
    """On a real run's weather: the WBGT bound is reported and its
    correction note is the not-collected one, never an answer state."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from trail_factors.analysis import report as REP

        cls.REP = REP
        (cls.aid,) = one(
            cls.con,
            "SELECT a.activity_id FROM activities a JOIN "
            "weather_records w USING (activity_id) WHERE w.weather_source "
            "<> 'assumed_indoor' ORDER BY 1 LIMIT 1",
            what="outdoor activity with weather",
        )
        cls.w = REP._estimated_wbgt(cls.con, cls.aid, "era5", {})

    def test_the_bound_stands_uncorrected_and_says_so(self):
        self.assertEqual(self.w["sun_exposure"]["status"], NOT_COLLECTED)
        self.assertIsNone(self.w["sun_exposure"]["value"])
        self.assertEqual(self.w["filters"]["correction"], self.REP.SUN_EXPOSURE_NOTE)

    def test_positive_twin_the_bound_itself_is_still_computed(self):
        """Not collecting shade withholds a CORRECTION, never the value."""
        self.assertEqual(self.w["status"], "ok")
        self.assertIsNotNone(self.w["max"])

    def test_no_answer_state_survives(self):
        for stale in ("answered", "skipped", "no_answer", "does not carry"):
            self.assertNotIn(stale, self.w["filters"]["correction"])


class TestHardRuleNothingNewEntersTheModel(unittest.TestCase):
    """WBGT and shortwave are display and storage only until their n
    threshold is met. Structural: neither name appears outside the weather
    module, its CLI, the schema and the report."""

    ALLOWED = {
        "src/trail_factors/ingest/weather.py",
        "src/trail_factors/cli/weather.py",
        "src/trail_factors/ingest/schema.py",
        "src/trail_factors/analysis/report.py",
    }

    @staticmethod
    def _code_tokens(path):
        """Names, attributes and string constants -- the things code can READ
        a channel through. NOT comments: a comment explaining that shade
        corrects the open-sky shortwave is documentation, and a guard that
        fires on comments is one that gets deleted."""
        import ast

        out = []
        for n in ast.walk(ast.parse(path.read_text())):
            if isinstance(n, ast.Name):
                out.append(n.id)
            elif isinstance(n, ast.Attribute):
                out.append(n.attr)
            elif isinstance(n, ast.Constant) and isinstance(n.value, str):
                out.append(n.value)
            elif isinstance(n, (ast.FunctionDef, ast.alias)):
                out.append(getattr(n, "name", ""))
        return " ".join(out).lower()

    def test_no_other_module_reads_them(self):
        hits = []
        for p in (REPO_ROOT / "src" / "trail_factors").rglob("*.py"):
            rel = p.relative_to(REPO_ROOT).as_posix()
            text = self._code_tokens(p)
            if ("wbgt" in text or "shortwave" in text) and rel not in self.ALLOWED:
                hits.append(rel)
        self.assertEqual(hits, [], "a new channel reached a model input")

    def test_positive_twin_the_guard_sees_code_not_comments(self):
        """It must still FIRE on code: the report module, which reads both,
        is detected; a comment-only mention is not. The comment-only case is
        a synthetic module, so the twin never depends on which real module
        happens to mention the word."""
        self.assertIn(
            "shortwave",
            self._code_tokens(REPO_ROOT / "src/trail_factors/analysis/report.py"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            comment_only = Path(tmp) / "comment_only.py"
            comment_only.write_text("# shade corrects the open-sky shortwave\nx = 1\n")
            reads_it = Path(tmp) / "reads_it.py"
            reads_it.write_text("x = row['shortwave_wm2']\n")
            self.assertNotIn("shortwave", self._code_tokens(comment_only))
            self.assertIn("shortwave", self._code_tokens(reads_it))


if __name__ == "__main__":
    unittest.main()
