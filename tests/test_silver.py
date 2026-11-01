"""Milestone 2 acceptance (SPEC.md §12.2).

GATE: resolution invariance. A feature that survives coarsening the DEM is in
the terrain, not in the method. Chosen over agreement with Garmin's profile
because Garmin's SHAPE is corrupted (§5a-bis.3) and it is itself a DEM lookup,
so agreement would be partly circular (§15.1) — a test a correct
implementation cannot pass is worse than no test.

The gate runs twice: on a SYNTHETIC tile (always, so a clean checkout tests
the pipeline), and on the corpus's best-covered run against whatever real
tiles are loaded (skipped when there are none).

BLIND SPOT, stated: this cannot catch a systematic error affecting all
resolutions equally, such as a wrong CRS. Independent absolute anchors (a
surveyed reference level, an independently published profile; §5a-bis.1)
are what cover that.
"""

import tempfile
import unittest
from pathlib import Path

import numpy as np
import tifffile

from tests.corpus import CorpusTestCase, one, use_config
from trail_factors.transform import dem as D
from trail_factors.transform import silver as S

# ARBITRARY GROUND for the synthetic gate: UTM 32N (EPSG:32632).
SYN_EPSG = 32632
SYN_X0, SYN_Y0 = 400_000.0, 5_150_000.0  # start of the synthetic path
SYN_LEN_M = 2_000.0
# Three well-separated Gaussian hills (centre m, half-width m, height m) on a
# flat base, constant across the path: the terrain the gate should recover.
SYN_HILLS = ((500.0, 120.0, 30.0), (1200.0, 150.0, 40.0), (1700.0, 100.0, 25.0))


def _syn_surface(along_m):
    z = np.full_like(along_m, 300.0, dtype=float)
    for c, w, h in SYN_HILLS:
        z += h * np.exp(-(((along_m - c) / w) ** 2))
    return z


