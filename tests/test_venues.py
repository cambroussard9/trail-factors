"""§19 venue identity: anchors, assignment, stability, and the comparison set.

THE RE-FOUNDING TEST IS THE ONE §19.3 NAMES. A segment_id is founder-derived,
so backfilling an older run changes it; an anchor stored as (run_id,
direction, at_distance_m) must resolve to the SAME runs before and after. It
runs the real matcher on synthetic traversals in an in-memory database.

No test writes into, or reads, the real athlete-state directory: every test
that touches the stores redirects their paths at a temporary directory. The
corpus tests run against TRAIL_FACTORS_DB with a synthetic cluster that covers
every position, so they need nobody's configuration.
"""

import ast
import contextlib
import datetime as dt
import inspect
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import duckdb

from trail_factors.analysis import report as R
from trail_factors.analysis import venues as V
from trail_factors.ingest import athlete_state as AS
from trail_factors.transform import matcher as M
from tests.corpus import CorpusTestCase, require_corpus, use_config

# SYNTHETIC: an arbitrary time and two places in an arbitrary metric grid.
T0 = dt.datetime(2023, 6, 20, 7, 0, 0)
P1 = (400_000.0, 5_150_000.0)
P2 = (400_600.0, 5_150_500.0)

# One synthetic cluster that contains every position on Earth (no two points
# are more than ~20,015 km apart), so a corpus test needs no real cluster.
CLUSTER = "CLUSTER_A"
WHOLE_EARTH = {"name": CLUSTER, "lat": 0.0, "lon": 0.0, "radius_km": 20_100.0}


@contextlib.contextmanager
def isolated_athlete_state():
    """Point every athlete-state file at an empty temporary directory, so a
    test neither reads the real one nor can write into it. Yields the dir."""
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(AS, "STATE_DIR", d))
            for name in ("CONFIRMATIONS", "SEGMENT_NAMES", "VENUES", "NOTES"):
                stack.enter_context(mock.patch.object(
                    AS, name, d / getattr(AS, name).name))
            yield d


class TestTheRule(unittest.TestCase):
    ANCHORS = {"Hill A": {"m1"}, "Hill B": {"r1", "r2"}}

    def test_one_venue(self):
        a = V.classify([("m1", 800.0), ("x", 300.0)], self.ANCHORS)
        self.assertEqual((a["state"], a["venue"]), (V.ASSIGNED, "Hill A"))
        self.assertEqual(a["venues_touched"][0]["share"], 1.0)

    def test_a_link_run_over_the_threshold_takes_the_larger_venue(self):
        a = V.classify([("m1", 700.0), ("r1", 300.0)], self.ANCHORS)
        self.assertEqual((a["state"], a["venue"]), (V.ASSIGNED, "Hill A"))
        self.assertEqual([t["venue"] for t in a["venues_touched"]],
                         ["Hill A", "Hill B"])

    def test_the_threshold_is_inclusive(self):
        a = V.classify([("m1", 600.0), ("r1", 400.0)], self.ANCHORS)
        self.assertEqual(a["state"], V.ASSIGNED)

    def test_a_link_run_under_it_is_mixed_and_names_both(self):
        a = V.classify([("m1", 500.0), ("r2", 500.0)], self.ANCHORS)
        self.assertEqual(a["state"], V.MIXED)
        self.assertIsNone(a["venue"])
        self.assertEqual(len(a["venues_touched"]), 2)
        self.assertIn("link run", a["reason"])

    def test_segments_but_no_anchor_is_unassigned_never_guessed(self):
        a = V.classify([("x", 900.0), ("y", 100.0)], self.ANCHORS)
        self.assertEqual((a["state"], a["venue"]), (V.UNASSIGNED, None))

    def test_no_efforts_is_none(self):
        self.assertEqual(V.classify([], self.ANCHORS)["state"], V.NONE)

    def test_no_venue_defined_is_not_configured_never_unassigned(self):
        """With nothing to cross, "crosses no venue's anchor" is a claim
        about the run that nothing checked; the cause is the configuration."""
        for efforts in ([("x", 900.0)], []):
            a = V.classify(efforts, {})
            self.assertEqual((a["state"], a["venue"]),
                             (V.NOT_CONFIGURED, None))
            self.assertEqual(a["reason"], "no venues are configured")
            self.assertNotIn("anchor", a["reason"])

    def test_positive_twin_a_venue_with_no_resolved_anchor_is_unassigned(self):
        """A venue exists, its anchors resolved to nothing: configured, so
        the run is unassigned and the reason is about the anchors."""
        a = V.classify([("x", 900.0)], {"Hill A": set()})
        self.assertEqual(a["state"], V.UNASSIGNED)
        self.assertIn("no venue's anchor", a["reason"])

    def test_the_threshold_is_in_the_digest(self):
        from trail_factors.analysis.versions import calc_params
        self.assertEqual(calc_params()["venue_link_share_min"], 0.60)

    def test_the_projection_rule_is_the_named_constant_in_the_digest(self):
        """An unconfigured run's zone is derived per run, and the zone is
        part of segment identity -- so the rule anchors and venues resolve
        through. The digest registers the CONSTANT the derivation is built
        around, so changing it cannot leave calc_version unmoved."""
        from trail_factors.analysis.versions import calc_params, digest
        from trail_factors.transform import dem as D
        p = calc_params()
        self.assertEqual(p["projection_default"], D.DERIVED_PROJECTION_RULE)
        with mock.patch.object(D, "DERIVED_PROJECTION_RULE",
                               "wgs84_utm_zone_of_first_position_per_run"):
            moved = calc_params()
        self.assertNotEqual(moved["projection_default"],
                            p["projection_default"])
        self.assertNotEqual(digest(moved), digest(p))


