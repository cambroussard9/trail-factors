"""A first-ever run on a NEW TRAIL — tile detection and matcher behaviour.

TWO SEPARATE GUARANTEES, and they are guaranteed differently.

THE TILE GAP IS EXPLAINED, NOT SILENT. A run on ground no loaded DEM tile
covers correctly refuses every terrain primitive. Before this, that produced a
report full of absent numbers with no reason, which reads as breakage. The
refusal now names the missing 10 km grid cell and the file to fetch.

THE MATCHER CANNOT CONFUSE A NEW TRAIL WITH AN OLD ONE, and that is structural
rather than tested-into-place: `compatible` rejects on position first, at
MATCH_RADIUS_M = 30 m, so places kilometres apart can never join. These tests
pin the lifecycle anyway -- create with n_efforts=1, stay separate, then match
on the second visit -- because the guarantee that matters is the whole
sequence, and a change to any one gate could break it while leaving the others
looking right.

These use SYNTHETIC traversals rather than a real new course. That is the
honest test available without one; the real test is the first run on a new
course once its tile is in, and §5 already records that a trail with no flats
between features cannot validate the boundary rule.
"""

import json
import unittest

from tests.corpus import CorpusTestCase, one, use_config
from trail_factors.transform import dem as D
from trail_factors.transform import matcher as M

# SYNTHETIC GROUND in EPSG:32632 (UTM 32N, arbitrary). An existing trail, and
# a different trail ~30 km away -- a different place by any measure.
OLD_TRAIL_X, OLD_TRAIL_Y = 400_000.0, 5_150_000.0
NEW_TRAIL_X, NEW_TRAIL_Y = 428_000.0, 5_133_000.0


def traversal(x, y, *, idx=0, sort_key=1.0, elev=1200.0, bearing=45.0, length=200.0):
    """One traversal in the shape load_traversals() produces."""
    return {
        "activity_id": f"act{sort_key}",
        "slope_index": idx,
        "direction": "ascent",
        "lo_x": x,
        "lo_y": y,
        "hi_x": x + 50,
        "hi_y": y + 50,
        "mid_x": x + 25,
        "mid_y": y + 25,
        "place_x": x,
        "place_y": y,
        "place_elevation_m": elev,
        "side_bearing_deg": bearing,
        "low_lat": 46.5,
        "low_lon": 7.5,
        "high_lat": 46.5,
        "high_lon": 7.5,
        "low_elevation_m": elev - 12.0,
        "high_elevation_m": elev,
        "vertical_m": 12.0,
        "length_m": length,
        "mean_grade_pct": 6.0,
        "start_distance_m": 0.0,
        "start_utc": None,
        "end_utc": None,
        "duration_s": 120.0,
        "vertical_speed_mps": 0.1,
        "avg_hr": 130.0,
        "sort_key": sort_key,
    }


class TestNewTrailNeverJoinsAnOldOne(unittest.TestCase):
    def test_a_distant_place_cannot_match(self):
        """The structural guarantee: position is the first gate."""
        a = traversal(OLD_TRAIL_X, OLD_TRAIL_Y)
        b = traversal(NEW_TRAIL_X, NEW_TRAIL_Y)
        self.assertFalse(M.compatible(a, b))
        self.assertFalse(M.compatible(b, a))

    def test_the_gate_is_position_not_elevation_or_bearing(self):
        """Make elevation and bearing IDENTICAL so only distance can reject.
        If this passed, a new trail at the same altitude would merge."""
        a = traversal(OLD_TRAIL_X, OLD_TRAIL_Y, elev=1200.0, bearing=45.0)
        b = traversal(NEW_TRAIL_X, NEW_TRAIL_Y, elev=1200.0, bearing=45.0)
        self.assertFalse(M.compatible(a, b))

    def test_the_same_place_DOES_match(self):
        """THE POSITIVE TWIN. A predicate that rejected everything would pass
        every test above and match nothing on the real corpus either."""
        a = traversal(NEW_TRAIL_X, NEW_TRAIL_Y, sort_key=1.0)
        b = traversal(NEW_TRAIL_X + 5, NEW_TRAIL_Y + 5, sort_key=2.0)
        self.assertTrue(M.compatible(a, b))


class TestANewTrailInAnotherZone(unittest.TestCase):
    """With no projection configured each run is projected in its own UTM
    zone. A new trail one zone over can then carry the SAME x/y as an old
    place; the frame is what keeps them apart."""

    def _in(self, zone_crs, **kw):
        t = traversal(OLD_TRAIL_X, OLD_TRAIL_Y, **kw)
        t.update(projection=zone_crs, projection_derived=True)
        return t

    def test_same_numbers_in_another_zone_are_a_new_place(self):
        old = self._in("EPSG:32632", sort_key=1.0)
        new = self._in("EPSG:32633", sort_key=2.0)
        self.assertFalse(M.compatible(old, new))
        segs = M.build_library([old, new])
        self.assertEqual(len({s["segment_id"] for s in segs}), 2)

    def test_the_same_zone_still_matches(self):
        """THE TWIN: derivation must not split a trail from itself."""
        a = self._in("EPSG:32632", sort_key=1.0)
        b = self._in("EPSG:32632", sort_key=2.0)
        b["place_x"] += 5
        self.assertTrue(M.compatible(a, b))
        self.assertEqual(len(M.build_library([a, b])), 1)


