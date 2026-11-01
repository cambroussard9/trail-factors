"""Milestone 5/6 acceptance tests for the SLOPE matcher (SPEC.md §12.3, §12.4).

The criteria were stated in the spec BEFORE the matcher was built, the way
§12.2's resolution-invariance test was, and are pinned here so a later change
that breaks one fails loudly rather than quietly producing a different library.

Boundary RULES are tested on synthetic profiles in tests/test_slopes.py --
a trail with no long flats between features cannot exercise them. This file
is the corpus regression plus the matching properties, run against whatever
corpus TRAIL_FACTORS_DB names.
"""

import unittest
from pathlib import Path

from trail_factors.transform import dem as D
from trail_factors.transform import matcher as M
from tests.corpus import CorpusTestCase, one, use_config


def corpus_projection(con) -> str:
    """A metric CRS for this corpus: the WGS84 UTM zone its positions centre
    on. Derived from the data so the test needs nobody's config; the library
    built here is compared only with itself, so any local metric CRS will do.
    """
    lat, lon = con.execute("SELECT median(lat), median(lon) FROM records "
                           "WHERE lat IS NOT NULL").fetchone()
    if lat is None:
        raise unittest.SkipTest("this corpus has no positioned record")
    zone, north = D.utm_zone_at(lat, lon)
    return f"EPSG:{(32600 if north else 32700) + zone}"


def load(con):
    """(traversals, reference activity). The reference is chosen by property:
    the activity with the most slope traversals -- the role a configured
    reference run plays in `match`."""
    from trail_factors.cli import load_traversals
    with use_config(projection_crs=corpus_projection(con)):
        traversals = load_traversals(con)
    if not traversals:
        raise unittest.SkipTest("this corpus has no slope traversals")
    ref = one(con, """
        SELECT activity_id FROM slope_traversals
        GROUP BY 1 ORDER BY count(*) DESC, 1 LIMIT 1""",
              what="activity with slope traversals")[0]
    return traversals, ref


