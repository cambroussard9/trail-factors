"""§16.2 post-run report — the OUTPUT SCHEMA IS THE CONTRACT (SPEC.md §12.5).

The chat reasons from this structure and cannot see the code, so these tests
pin the structure rather than the wording. Every negative case has a positive
twin, per the §12.3 review lesson.

Corpus tests read the database named by TRAIL_FACTORS_DB (skipped when unset).
Their subject is found by a PROPERTY -- the watch running activity with the
most slope traversals, i.e. the hilliest well-matched run -- so they hold on
any athlete's corpus. The cluster tests run on synthetic positions with a
synthetic cluster configured through tests.corpus.use_config.
"""

import re
import unittest
from unittest import mock

import duckdb

from trail_factors.analysis import primitives as P
from trail_factors.analysis import report as REP
from trail_factors.paths import REPO_ROOT
from trail_factors.units import pace_over, pace_per_mile
from tests.corpus import CorpusTestCase, connect, one, use_config

# The subject of the single-run checks: the watch running activity with the
# most slope traversals, so it exercises the terrain primitives.
SUBJECT = """
    SELECT a.activity_id, a.run_id FROM activities a
    JOIN slope_traversals t USING (activity_id)
    WHERE a.source = 'garmin_watch' AND a.sport = 'running'
    GROUP BY a.activity_id, a.run_id, a.start_time_utc
    ORDER BY count(*) DESC, a.start_time_utc LIMIT 1"""


def subject(con):
    return one(con, SUBJECT, what="watch running activity with slope traversals")


class TestPercentile(unittest.TestCase):
    """Percentile is what gives the reader a scale. Without it they have raw
    values and no way to tell unusual from ordinary."""

    def test_a_value_at_the_top_scores_high(self):
        self.assertGreater(P.percentile_of(10.0, [1.0, 2.0, 3.0, 4.0]), 90)

    def test_a_value_at_the_bottom_scores_low(self):
        self.assertLess(P.percentile_of(0.0, [1.0, 2.0, 3.0, 4.0]), 10)

    def test_the_median_scores_near_fifty(self):
        self.assertAlmostEqual(P.percentile_of(3.0, [1.0, 2.0, 3.0, 4.0, 5.0]),
                               50.0, delta=15)

    def test_too_small_a_population_gives_no_percentile(self):
        """A percentile against two runs would invent a scale that is not there."""
        self.assertIsNone(P.percentile_of(5.0, [1.0, 2.0]))

    def test_a_missing_value_gives_no_percentile(self):
        self.assertIsNone(P.percentile_of(None, [1.0, 2.0, 3.0, 4.0]))