class TestFirstRunOnANewTrail(unittest.TestCase):
    def test_it_creates_its_own_places_with_one_effort_each(self):
        old = [
            traversal(OLD_TRAIL_X, OLD_TRAIL_Y, idx=0, sort_key=1.0),
            traversal(OLD_TRAIL_X + 400, OLD_TRAIL_Y, idx=1, sort_key=1.0, elev=1215.0),
        ]
        first_new = [
            traversal(NEW_TRAIL_X, NEW_TRAIL_Y, idx=0, sort_key=2.0),
            traversal(NEW_TRAIL_X + 500, NEW_TRAIL_Y, idx=1, sort_key=2.0, elev=1260.0),
        ]
        segs = M.build_library(old + first_new)
        self.assertEqual(len(segs), 4, "each distinct place is its own segment")
        for s in segs:
            self.assertEqual(len(s["members"]), 1)

    def test_the_new_places_are_not_the_old_places(self):
        old = [traversal(OLD_TRAIL_X, OLD_TRAIL_Y, sort_key=1.0)]
        new = [traversal(NEW_TRAIL_X, NEW_TRAIL_Y, sort_key=2.0)]
        segs = M.build_library(old + new)
        ids = {s["segment_id"] for s in segs}
        self.assertEqual(len(ids), 2)

    def test_the_second_visit_matches_the_first(self):
        """The lifecycle: create on run 1, MATCH on run 2. A library that
        created a fresh place every visit would trend nothing."""
        first = traversal(NEW_TRAIL_X, NEW_TRAIL_Y, sort_key=2.0)
        # Same ground, next week, with the endpoint scatter §12.3 measured.
        second = traversal(NEW_TRAIL_X + 8, NEW_TRAIL_Y - 6, sort_key=3.0, elev=1201.0)
        segs = M.build_library([first, second])
        self.assertEqual(len(segs), 1)
        self.assertEqual(len(segs[0]["members"]), 2)

    def test_a_new_trail_does_not_disturb_the_existing_library(self):
        """Adding a trail must not renumber or merge what is already there."""
        old = [
            traversal(OLD_TRAIL_X, OLD_TRAIL_Y, sort_key=1.0),
            traversal(OLD_TRAIL_X + 400, OLD_TRAIL_Y, sort_key=1.0, idx=1, elev=1215.0),
        ]
        before = {s["segment_id"] for s in M.build_library(old)}
        after = {
            s["segment_id"]
            for s in M.build_library(
                old + [traversal(NEW_TRAIL_X, NEW_TRAIL_Y, sort_key=2.0)]
            )
        }
        self.assertTrue(before.issubset(after))
        self.assertEqual(len(after), len(before) + 1)


def _grid_for(con, activity_id) -> str:
    """The NAD83 / UTM grid of the zone this run's positions sit in.

    Derived from the data, not read from anyone's config: the coverage grid
    must be SOME configured projection for a filename to be constructed, and
    the run's own zone is the one that makes the cell ids USGS names.
    """
    lat, lon = con.execute(
        "SELECT median(lat), median(lon) FROM records "
        "WHERE activity_id = ? AND lat IS NOT NULL",
        [activity_id],
    ).fetchone()
    zone, north = D.utm_zone_at(lat, lon)
    grid = D.usgs_grid_for_zone(zone, north)
    if grid is None:
        raise unittest.SkipTest(
            "the uncovered run is outside NAD83 / UTM "
            "zones, so no USGS filename exists for it"
        )
    return grid


