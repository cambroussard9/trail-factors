"""A missing tile with NO fetch command is reported without describing a fetch.

Ground in a southern UTM zone, or a northern zone past NAD83's, has no USGS
grid, so coverage gives it no fetch_command. The report must neither crash on
the None nor tell the reader that a download will resolve and verify a tile.
"""

import unittest

from trail_factors.analysis import report as R


def _rep(cells):
    return {"terrain_coverage": {
        "status": "outside_loaded_tiles", "n_uncovered": 10, "n_points": 10,
        "fraction_covered": 0.0, "missing_tiles": cells},
        "flags": [], "primitives": []}


def _cell(cmd, reason=None):
    return {"usgs_cell": "x38y516", "easting_range": [380000, 390000],
            "northing_range": [5150000, 5160000], "n_points": 10,
            "fetch_command": cmd, "fetch_unavailable_reason": reason}


def _gap_text(rep):
    [item] = [i for i in R.notable(rep) if i["kind"] == "terrain_gap"]
    return item["text"]


class TestTheFixSentenceIsTrueOfTheGround(unittest.TestCase):
    def test_no_command_says_no_tile_can_be_fetched(self):
        text = _gap_text(_rep([_cell(None, "no USGS grid exists for UTM zone 32N")]))
        self.assertIn("NO TILE CAN BE FETCHED", text)
        self.assertIn("zone 32N", text)
        self.assertNotIn("3DEP index", text)
        self.assertNotIn("None", text)

    def test_positive_twin_a_command_is_given_and_explained(self):
        cmd = "uv run trail-factors fetch-tile x38y516 --zone 16"
        text = _gap_text(_rep([_cell(cmd)]))
        self.assertIn(f"TO FIX: {cmd}", text)
        self.assertIn("3DEP index", text)


if __name__ == "__main__":
    unittest.main()
