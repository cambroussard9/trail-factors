"""The ceiling gate is MEASURED ON THE SELECTION: the declared-vs-demonstrated
comparison is reported only when a run IN THE SELECTION ITSELF reached within
CEILING_BAND_BPM of the demonstrated 30 s ceiling. No hand-set list of run
types, and no grouping by what a run was called.

The rule is tested on a pure fixture (selection_reach takes rows, not a
database). The corpus tests find their subjects by PROPERTY -- a run outside
the band, the run that sets the ceiling -- and assert invariants, never one
athlete's values. Read-only, with an empty athlete-state directory.
"""

import unittest
from unittest import mock

from trail_factors.analysis import over as O
from tests.corpus import CorpusTestCase, corpus_path, isolated


def rows(*vals):
    return [{"max_hr_30s": v, "run_id": f"r-{i}"} for i, v in enumerate(vals)]


class TestTheRule(unittest.TestCase):
    CEILING = 170.0           # made up; band floor 165.0

    def test_a_selection_with_no_run_near_the_ceiling_does_not_reach(self):
        r = O.selection_reach(rows(150.0, 155.0), self.CEILING)
        self.assertFalse(r["reaches"])
        self.assertEqual(r["n_runs"], 2)
        self.assertEqual(r["n_reaching"], 0)
        self.assertEqual(r["best_30s_bpm"], 155.0)
        self.assertEqual(r["best_run_id"], "r-1")
        self.assertAlmostEqual(r["below_ceiling_bpm"], 15.0)
        self.assertAlmostEqual(r["band_floor_bpm"], 165.0)

    def test_one_reaching_run_lets_it_in(self):
        """Positive twin: the same rows plus one run inside the band."""
        r = O.selection_reach(rows(150.0, 155.0, 166.0), self.CEILING)
        self.assertTrue(r["reaches"])
        self.assertEqual(r["n_reaching"], 1)
        self.assertEqual(r["best_run_id"], "r-2")

    def test_the_band_edge_is_inclusive(self):
        self.assertTrue(O.selection_reach(rows(165.0), self.CEILING)["reaches"])
        self.assertFalse(
            O.selection_reach(rows(164.9), self.CEILING)["reaches"])

    def test_a_missing_figure_counts_as_a_run_but_never_reaches(self):
        r = O.selection_reach(rows(None, None), self.CEILING)
        self.assertFalse(r["reaches"])
        self.assertEqual(r["n_runs"], 2)
        self.assertIsNone(r["best_30s_bpm"])
        self.assertIsNone(r["below_ceiling_bpm"])

    def test_an_empty_selection_never_reaches(self):
        r = O.selection_reach([], self.CEILING)
        self.assertFalse(r["reaches"])
        self.assertEqual(r["n_runs"], 0)

    def test_the_band_is_the_named_constant(self):
        self.assertEqual(O.CEILING_BAND_BPM, 5.0)
        r = O.selection_reach(rows(160.0), self.CEILING, band_bpm=10.0)
        self.assertTrue(r["reaches"])

    def test_no_run_type_machinery_remains(self):
        for gone in ("CEILING_CAPABLE_RUN_TYPES", "capable_groups",
                     "run_type_group"):
            self.assertFalse(hasattr(O, gone), gone)


class TestTheGateIsInTheDigest(unittest.TestCase):
    """Moving the band flips selections between ok and refused, so it must
    move calc_version; so must a change to what the gate compares."""

    def test_the_band_and_the_rule_are_registered_live(self):
        from trail_factors.analysis.versions import calc_params, digest
        p = calc_params()
        self.assertEqual(p["max_hr_ceiling_band_bpm"], O.CEILING_BAND_BPM)
        self.assertEqual(p["max_hr_ceiling_gate"], O.CEILING_GATE_RULE)
        with mock.patch.object(O, "CEILING_BAND_BPM", 8.0):
            moved = calc_params()
        self.assertEqual(moved["max_hr_ceiling_band_bpm"], 8.0)
        self.assertNotEqual(digest(moved), digest(p))
        with mock.patch.object(O, "CEILING_GATE_RULE", "something_else"):
            self.assertNotEqual(digest(calc_params()), digest(p))


