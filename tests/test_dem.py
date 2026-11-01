"""DEM lookup guards (SPEC.md §5a).

What they pin is the behaviour that must never regress: a coordinate outside
the tile FAILS LOUDLY rather than returning NoData that gets silently
smoothed. The tile is SYNTHETIC -- a small, genuinely georeferenced GeoTIFF
written to a temporary directory -- so these run in a clean checkout; a real
USGS tile is ~430 MB and gitignored.
"""

import tempfile
import unittest
from pathlib import Path

import numpy as np
import tifffile

from trail_factors.transform.dem import DemTile, OutsideTile, transformer, WGS84
from tests.corpus import CorpusTestCase, one

# ARBITRARY GROUND, NOTHING TO DO WITH ANY ATHLETE: UTM 32N around 46.5 N,
# 7.5 E. The tile's top-left corner is placed so that this point sits well
# inside it.
EPSG = 32632
LAT, LON = 46.5, 7.5
SIZE = 200                      # pixels per side, 1 m posting


def _plane(col, row):
    """The synthetic surface: a tilted plane, so bilinear interpolation is
    EXACT and any pixel-indexing slip shows up as a wrong elevation."""
    return 100.0 + 0.5 * col + 0.25 * row


class DemTileTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        x, y = transformer(WGS84, f"EPSG:{EPSG}").transform(LON, LAT)
        # Put the point at pixel (col, row) = (80.25, 120.75), off-centre and
        # off-grid, so a transposed axis or a half-pixel shift cannot pass.
        cls.col, cls.row = 80.25, 120.75
        ox, oy = x - cls.col, y + cls.row
        rows, cols = np.mgrid[0:SIZE, 0:SIZE]
        grid = _plane(cols, rows).astype(np.float32)
        path = Path(cls._tmp.name) / "synthetic.tif"
        tifffile.imwrite(path, grid, extratags=[
            (33550, "d", 3, (1.0, 1.0, 0.0), True),
            (33922, "d", 6, (0.0, 0.0, 0.0, ox, oy, 0.0), True),
            (34735, "H", 8, (1, 1, 0, 1, 3072, 0, 1, EPSG), True)])
        cls.tile = DemTile(path)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_a_point_inside_reads_the_interpolated_surface(self):
        """Bilinear on a plane is exact: the lookup must land on the pixel the
        georeferencing says, to well under a metre."""
        v = self.tile.elevation([LAT], [LON])[0]
        self.assertAlmostEqual(v, _plane(self.col, self.row), delta=0.01)

    def test_the_tile_reads_its_own_crs(self):
        self.assertEqual(self.tile.crs, f"EPSG:{EPSG}")

    def test_point_outside_tile_raises_not_nodata(self):
        """A miss must be loud. Silent NoData would be smoothed over (§5a)."""
        with self.assertRaises(OutsideTile):
            self.tile.elevation([LAT + 1.0], [LON + 1.0])

    def test_partial_miss_still_raises(self):
        """One bad point in a good batch must not be quietly dropped."""
        with self.assertRaises(OutsideTile):
            self.tile.elevation([LAT, LAT + 1.0], [LON, LON + 1.0])

    def test_a_tile_with_no_path_is_refused(self):
        """There is no default tile: a caller must say which one."""
        with self.assertRaises(FileNotFoundError):
            DemTile(Path(self._tmp.name) / "absent.tif")


class DemReliefTest(CorpusTestCase):

    def test_dem_relief_is_far_below_gps_relief(self):
        """The headline §5a-bis finding: GPS-derived altitude inflates relief.

        Subject: the DEM-covered run with GPS-derived (not barometric) device
        altitude that has the most DEM samples -- any corpus's, by property.
        """
        aid = one(self.con, """
            SELECT d.activity_id FROM dem_elevations d
            JOIN activities a USING (activity_id)
            WHERE a.altitude_source = 'gps' AND a.source = 'garmin_watch'
            GROUP BY 1 ORDER BY count(*) DESC LIMIT 1""",
                  what="DEM-covered run with GPS-derived device altitude")[0]
        dem_relief, gps_relief = self.con.execute("""
            SELECT max(d.altitude_dem_m) - min(d.altitude_dem_m),
                   max(r.altitude_device_m) - min(r.altitude_device_m)
            FROM dem_elevations d JOIN records r USING (activity_id, timestamp)
            WHERE d.activity_id = ?""", [aid]).fetchone()
        self.assertGreater(dem_relief, 0)
        self.assertLess(dem_relief, gps_relief * 0.7,
                        "GPS relief should far exceed DEM relief")


if __name__ == "__main__":
    unittest.main()
