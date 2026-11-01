"""§16.2's four statuses, enforced — and reasons that checked what they claim.

FOUR DEFECTS, ONE FAMILY. Each is a payload asserting something it had not
established:

  * climb_recovery_tau returned status `ok` with value None, because the value
    was never wired while the status branch could reach OK. render() died on
    abs(None) and took a complete 19-section payload with it.
  * cadence_by_grade said "no DEM tile covers this run (N of M garmin
    activities)" on a run the tile covers 100%, in the same payload as a
    terrain_coverage block saying so -- and N/M was a dead literal from a
    corpus that had since grown several-fold.
  * durations blamed "the Strava phone file" on a garmin_watch hike. Of the
    splitless activities, exactly ONE was the Strava file and most were
    non-running sports the string did not know about.
  * max_hr_over compared the declared ceiling against any selection, so an
    easy-run week produced a gap that measures the SELECTION, not the heart.

Every negative has a positive twin.

Corpus tests read the database named by TRAIL_FACTORS_DB (skipped when unset)
and find each subject by a PROPERTY, so they hold on any athlete's corpus.
Whether a DEM tile covers a run is decided by mocking the tile set's
coverage answer, so the terrain-state tests do not depend on which tiles
happen to be on disk. The ceiling-gate tests choose their selections by a
measured property (a date whose runs sit outside the ceiling band, the date
of the run that sets it), never by what a run was called.
"""

from __future__ import annotations

import unittest
from unittest import mock

from tests.corpus import CorpusTestCase, connect, one
from trail_factors.analysis import basic as B
from trail_factors.analysis import report as R

# The verified-crasher shape: the watch running activity with the most
# slope traversals, where every terrain primitive (tau included) is reached.
RICH = """
    SELECT a.activity_id FROM activities a
    JOIN slope_traversals t USING (activity_id)
    WHERE a.source = 'garmin_watch' AND a.sport = 'running'
    GROUP BY a.activity_id, a.start_time_utc
    ORDER BY count(*) DESC, a.start_time_utc LIMIT 1"""

# A positioned watch activity of a NON-running sport with no enriched rows:
# the terrain layers are held to running, so this is the held_by_sport case
# whenever a tile covers it.
NON_RUNNING = """
    SELECT a.activity_id, a.sport FROM activities a
    WHERE a.source = 'garmin_watch' AND a.sport <> 'running'
      AND EXISTS (SELECT 1 FROM records r WHERE r.activity_id = a.activity_id
                    AND r.lat IS NOT NULL)
      AND NOT EXISTS (SELECT 1 FROM records_enriched e
                      WHERE e.activity_id = a.activity_id)
      AND NOT EXISTS (SELECT 1 FROM splits s
                      WHERE s.activity_id = a.activity_id
                        AND s.split_type LIKE 'rwd\\_%' ESCAPE '\\')
    ORDER BY a.start_time_utc LIMIT 1"""

# A running activity WITH enriched rows: terrain state ok.
ENRICHED_RUN = """
    SELECT a.activity_id FROM activities a
    WHERE a.sport = 'running' AND EXISTS (
        SELECT 1 FROM records_enriched e WHERE e.activity_id = a.activity_id)
    ORDER BY a.start_time_utc LIMIT 1"""


def rich(con):
    return one(con, RICH, what="watch running activity with slope traversals")[0]


def tiles_covering(fraction: float):
    """A TileSet stand-in that answers coverage for exactly the points it is
    given -- so n_points in a reason is the run's own count, not a literal."""

    def coverage(lats, lons):
        n = len(lats)
        n_unc = int(round(n * (1 - fraction)))
        missing = (
            []
            if n_unc == 0
            else [
                {
                    "usgs_cell": "x40y500",
                    "filename": None,
                    "fetch_command": "fetch-tile x40y500",
                }
            ]
        )
        return {
            "fraction_covered": fraction,
            "n_uncovered": n_unc,
            "n_points": n,
            "missing_tiles": missing,
        }

    ts = mock.MagicMock()
    ts.return_value.coverage.side_effect = coverage
    return mock.patch("trail_factors.transform.dem.TileSet", ts)