def traversal(run, idx, place, start_m, *, days=0, dx=0.0, direction="ascent"):
    x, y = place[0] + dx, place[1]
    return {
        "activity_id": f"act_{run}", "slope_index": idx, "direction": direction,
        "lo_x": x, "lo_y": y, "hi_x": x + 50, "hi_y": y + 50,
        "mid_x": x + 25, "mid_y": y + 25,
        "place_x": x, "place_y": y, "place_elevation_m": 1200.0,
        "side_bearing_deg": 45.0,
        "low_lat": 46.5, "low_lon": 7.5, "high_lat": 46.5, "high_lon": 7.5,
        "low_elevation_m": 1188.0, "high_elevation_m": 1200.0,
        "vertical_m": 12.0, "length_m": 200.0, "mean_grade_pct": 6.0,
        "start_distance_m": start_m, "end_distance_m": start_m + 200.0,
        "start_utc": T0 + dt.timedelta(days=days, seconds=start_m),
        "end_utc": None, "duration_s": 120.0, "vertical_speed_mps": 0.1,
        "avg_hr": 130.0, "sort_key": T0 + dt.timedelta(days=days),
    }


def corpus(runs):
    """Three runs crossing P1 then P2; each run's day is its index."""
    out = []
    for i, r in enumerate(runs):
        out += [traversal(r, 0, P1, 1000.0, days=i),
                traversal(r, 1, P2, 3000.0, days=i)]
    return out


def fixture_db():
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE activities (activity_id VARCHAR, run_id VARCHAR)")
    con.execute("CREATE TABLE runs (run_id VARCHAR)")
    con.execute("CREATE TABLE dem_elevations (activity_id VARCHAR)")
    con.execute("CREATE TABLE slope_traversals (activity_id VARCHAR, "
                "slope_index INTEGER, direction VARCHAR, "
                "start_distance_m DOUBLE, end_distance_m DOUBLE)")
    con.execute("CREATE TABLE segment_efforts (segment_id VARCHAR, "
                "activity_id VARCHAR, slope_index INTEGER, "
                "start_utc TIMESTAMP, length_m DOUBLE)")
    return con