class _ResolutionGate:
    """The gate's assertions, shared by the synthetic and the corpus cases.

    Subclasses set `surface` (anything with .elevation(lats, lons)), `lat`,
    `lon`, `cum` and `projection`.
    """

    POSTINGS = (1, 3, 9, 21)

    def hills_at_posting(self, k):
        """Re-sample the same path from a k x k averaged (coarsened) DEM."""
        if k == 1:
            elev = self.surface.elevation(self.lat, self.lon)
        else:
            # One metre in degrees at this latitude, so the averaging kernel
            # is k x k METRES wherever the path is.
            dlat = 1.0 / 111_320.0
            dlon = dlat / np.cos(np.radians(float(np.median(self.lat))))
            offs = np.linspace(-(k // 2), k // 2, k)
            acc = np.zeros(len(self.lat))
            for dx in offs:
                for dy in offs:
                    acc += self.surface.elevation(
                        self.lat + dy * dlat, self.lon + dx * dlon
                    )
            elev = acc / (k * k)
        smooth = S.smooth_by_distance(elev, self.cum)
        return S.detect_hills(smooth, self.cum)

    def test_hill_count_survives_coarsening(self):
        """The same number of hills at 1 m as at 21 m posting."""
        counts = {k: len(self.hills_at_posting(k)) for k in self.POSTINGS}
        self.assertGreater(counts[1], 0, "no hills at all: nothing was tested")
        self.assertEqual(
            len(set(counts.values())),
            1,
            f"hill count varies with DEM posting: {counts} — the "
            f"hills are an artefact of sampling, not terrain",
        )

    def test_hill_positions_survive_coarsening(self):
        """Not just the count: the same hills, in the same places.

        Tolerance is RELATIVE to each hill's own length, derived from
        measurement rather than assumed. A broad summit's exact high point is
        not well defined and shifts under coarsening; measured on the
        development corpus the largest shift across 3/9/21 m postings was
        well under a tenth of the hill's length. The gate is 15%, which leaves
        headroom without admitting a peak that has moved to a different hill.
        """
        base = sorted(self.hills_at_posting(1), key=lambda h: h["peak_distance_m"])
        for k in self.POSTINGS[1:]:
            coarse = sorted(
                self.hills_at_posting(k), key=lambda h: h["peak_distance_m"]
            )
            self.assertEqual(len(base), len(coarse), f"count changed at {k} m")
            for a, b in zip(base, coarse, strict=False):
                shift = abs(a["peak_distance_m"] - b["peak_distance_m"])
                self.assertLess(
                    shift / max(a["length_m"], 1.0),
                    0.15,
                    f"at {k} m posting a peak moved {shift:.0f} m within a "
                    f"{a['length_m']:.0f} m hill — that is a different hill",
                )


class SyntheticResolutionInvarianceTest(_ResolutionGate, unittest.TestCase):
    """THE MILESTONE 2 GATE on a synthetic tile with known hills."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        pad, half_h = 60, 40  # metres, 1 m posting
        cols = np.arange(-pad, SYN_LEN_M + pad, dtype=float)
        grid = np.repeat(_syn_surface(cols)[None, :], 2 * half_h, axis=0)
        ox, oy = SYN_X0 - pad, SYN_Y0 + half_h
        path = Path(cls._tmp.name) / "synthetic.tif"
        tifffile.imwrite(
            path,
            grid.astype(np.float32),
            extratags=[
                (33550, "d", 3, (1.0, 1.0, 0.0), True),
                (33922, "d", 6, (0.0, 0.0, 0.0, ox, oy, 0.0), True),
                (34735, "H", 8, (1, 1, 0, 1, 3072, 0, 1, SYN_EPSG), True),
            ],
        )
        cls.surface = D.DemTile(path)
        along = np.arange(0.0, SYN_LEN_M, 2.0)
        lon, lat = D.transformer(f"EPSG:{SYN_EPSG}", D.WGS84).transform(
            SYN_X0 + along, np.full_like(along, SYN_Y0)
        )
        cls.lat, cls.lon = np.asarray(lat), np.asarray(lon)
        with use_config(projection_crs=f"EPSG:{SYN_EPSG}"):
            cls.cum, _, _ = S.path_distance(cls.lat, cls.lon)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_the_known_hills_are_found(self):
        """THE TWIN of invariance: three hills went in, three come out, each
        near where it was put. A detector that found nothing at every posting
        would be perfectly 'invariant'."""
        hills = sorted(self.hills_at_posting(1), key=lambda h: h["peak_distance_m"])
        self.assertEqual(len(hills), len(SYN_HILLS))
        for h, (c, _, _) in zip(hills, SYN_HILLS, strict=False):
            self.assertLess(abs(h["peak_distance_m"] - c), 25.0)


class CorpusResolutionInvarianceTest(_ResolutionGate, CorpusTestCase):
    """The same gate on real terrain: the corpus's best-covered running
    activity, against the DEM tiles that are loaded."""

    @classmethod
    def setUpClass(cls):
        if not D.loaded_tiles():
            raise unittest.SkipTest("no DEM tile is loaded in the DEM dir")
        super().setUpClass()
        cls.surface = D.TileSet()
        aid = one(
            cls.con,
            """
            SELECT d.activity_id FROM dem_elevations d
            JOIN activities a USING (activity_id)
            WHERE a.sport = 'running'
            GROUP BY 1 ORDER BY count(*) DESC, 1 LIMIT 1""",
            what="running activity with DEM elevations",
        )[0]
        rows = cls.con.execute(
            """
            SELECT r.lat, r.lon FROM records r
            JOIN dem_elevations d USING (activity_id, timestamp)
            WHERE r.activity_id = ? AND r.lat IS NOT NULL
            ORDER BY r.timestamp""",
            [aid],
        ).fetchall()
        cls.lat = np.array([r[0] for r in rows])
        cls.lon = np.array([r[1] for r in rows])
        try:
            cls.surface.elevation(cls.lat, cls.lon)
        except D.OutsideTile as exc:
            raise unittest.SkipTest(
                "the loaded tiles do not cover the best-covered run"
            ) from exc
        zone, north = D.utm_zone_at(
            float(np.median(cls.lat)), float(np.median(cls.lon))
        )
        with use_config(projection_crs=f"EPSG:{(32600 if north else 32700) + zone}"):
            cls.cum, _, _ = S.path_distance(cls.lat, cls.lon)


class SilverInvariantsTest(CorpusTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        one(
            cls.con,
            "SELECT 1 FROM records_enriched LIMIT 1",
            what="silver layer (records_enriched is empty)",
        )

    def test_grade_is_null_not_flagged_outside_usable_blocks(self):
        """NULL fails loudly; a flagged number fails quietly (§12.2)."""
        bad = self.con.execute(
            "SELECT count(*) FROM records_enriched "
            "WHERE NOT terrain_usable AND grade_pct IS NOT NULL"
        ).fetchone()[0]
        self.assertEqual(bad, 0)

    def test_missing_grade_does_not_forfeit_the_record(self):
        """§3.1: HR stays valid where grade is unavailable."""
        n = self.con.execute("""
            SELECT count(*) FROM records_enriched e
            JOIN records r USING (activity_id, timestamp)
            WHERE e.grade_pct IS NULL AND r.heart_rate IS NOT NULL""").fetchone()[0]
        self.assertGreater(n, 0)

    def test_gap_is_null_wherever_grade_is_null(self):
        """GAP inherits the trustworthiness of the grade beneath it."""
        bad = self.con.execute(
            "SELECT count(*) FROM records_enriched "
            "WHERE grade_pct IS NULL AND gap_mps IS NOT NULL"
        ).fetchone()[0]
        self.assertEqual(bad, 0)

    def test_grade_window_is_recorded_in_the_row(self):
        """So a change of window is detectable in the data, not silent."""
        vals = [
            r[0]
            for r in self.con.execute(
                "SELECT DISTINCT grade_window_m FROM records_enriched"
            ).fetchall()
        ]
        self.assertEqual(vals, [S.GRADE_WINDOW_M])

    def test_edge_rule_cost_stays_small(self):
        """Measured small on the development corpus; a regression that lost
        far more would matter."""
        usable, graded = self.con.execute("""
            SELECT count(*) FILTER (WHERE terrain_usable),
                   count(*) FILTER (WHERE grade_pct IS NOT NULL)
            FROM records_enriched""").fetchone()
        self.assertLess((usable - graded) / usable, 0.05)


class GapCurveIsThePopulationOneTest(unittest.TestCase):
    """GAP uses the population Minetti curve, not a personal refit, and says
    so (§6.1). Whether that curve predicts HR better than raw speed is a
    property of the athlete -- someone who walks climbs keeps HR near-flat
    across grade while the curve predicts high effort there -- so it is
    examined in the analysis, not pinned in a test.
    """

    def test_population_minetti_is_recorded_as_not_personal(self):
        """§6.1: the curve in use is the population one, not a refit."""
        import inspect

        from trail_factors.transform import silver

        src = inspect.getsource(silver.minetti_cost)
        self.assertIn("population", src.lower())


if __name__ == "__main__":
    unittest.main()
