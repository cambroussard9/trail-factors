"""fetch-tile works with NO projection configured: the zone travels with the cell.

Coverage labels a run's missing cells in the UTM zone derived from the run's
own positions, so its fetch_command must carry that zone -- a bare cell name
means nothing to fetch-tile without it. A configured projection_crs wins.
No network: only grid resolution and the printed command are exercised.
"""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import duckdb

from tests.corpus import use_config
from trail_factors import config
from trail_factors.cli import terrain as T
from trail_factors.transform import dem as D
from trail_factors.transform import dem_fetch as F

# Synthetic ground in UTM zone 16N (inside NAD83 / UTM's northern zones).
LATS, LONS = [45.20, 45.21], [-87.50, -87.49]
# Synthetic ground in UTM zone 18S. Its NORTHERN mirror -- the same x/y read
# in zone 18N's grid -- is US ground, which is what makes the bug dangerous.
S_LATS, S_LONS = [-45.00, -45.01], [-72.50, -72.49]
# Synthetic ground in zone 34N: northern, but past NAD83 / UTM's zone 23.
E_LATS, E_LONS = [50.00, 50.01], [20.00, 20.01]
# Two central meridians at one latitude: zones 14N and 15N label these with
# the SAME cell name for ground 600 km apart.
Z14_LATS, Z14_LONS = [30.0], [-98.95]
Z15_LATS, Z15_LONS = [30.0], [-92.95]


class TestTheZoneTravelsWithTheCell(unittest.TestCase):
    def test_unset_projection_fetch_command_carries_the_derived_zone(self):
        with use_config():
            m = D.TileSet([]).coverage(LATS, LONS)["missing_tiles"][0]
        self.assertTrue(m["fetch_command"].endswith(" --zone 16"), m["fetch_command"])

    def test_configured_projection_needs_no_zone(self):
        """Positive twin: with a projection set the command stays bare."""
        with use_config(projection_crs="EPSG:26916"):
            m = D.TileSet([]).coverage(LATS, LONS)["missing_tiles"][0]
        self.assertNotIn("--zone", m["fetch_command"])


class TestTheGridResolvesFromTheZone(unittest.TestCase):
    def test_a_zone_names_the_usgs_grid(self):
        with use_config(), F.derived_zone(16, True):
            self.assertEqual(F.grid_crs(), "EPSG:26916")

    def test_no_zone_and_no_projection_is_refused_with_the_fix(self):
        with use_config(), self.assertRaises(config.ConfigError) as cm:
            F.grid_crs()
        self.assertIn("--zone", str(cm.exception))
        self.assertIn("projection_crs", str(cm.exception))

    def test_a_zone_outside_nad83_is_refused_before_any_request(self):
        with use_config(), F.derived_zone(33, True), self.assertRaises(F.NotInTheUS):
            F.grid_crs()

    def test_a_configured_projection_wins_over_the_zone(self):
        with use_config(projection_crs="EPSG:26915"), F.derived_zone(16, True):
            self.assertEqual(F.grid_crs(), "EPSG:26915")

    def test_the_zone_does_not_leak_past_its_block(self):
        with use_config():
            with F.derived_zone(16, True):
                pass
            with self.assertRaises(config.ConfigError):
                F.grid_crs()

    def test_a_zone_without_its_hemisphere_is_refused(self):
        with self.assertRaises(ValueError):
            with F.derived_zone(18, None):
                pass