class TestMatcherAcceptance(CorpusTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.traversals, cls.ref = load(cls.con)
        cls.segments = M.build_library(cls.traversals)
        if not any(s["n_efforts"] >= 2 for s in cls.segments):
            raise unittest.SkipTest("this corpus has no slope crossed twice, "
                                    "so the matching gates have nothing to test")

    def test_the_corpus_is_not_empty(self):
        """Several tests below iterate the corpus and would report ok on zero
        rows, having asserted nothing."""
        self.assertGreater(len(self.traversals), 0)
        self.assertGreater(len(self.segments), 0)
        self.assertLess(len(self.segments), len(self.traversals),
                        "some traversals must share a segment")

    # ---------- THE GATE (§12.3) ----------
    #
    # Hold-out MEMBERSHIP is frozen at the shipped radius and does not follow
    # MATCH_RADIUS_M. Deriving it from the live constant made the gate
    # self-cancelling: inflating the radius merged neighbours into the held-out
    # segment, removing the very traversals that would have been the
    # false-match target. It passed at every radius up to 4 km. A gate that
    # cannot fire is worse than no gate (§4.2).

    def _frozen_holdout(self, seg):
        keys = {(m["activity_id"], m["slope_index"]) for m in seg["members"]}
        held = [t for t in self.traversals
                if (t["activity_id"], t["slope_index"]) in keys]
        remaining = [t for t in self.traversals
                     if (t["activity_id"], t["slope_index"]) not in keys]
        return held, remaining

    def test_gate_no_false_matches_under_spatial_holdout(self):
        """A run on ground the library has never seen must create a NEW segment.

        This is the failure the athlete cares most about, so it is the gate.
        Holding out a whole slope and re-feeding its traversals tests the real
        failure mode -- a false match to something NEARBY -- which plain
        geographic distance would never catch.
        """
        for seg in self.segments:
            held, remaining = self._frozen_holdout(seg)
            library = M.build_library(remaining)
            for m in held:
                hit = M.match_traversal(m, library)
                self.assertIsNone(
                    hit, f"held-out {seg['segment_id']} traversal "
                         f"{m['activity_id']}/{m['slope_index']} falsely matched "
                         f"{hit['segment_id'] if hit else None}")

    def test_the_gate_can_actually_fire(self):
        """The gate must be sensitive to the one tunable in the design."""
        original = M.MATCH_RADIUS_M
        try:
            M.MATCH_RADIUS_M = 400.0
            false_matches = 0
            for seg in self.segments:
                held, remaining = self._frozen_holdout(seg)
                library = M.build_library(remaining, radius_m=M.MATCH_RADIUS_M)
                false_matches += sum(
                    1 for m in held if M.match_traversal(m, library) is not None)
            self.assertGreater(
                false_matches, 0,
                "the gate reports clean at a 400 m radius -- self-cancelling")
        finally:
            M.MATCH_RADIUS_M = original

    def test_gate_a_distant_trail_produces_no_candidate(self):
        """The guarantee is structural: a new trail generates no candidate."""
        far = dict(self.traversals[0])
        far["place_x"] += 5000.0      # 5 km east, everything else identical
        self.assertIsNone(M.match_traversal(far, self.segments))

    def test_the_matcher_actually_matches(self):
        """Every negative test above passes vacuously if nothing ever matches.

        The whole incremental path could be stubbed to `return None` and the
        gate, the distant-trail test and the elevation test would all stay
        green. This is the positive assertion that makes them mean something.
        """
        hits = [M.match_traversal(m, self.segments) for m in self.traversals]
        self.assertEqual(len([h for h in hits if h]), len(self.traversals),
                         "every library member must match its own segment")
        for m, h in zip(self.traversals, hits):
            owner = next(s for s in self.segments
                         if any(k["activity_id"] == m["activity_id"]
                                and k["slope_index"] == m["slope_index"]
                                for k in s["members"]))
            self.assertEqual(h["segment_id"], owner["segment_id"])

    # ---------- CORRECTNESS (§12.4) ----------
    def test_one_slope_carries_both_directions(self):
        """THE POINT OF THE SLOPE UNIT (§5, design A).

        An ascent and the descent of the SAME face must be two EFFORTS on ONE
        segment, not two segments. That is what makes the §6.3 tau comparison
        like-for-like: recovery after climbing slope X against recovery after
        descending the same slope X. Keying position on the endpoints could not
        do this -- same-face endpoints scatter up to 380 m.
        """
        if not any(t["direction"] == "ascent" for t in self.traversals) or \
                not any(t["direction"] == "descent" for t in self.traversals):
            self.skipTest("this corpus has traversals in one direction only")
        bidirectional = [s for s in self.segments
                         if s["n_ascents"] and s["n_descents"]]
        self.assertGreaterEqual(
            len(bidirectional), 1,
            "no segment carries both directions: the slope unit is not "
            "joining an ascent and the descent of the same face")
        for s in self.segments:
            self.assertEqual(s["n_ascents"] + s["n_descents"], s["n_efforts"],
                             s["segment_id"])

    def test_segment_count_is_stable_across_the_fitted_radius_band(self):
        counts = {r: len(M.build_library(self.traversals, radius_m=r))
                  for r in (25, 30, 35, 40)}
        # RELATIVE, not an absolute count: the bound was fitted as a swing of
        # about an eighth of the library, which is a property of the method;
        # an absolute number is a property of one corpus's size.
        self.assertLessEqual(max(counts.values()) - min(counts.values()),
                             max(3, 0.2 * counts[30]),
                             f"segment count should not swing wildly: {counts}")
        # It must MOVE outside the band, or the test would also pass if
        # build_library silently ignored radius_m.
        off = len(M.build_library(self.traversals, radius_m=200))
        self.assertNotEqual(off, counts[40], "radius_m has no effect")

    def test_reference_run_traversals_all_belong_to_a_segment(self):
        mine = [(m["slope_index"], s["segment_id"]) for s in self.segments
                for m in s["members"] if m["activity_id"] == self.ref]
        fed = [t["slope_index"] for t in self.traversals
               if t["activity_id"] == self.ref]
        self.assertGreater(len(fed), 0)
        # Every traversal fed in lands in exactly ONE segment.
        self.assertEqual(sorted(i for i, _ in mine), sorted(fed))

    def test_reference_run_gives_an_orderable_tour_of_the_loop(self):
        """`match` locates segments by where the reference run meets them.

        A segment_id is unrecognisable on its own, so this ordering is what
        makes the library nameable at all.
        """
        rows = self.con.execute(
            "SELECT slope_index, start_distance_m FROM slope_traversals "
            "WHERE activity_id = ? ORDER BY start_distance_m", [self.ref]
        ).fetchall()
        dists = [d for _, d in rows]
        self.assertGreater(len(dists), 1)
        self.assertEqual(dists, sorted(dists))
        self.assertEqual(len(set(dists)), len(dists))

    # ---------- CONSISTENCY (§12.3) ----------
    def test_no_segment_has_merged_two_slopes(self):
        """Endpoint-elevation SD above the bound means two slopes were merged.

        Checks BOTH ends: a merge of two slopes off a shared saddle would agree
        at the low end and disagree at the high one.
        """
        bad = M.consistency_failures(self.segments)
        self.assertEqual(
            bad, [], "merged segments: "
            + ", ".join(f"{s['segment_id']} "
                        f"lo={s['low_elevation_sd_m']:.2f} "
                        f"hi={s['high_elevation_sd_m']:.2f}" for s in bad))

    def test_the_consistency_check_can_fire(self):
        """It must reject a segment built from two genuinely different slopes."""
        fake = [{"n_efforts": 2, "place_elevation_sd_m": 10.0,
                 "segment_id": "fake"}]
        self.assertEqual(len(M.consistency_failures(fake)), 1)
        ok = [{"n_efforts": 2, "place_elevation_sd_m": 0.4,
               "segment_id": "ok"}]
        self.assertEqual(M.consistency_failures(ok), [])


class TestMatcherProperties(CorpusTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.traversals, cls.ref = load(cls.con)

    def test_library_is_independent_of_input_order(self):
        """Ingest-order dependence is a defect this project has fixed once
        already (§4.2). Connected components make it structurally impossible."""
        forward = M.build_library(self.traversals)
        backward = M.build_library(list(reversed(self.traversals)))
        self.assertEqual([s["segment_id"] for s in forward],
                         [s["segment_id"] for s in backward])
        self.assertEqual([s["n_efforts"] for s in forward],
                         [s["n_efforts"] for s in backward])

    def test_elevation_gate_separates_two_places_at_the_same_position(self):
        """Position alone must not merge places at different heights."""
        base = dict(self.traversals[0])
        higher = dict(base)
        higher["place_elevation_m"] += 40.0
        self.assertFalse(M.compatible(base, higher))
        # positive twin: inside tolerance it MUST match
        near = dict(base)
        near["place_elevation_m"] += M.ELEV_TOL_M / 2
        self.assertTrue(M.compatible(base, near))

    def test_the_two_flanks_of_one_peak_are_different_segments(self):
        """SIDE is half of the identity: same place, opposite flank."""
        base = dict(self.traversals[0])
        other = dict(base)
        other["side_bearing_deg"] = (base["side_bearing_deg"] + 180.0) % 360.0
        self.assertFalse(M.compatible(base, other))
        same = dict(base)
        same["side_bearing_deg"] = (base["side_bearing_deg"] + 20.0) % 360.0
        self.assertTrue(M.compatible(base, same))

    def test_direction_cannot_split_a_slope(self):
        """The same slope run the other way must be the SAME segment.

        Direction is a property of the EFFORT; identity is (place, side), and
        neither changes when the athlete turns round.
        """
        a = dict(self.traversals[0])
        b = dict(a)
        b["direction"] = ("descent" if a["direction"] == "ascent" else "ascent")
        self.assertTrue(M.compatible(a, b))

    def test_unconfigured_corpus_derives_and_labels_each_run(self):
        """With no projection_crs every traversal carries its run's DERIVED
        frame. Where the whole corpus derives ONE zone -- the zone the
        configured run above uses -- the grouping is identical; only the ids
        differ, because a derived id carries its zone."""
        from trail_factors.cli import load_traversals
        with use_config():
            derived = load_traversals(self.con)
        self.assertEqual(len(derived), len(self.traversals))
        for t in derived:
            self.assertTrue(t["projection_derived"])
            self.assertTrue(t["projection_basis"].startswith(
                "derived from this run's position: UTM zone "))
        if len({t["projection"] for t in derived}) != 1:
            self.skipTest("this corpus spans more than one UTM zone, so its "
                          "derived library legitimately differs")
        if derived[0]["projection"] != corpus_projection(self.con):
            self.skipTest("the corpus median sits in a different zone from "
                          "every run")

        def groups(lib):
            return sorted(sorted((m["activity_id"], m["slope_index"])
                                 for m in s["members"]) for s in lib)
        a, b = M.build_library(derived), M.build_library(self.traversals)
        self.assertEqual(groups(a), groups(b))
        self.assertFalse({s["segment_id"] for s in a}
                         & {s["segment_id"] for s in b})

    def test_configured_traversals_keep_the_pre_derivation_ids(self):
        """Configured, a traversal's projection key does not enter its id."""
        lib = M.build_library(self.traversals)
        stripped = [{k: v for k, v in t.items()
                     if not k.startswith("projection")}
                    for t in self.traversals]
        self.assertEqual([s["segment_id"] for s in lib],
                         [s["segment_id"] for s in M.build_library(stripped)])

    def test_no_heart_rate_or_pace_in_the_match_key(self):
        """§12.3: the matcher must not use HR or pace. Median HR is nearly
        flat across grade bands (§6.2) and is not on a stable scale (§14e).

        A source check, deliberately: the property is about what the code is
        ALLOWED TO REFERENCE, which is what a source check tests.
        """
        src = Path("src/trail_factors/transform/matcher.py").read_text()
        body = src.split('"""', 2)[2]          # skip the module docstring
        for banned in ("heart_rate", "avg_hr", "speed_mps", "gap_mps", "pace"):
            self.assertNotIn(banned, body,
                             f"{banned} must not appear in the matcher body")


if __name__ == "__main__":
    unittest.main()