class TestTheVerifiedCrasher(CorpusTestCase):
    """Item 1's regression fixture."""

    def test_the_payload_returns_and_renders(self):
        rep = R.build(self.con, rich(self.con))
        self.assertGreater(len(rep), 10)
        text, err = R.render_safe(rep)
        self.assertIsNone(err, err)
        self.assertIn("POST-RUN REPORT", text)

    def test_tau_carries_a_value_when_it_claims_ok(self):
        prim = [
            p
            for p in R.build(self.con, rich(self.con))["primitives"]
            if p["name"] == "climb_recovery_tau"
        ][0]
        if prim["status"] == R.OK:
            self.assertIsNotNone(prim["value"])
        else:
            self.assertIsNotNone(prim.get("reason"))


class TestTheStatusContractIsEnforced(unittest.TestCase):
    def test_an_injected_ok_null_primitive_is_demoted_with_a_reason(self):
        out = R.enforce_status_contract(
            [
                {
                    "name": "injected",
                    "status": R.OK,
                    "value": None,
                    "reason": "its own words",
                }
            ]
        )
        self.assertEqual(out[0]["status"], R.UNAVAILABLE)
        self.assertTrue(out[0]["defect"])
        self.assertIn("CONTRACT BREACH", out[0]["reason"])
        self.assertIn("DEFECT IS IN THE PRIMITIVE", out[0]["reason"])
        self.assertIn("its own words", out[0]["reason"])

    def test_a_healthy_primitive_is_untouched(self):
        """THE TWIN: an enforcer that demoted everything would pass above."""
        good = {"name": "fine", "status": R.OK, "value": 1.5}
        self.assertEqual(R.enforce_status_contract([good]), [good])

    def test_the_other_three_statuses_may_carry_a_null(self):
        """`refused`, `gated` and `unavailable` MEAN there is no number. Only
        `ok` promises one."""
        for st in (R.REFUSED, R.GATED, R.UNAVAILABLE):
            p = {"name": "x", "status": st, "value": None}
            self.assertEqual(R.enforce_status_contract([p])[0]["status"], st)

    def test_the_demotion_survives_rendering(self):
        con = connect()
        try:
            rep = R.build(con, rich(con))
            rep["primitives"] = R.enforce_status_contract(
                [{**rep["primitives"][0], "status": R.OK, "value": None}]
                + rep["primitives"][1:]
            )
            _, err = R.render_safe(rep)
        finally:
            con.close()
        self.assertIsNone(err, err)


class TestABrokenRendererStillReturnsThePayload(unittest.TestCase):
    """Item 1c. render() runs OUTSIDE _section, so a formatter took a payload
    that had already been computed correctly."""

    def test_render_safe_returns_the_error_rather_than_raising(self):
        with mock.patch.object(R, "render", mock.Mock(side_effect=TypeError("boom"))):
            text, err = R.render_safe({"anything": 1})
        self.assertEqual(text, "")
        self.assertIn("TypeError: boom", err)

    def test_the_mcp_tool_returns_the_payload_when_render_dies(self):
        import json

        import trail_factors.mcp_server as M

        con = connect()
        try:
            aid = rich(con)
        finally:
            con.close()
        with mock.patch(
            "trail_factors.analysis.report.render",
            mock.Mock(side_effect=ValueError("formatter")),
        ):
            env = json.loads(M.report(aid))
        self.assertEqual(env["outcome"], "result")
        self.assertIsNotNone(env["payload"])
        self.assertGreater(len(env["payload"]), 10)
        self.assertIn("formatter", env["payload"]["render_error"])
        self.assertIn("formatting defect", env["text"])

    def test_render_safe_passes_a_good_render_through(self):
        """THE TWIN."""
        con = connect()
        try:
            text, err = R.render_safe(R.build(con, rich(con)))
        finally:
            con.close()
        self.assertIsNone(err)
        self.assertTrue(text)