class TestASouthernZoneHasNoFetchCommand(unittest.TestCase):
    """A SOUTHERN ZONE NUMBER IS NOT A NORTHERN ONE. `--zone 18` for a 18S
    run resolved the cell in zone 18N's grid -- the northern mirror, which
    for this ground is Vermont: inside the US box, so a real tile was
    requested, verified against the same mirrored cell, and kept."""

    def test_derived_southern_zone_gives_no_command_and_says_why(self):
        with use_config():
            cov = D.TileSet([]).coverage(S_LATS, S_LONS)
        m = cov["missing_tiles"][0]
        self.assertIsNone(m["fetch_command"])
        self.assertIn(
            "no USGS grid exists for UTM zone 18S", m["fetch_unavailable_reason"]
        )
        self.assertEqual(cov["crs"], "EPSG:32718")

    def test_configured_southern_projection_gives_no_command(self):
        with use_config(projection_crs="EPSG:32718"):
            m = D.TileSet([]).coverage(S_LATS, S_LONS)["missing_tiles"][0]
        self.assertIsNone(m["fetch_command"])
        self.assertIn("18S", m["fetch_unavailable_reason"])

    def test_northern_zone_past_nad83_gives_no_command(self):
        with use_config():
            m = D.TileSet([]).coverage(E_LATS, E_LONS)["missing_tiles"][0]
        self.assertIsNone(m["fetch_command"])
        self.assertIn("UTM zone 34N", m["fetch_unavailable_reason"])

    def test_a_usgs_zone_carries_no_unavailable_reason(self):
        """Positive twin: a zone with a grid gets a command and no reason."""
        with use_config():
            m = D.TileSet([]).coverage(LATS, LONS)["missing_tiles"][0]
        self.assertIsNotNone(m["fetch_command"])
        self.assertIsNone(m["fetch_unavailable_reason"])

    def test_a_southern_derived_zone_never_names_the_northern_grid(self):
        with (
            use_config(),
            F.derived_zone(18, False),
            mock.patch.object(F, "_open", side_effect=AssertionError("request sent")),
        ):
            with self.assertRaises(F.NotInTheUS) as cm:
                F.grid_crs()
            self.assertIn("18S", str(cm.exception))
            with self.assertRaises(F.NotInTheUS):
                F.resolve(69, 501)


def _missing_cells(lats, lons):
    """corpus_missing_cells' output for one synthetic run, no database."""
    cov = D.TileSet([]).coverage(lats, lons)
    return [
        {
            **m,
            "grid_crs": cov["crs"],
            "projection_configured": cov["projection_configured"],
            "n_activities": 1,
        }
        for m in cov["missing_tiles"]
    ]