class TestTheGateOnASyntheticSelection(unittest.TestCase):
    """The whole verdict, end to end, on an in-memory activities table: the
    declared value, the sport and the session summary are all the gate reads
    from the database. Every figure is made up."""

    CEILING = 170.0
    REC_CEILING = 173.0
    SESSION_CEILING = 175.0
    DECLARED = 180.0

    def setUp(self):
        import duckdb
        self.con = duckdb.connect()
        self.con.execute("""CREATE TABLE activities (activity_id VARCHAR,
            sport VARCHAR, start_time_utc TIMESTAMP, declared_max_hr DOUBLE,
            session_max_hr DOUBLE)""")
        for aid in ("near", "far1", "far2"):
            self.con.execute("INSERT INTO activities VALUES "
                             "(?, 'running', '2025-01-01 07:00', ?, NULL)",
                             [aid, self.DECLARED])
        # The demonstrated ceiling sits on a run OUTSIDE every selection
        # below, and above anything a selection shows: 173 in the record
        # stream, 175 in the watch's session summary.
        self.ev = {"status": "ok", "ceiling_30s": self.CEILING,
                   "ceiling_run_id": "elsewhere",
                   "max_recorded_bpm": self.REC_CEILING,
                   "max_recorded_run_id": "elsewhere",
                   "session_max_bpm": self.SESSION_CEILING,
                   "session_max_activity_id": "act-elsewhere",
                   "band_bpm": O.CEILING_BAND_BPM,
                   "band_floor_bpm": self.CEILING - O.CEILING_BAND_BPM,
                   "as_of": "2025-01-31"}

    def tearDown(self):
        self.con.close()

    def gate(self, per_run):
        top30 = max((r for r in per_run if r["max_hr_30s"] is not None),
                    key=lambda r: r["max_hr_30s"], default=None)
        top30 = top30 and {**top30, "value": top30["max_hr_30s"]}
        rec = max(per_run, key=lambda r: r["max_hr_recorded"])
        rec = {**rec, "value": rec["max_hr_recorded"]}
        return O._declared_ceiling(self.con, top30, rec, per_run=per_run,
                                   evidence=self.ev)

    @staticmethod
    def row(aid, w30, rec):
        return {"activity_id": aid, "run_id": f"run-{aid}",
                "max_hr_30s": w30, "max_hr_recorded": rec}

    def test_a_selection_inside_the_band_is_compared(self):
        d = self.gate([self.row("near", 166.0, 168.0),
                       self.row("far1", 150.0, 152.0)])
        self.assertEqual(d["status"], "ok", d.get("reason"))
        self.assertEqual(d["gap_bpm"], self.DECLARED - self.REC_CEILING)
        self.assertTrue(d["selection_reach"]["reaches"])
        self.assertEqual(d["selection_reach"]["best_run_id"], "run-near")

    def test_the_gap_is_against_the_ceiling_not_the_selection_peak(self):
        """The selection reaches the band (166 >= 165) but peaks at 168;
        the heart had already done 173 / 175 elsewhere. An anchor of 174 is
        above the selection and NOT above the heart."""
        self.con.execute("UPDATE activities SET declared_max_hr = 174.0")
        d = self.gate([self.row("near", 166.0, 168.0)])
        self.assertEqual(d["status"], "ok", d.get("reason"))
        self.assertEqual(d["demonstrated_single_sample"], self.REC_CEILING)
        self.assertEqual(d["demonstrated_30s"], self.CEILING)
        self.assertEqual(d["demonstrated_session_summary"],
                         self.SESSION_CEILING)
        self.assertEqual(d["demonstrated_session_summary_activity"],
                         "act-elsewhere")
        self.assertEqual(d["gap_bpm"], 174.0 - self.REC_CEILING)
        self.assertEqual(d["gap_vs_session_summary_bpm"],
                         174.0 - self.SESSION_CEILING)
        self.assertNotIn("upward", d["note"])
        self.assertIn("meets or exceeds the declared anchor", d["note"])
        self.assertIn("2025-01-31", d["note"])
        # The selection's own peak is reported, beside the ceiling, as such.
        sp = d["selection_peak"]
        self.assertEqual((sp["single_sample"], sp["sustained_30s"]),
                         (168.0, 166.0))
        self.assertIn("not the demonstrated ceiling", sp["label"])
        self.assertIn("selection itself peaks at 168", d["note"])
        self.assertIn("not this selection", d["demonstrated_basis"])

    def test_positive_twin_an_anchor_above_the_ceiling_shifts_upward(self):
        d = self.gate([self.row("near", 166.0, 168.0)])
        self.assertEqual(d["status"], "ok", d.get("reason"))
        self.assertIn(f"a gap of {self.DECLARED - self.SESSION_CEILING:.0f} "
                      f"bpm shifts every one of them upward", d["note"])
        self.assertIn(f"is {self.SESSION_CEILING:.0f}", d["note"])

    def test_a_selection_outside_the_band_is_refused_naming_best_and_band(self):
        """The twin: the same declared value, no run within 5 bpm."""
        d = self.gate([self.row("far1", 150.0, 152.0),
                       self.row("far2", 158.5, 161.0)])
        self.assertEqual(d["status"], "refused")
        self.assertIsNone(d["gap_bpm"])
        self.assertIn("158.5 bpm", d["reason"])          # the selection's best
        self.assertIn("11.5 below", d["reason"])         # how far below
        self.assertIn("170.0", d["reason"])              # the ceiling
        self.assertIn("5 bpm", d["reason"])              # the band
        self.assertIn("165.0", d["reason"])              # the floor
        self.assertNotIn("run_type", d["reason"])
        # The selection's own figure is labelled as its own; the
        # demonstrated figure is the ceiling's.
        self.assertEqual(d["selection_peak"]["single_sample"], 161.0)
        self.assertEqual(d["demonstrated_single_sample"], self.REC_CEILING)

    def test_no_ceiling_to_reach_is_refused_and_says_so(self):
        self.ev = {"status": "unavailable", "reason": "no 30 s HR"}
        d = self.gate([self.row("near", 166.0, 168.0)])
        self.assertEqual(d["status"], "refused")
        self.assertIn("no demonstrated 30 s ceiling", d["reason"])


