"""§17 session metrics: the laps fix, hill pace, direction drift, the series.

Each class pins a defect that was found by measurement, so a regression to the
old behaviour fails rather than being rediscovered. Every negative assertion
has a positive twin (CLAUDE.md).

The corpus subjects are found by PROPERTY -- the run with the most slope
efforts, the run with the most complete run/walk cycles -- and the assertions
are invariants, never one athlete's figures.
"""

import contextlib
import json
import re
import unittest

from tests.corpus import (
    CorpusTestCase,
    corpus_path,
    corpus_projection,
    isolated,
    one,
    use_config,
)
from trail_factors.analysis import basic as B
from trail_factors.analysis import over as O
from trail_factors.analysis import versions as V
from trail_factors.analysis.series import binned_series

# --- isolation: never the real athlete state or config ----------------------
PROJECTION = None


# Entered in setUpModule, CLOSED in tearDownModule -- see tests/corpus.py
# isolated() for why this is not unittest.enterModuleContext.
_ISOLATION = contextlib.ExitStack()


def setUpModule():
    global PROJECTION
    if corpus_path() is not None:
        PROJECTION = corpus_projection()
        _ISOLATION.enter_context(isolated(projection_crs=PROJECTION))


def tearDownModule():
    _ISOLATION.close()


def garmin_listing() -> dict:
    """Garmin's own activity summaries, keyed by Garmin id, from any cached
    listing under the raw directory. Empty when none is cached."""
    from trail_factors.paths import RAW_DIR

    out = {}
    for f in sorted((RAW_DIR / "garmin_api").glob("activities_*.json")):
        out.update({str(a["activityId"]): a for a in json.loads(f.read_text())})
    return out


def most_efforts(con) -> str:
    """The watch run with the most slope efforts in both directions."""
    return one(
        con,
        """
        SELECT e.activity_id FROM segment_efforts e
        JOIN activities a USING (activity_id)
        WHERE a.source = 'garmin_watch' AND a.sport = 'running'
        GROUP BY 1
        HAVING count(*) FILTER (WHERE e.direction = 'ascent') > 0
           AND count(*) FILTER (WHERE e.direction = 'descent') > 0
        ORDER BY count(*) DESC, 1 LIMIT 1""",
        what="watch run with slope efforts in both directions",
    )[0]


class TestLapsUseTimerTime(CorpusTestCase):
    """laps.duration_s stored total_elapsed_time while the FIT carries both --
    the identical defect already fixed for splits. Per-lap time is an ORACLE
    field (§4.2), so a disagreement with Garmin is OUR error."""

    def test_the_column_exists_and_is_populated(self):
        n, filled = self.con.execute(
            "SELECT count(*), count(total_timer_time) FROM laps"
        ).fetchone()
        if n == 0:
            self.skipTest("this corpus has no laps")
        self.assertEqual(n, filled, "every lap must carry total_timer_time")

    def test_timer_time_never_exceeds_elapsed_time(self):
        """Timer time is elapsed time minus pauses, so it can never be the
        larger -- on any lap, in any corpus."""
        n = self.con.execute(
            "SELECT count(*) FROM laps WHERE total_timer_time > duration_s + 0.5"
        ).fetchone()[0]
        self.assertEqual(n, 0)

    def test_timer_time_reproduces_garmins_minimum_lap_on_every_activity(self):
        oracle = garmin_listing()
        if not oracle:
            self.skipTest("no cached Garmin listing under the raw directory")
        checked = 0
        for aid, sf in self.con.execute(
            "SELECT activity_id, source_file FROM activities "
            "WHERE source = 'garmin_watch' AND sport = 'running'"
        ).fetchall():
            g = re.search(r"(\d+)_ACTIVITY", sf or "")
            a = oracle.get(g.group(1)) if g else None
            if not a or a.get("minActivityLapDuration") is None:
                continue
            got = self.con.execute(
                "SELECT min(total_timer_time) FROM laps WHERE activity_id = ?", [aid]
            ).fetchone()[0]
            if got is None:
                continue
            checked += 1
            self.assertAlmostEqual(got, a["minActivityLapDuration"], delta=0.5, msg=aid)
        if not checked:
            self.skipTest(
                "no cached summary carries a minimum lap for a run in this corpus"
            )

    def test_elapsed_and_timer_actually_differ_somewhere(self):
        """THE PRESENCE TWIN. If they were identical everywhere the test above
        would pass against the old buggy column and prove nothing."""
        n = self.con.execute(
            "SELECT count(*) FROM laps WHERE abs(duration_s - total_timer_time) > 0.5"
        ).fetchone()[0]
        if n == 0:
            self.skipTest(
                "no lap in this corpus contains a pause, so the two "
                "columns cannot be told apart here"
            )
        self.assertGreater(n, 0)


