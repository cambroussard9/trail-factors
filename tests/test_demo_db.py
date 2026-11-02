"""The demo database, built fresh from demo/data into a TemporaryDirectory.

TWO DEFECTS WERE FOUND ON IT, and each is pinned with a twin.

THE DATABASE DECIDES WHETHER A RUN HAS ELEVATION, NOT THE TILE DIRECTORY. The
demo ships complete DEM elevations and no tile. Asking only the tiles told
every demo run to `fetch-tile` ground it already had. Coverage is now read
from dem_elevations / records_enriched first; a run whose elevation is truly
absent is still told which cell to fetch.

THE LIBRARY'S COUNTS ARE THE EFFORTS THE DATABASE HOLDS. The demo export
copied `segments` rows with corpus-wide counts but only eleven runs' efforts,
so list_segments said 19 ascents where segment_history listed 6. The export
now recounts over the exported runs, and both tools say what they count.

No tile is loaded in any test here (an empty TileSet), so nothing depends on
the tiles a machine happens to hold, and no config file is read.
"""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import duckdb

from tests.corpus import use_config
from trail_factors.analysis import basic as B
from trail_factors.analysis import report as R
from trail_factors.analysis.over import segment_history
from trail_factors.cli import terrain as T
from trail_factors.cli.demo import DEMO_DIR, demo_build
from trail_factors.transform import dem as D


def build_demo(directory: str) -> Path:
    """Build the demo database inside `directory`; its output is quiet."""
    if not any(DEMO_DIR.glob("*.parquet")):
        raise unittest.SkipTest(f"no demo data in {DEMO_DIR}")
    with contextlib.redirect_stdout(io.StringIO()):
        return demo_build(dest=Path(directory) / "demo.duckdb")


def no_tiles():
    """Every TileSet() is an empty one: nothing on disk covers anything."""
    return mock.patch.object(D, "TileSet", return_value=D.TileSet([]))


def strip_elevation(con, activity_id: str, *, dem: bool = True) -> None:
    """Remove a run's stored elevation: silver always, dem_elevations too."""
    con.execute("DELETE FROM records_enriched WHERE activity_id = ?", [activity_id])
    if dem:
        con.execute("DELETE FROM dem_elevations WHERE activity_id = ?", [activity_id])


class DemoTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.path = build_demo(cls._tmp.name)
        cls.con = duckdb.connect(str(cls.path))
        cls.aids = [
            r[0]
            for r in cls.con.execute(
                "SELECT activity_id FROM activities ORDER BY start_time_utc"
            ).fetchall()
        ]

    @classmethod
    def tearDownClass(cls):
        cls.con.close()
        cls._tmp.cleanup()


class TestStoredElevationNeedsNoTile(DemoTestCase):
    def test_every_demo_run_has_its_elevation_stored(self):
        """The premise. Without it the next test passes for the wrong reason."""
        self.assertTrue(self.aids)
        for aid in self.aids:
            self.assertTrue(B.db_elevation(self.con, aid)["complete"], aid)

    def test_no_report_suggests_fetch_tile_for_a_run_with_elevation(self):
        with use_config(), no_tiles():
            for aid in self.aids:
                rep = R.build(self.con, aid)
                cov = rep["terrain_coverage"]
                self.assertEqual(cov["status"], "covered", aid)
                self.assertTrue(cov["basis"].startswith("database"), cov)
                self.assertFalse(
                    [i for i in rep["notable"] if i["kind"] == "terrain_gap"], aid
                )
                self.assertNotIn("fetch-tile", R.render(rep), aid)

    def test_a_run_whose_elevation_is_absent_is_still_told_to_fetch(self):
        """THE PRESENCE TWIN: with the elevation gone, the gap is real."""
        aid = self.aids[-1]
        self.con.execute("BEGIN")
        try:
            strip_elevation(self.con, aid)
            with use_config(), no_tiles():
                rep = R.build(self.con, aid)
        finally:
            self.con.execute("ROLLBACK")
        self.assertEqual(rep["terrain_coverage"]["status"], "outside_loaded_tiles")
        self.assertTrue(rep["terrain_coverage"]["basis"].startswith("tiles"))
        self.assertEqual(rep["notable"][0]["kind"], "terrain_gap")
        self.assertIn("fetch-tile", R.render(rep))

    def test_stored_dem_without_silver_names_silver_not_a_tile(self):
        aid = self.aids[-1]
        self.con.execute("BEGIN")
        try:
            strip_elevation(self.con, aid, dem=False)
            with no_tiles():
                kept = B.terrain_state(self.con, aid)
            strip_elevation(self.con, aid)
            with no_tiles():
                gone = B.terrain_state(self.con, aid)
        finally:
            self.con.execute("ROLLBACK")
        self.assertEqual(kept["state"], "unpopulated")
        self.assertIn("silver", kept["reason"])
        self.assertNotIn("fetch", kept["reason"])
        # ...and with dem_elevations gone too, the tiles decide, and none loads.
        self.assertEqual(gone["state"], "outside_loaded_tiles")