class TestFetchMissingCarriesTheHemisphere(unittest.TestCase):
    def test_zone_of_cell_keeps_the_hemisphere(self):
        self.assertEqual(
            T._zone_of_cell({"grid_crs": "EPSG:32718", "projection_configured": False}),
            (18, False),
        )
        self.assertEqual(
            T._zone_of_cell({"grid_crs": "EPSG:26916", "projection_configured": False}),
            (16, True),
        )

    def test_a_southern_cell_is_refused_before_resolve(self):
        with use_config():
            cells = _missing_cells(S_LATS, S_LONS)
        out = io.StringIO()
        with (
            use_config(),
            mock.patch.object(T, "corpus_missing_cells", return_value=cells),
            mock.patch.object(F, "resolve", side_effect=AssertionError("resolved")),
            contextlib.redirect_stdout(out),
        ):
            T.fetch_tile(missing=True)
        self.assertIn("REFUSED", out.getvalue())
        self.assertIn("18S", out.getvalue())

    def test_a_northern_cell_resolves_in_its_own_zone(self):
        """Positive twin: the zone reaches resolve, in the northern grid."""
        with use_config():
            cells = _missing_cells(LATS, LONS)
        seen = []

        def fake_resolve(tx, ty):
            seen.append(F.grid_crs())
            raise F.NoProductCovers("synthetic: nothing covers it")

        with (
            use_config(),
            mock.patch.object(T, "corpus_missing_cells", return_value=cells),
            mock.patch.object(F, "resolve", side_effect=fake_resolve),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            T.fetch_tile(missing=True)
        self.assertEqual(seen, ["EPSG:26916"])


class TestDemKeysMissingCellsByGrid(unittest.TestCase):
    """`dem` keyed the tally by cell name alone, so the same name labelled in
    two derived zones -- two pieces of ground -- printed as ONE cell needed by
    two activities, with a bare `fetch-tile <cell>` that has no zone."""

    def test_same_name_in_two_zones_is_two_cells_each_with_its_command(self):
        outside = {}
        with use_config():
            c14 = D.TileSet([]).coverage(Z14_LATS, Z14_LONS)
            c15 = D.TileSet([]).coverage(Z15_LATS, Z15_LONS)
            self.assertEqual(
                c14["missing_tiles"][0]["usgs_cell"],
                c15["missing_tiles"][0]["usgs_cell"],
            )
            T.tally_missing(outside, c14)
            T.tally_missing(outside, c15)
        self.assertEqual(len(outside), 2)
        text = "\n".join(T.missing_tile_lines(outside))
        self.assertIn(c14["missing_tiles"][0]["fetch_command"], text)
        self.assertIn(c15["missing_tiles"][0]["fetch_command"], text)
        self.assertIn("--zone 14", text)
        self.assertIn("--zone 15", text)
        self.assertNotIn("<cell>", text)

    def test_one_cell_twice_is_counted_twice(self):
        """Positive twin: the same ground from two runs still merges."""
        outside = {}
        with use_config():
            for _ in range(2):
                T.tally_missing(outside, D.TileSet([]).coverage(LATS, LONS))
        self.assertEqual([e["n_activities"] for e in outside.values()], [2])

    def test_dem_command_prints_each_cells_command(self):
        """End to end through `dem`, on a throwaway database: two runs in
        zones 14N and 15N plus one in 18S, none covered by any tile."""
        runs = {
            "a14": (Z14_LATS, Z14_LONS),
            "a15": (Z15_LATS, Z15_LONS),
            "a18s": (S_LATS, S_LONS),
        }

        class NoTiles(D.TileSet):
            def __init__(self):
                super().__init__([])

            def __bool__(self):
                return True

            def elevation(self, lats, lons, with_tiles=False):
                raise D.OutsideTile("synthetic: no tile")

        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.duckdb"
            con = duckdb.connect(str(db))
            con.execute("CREATE TABLE activities (activity_id VARCHAR, sport VARCHAR)")
            con.execute(
                "CREATE TABLE records (activity_id VARCHAR, "
                "timestamp TIMESTAMP, lat DOUBLE, lon DOUBLE)"
            )
            con.execute(
                "CREATE TABLE dem_elevations (activity_id VARCHAR, "
                "timestamp TIMESTAMP, altitude_dem_m DOUBLE, "
                "tile VARCHAR, calc_version INTEGER)"
            )
            for aid, (lats, lons) in runs.items():
                con.execute("INSERT INTO activities VALUES (?, 'running')", [aid])
                for i, (la, lo) in enumerate(zip(lats, lons, strict=False)):
                    con.execute(
                        "INSERT INTO records VALUES (?, TIMESTAMP "
                        "'2026-01-01 00:00:00' + to_seconds(?), ?, ?)",
                        [aid, i, la, lo],
                    )
            con.close()
            out = io.StringIO()
            with (
                use_config(),
                mock.patch.object(T, "DB_PATH", db),
                mock.patch.object(D, "TileSet", NoTiles),
                contextlib.redirect_stdout(out),
            ):
                T.dem_populate()
        text = out.getvalue()
        self.assertIn("--zone 14", text)
        self.assertIn("--zone 15", text)
        self.assertIn("no USGS grid exists for UTM zone 18S", text)
        with use_config():
            s_cell = D.TileSet([]).coverage(S_LATS, S_LONS)["missing_tiles"][0][
                "usgs_cell"
            ]
        self.assertNotIn(f"fetch-tile {s_cell}", text)
        self.assertNotIn("fetch-tile <cell>", text)
        self.assertIn("dem_elevations: 0 rows", text)


if __name__ == "__main__":
    unittest.main()