class TestReintroducingTheStatusBugFailsTheFixture(unittest.TestCase):
    """Item 1's fourth test: the fix must be load-bearing."""

    def test_an_unwired_value_under_an_ok_status_is_caught(self):
        prims = [
            {"name": "climb_recovery_tau", "status": R.OK, "value": None, "unit": "s"}
        ]
        # WITHOUT the enforcer this is what shipped, and render dies on it.
        with self.assertRaises(TypeError):
            abs(prims[0]["value"])
        # WITH it, the payload is safe.
        fixed = R.enforce_status_contract(prims)
        self.assertEqual(fixed[0]["status"], R.UNAVAILABLE)
        self.assertIsNone(fixed[0]["value"])


class TestTerrainAbsenceResolvesToOneOfFourStates(CorpusTestCase):
    """Item 4. Decided by looking, never asserted."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.other, cls.sport = one(
            cls.con,
            NON_RUNNING,
            what="positioned non-running watch activity without splits",
        )

    def test_held_by_sport_and_outside_tiles_are_different_answers(self):
        """BOTH IN ONE TEST, because the bug was that they were the same
        string."""
        with tiles_covering(1.0):
            held = B.terrain_state(self.con, self.other)
        self.assertEqual(held["state"], "held_by_sport")
        self.assertEqual(held["covered_fraction"], 1.0)
        self.assertIn(self.sport, held["reason"])
        # THE FILTER IS QUOTED WITH ITS FILE AND FUNCTION.
        self.assertIn("terrain.py", held["hold_site"])
        self.assertIn("a.sport = 'running'", held["hold_site"])

        # ...and a run genuinely outside every tile says so instead.
        with tiles_covering(0.0):
            outside = B.terrain_state(self.con, self.other)
        self.assertEqual(outside["state"], "outside_loaded_tiles")
        self.assertNotEqual(held["reason"], outside["reason"])

    def test_a_covered_non_running_activity_does_not_contradict_its_payload(self):
        with tiles_covering(1.0):
            rep = R.build(self.con, self.other)
            cbg = B.cadence_by_grade(self.con, self.other)
        self.assertEqual(rep["terrain_coverage"].get("status"), "covered")
        self.assertEqual(cbg["terrain_state"], "held_by_sport")
        # The old string said no tile covers it. It does.
        self.assertNotIn("no DEM tile covers", cbg["reason"])
        self.assertIn("DOES cover", cbg["reason"])

    def test_no_count_in_a_reason_is_a_dead_literal(self):
        """The number in the reason is THIS run's position count, computed at
        call time -- and a different count changes it."""
        n = self.con.execute(
            "SELECT count(*) FROM records WHERE activity_id = ? AND lat IS NOT NULL",
            [self.other],
        ).fetchone()[0]
        with tiles_covering(1.0):
            real = B.terrain_state(self.con, self.other)["reason"]
        self.assertIn(f"{n:,}", real)  # computed from this run
        with mock.patch("trail_factors.transform.dem.TileSet") as TS:
            TS.return_value.coverage.return_value = {
                "fraction_covered": 1.0,
                "n_uncovered": 0,
                "n_points": 7,
                "missing_tiles": [],
            }
            small = B.terrain_state(self.con, self.other)["reason"]
        self.assertIn("7", small)
        self.assertNotEqual(real, small)

    def test_an_ok_activity_reports_ok(self):
        """THE TWIN."""
        aid = one(
            self.con, ENRICHED_RUN, what="running activity with enriched records"
        )[0]
        self.assertEqual(B.terrain_state(self.con, aid)["state"], "ok")


class TestTheNoSplitsReasonNamesWhatItChecked(CorpusTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.other, cls.sport = one(
            cls.con,
            NON_RUNNING,
            what="positioned non-running watch activity without splits",
        )

    def test_a_non_running_activity_blames_its_sport_not_Strava(self):
        d = B.durations(self.con, self.other)
        self.assertEqual(d["no_splits_cause"], "non_running_sport")
        self.assertIn(self.sport, d["reason"])
        self.assertNotIn("Strava", d["reason"])

    def test_the_actual_strava_file_DOES_blame_the_phone(self):
        """THE TWIN: the cause must still be named where it is the real one."""
        row = self.con.execute(
            "SELECT activity_id FROM activities WHERE source = 'strava_phone' LIMIT 1"
        ).fetchone()
        if row is None:
            self.skipTest("no strava_phone activity in the corpus")
        why = B.no_splits_reason(self.con, row[0])
        self.assertEqual(why["cause"], "not_the_watch")
        self.assertIn("strava_phone", why["reason"])

    def test_run_walk_cycles_gives_the_same_derived_cause(self):
        from trail_factors.analysis.cycles import cycles

        c = cycles(self.con, self.other)
        self.assertEqual(c["no_splits_cause"], "non_running_sport")
        self.assertNotIn("Strava", c["reason"])


class TestTheCeilingIsGatedOnTheSelection(CorpusTestCase):
    """Item 5. A ceiling gap over easy runs measures the selection.

    Whether a selection reached a ceiling is MEASURED on the selection: the
    refused subject is a single run date whose every run sits outside the
    band below the ceiling as of that date, so the verdict follows from the
    corpus and from nothing anyone said about the runs.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from trail_factors.analysis.over import max_hr_over

        cls.whole = max_hr_over(cls.con)
        if cls.whole["declared_ceiling"]["status"] == "unavailable":
            raise unittest.SkipTest(
                "this corpus has no declared max HR to compare a ceiling against"
            )
        cls.refused = None
        days = sorted({r["date"] for r in cls.whole["per_run"]}, reverse=True)
        for day in days[:20]:
            r = max_hr_over(cls.con, start_date=day, end_date=day)
            if r["declared_ceiling"]["status"] == "refused":
                cls.refused, cls.day = r, day
                break
        if cls.refused is None:
            raise unittest.SkipTest(
                "no recent run date in this corpus sits outside its own ceiling band"
            )

    def test_a_selection_that_never_reached_the_band_refuses_and_names_it(self):
        dc = self.refused["declared_ceiling"]
        reach = dc["selection_reach"]
        self.assertEqual(dc["status"], "refused")
        self.assertIsNone(dc["gap_bpm"])
        self.assertFalse(reach["reaches"])
        self.assertEqual(reach["n_runs"], self.refused["selection"]["n_included"])
        self.assertIn("SELECTION", dc["reason"])
        if reach["best_30s_bpm"] is not None:
            self.assertIn(f"{reach['best_30s_bpm']:.1f} bpm", dc["reason"])
        self.assertIn(f"{reach['band_floor_bpm']:.1f}", dc["reason"])
        self.assertNotIn("run_type", dc["reason"])
        # The demonstrated figures survive; only the comparison is refused.
        self.assertIsNotNone(dc["demonstrated_single_sample"])

    def test_a_capable_selection_still_reports_the_gap(self):
        """THE TWIN. The whole corpus holds the run that SET the ceiling, and
        that run is always in its own band (the gate's stated
        circularity)."""
        dc = self.whole["declared_ceiling"]
        self.assertEqual(dc["status"], "ok", dc.get("reason"))
        self.assertIsNotNone(dc["gap_bpm"])
        self.assertIn("satisfied", dc["selection_gate"])
        self.assertTrue(dc["selection_reach"]["reaches"])
        self.assertNotIn("by_group", dc["ceiling_evidence"])


if __name__ == "__main__":
    unittest.main()