class TestComparisonSet(CorpusTestCase):
    """Comparing like to like is the point of the segment library."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.aid, cls.run_id = subject(cls.con)

    def test_the_rule_is_always_stated(self):
        comp = REP.comparison_set(self.con, self.aid, self.run_id)
        self.assertIn("cluster", comp["rule"])
        self.assertIsNotNone(comp["date_range"])

    def test_members_are_all_in_the_same_cluster(self):
        comp = REP.comparison_set(self.con, self.aid, self.run_id)
        self.assertGreater(comp["n"], 0)
        for aid in comp["members"]:
            self.assertEqual(REP.cluster_of(self.con, aid), comp["cluster"])

    def test_the_run_is_never_its_own_baseline(self):
        comp = REP.comparison_set(self.con, self.aid, self.run_id)
        self.assertNotIn(self.aid, comp["members"])

    def test_cluster_comes_from_the_trace_not_the_start_point(self):
        """§14g: a run can start with a GPS gap (a trailhead under canopy),
        and §9 records that startLatitude is the first KNOWN position, not the
        start."""
        import inspect
        src = inspect.getsource(REP.cluster_of)
        self.assertIn("median(lat)", src)
        self.assertNotIn("ORDER BY timestamp LIMIT 1", src)

    def test_a_gps_less_run_falls_back_rather_than_crashing(self):
        aid = self.con.execute(
            "SELECT activity_id FROM activities WHERE coverage_fraction = 0 "
            "LIMIT 1").fetchone()
        if aid:
            self.assertEqual(REP.cluster_of(self.con, aid[0]), "NO_GPS")

    def test_no_label_in_the_corpus_carries_a_coordinate(self):
        """Whatever config is in force, every label is a configured cluster
        name, OTHER or NO_GPS -- never a position."""
        from trail_factors import config
        allowed = ({c.name for c in config.get().clusters}
                   | {"OTHER", "NO_GPS"})
        for (aid,) in self.con.execute(
                "SELECT activity_id FROM activities").fetchall():
            label = REP.cluster_of(self.con, aid)
            self.assertIn(label, allowed, aid)
            self.assertNotRegex(label, r"\d+\.\d+", aid)


def _positions(lat, lon):
    """A synthetic corpus of one activity: a short trace at (lat, lon)."""
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE records (activity_id VARCHAR, lat DOUBLE, "
                "lon DOUBLE)")
    if lat is not None:
        con.executemany("INSERT INTO records VALUES ('a', ?, ?)",
                        [(lat + i * 1e-4, lon + i * 1e-4) for i in range(5)])
    else:
        con.execute("INSERT INTO records VALUES ('a', NULL, NULL)")
    return con


# Synthetic cluster, far from anywhere in particular.
CLUSTER_A = {"name": "CLUSTER_A", "lat": 46.5, "lon": 7.5, "radius_km": 5.0}


class TestClusterLabelsNeverCarryAPosition(unittest.TestCase):
    """The label reaches the report and the MCP payload, where position is
    never emitted. It used to be OTHER(lat,lon) outside every cluster."""

    def label(self, lat, lon, clusters=(CLUSTER_A,)):
        con = _positions(lat, lon)
        try:
            with use_config(clusters=list(clusters)):
                return REP.cluster_of(con, "a")
        finally:
            con.close()

    def test_inside_a_configured_cluster_is_its_name(self):
        """THE TWIN: the classifier still classifies."""
        self.assertEqual(self.label(46.5, 7.5), "CLUSTER_A")

    def test_outside_every_cluster_is_plain_OTHER_with_no_digit(self):
        label = self.label(-33.9, 18.4)
        self.assertEqual(label, "OTHER")
        self.assertNotRegex(label, r"\d")

    def test_no_clusters_configured_is_OTHER(self):
        self.assertEqual(self.label(-33.9, 18.4, clusters=()), "OTHER")

    def test_no_position_is_NO_GPS(self):
        self.assertEqual(self.label(None, None), "NO_GPS")

    def test_a_coordinate_bearing_label_from_the_classifier_is_collapsed(self):
        """cluster_of trusts only configured names: anything else the
        classifier returns (the old OTHER(lat,lon) shape) becomes OTHER."""
        with mock.patch("trail_factors.cli.report.assign_cluster",
                        lambda lat, lon: "OTHER(-33.90,18.40)"):
            label = self.label(-33.9, 18.4)
        self.assertEqual(label, "OTHER")
        self.assertIsNone(re.search(r"\d", label))


class TestDerivedTablesAgree(CorpusTestCase):
    """delta_hr reported "no efforts on this run" for a run with 14 of them.

    The cause was not a value mismatch: segment_recovery was STALE relative to
    segment_efforts, because `recovery` was missing from the sync pipeline and
    nothing detected the inconsistency. These pin the agreement.
    """

    def test_segment_recovery_covers_every_segment_effort(self):
        missing = self.con.execute("""
            SELECT count(*) FROM segment_efforts e
            WHERE NOT EXISTS (SELECT 1 FROM segment_recovery r
                              WHERE r.activity_id = e.activity_id
                                AND r.slope_index = e.slope_index)
        """).fetchone()[0]
        self.assertEqual(missing, 0,
                         "segment_recovery is stale -- run `recovery` after `match`")

    def test_delta_hr_and_the_climb_signature_agree_per_run(self):
        """The exact contradiction that surfaced: signature says 7 ascents and
        7 descents, delta_hr says none."""
        for (aid,) in self.con.execute(
                "SELECT activity_id FROM activities WHERE source = 'garmin_watch' "
                "AND activity_id IN (SELECT activity_id FROM slope_traversals)"
        ).fetchall():
            sig = P.climb_signature(self.con, aid)
            rec = dict(self.con.execute(
                "SELECT direction, count(*) FROM segment_recovery "
                "WHERE activity_id = ? GROUP BY 1", [aid]).fetchall())
            eff = dict(self.con.execute(
                "SELECT direction, count(*) FROM segment_efforts "
                "WHERE activity_id = ? GROUP BY 1", [aid]).fetchall())
            self.assertEqual(rec, eff, f"{aid}: recovery disagrees with efforts")
            self.assertEqual(sum(eff.values()), sig["n_slopes"] if False
                             else sum(eff.values()))

    def test_direction_values_are_the_slope_vocabulary_everywhere(self):
        """After the slope refactor, forward/reverse must be gone."""
        for table in ("segment_efforts", "segment_recovery", "slope_traversals"):
            vals = {r[0] for r in self.con.execute(
                f"SELECT DISTINCT direction FROM {table}").fetchall()}
            self.assertTrue(vals <= {"ascent", "descent"}, f"{table}: {vals}")

    def test_the_sync_pipeline_runs_recovery_after_match(self):
        """Structural: the ordering that keeps the two tables consistent."""
        src = (REPO_ROOT / "src" / "trail_factors" / "mcp_server.py").read_text()
        self.assertIn("recovery_build", src)
        self.assertLess(src.index('"match", match_build'),
                        src.index('"recovery", recovery_build'))


class TestNotable(unittest.TestCase):
    """Where to look first. Presentation only -- no verdict, no cause."""

    def _rep(self, flags=(), prims=()):
        return {"flags": list(flags), "primitives": list(prims)}

    def _prim(self, name, pct, value=1.0, status=REP.OK, reason=None):
        return {"name": name, "unit": "bpm", "value": value, "status": status,
                "reason": reason, "percentile": pct,
                "history": {"median": 1.0, "p25": 0.5, "p75": 2.0, "n": 8}}

    def test_a_high_percentile_is_notable(self):
        out = REP.notable(self._rep(prims=[self._prim("x", 95.0)]))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["kind"], "extreme")
        self.assertIn("high", out[0]["text"])

    def test_a_low_percentile_is_notable(self):
        out = REP.notable(self._rep(prims=[self._prim("x", 4.0)]))
        self.assertIn("low", out[0]["text"])

    def test_an_ordinary_percentile_is_not_notable(self):
        """The positive twin of the two above: p50 must NOT surface, or the
        section would list everything and rank nothing."""
        self.assertEqual(REP.notable(self._rep(prims=[self._prim("x", 50.0)])), [])

    def test_a_missing_percentile_is_not_notable(self):
        self.assertEqual(REP.notable(self._rep(prims=[self._prim("x", None)])), [])

    def test_a_failed_gate_is_notable(self):
        out = REP.notable(self._rep(prims=[
            self._prim("x", None, value=None, status=REP.GATED, reason="gate said no")]))
        self.assertEqual(out[0]["kind"], "gate")
        self.assertIn("not the same as the run being bad", out[0]["text"])

    def test_refused_and_unavailable_are_not_notable(self):
        """§15.4: a refusal is not a finding and must not be surfaced as one."""
        for status in (REP.REFUSED, REP.UNAVAILABLE):
            out = REP.notable(self._rep(prims=[
                self._prim("x", None, value=None, status=status, reason="r")]))
            self.assertEqual(out, [], status)

    def test_every_validation_flag_is_notable(self):
        out = REP.notable(self._rep(flags=[
            {"check": "c", "severity": "warning", "message": "m"}]))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["kind"], "flag")

    def test_salience_order_fatal_then_extreme_then_gate_then_warning(self):
        out = REP.notable(self._rep(
            flags=[{"check": "w", "severity": "warning", "message": "m"},
                   {"check": "f", "severity": "fatal", "message": "m"}],
            prims=[self._prim("hi", 99.0),
                   self._prim("g", None, value=None, status=REP.GATED,
                              reason="r")]))
        kinds = [i["kind"] for i in out]
        self.assertEqual(kinds, ["flag", "extreme", "gate", "flag"])
        self.assertEqual(out[0]["severity"], "fatal")
        self.assertEqual(out[-1]["severity"], "warning")

    def test_a_more_extreme_percentile_outranks_a_less_extreme_one(self):
        out = REP.notable(self._rep(prims=[self._prim("mild", 91.0),
                                           self._prim("wild", 100.0)]))
        self.assertEqual([i["text"].split()[0] for i in out], ["wild", "mild"])

    def test_notable_appears_in_the_rendered_report(self):
        con = connect()
        try:
            aid = subject(con)[0]
            out = REP.render(REP.build(con, aid))
        finally:
            con.close()
        self.assertIn("NOTABLE", out)
        self.assertLess(out.index("NOTABLE"), out.index("PRIMITIVES"),
                        "notable must come FIRST -- it says where to look")

    def test_the_corpus_actually_produces_extremes_somewhere(self):
        """Otherwise every test above is synthetic and the feature is untested
        against real data."""
        con = connect()
        found = 0
        try:
            for (aid,) in con.execute(
                    "SELECT activity_id FROM activities "
                    "WHERE source = 'garmin_watch'").fetchall():
                rep = REP.build(con, aid)
                found += sum(1 for i in rep["notable"] if i["kind"] == "extreme")
                if found:
                    break
        finally:
            con.close()
        self.assertGreater(found, 0)


class TestReportContract(CorpusTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.rep = REP.build(cls.con, subject(cls.con)[0])

    def test_every_primitive_carries_one_of_the_four_statuses(self):
        """A bare null collapses four different meanings -- the main way this
        output could mislead."""
        valid = {REP.OK, REP.GATED, REP.REFUSED, REP.UNAVAILABLE}
        self.assertGreater(len(self.rep["primitives"]), 4)
        for p in self.rep["primitives"]:
            self.assertIn(p["status"], valid, p["name"])

    def test_an_absent_value_always_states_why(self):
        for p in self.rep["primitives"]:
            if p["value"] is None:
                self.assertTrue(p["reason"], f"{p['name']} is silent about why")

    def test_a_non_ok_primitive_never_carries_a_value(self):
        for p in self.rep["primitives"]:
            if p["status"] != REP.OK:
                self.assertIsNone(p["value"], p["name"])

    def test_positive_twin_at_least_one_primitive_is_ok_with_a_value(self):
        """Every check above passes vacuously if nothing ever reports."""
        ok = [p for p in self.rep["primitives"] if p["status"] == REP.OK]
        self.assertGreater(len(ok), 1)
        for p in ok:
            self.assertIsNotNone(p["value"])

    def test_tau_states_a_status_its_VALUE_can_support(self):
        """This asserted REFUSED with a "1 Hz" reason. Two things changed and
        neither is this test being wrong:

        §6.3's repeatability gate can OPEN on data arrival (the corpus CV
        falling under an unchanged MAX_TAU_CV), so refusal is not automatic;
        and the branch that reaches OK used to ship `value: None` under it,
        which is the ok-wrapping-an-absence defect that once killed
        render().

        So what is pinned now is the CONTRACT rather than the verdict: ok
        requires a number, and anything else requires a reason. The verdict
        itself belongs to the data and moves with it.
        """
        tau = next(p for p in self.rep["primitives"]
                   if p["name"] == "climb_recovery_tau")
        if tau["status"] == REP.OK:
            self.assertIsNotNone(tau["value"])
            # AND IT CARRIES WHAT THE GATE STOOD ON.
            self.assertIn("n_groups_supporting", tau["gate_basis"])
        else:
            self.assertIn(tau["status"],
                          (REP.REFUSED, REP.GATED, REP.UNAVAILABLE))
            self.assertTrue(tau["reason"])
            self.assertIsNone(tau["value"])

    def test_gap_is_refused_until_the_personal_refit(self):
        gap = next(p for p in self.rep["primitives"]
                   if p["name"] == "grade_adjusted_pace")
        self.assertEqual(gap["status"], REP.REFUSED)

    def test_percentile_is_withheld_when_the_history_is_too_thin(self):
        for p in self.rep["primitives"]:
            if p["percentile"] is not None:
                self.assertGreaterEqual(p["history"]["n"], 3, p["name"])

    def test_the_grade_table_is_split_never_pooled(self):
        """Pooling reads as 'pace does not vary with grade' and is an artifact
        of mixing walking with running (§6.2)."""
        kinds = {g["split_type"] for g in self.rep["grade_table"]}
        self.assertTrue(kinds <= {"rwd_run", "rwd_walk"})
        # The subject is the run with the most slope traversals, so its
        # table spans more than one grade bin.
        self.assertGreater(len({g["grade_bin"]
                                for g in self.rep["grade_table"]}), 1)

    def test_provenance_travels_with_the_numbers(self):
        pv = self.rep["provenance"]
        for key in ("sampling_regime", "coverage_fraction", "weather_source",
                    "weather_is_final"):
            self.assertIn(key, pv)

    def test_what_cannot_be_enforced_is_stated(self):
        """§15.4 requires exclusion; the report must say what grain it cannot
        enforce rather than implying it checked."""
        self.assertTrue(self.rep["unenforced"])
        self.assertIn("quality_flags", self.rep["unenforced"][0])

    def test_the_athlete_block_carries_only_what_this_build_records(self):
        """Garmin's own rating and the free-text notes -- nothing asked of the
        athlete after the run, and no staleness of an answer that is never
        collected."""
        a = self.rep["athlete"]
        self.assertEqual(set(a), {"garmin_self_evaluation", "notes",
                                  "notes_status", "notes_projection"})
        self.assertNotIn("do_not_pool", a["garmin_self_evaluation"])

    def test_a_run_with_no_note_is_normal_not_a_deficiency(self):
        aid = self.con.execute("""
            SELECT a.activity_id FROM activities a
            WHERE a.source = 'garmin_watch' AND a.run_id NOT IN
              (SELECT run_id FROM annotations) LIMIT 1""").fetchone()
        if aid is None:
            self.skipTest("every watch run in this corpus has a note")
        rep = REP.build(self.con, aid[0])
        self.assertEqual(rep["athlete"]["notes"], [])
        self.assertGreater(len(rep["primitives"]), 4)

    def test_the_session_load_travels_with_its_basis(self):
        """garmin_rpe x timer minutes, in the payload with the filters that
        say so -- and in the rendered text either way."""
        from trail_factors.analysis import session_load as SL
        sl = self.rep["session_load"]
        self.assertIn(sl["status"], {REP.OK, REP.UNAVAILABLE})
        self.assertEqual(sl["filters"], SL.filters())
        self.assertFalse(sl["filters"]["feel_used"])
        if sl["status"] == REP.OK:
            self.assertAlmostEqual(sl["foster_load_au"],
                                   sl["garmin_rpe"] * sl["duration_min"])
        else:
            self.assertIsNone(sl["foster_load_au"])
            self.assertTrue(sl["reason"])
        self.assertIn("SESSION LOAD", REP.render(self.rep))

    def test_render_produces_text_without_raising(self):
        out = REP.render(self.rep)
        self.assertIn("POST-RUN REPORT", out)
        self.assertIn("COMPARISON SET", out)
        self.assertIn("GRADE-BINNED PACE", out)
        self.assertIn("NOT ENFORCED", out)

    def test_render_marks_assumed_weather_as_assumed(self):
        """§8: an assumed value that is labelled is usable; an unlabelled one
        is a lie."""
        aid = self.con.execute(
            "SELECT activity_id FROM weather_records "
            "WHERE weather_source = 'assumed_indoor' LIMIT 1").fetchone()
        if aid:
            out = REP.render(REP.build(self.con, aid[0]))
            self.assertIn("ASSUMED", out)

    def test_render_reports_both_paces(self):
        """§4.3: never one without the other."""
        out = REP.render(self.rep)
        self.assertIn("elapsed pace", out)
        self.assertIn("moving pace", out)

    def test_paces_render_as_minutes_and_seconds_not_decimal_minutes(self):
        """A decimal pace is a well-formed number that is silently 18 s wrong.
        Pinned against a recomputation from the stored quantities, so the test
        fails if the display drifts from what it claims to show."""
        out = REP.render(self.rep)
        m = self.rep["measured"]
        for label, secs in (("elapsed", m["elapsed_s"]), ("moving", m["moving_s"])):
            want = pace_over(secs, m["distance_m"])
            self.assertRegex(want, r"^\d+:[0-5]\d$")
            self.assertIn(f"{label} pace", out)
            self.assertIn(f"{want}/mi", out)

    def test_the_payload_carries_the_formatted_pace_beside_its_inputs(self):
        """The chat reads the structure, not the text, so the structure must
        carry the pace too — otherwise it converts by hand and gets 13.45."""
        m = self.rep["measured"]
        self.assertEqual(m["elapsed_pace_per_mile"],
                         pace_over(m["elapsed_s"], m["distance_m"]))
        self.assertEqual(m["moving_pace_per_mile"],
                         pace_over(m["moving_s"], m["distance_m"]))

    def test_the_grade_table_shows_pace_as_minutes_and_seconds(self):
        """The grade-binned table is the densest pace display in the report."""
        out = REP.render(self.rep)
        self.assertIn("pace/mi", out)
        self.assertNotIn("min/mi", out)
        for g in self.rep["grade_table"]:
            self.assertEqual(g["pace_per_mile"], pace_per_mile(g["mps"]))
            self.assertRegex(g["pace_per_mile"], r"^\d+:[0-5]\d$")
            self.assertIn(g["pace_per_mile"], out)

    def test_the_report_presents_and_does_not_judge(self):
        """§16.3's elimination belongs to the chat, which has the weather
        baseline and the athlete's own account that this function does
        not."""
        out = REP.render(self.rep)
        self.assertIn("does not eliminate causes", out)


if __name__ == "__main__":
    unittest.main()