def load(con, traversals):
    """Re-found: rebuild the library with the REAL matcher and rewrite."""
    for t in ("activities", "runs", "dem_elevations", "slope_traversals",
              "segment_efforts"):
        con.execute(f"DELETE FROM {t}")
    runs = sorted({t["activity_id"][4:] for t in traversals})
    con.executemany("INSERT INTO activities VALUES (?, ?)",
                    [(f"act_{r}", r) for r in runs])
    con.executemany("INSERT INTO runs VALUES (?)", [(r,) for r in runs])
    con.executemany("INSERT INTO dem_elevations VALUES (?)",
                    [(f"act_{r}",) for r in runs])
    con.executemany("INSERT INTO slope_traversals VALUES (?,?,?,?,?)",
                    [(t["activity_id"], t["slope_index"], t["direction"],
                      t["start_distance_m"], t["end_distance_m"])
                     for t in traversals])
    segs = M.build_library(traversals)
    con.executemany("INSERT INTO segment_efforts VALUES (?,?,?,?,?)",
                    [(s["segment_id"], m["activity_id"], m["slope_index"],
                      m["start_utc"], m["length_m"])
                     for s in segs for m in s["members"]])
    return segs


def runs_of(con, segment_id):
    return {r[0] for r in con.execute(
        "SELECT a.run_id FROM segment_efforts e JOIN activities a "
        "USING (activity_id) WHERE e.segment_id = ?", [segment_id]).fetchall()}


class TestAnAnchorSurvivesReFounding(unittest.TestCase):
    def setUp(self):
        self.con = fixture_db()
        self.base = corpus(["r1", "r2", "r3"])
        segs = load(self.con, self.base)
        self.p1 = next(s["segment_id"] for s in segs
                       if s["members"][0]["slope_index"] == 0)
        self.anchor = V.anchor_for_segment(self.con, self.p1)
        self.venues = [{"name": "Hill A", "anchors": [self.anchor]}]

    def tearDown(self):
        self.con.close()

    def test_the_anchor_is_run_anchored_and_coordinate_free(self):
        self.assertEqual(set(self.anchor), set(V.ANCHOR_KEYS))
        self.assertEqual(self.anchor, {"run_id": "r1", "direction": "ascent",
                                       "at_distance_m": 1100.0})

    def test_every_anchor_resolves_to_the_same_runs_after_a_backfill(self):
        before = runs_of(self.con, V.resolve_anchor(self.con, self.anchor)
                         ["segment_id"])
        assigned_before = {r for r, a in V.assign_all(self.con, self.venues)
                           ["by_run"].items() if a["venue"] == "Hill A"}
        # An OLDER run crossing P1 3 m off re-founds that segment.
        older = traversal("r0", 0, P1, 1500.0, days=-365, dx=3.0)
        segs = load(self.con, self.base + [older])
        new_p1 = next(s["segment_id"] for s in segs
                      if any(m["activity_id"] == "act_r0" for m in s["members"]))
        self.assertNotEqual(new_p1, self.p1, "the fixture did not re-found")

        hit = V.resolve_anchor(self.con, self.anchor)
        self.assertEqual(hit["segment_id"], new_p1)
        self.assertEqual(runs_of(self.con, new_p1), before | {"r0"})
        assigned_after = {r for r, a in V.assign_all(self.con, self.venues)
                          ["by_run"].items() if a["venue"] == "Hill A"}
        self.assertEqual(assigned_after, assigned_before | {"r0"})

    def test_a_name_follows_its_slope_through_the_re_found(self):
        names = [{"name": "the long climb", "anchor": self.anchor}]
        self.assertEqual(V.names_by_segment(self.con, names),
                         {self.p1: "the long climb"})
        segs = load(self.con, self.base + [
            traversal("r0", 0, P1, 1500.0, days=-365, dx=3.0)])
        new_p1 = next(s["segment_id"] for s in segs
                      if any(m["activity_id"] == "act_r0" for m in s["members"]))
        self.assertEqual(V.names_by_segment(self.con, names),
                         {new_p1: "the long climb"})

    def test_a_run_off_the_anchor_is_unassigned_and_a_flat_one_none(self):
        self.con.execute("INSERT INTO runs VALUES ('flat')")
        self.con.execute("INSERT INTO activities VALUES ('act_flat', 'flat')")
        self.con.execute("INSERT INTO dem_elevations VALUES ('act_flat')")
        out = V.assign_all(self.con, [{"name": "Hill B", "anchors": [
            V.anchor_for_segment(self.con, next(
                s for (s,) in self.con.execute(
                    "SELECT DISTINCT segment_id FROM segment_efforts").fetchall()
                if s != self.p1))]}])["by_run"]
        self.assertEqual(out["r1"]["venue"], "Hill B")      # crosses both
        self.assertEqual(out["flat"]["state"], V.NONE)
        self.assertIn("no slope was found", out["flat"]["reason"])
        out2 = V.assign_all(self.con, [])["by_run"]
        self.assertEqual(out2["r1"]["state"], V.NOT_CONFIGURED)
        self.assertEqual(out2["r1"]["reason"], "no venues are configured")

    def test_an_anchor_with_no_slope_there_is_unresolved_not_dropped(self):
        lost = {"run_id": "r1", "direction": "ascent", "at_distance_m": 2000.0}
        out = V.assign_all(self.con, [{"name": "Gone", "anchors": [lost]}])
        self.assertEqual(out["anchors"][0]["status"], "anchor_unresolved")
        self.assertIn("2000 m", out["anchors"][0]["reason"])
        self.assertEqual(out["by_run"]["r1"]["state"], V.UNASSIGNED)

    def test_the_wrong_direction_does_not_resolve(self):
        flipped = dict(self.anchor, direction="descent")
        self.assertEqual(V.resolve_anchor(self.con, flipped)["status"],
                         "anchor_unresolved")

    def test_a_segment_anchoring_two_venues_decides_neither(self):
        out = V.assign_all(self.con, [{"name": "A", "anchors": [self.anchor]},
                                      {"name": "B", "anchors": [self.anchor]}])
        self.assertEqual(out["shared_anchor_segments"], {self.p1: ["A", "B"]})
        self.assertEqual(out["by_run"]["r1"]["state"], V.UNASSIGNED)