class TestTileCoverageIsReported(CorpusTestCase):
    def _report(self, activity_id, **cfg):
        from trail_factors.analysis import report as REP

        with use_config(**cfg):
            return REP.build(self.con, activity_id)

    # THE FIXTURE IS CHOSEN FROM THE CORPUS, NOT HARD-CODED. A pinned run is
    # a premise that data arrival expires: the moment its tile is fetched it
    # stops being a gap. With tiles on disk, a positioned RUNNING activity
    # with no DEM elevations is the case; with NO tile loaded at all, every
    # positioned run is outside the loaded tiles, so the latest one serves.
    def _uncovered(self) -> str:
        if D.loaded_tiles():
            sql = """
                SELECT a.activity_id FROM activities a
                WHERE a.source = 'garmin_watch' AND a.sport = 'running'
                  AND a.coverage_fraction > 0
                  AND NOT EXISTS (SELECT 1 FROM dem_elevations d
                                  WHERE d.activity_id = a.activity_id)
                ORDER BY a.start_time_utc DESC LIMIT 1"""
        else:
            sql = """
                SELECT a.activity_id FROM activities a
                WHERE a.source = 'garmin_watch' AND a.sport = 'running'
                  AND a.coverage_fraction > 0
                ORDER BY a.start_time_utc DESC LIMIT 1"""
        return one(self.con, sql, what="positioned run outside every loaded DEM tile")[
            0
        ]

    def test_a_run_outside_every_tile_names_the_tile_it_needs(self):
        aid = self._uncovered()
        rep = self._report(
            aid,
            projection_crs=_grid_for(self.con, aid),
            dem_tile_suffix_guess="XX_Synthetic_2020",
        )
        cov = rep["terrain_coverage"]
        self.assertEqual(cov["status"], "outside_loaded_tiles")
        self.assertEqual(cov["projection"]["basis"], "configured")
        self.assertTrue(cov["missing_tiles"])
        for m in cov["missing_tiles"]:
            self.assertTrue(m["filename"].startswith("USGS_one_meter_x"))
            self.assertTrue(m["filename"].endswith(".tif"))
            # ...and the guess is LABELLED a guess. The suffix is copied from
            # the tiles on disk (or the configured guess) and the x/y is only
            # a USGS name inside the grid's own UTM zone, so a filename stated
            # with confidence sends the athlete to a 404 (§5a.1-bis).
            self.assertFalse(m["filename_is_resolved"])
            self.assertIn("usgs_cell", m)

    def test_with_no_projection_the_gap_is_named_but_the_file_is_not(self):
        """THE UNCONFIGURED CASE: the cell is still labelled (a report must
        name its gap) and the filename is null rather than a fabrication.
        The run's projection is DERIVED, and the block says so."""
        rep = self._report(self._uncovered())
        cov = rep["terrain_coverage"]
        self.assertEqual(cov["status"], "outside_loaded_tiles")
        self.assertIs(cov["projection_configured"], False)
        self.assertTrue(
            cov["projection"]["basis"].startswith(
                "derived from this run's position: UTM zone "
            ),
            cov["projection"],
        )
        for m in cov["missing_tiles"]:
            self.assertIsNone(m["filename"])
            self.assertIn(m["usgs_cell"], m["fetch_command"])

    def test_the_gap_is_the_FIRST_thing_in_notable(self):
        """Otherwise the reader meets a page of absent terrain numbers and no
        reason, which reads as breakage rather than a missing tile."""
        rep = self._report(self._uncovered())
        self.assertTrue(rep["notable"])
        self.assertEqual(rep["notable"][0]["kind"], "terrain_gap")
        self.assertIn("retile", rep["notable"][0]["text"])

    def test_it_names_the_fix_not_just_the_problem(self):
        """PINS A RUNNABLE COMMAND, not a directory.

        This once asserted the text named the DEM directory -- correct while
        fetching a tile was a manual step, and obsolete the moment
        `fetch-tile` shipped (§5a.1-bis): the athlete no longer places the
        file, so naming the directory is not the fix any more. The property
        the test was really protecting is that the report tells them what to
        DO, so that is what it pins now, including that the command actually
        resolves.
        """
        rep = self._report(self._uncovered())
        text = rep["notable"][0]["text"]
        self.assertIn("fetch-tile", text)
        self.assertIn("3DEP", text)
        for m in rep["terrain_coverage"]["missing_tiles"]:
            self.assertIn(m["fetch_command"], text)
            self.assertIn(m["usgs_cell"], m["fetch_command"])

    def test_a_covered_run_says_covered_and_notable_is_clean(self):
        """THE POSITIVE TWIN. A check that fired on every run would be noise.

        Subject: a run whose every positioned record has a DEM elevation --
        covered by the tiles that populated it, which must still be on disk.
        """
        if not D.loaded_tiles():
            self.skipTest("no DEM tile is loaded, so no run can be covered")
        aid = one(
            self.con,
            """
            SELECT a.activity_id FROM activities a
            JOIN records r USING (activity_id)
            LEFT JOIN dem_elevations d USING (activity_id, timestamp)
            WHERE a.sport = 'running' AND r.lat IS NOT NULL
            GROUP BY 1, a.start_time_utc
            HAVING count(*) = count(d.altitude_dem_m)
            ORDER BY a.start_time_utc DESC LIMIT 1""",
            what="run fully covered by DEM elevations",
        )[0]
        rep = self._report(aid)
        self.assertEqual(rep["terrain_coverage"]["status"], "covered")
        self.assertFalse([i for i in rep["notable"] if i["kind"] == "terrain_gap"])

    def test_no_coordinate_is_emitted_in_the_coverage_block(self):
        """A 10 km grid cell is what fetching a tile needs; a position is not,
        and raw positions are private (§5a)."""
        aid = self._uncovered()
        for cfg in ({}, {"projection_crs": _grid_for(self.con, aid)}):
            rep = self._report(aid, **cfg)
            blob = json.dumps(rep["terrain_coverage"])
            for banned in ('"lat"', '"lon"', "latitude", "longitude"):
                self.assertNotIn(banned, blob)


if __name__ == "__main__":
    unittest.main()