class TestCalcVersionCoversTheDriftAndTauGates(unittest.TestCase):
    """The digest exists to catch a constant changing under a number. It did
    not cover §6.7's tolerances or §6.3's reportability gate."""

    def test_changing_the_drift_tolerance_moves_the_digest(self):
        p = V.calc_params()
        self.assertIn("drift_speed_tol_mps", p)
        before = V.digest(p)
        p["drift_speed_tol_mps"] = p["drift_speed_tol_mps"] + 0.15
        self.assertNotEqual(before, V.digest(p))

    def test_changing_the_tau_gate_moves_the_digest(self):
        p = V.calc_params()
        self.assertIn("tau_max_cv", p)
        before = V.digest(p)
        p["tau_max_cv"] = 0.9
        self.assertNotEqual(before, V.digest(p))

    def test_the_registered_values_track_the_live_constants(self):
        """Registering a stale copy would be worse than not registering."""
        from trail_factors.analysis import primitives as PR
        from trail_factors.analysis import recovery as RC

        p = V.calc_params()
        self.assertEqual(p["drift_speed_tol_mps"], PR.DRIFT_SPEED_TOL_MPS)
        self.assertEqual(p["tau_max_cv"], RC.MAX_TAU_CV)


class TestEveryOkPrimitiveStatesItsFilters(CorpusTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from trail_factors.analysis import report as REP

        aid = one(
            cls.con,
            """
            SELECT activity_id FROM activities
            WHERE source = 'garmin_watch' AND sport = 'running'
            ORDER BY start_time_utc DESC LIMIT 1""",
            what="watch run",
        )[0]
        cls.rep = REP.build(cls.con, aid)

    def test_no_ok_primitive_lacks_applied_filters(self):
        bare = [
            p["name"]
            for p in self.rep["primitives"]
            if p["status"] == "ok" and not p.get("applied_filters")
        ]
        self.assertEqual(bare, [])

    def test_delta_hr_names_its_direction_and_its_incomparability(self):
        got = [p for p in self.rep["primitives"] if p["name"].startswith("delta_hr_")]
        self.assertEqual(len(got), 2)
        for p in got:
            if p["status"] != "ok":
                continue
            f = p["applied_filters"]
            self.assertIn(f["direction"], ("ascent", "descent"))
            self.assertIn("NOT comparable", f["note"])


class TestHillPaceByDirection(CorpusTestCase):
    """Hill pace by direction. It was one GROUP BY away and reachable only
    through one call per segment."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.aid = most_efforts(cls.con)
        cls.r = B.effort_pace_by_direction(cls.con, cls.aid)

    def test_every_effort_carries_pace_direction_grade_and_hr(self):
        self.assertTrue(self.r["efforts"])
        for e in self.r["efforts"]:
            self.assertIn(e["direction"], ("ascent", "descent"))
            for k in (
                "mean_pace_per_mile",
                "best_30s_pace_per_mile",
                "mean_grade_pct",
                "avg_hr",
            ):
                self.assertIn(k, e)

    def test_both_directions_aggregate(self):
        for d in ("ascent", "descent"):
            v = self.r["by_direction"][d]
            self.assertGreater(v["n_efforts"], 0)
            self.assertRegex(v["pace_per_mile"], r"^\d+:[0-5]\d$")

    def test_the_aggregate_is_distance_weighted_not_a_mean_of_paces(self):
        """A short effort and a long one do not carry equal evidence."""
        for d in ("ascent", "descent"):
            sel = [
                e
                for e in self.r["efforts"]
                if e["direction"] == d and e["length_m"] and e["duration_s"]
            ]
            want = sum(e["length_m"] for e in sel) / sum(e["duration_s"] for e in sel)
            self.assertAlmostEqual(self.r["by_direction"][d]["mps"], want, places=9)

    def test_the_terrain_confound_is_reported(self):
        """Ascents and descents do not cover the same ground (§14k), so the
        direction comparison must carry the ratio that says how far off it is
        -- longer over shorter, from the distances beside it."""
        cg = self.r["comparable_ground"]
        a, d = cg["ascent_distance_m"], cg["descent_distance_m"]
        self.assertAlmostEqual(cg["length_ratio"], max(a, d) / min(a, d), places=9)
        self.assertGreaterEqual(cg["length_ratio"], 1.0)
        self.assertIn("terrain", cg["note"])

    def test_the_report_renders_it(self):
        from trail_factors.analysis import report as REP

        out = REP.render(REP.build(self.con, self.aid))
        self.assertIn("HILL PACE BY DIRECTION", out)
        self.assertIn("ground ratio", out)


class TestDirectionAwareDrift(CorpusTestCase):
    """segment_effort_drift ships the direction split because its pairs are
    same-direction BY CONSTRUCTION. §6.7 cannot: on an out-and-back course a
    large share of its pairs join records travelling in opposite directions.
    The cluster is the one the run with the most efforts falls in, under the
    test's config."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from trail_factors.analysis.report import cluster_of

        cls.cluster = cluster_of(cls.con, most_efforts(cls.con))

    def drift(self, d):
        return O.segment_effort_drift(self.con, direction=d, cluster=self.cluster)

    def test_the_direction_split_returns_values(self):
        for d in ("ascent", "descent"):
            r = self.drift(d)
            self.assertEqual(r["applied_filters"]["direction"], d)
            if r["n_with_value"] == 0:
                self.skipTest(f"no repeated {d} pair in this cluster")
            self.assertGreater(r["n_with_value"], 0, d)

    def test_it_is_named_for_itself_and_never_as_6_7(self):
        r = self.drift("ascent")
        self.assertEqual(r["primitive"], "segment_effort_drift_ascent")
        self.assertIn("NOT §6.7", r["applied_filters"]["note"])

    def test_pairs_are_same_direction_by_construction(self):
        from trail_factors.analysis import primitives as PR
        from trail_factors.paths import REPO_ROOT

        src = (
            REPO_ROOT / "src" / "trail_factors" / "analysis" / "primitives.py"
        ).read_text()
        self.assertIn("a.direction = b.direction", src)
        self.assertTrue(PR.segment_drift_filters()["same_direction_pairs_only"])

    def test_the_ceiling_effect_caveat_travels_with_the_number(self):
        """An asymmetry can be real and still not be eccentric cost: ascents
        start near the held effort and have less room to rise."""
        caveat = self.drift("descent")["applied_filters"]["ceiling_effect_caveat"]
        self.assertIn("headroom", caveat)
        self.assertIn("opening HR", caveat)

    def test_6_7_still_refuses_a_direction(self):
        """The negative twin: the honest split lives here, so §6.7 must not
        quietly grow one."""
        import inspect

        from trail_factors.analysis import primitives as PR

        self.assertNotIn(
            "direction", inspect.signature(PR.matched_terrain_drift).parameters
        )


class TestBinnedSeries(CorpusTestCase):
    """The subject is the run with the most slope efforts: DEM-covered, with
    terrain, so the elevation gate has something to withhold."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.aid = most_efforts(cls.con)
        cls.s = binned_series(cls.con, cls.aid)

    def test_state_is_seconds_not_a_label(self):
        for b in self.s["bins"]:
            for k in ("run_s", "walk_s", "stand_s"):
                self.assertIsInstance(b[k], float)
        self.assertFalse(self.s["filters"]["state_is_majority_label"])

    def test_mixed_bins_are_kept_as_seconds(self):
        """THE MEASUREMENT THAT DECIDED THE DESIGN: a bin can hold both gaits,
        and a label would erase the minority one. Where such a bin exists,
        both gaits' seconds survive."""
        mixed = [b for b in self.s["bins"] if b["run_s"] > 0 and b["walk_s"] > 0]
        if not mixed:
            self.skipTest("no bin on the subject mixes running and walking")
        for b in mixed:
            self.assertLess(b["run_s"], self.s["bin_s"] + 1e-6)
            self.assertLess(b["walk_s"], self.s["bin_s"] + 1e-6)

    def test_state_seconds_never_exceed_the_bin(self):
        for b in self.s["bins"]:
            total = b["run_s"] + b["walk_s"] + b["stand_s"]
            self.assertLessEqual(total, self.s["bin_s"] + 1e-6)

    def test_every_bin_carries_both_indices(self):
        with_data = [b for b in self.s["bins"] if b["n_records"] > 0]
        self.assertTrue(with_data)
        for b in with_data:
            self.assertIsNotNone(b["elapsed_start_s"])
            self.assertIsNotNone(b["distance_start_m"])

    def test_no_bin_carries_a_coordinate(self):
        """Position is never emitted, whatever else the athlete accepted."""
        blob = json.dumps(self.s, default=str).lower()
        for banned in ("lat", "lon"):
            self.assertNotIn(f'"{banned}"', blob)

    def test_elevation_is_withheld_unless_the_fingerprint_is_accepted(self):
        """Elevation by distance is a ROUTE FINGERPRINT. The default config
        does not accept it, so no bin carries elevation and the payload says
        why -- on a run the DEM covers, where there was something to withhold."""
        with use_config(projection_crs=PROJECTION, route_fingerprint_accepted=False):
            s = binned_series(self.con, self.aid)
        f = s["filters"]
        self.assertFalse(f["route_fingerprint_accepted"])
        self.assertFalse(f["position_emitted"])
        self.assertTrue(all(b["elevation_m"] is None for b in s["bins"]))
        self.assertFalse(s["elevation_emitted"])
        if not s["elevation_available"]:
            self.skipTest("the DEM does not cover the subject run")
        self.assertIn("route_fingerprint_accepted", s["elevation_withheld_reason"])

    def test_accepted_the_bins_carry_elevation(self):
        """THE TWIN: a gate that always withheld would pass the test above."""
        with use_config(projection_crs=PROJECTION, route_fingerprint_accepted=True):
            s = binned_series(self.con, self.aid)
        f = s["filters"]
        self.assertTrue(f["route_fingerprint_accepted"])
        self.assertFalse(f["position_emitted"])
        if not s["elevation_available"]:
            self.skipTest("the DEM does not cover the subject run")
        self.assertTrue(s["elevation_emitted"])
        self.assertIsNone(s["elevation_withheld_reason"])
        self.assertTrue(any(b["elevation_m"] is not None for b in s["bins"]))

    def test_paces_are_minutes_and_seconds(self):
        paced = [b for b in self.s["bins"] if b["pace_per_mile"]]
        self.assertTrue(paced)
        for b in paced:
            self.assertRegex(b["pace_per_mile"], r"^\d+:[0-5]\d$")


class TestRunWalkCycles(CorpusTestCase):
    """M3 (§17.2). CHOICE, not capacity: how the run was structured. The
    subject is the watch run with the most walks whose last run block has no
    walk after it."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from trail_factors.analysis.cycles import cycles

        # staticmethod: a bare function stored on the class becomes a bound
        # method and swallows `self` as its first argument.
        cls.cycles = staticmethod(cycles)
        cls.aid = one(
            cls.con,
            r"""
            SELECT s.activity_id FROM splits s
            JOIN activities a USING (activity_id)
            WHERE a.source = 'garmin_watch' AND a.sport = 'running'
              AND s.split_type LIKE 'rwd\_%' ESCAPE '\'
            GROUP BY 1
            HAVING count(*) FILTER (WHERE s.split_type = 'rwd_walk') >= 4
               AND arg_max(s.split_type, s.start_time)
                   FILTER (WHERE s.split_type <> 'rwd_stand') = 'rwd_run'
            ORDER BY count(*) FILTER (WHERE s.split_type = 'rwd_walk') DESC, 1
            LIMIT 1""",
            what="watch run with 4+ walks ending on a run block",
        )[0]
        cls.r = cycles(cls.con, cls.aid)

    def test_cycles_reconstruct_on_the_subject_run(self):
        self.assertEqual(self.r["status_hint"], "ok")
        self.assertGreaterEqual(self.r["n_complete_cycles"], 4)

    def test_a_cycle_is_a_run_block_plus_the_walk_that_follows(self):
        for c in self.r["cycles"]:
            self.assertGreater(c["run_duration_s"], 0)
        complete = [c for c in self.r["cycles"] if c["walk_duration_s"] is not None]
        self.assertEqual(len(complete), self.r["n_complete_cycles"])

    def test_a_run_block_with_no_following_walk_is_none_not_zero(self):
        """No walk and a zero-length walk are different things."""
        trailing = [c for c in self.r["cycles"] if c["walk_duration_s"] is None]
        self.assertTrue(trailing, "the last run block should have no walk")
        for c in trailing:
            self.assertIsNone(c["hr_recovered_bpm"])

    def test_short_run_blocks_report_duration_but_no_pace(self):
        """§6.0's fitted floor. The DURATION is still data."""
        gated = [c for c in self.r["cycles"] if c["run_pace_gated"]]
        if not gated:
            self.skipTest("no run block on the subject was short enough to gate")
        for c in gated:
            self.assertIsNone(c["run_pace_per_mile"])
            self.assertGreater(c["run_duration_s"], 0)

    def test_long_run_blocks_do_report_a_pace(self):
        """The positive twin."""
        paced = [c for c in self.r["cycles"] if not c["run_pace_gated"]]
        if not paced:
            self.skipTest("every run block on the subject was gated")
        for c in paced:
            self.assertRegex(c["run_pace_per_mile"], r"^\d+:[0-5]\d$")

    def test_hr_recovered_is_positive_when_hr_falls(self):
        """Named for the direction it reads in, so the sign cannot be got
        backwards."""
        checked = 0
        for c in self.r["cycles"]:
            if c["hr_recovered_bpm"] is None:
                continue
            checked += 1
            self.assertAlmostEqual(
                c["hr_recovered_bpm"],
                c["hr_at_walk_start"] - c["hr_at_walk_end"],
                places=6,
            )
        if not checked:
            self.skipTest("no walk on the subject outlasts the HR lag")

    def test_purchase_pairs_carry_the_time_confound(self):
        """Later walks are longer AND later runs are slower; a reader must be
        able to see that before concluding the walk caused anything."""
        if not self.r["purchase"]:
            self.skipTest("no purchase pair on the subject")
        for p in self.r["purchase"]:
            self.assertIn("elapsed_at_next_run_s", p)
            self.assertLess(p["walk_of_cycle"], p["next_run_cycle"])

    def test_no_fitted_slope_is_returned(self):
        """A coefficient over four points would be wider than itself."""
        blob = json.dumps(self.r, default=str).lower()
        for banned in ("slope", "coefficient", "r_squared", "fit"):
            self.assertNotIn(f'"{banned}"', blob)

    def test_it_works_on_smart_recording_and_needs_no_dem(self):
        """The reason M3 is built first: it needs no DEM, no GPS and no 1 Hz."""
        rows = self.con.execute("""
            SELECT a.activity_id FROM activities a
            WHERE a.source = 'garmin_watch' AND a.sport = 'running'
              AND a.sampling_regime = 'smart_recording'
              AND NOT EXISTS (SELECT 1 FROM records_enriched e
                              WHERE e.activity_id = a.activity_id)
              AND EXISTS (SELECT 1 FROM splits s WHERE s.activity_id = a.activity_id
                          AND s.split_type = 'rwd_run')
            ORDER BY a.activity_id LIMIT 3""").fetchall()
        if not rows:
            self.skipTest("no DEM-less Smart Recording run with run splits")
        for (aid,) in rows:
            r = self.cycles(self.con, aid)
            self.assertGreater(r["n_cycles"], 0, aid)

    def test_a_run_with_no_walking_is_gated_not_broken(self):
        aid = one(
            self.con,
            """
            SELECT a.activity_id FROM activities a
            WHERE a.source = 'garmin_watch' AND a.sport = 'running'
              AND EXISTS (SELECT 1 FROM splits s WHERE s.activity_id = a.activity_id
                          AND s.split_type = 'rwd_run')
              AND NOT EXISTS (SELECT 1 FROM splits s
                              WHERE s.activity_id = a.activity_id
                                AND s.split_type = 'rwd_walk')
            ORDER BY a.activity_id LIMIT 1""",
            what="watch run that never walks",
        )[0]
        r = self.cycles(self.con, aid)
        self.assertEqual(r["status_hint"], "gated")
        self.assertEqual(r["n_complete_cycles"], 0)
        self.assertIsNone(r["summary"]["median_walk_duration_s"])

    def test_a_source_with_no_splits_is_unavailable(self):
        aid = one(
            self.con,
            "SELECT activity_id FROM activities WHERE source = 'strava_phone' LIMIT 1",
            what="phone activity",
        )[0]
        r = self.cycles(self.con, aid)
        self.assertEqual(r["status_hint"], "unavailable")
        self.assertIn("no rwd_* splits", r["reason"])

    def test_it_declares_which_side_of_6_6_it_is_on(self):
        self.assertIn("choice", self.r["filters"]["side"])
        self.assertFalse(self.r["filters"]["sampling_regime_dependent"])

    def test_the_report_renders_it(self):
        from trail_factors.analysis import report as REP

        out = REP.render(REP.build(self.con, self.aid))
        self.assertIn("RUN/WALK CYCLES", out)
        self.assertIn("does the walk buy anything", out)


if __name__ == "__main__":
    unittest.main()