class TestFetchMissingSkipsStoredElevation(unittest.TestCase):
    """`fetch-tile --missing` opens its own read-only connection, so each
    test builds its own demo database rather than share a writable one."""

    def cells(self, strip_latest: bool = False) -> list[dict]:
        with tempfile.TemporaryDirectory() as d:
            path = build_demo(d)
            if strip_latest:
                con = duckdb.connect(str(path))
                aid = con.execute(
                    "SELECT activity_id FROM activities "
                    "ORDER BY start_time_utc DESC LIMIT 1"
                ).fetchone()[0]
                strip_elevation(con, aid)
                con.close()
            with use_config(), no_tiles(), mock.patch.object(T, "DB_PATH", path):
                return T.corpus_missing_cells()

    def test_the_demo_needs_no_tile(self):
        self.assertEqual(self.cells(), [])

    def test_a_run_without_elevation_contributes_its_cells(self):
        """THE TWIN: the skip is per run, not a blanket no."""
        cells = self.cells(strip_latest=True)
        self.assertTrue(cells)
        self.assertTrue(all(c["n_activities"] == 1 for c in cells))


class TestSegmentCountsAgree(DemoTestCase):
    def test_library_counts_are_the_efforts_the_demo_holds(self):
        rows = self.con.execute("""
            SELECT s.segment_id, s.n_efforts, s.n_ascents, s.n_descents,
                   count(e.segment_id),
                   count(e.segment_id) FILTER (WHERE e.direction = 'ascent'),
                   count(e.segment_id) FILTER (WHERE e.direction = 'descent')
            FROM segments s LEFT JOIN segment_efforts e USING (segment_id)
            GROUP BY ALL""").fetchall()
        # Not vacuous: there are segments, and some were climbed.
        self.assertTrue(rows)
        self.assertTrue(any(r[2] > 0 for r in rows))
        for sid, n, up, down, n_e, up_e, down_e in rows:
            self.assertEqual((n, up, down), (n_e, up_e, down_e), sid)

    def test_segment_history_matches_the_library_and_says_what_it_counts(self):
        sid = self.con.execute("""
            SELECT segment_id FROM segment_efforts GROUP BY 1
            HAVING count(DISTINCT direction) = 2
            ORDER BY count(*) DESC, 1 LIMIT 1""").fetchone()[0]
        with use_config():
            r = segment_history(self.con, sid, "ascent")
        # The demo's default selection holds every run, so the counts agree.
        self.assertEqual(r["n_efforts_in_selection"], r["segment"]["n_ascents"])
        self.assertGreater(r["n_efforts_in_selection"], 0)
        self.assertIn("ALL runs", r["counts"]["segment"])
        self.assertIn("selection", r["counts"]["n_efforts_in_selection"])
        self.assertIn("direction=ascent", r["counts"]["n_efforts_in_selection"])


if __name__ == "__main__":
    unittest.main()