# --- isolation: never the real athlete state or config ----------------------
def setUpModule():
    if corpus_path() is not None:
        unittest.enterModuleContext(isolated())


def _run_dates(con):
    """Distinct UTC dates of watch runs, in order -- the convention
    select_runs applies to start_date/end_date."""
    return [str(d)[:10] for d, in con.execute("""
        SELECT DISTINCT CAST(start_time_utc AS DATE) FROM activities
        WHERE source = 'garmin_watch' AND sport = 'running'
        ORDER BY 1""").fetchall()]


class TestThePayloadShowsTheEvidence(CorpusTestCase):
    """The comparison follows the SELECTION's reach, and a refusal carries
    the measurement it was made on."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.ev = O.ceiling_evidence(cls.con)
        if cls.ev["status"] != "ok":
            raise unittest.SkipTest("this corpus has no run with a 30 s HR")
        if O.max_hr_over(cls.con)["declared_ceiling"]["status"] == "unavailable":
            raise unittest.SkipTest("this corpus has no declared max HR to "
                                    "compare a ceiling against")

    def test_the_evidence_has_no_run_type_groups(self):
        self.assertNotIn("by_group", self.ev)
        for k in ("ceiling_30s", "ceiling_run_id", "band_bpm",
                  "band_floor_bpm", "as_of", "rule", "circularity"):
            self.assertIn(k, self.ev)
        self.assertAlmostEqual(self.ev["band_floor_bpm"],
                               self.ev["ceiling_30s"] - O.CEILING_BAND_BPM)
        self.assertIn("selection itself", self.ev["rule"])

    def test_last_n_1_outside_the_band_is_refused_naming_its_best(self):
        """The newest run, if it sits outside the band, is a selection that
        never came near a ceiling: refused, and the reason names the
        selection's best 30 s figure and the band."""
        r = O.max_hr_over(self.con, last_n=1)
        d = r["declared_ceiling"]
        if d["status"] == "unavailable":
            self.skipTest(f"no declared max to compare: {d['reason'][:60]}")
        reach = d["selection_reach"]
        if reach["reaches"]:
            self.skipTest("this corpus's newest run is inside the ceiling band")
        self.assertEqual(d["status"], "refused")
        self.assertIsNone(d["gap_bpm"])
        self.assertEqual(reach["n_runs"], 1)
        self.assertEqual(reach["n_reaching"], 0)
        best = reach["best_30s_bpm"]
        self.assertEqual(best, r["per_run"][0]["max_hr_30s"])
        if best is not None:
            self.assertIn(f"{best:.1f} bpm", d["reason"])
        self.assertIn(f"{O.CEILING_BAND_BPM:.0f} bpm", d["reason"])
        self.assertIn(f"{reach['band_floor_bpm']:.1f}", d["reason"])
        # The demonstrated figures survive; only the comparison is refused.
        self.assertIsNotNone(d["demonstrated_single_sample"])
        self.assertEqual(d["selection_peak"]["single_sample"],
                         r["per_run"][0]["max_hr_recorded"])

    def test_positive_twin_selecting_the_ceiling_run_is_ok(self):
        """The run that sets the ceiling is always in its own band (the
        gate's stated circularity), so a selection of its date qualifies --
        a gate that refused everything would pass the test above."""
        day = next(r["date"] for r in O.max_hr_over(self.con)["per_run"]
                   if r["run_id"] == self.ev["ceiling_run_id"])
        r = O.max_hr_over(self.con, start_date=day, end_date=day)
        d = r["declared_ceiling"]
        self.assertEqual(d["status"], "ok", d.get("reason"))
        self.assertIsNotNone(d["gap_bpm"])
        self.assertTrue(d["selection_reach"]["reaches"])
        self.assertIn("satisfied", d["selection_gate"])
        self.assertIn(self.ev["ceiling_run_id"],
                      {x["run_id"] for x in r["per_run"]})

    def test_a_refusal_matches_selection_reach_on_the_same_rows(self):
        """The verdict in the payload is the pure rule applied to the
        selection's own rows and the bounded ceiling -- nothing else."""
        for kw in ({"last_n": 1}, {"last_n": 3}, {}):
            r = O.max_hr_over(self.con, **kw)
            d = r["declared_ceiling"]
            if d["status"] == "unavailable":
                continue
            expect = O.selection_reach(r["per_run"], d["ceiling_evidence"]
                                       ["ceiling_30s"])
            self.assertEqual(d["selection_reach"], expect, kw)
            self.assertEqual(d["status"] == "ok", expect["reaches"], kw)

    def test_an_ok_gap_is_never_against_less_than_the_bounded_ceiling(self):
        """Whatever the selection, an ok comparison's demonstrated figures
        are at least the bounded corpus evidence the payload carries."""
        days = _run_dates(self.con)
        for kw in ({}, {"start_date": days[-1], "end_date": days[-1]},
                   {"end_date": days[len(days) // 2]}):
            d = O.max_hr_over(self.con, **kw)["declared_ceiling"]
            if d["status"] != "ok":
                continue
            ev = d["ceiling_evidence"]
            self.assertGreaterEqual(d["demonstrated_single_sample"],
                                    ev["max_recorded_bpm"], kw)
            self.assertGreaterEqual(d["demonstrated_single_sample"],
                                    d["selection_peak"]["single_sample"], kw)
            self.assertEqual(d["gap_bpm"], d["declared"][-1]
                             - d["demonstrated_single_sample"], kw)

    def test_evidence_is_bounded_to_the_selection_end(self):
        """No look-ahead: an earlier end_date sees an earlier ceiling."""
        days = _run_dates(self.con)
        mid = days[len(days) // 2]
        early = O.ceiling_evidence(self.con, mid)
        if early["status"] != "ok":
            self.skipTest("no 30 s HR before the corpus's median date")
        self.assertLessEqual(early["ceiling_30s"], self.ev["ceiling_30s"])
        self.assertEqual(early["as_of"], mid)


if __name__ == "__main__":
    unittest.main()