class TestTheStoresHoldNoCoordinate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.patches = [mock.patch.object(AS, "SEGMENT_NAMES",
                                          d / "segment_names.json"),
                        mock.patch.object(AS, "VENUES", d / "venues.json")]
        for p in self.patches:
            p.start()
        self.anchor = {"run_id": "r1", "direction": "ascent",
                       "at_distance_m": 1100.0}

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_a_saved_name_carries_only_the_anchor(self):
        AS.save_segment_name("the long climb", self.anchor, T0)
        rows = json.loads(AS.SEGMENT_NAMES.read_text())
        self.assertEqual(set(rows[0]), {"name", "anchor", "named_at",
                                        "revision"})
        self.assertEqual(rows[0]["anchor"], self.anchor)

    def test_a_rename_is_a_revision_not_a_second_entry(self):
        first = AS.save_segment_name("a", self.anchor, T0)
        AS.save_segment_name("b", self.anchor, T0, previous=first)
        rows = AS.load_segment_names()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["name"], rows[0]["revision"]), ("b", 1))
        self.assertEqual(rows[0]["superseded"][0]["name"], "a")

    def test_a_position_is_refused_by_name_on_write(self):
        for k in ("peak_lat", "place_x", "lat"):
            with self.subTest(k=k), self.assertRaises(ValueError) as cm:
                AS.save_venue_anchor("V", dict(self.anchor, **{k: 1.0}), T0)
            self.assertIn("position", str(cm.exception))
        self.assertFalse(AS.VENUES.exists())

    def test_a_position_is_refused_on_read(self):
        AS.SEGMENT_NAMES.write_text(json.dumps([{
            "name": "x", "anchor": dict(self.anchor, peak_lon=7.5)}]))
        with self.assertRaises(ValueError):
            AS.load_segment_names()

    def test_venue_anchors_accumulate_with_history(self):
        AS.save_venue_anchor("Hill A", self.anchor, T0)
        second = dict(self.anchor, at_distance_m=3100.0)
        e = AS.save_venue_anchor("Hill A", second, T0)
        self.assertEqual(e["anchors"], [self.anchor, second])
        self.assertEqual(e["revision"], 1)
        with self.assertRaises(ValueError):
            AS.save_venue_anchor("Hill A", second, T0)

    def test_the_coordinate_storing_path_is_gone(self):
        params = inspect.signature(AS.save_segment_name).parameters
        self.assertFalse({"peak_lat", "peak_lon", "place_x", "place_y"}
                         & set(params))
        self.assertFalse(hasattr(M, "attach_names"))
        # Code, not comments: no identifier or key in the module names one.
        tree = ast.parse(Path(AS.__file__).read_text())
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
            n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        self.assertFalse({"peak_lat", "peak_lon", "place_x", "place_y"} & names)


class TestTheComparisonSetIsVenueAware(CorpusTestCase):
    """On the corpus, with the assignment MOCKED: the step must be shown to
    narrow, to fall back, and to say which."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        rows = cls.con.execute("""
            SELECT activity_id, run_id FROM activities
            WHERE source = 'garmin_watch' AND sport = 'running'
              AND activity_id IN (SELECT activity_id FROM segment_efforts)
            ORDER BY start_time_utc DESC LIMIT 1""").fetchall()
        if not rows:
            raise unittest.SkipTest("this corpus has no watch run with "
                                    "segment efforts")
        cls.aid, cls.rid = rows[0]
        # NO VENUES: an empty, isolated athlete-state directory.
        with isolated_athlete_state(), use_config(clusters=[WHOLE_EARTH]):
            cls.cluster = R.comparison_set(cls.con, cls.aid, cls.rid)
        cls.member_runs = [cls.con.execute(
            "SELECT run_id FROM activities WHERE activity_id = ?",
            [a]).fetchone()[0] for a in cls.cluster["members"]]

    def _with(self, mine, venue_runs):
        by_run = {r: {"state": V.ASSIGNED, "venue": "Test Venue",
                      "venues_touched": []} for r in venue_runs}
        by_run[self.rid] = mine
        with isolated_athlete_state(), use_config(clusters=[WHOLE_EARTH]), \
                mock.patch.object(V, "assign_all",
                                  return_value={"by_run": by_run}):
            return R.comparison_set(self.con, self.aid, self.rid)

    def test_an_assigned_run_is_measured_against_its_venue(self):
        if len(self.member_runs) < 4:
            self.skipTest("the cluster needs 4+ members for a narrowing test")
        venue_runs = self.member_runs[1:4]
        cs = self._with({"state": V.ASSIGNED, "venue": "Test Venue",
                         "venues_touched": []}, venue_runs)
        self.assertIn("same venue (Test Venue)", cs["rule"])
        self.assertEqual(len(cs["members"]), 3)
        self.assertEqual(set(cs["members"]),
                         set(self.cluster["members"][1:4]))
        self.assertEqual(cs["venue"]["venue"], "Test Venue")

    def test_a_venue_too_small_says_so_and_keeps_the_cluster(self):
        cs = self._with({"state": V.ASSIGNED, "venue": "Test Venue",
                         "venues_touched": []}, self.member_runs[:2])
        self.assertIn("venue step skipped: 2 earlier run(s)", cs["rule"])
        self.assertEqual(cs["members"], self.cluster["members"])

    def test_a_mixed_run_falls_back_to_the_cluster_labelled(self):
        cs = self._with({"state": V.MIXED, "venue": None, "venues_touched": [],
                         "reason": "a link run"}, self.member_runs[:3])
        self.assertIn("venue mixed, so the cluster stands (a link run)",
                      cs["rule"])
        self.assertEqual(cs["members"], self.cluster["members"])

    def test_with_no_venues_every_run_keeps_its_cluster_set(self):
        """No venues.json: the step changes the LABEL and nothing else, and
        the label names the configuration, not the run."""
        self.assertIn(f"same cluster ({CLUSTER})", self.cluster["rule"])
        self.assertIn("venue not_configured, so the cluster stands (no "
                      "venues are configured)", self.cluster["rule"])
        self.assertNotIn("no venue's anchor", self.cluster["rule"])
        self.assertEqual(self.cluster["venue"]["state"], V.NOT_CONFIGURED)


class TestTheRuleNamesWhyTheClusterStands(unittest.TestCase):
    """Synthetic, no corpus: the comparison-set rule carries the state the
    classifier returned, so "no venues" and "missed every anchor" read
    differently."""

    def setUp(self):
        self.con = duckdb.connect(":memory:")
        self.con.execute("CREATE TABLE activities (activity_id VARCHAR, "
                         "run_id VARCHAR, start_time_utc TIMESTAMP, "
                         "sport VARCHAR, source VARCHAR)")
        for i in range(4):
            self.con.execute(
                "INSERT INTO activities VALUES (?, ?, ?, 'running', "
                "'garmin_watch')",
                [f"a{i}", f"run-a{i}", T0 - dt.timedelta(days=i)])

    def tearDown(self):
        self.con.close()

    def rule(self, anchors_by_venue):
        mine = V.classify([("x", 500.0)], anchors_by_venue)
        with mock.patch.object(R, "cluster_of", lambda con, aid: CLUSTER), \
                mock.patch.object(V, "assign_all",
                                  return_value={"by_run": {"run-a0": mine}}):
            return R.comparison_set(self.con, "a0", "run-a0")

    def test_no_venues_says_no_venues_are_configured(self):
        cs = self.rule({})
        self.assertIn("venue not_configured, so the cluster stands (no "
                      "venues are configured)", cs["rule"])
        self.assertNotIn("no venue's anchor", cs["rule"])
        self.assertEqual(cs["venue"]["state"], V.NOT_CONFIGURED)
        self.assertEqual(sorted(cs["members"]), ["a1", "a2", "a3"])

    def test_positive_twin_venues_exist_and_the_run_misses_them(self):
        cs = self.rule({"Hill A": {"m1"}})
        self.assertIn("venue unassigned, so the cluster stands (this run "
                      "crosses segments but no venue's anchor)", cs["rule"])
        self.assertNotIn("no venues are configured", cs["rule"])
        self.assertEqual(sorted(cs["members"]), ["a1", "a2", "a3"])


class TestCandidatesProposeAndWriteNothing(CorpusTestCase):
    """Item 3: anchors are PROPOSED for the athlete to confirm in plain
    words, never chosen, and nothing reaches the athlete-state directory."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        import io
        from trail_factors.cli import terrain
        buf = io.StringIO()
        with isolated_athlete_state() as d, \
                use_config(clusters=[WHOLE_EARTH]), \
                mock.patch.object(terrain, "DB_PATH", require_corpus()):
            cls.out = V.candidates(cls.con, CLUSTER)
            with contextlib.redirect_stdout(buf):
                terrain.venue_candidates(CLUSTER)
            cls.written = sorted(f.name for f in d.iterdir())
        cls.printed = buf.getvalue()
        if not cls.out["groups"]:
            raise unittest.SkipTest("this corpus has no segment crossed by "
                                    f"{cls.out['min_runs']}+ runs")

    def test_nothing_in_the_athlete_state_directory_changed(self):
        self.assertEqual(self.written, [])
        self.assertIn("Nothing is written", self.printed)

    def test_every_reference_resolves_to_its_own_segment(self):
        for g in self.out["groups"]:
            for c in g["candidates"]:
                ref = {k: c["reference"][k] for k in V.ANCHOR_KEYS}
                with self.subTest(segment=c["segment_id"]):
                    self.assertEqual(V.resolve_anchor(self.con, ref)
                                     ["segment_id"], c["segment_id"])
                    lo, hi = c["reference"]["span_mi"]
                    self.assertLessEqual(lo, c["reference"]["at_mi"])
                    self.assertLessEqual(c["reference"]["at_mi"], hi)

    def test_most_crossed_first_and_every_candidate_meets_the_floor(self):
        sizes = [g["n_runs"] for g in self.out["groups"]]
        self.assertEqual(sizes, sorted(sizes, reverse=True))
        for g in self.out["groups"]:
            n = [c["n_runs"] for c in g["candidates"]]
            self.assertEqual(n, sorted(n, reverse=True))
            self.assertTrue(all(k >= self.out["min_runs"] for k in n))
            for c in g["candidates"]:
                self.assertEqual(len(c["runs"]), c["n_runs"])

    def test_the_two_climbing_figures_are_labelled_apart(self):
        self.assertIn("steepness", self.printed)
        self.assertIn("runs climb", self.printed)
        c = self.out["groups"][0]["candidates"][0]
        self.assertAlmostEqual(c["ft_per_mi"], c["grade_pct"] * 52.8)


if __name__ == "__main__":
    unittest.main()
