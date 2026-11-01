"""The terrain projection: CONFIGURED when set, otherwise DERIVED per run and
labelled -- and the zone is part of segment identity.

Synthetic positions only. Ground near lat 46.5, lon 7.5 sits in UTM zone 32N;
the "other zone" fixture is the point whose zone-33N coordinates are
numerically identical to that point's zone-32N ones, about six degrees east.
"""

import hashlib
import json
import math
import unittest

import numpy as np

from trail_factors import config
from trail_factors.transform import dem, matcher, silver
from tests.corpus import CorpusTestCase, needs_corpus, one, use_config

LATS, LONS = [46.50, 46.51], [7.50, 7.50]
ZONE_32N, ZONE_33N = "EPSG:32632", "EPSG:32633"


def _run_around(lat, lon, n=40, north_offset_deg=0.0):
    """A synthetic run: a short north-south line through (lat, lon), centred
    `north_offset_deg` north of it (so the run's median can sit away from the
    one point two runs share)."""
    lats = lat + north_offset_deg + np.linspace(-0.003, 0.003, n)
    lats = np.append(lats, lat)                 # the run passes the point
    lons = np.full(lats.shape, lon)
    return lats, lons


def _traversal_at(lat, lon, run_lats, run_lons, *, sort_key):
    """The traversal load_traversals() would build for a peak at (lat, lon)
    on a run with positions (run_lats, run_lons): projected in THAT RUN's
    resolved projection, carrying its key."""
    proj = dem.resolve_projection(run_lats, run_lons)
    x, y = matcher.to_utm([lat], [lon], proj["crs"])
    x, y = float(x[0]), float(y[0])
    return {
        "activity_id": f"act{sort_key}", "slope_index": 0,
        "direction": "ascent",
        "lo_x": x + 40, "lo_y": y, "hi_x": x, "hi_y": y,
        "mid_x": x + 20, "mid_y": y,
        "place_x": x, "place_y": y, "place_elevation_m": 900.0,
        "side_bearing_deg": 90.0,
        "low_lat": lat, "low_lon": lon, "high_lat": lat, "high_lon": lon,
        "projection": proj["crs"],
        "projection_derived": not proj["configured"],
        "projection_basis": proj["basis"],
        "low_elevation_m": 888.0, "high_elevation_m": 900.0,
        "vertical_m": 12.0, "length_m": 200.0, "mean_grade_pct": 6.0,
        "start_distance_m": 0.0, "start_utc": None, "end_utc": None,
        "duration_s": 120.0, "vertical_speed_mps": 0.1, "avg_hr": None,
        "sort_key": sort_key,
    }


def _legacy_id(x, y, side, z):
    """The id formula as it stood before derivation existed -- pinned
    literally, so a configured corpus can be shown to keep its ids."""
    return hashlib.sha256(
        f"{round(x)}:{round(y)}:{round(side)}:{round(z)}".encode()
    ).hexdigest()[:12]


class TestUnsetProjectionIsDerivedAndLabelled(unittest.TestCase):

    def test_the_zone_comes_from_the_positions_and_says_so(self):
        with use_config():
            p = dem.resolve_projection(LATS, LONS)
        self.assertEqual(p["crs"], ZONE_32N)
        self.assertFalse(p["configured"])
        self.assertEqual(p["basis"],
                         "derived from this run's position: UTM zone 32N")

    def test_southern_hemisphere_is_the_327NN_code(self):
        with use_config():
            p = dem.resolve_projection([-33.9], [18.4])
        self.assertEqual(p["crs"], "EPSG:32734")
        self.assertTrue(p["basis"].endswith("UTM zone 34S"), p["basis"])

    def test_path_distance_needs_no_setting_and_matches_the_configured_zone(self):
        """Unset is no longer a refusal: the derived zone gives the SAME
        metres as configuring that zone by hand."""
        with use_config():
            d0, x0, _ = silver.path_distance(LATS, LONS)
        with use_config(projection_crs=ZONE_32N):
            d1, x1, _ = silver.path_distance(LATS, LONS)
        np.testing.assert_allclose(d0, d1)
        np.testing.assert_allclose(x0, x1)
        self.assertTrue(1100 < float(d0[-1]) < 1125, float(d0[-1]))

    def test_the_zone_is_the_runs_median_not_each_points(self):
        """A run that strays over a zone line stays in ONE frame: every point
        is projected in the zone of the run's median, so the stray point's
        x is continuous with its neighbours rather than ~500 km away."""
        lats = [46.50] * 9 + [46.50]
        lons = [11.99] * 9 + [12.01]            # last point is in zone 33
        with use_config():
            p = dem.resolve_projection(lats, lons)
            x, _ = matcher.to_utm(lats, lons)
        self.assertEqual(p["crs"], ZONE_32N)
        self.assertLess(abs(x[-1] - x[0]), 5_000)

    def test_an_explicit_crs_is_used_whatever_the_positions(self):
        """Callers projecting a FEW of a run's points pass the run's CRS."""
        with use_config():
            x, _ = matcher.to_utm(LATS, LONS, ZONE_33N)
        ref, _ = dem.transformer(dem.WGS84, ZONE_33N).transform(LONS[0], LATS[0])
        self.assertAlmostEqual(float(x[0]), float(ref), places=6)

    def test_unplaceable_positions_are_the_one_remaining_error(self):
        with use_config(), self.assertRaises(dem.Unplaceable) as cm:
            dem.resolve_projection([float("nan")], [float("nan")])
        # A ConfigError whose text starts with the field, so the existing
        # "name the missing setting" handlers describe it correctly.
        self.assertIsInstance(cm.exception, config.ConfigError)
        self.assertTrue(str(cm.exception).startswith("projection_crs"))

    def test_no_positions_projects_to_nothing_without_error(self):
        with use_config():
            x, y = dem.project([], [])
        self.assertEqual((x.size, y.size), (0, 0))

    def test_utm_zone_at(self):
        self.assertEqual(dem.utm_zone_at(46.5, 7.5), (32, True))
        self.assertEqual(dem.utm_zone_at(-33.9, 18.4), (34, False))

    def test_coverage_carries_the_projection_and_its_basis(self):
        with use_config():
            cov = dem.TileSet([]).coverage(LATS, LONS)
        self.assertEqual(cov["projection"],
                         {"crs": ZONE_32N,
                          "basis": "derived from this run's position: "
                                   "UTM zone 32N"})
        self.assertIs(cov["projection_configured"], False)
        # Still a cell, never a position (§5a).
        blob = json.dumps(cov)
        for banned in ('"lat"', '"lon"', "latitude", "longitude"):
            self.assertNotIn(banned, blob)


class TestConfiguredProjectionWins(unittest.TestCase):
    """With projection_crs set, behaviour and segment ids are unchanged."""

    def test_path_distance_in_metres(self):
        with use_config(projection_crs=ZONE_32N):
            d, x, y = silver.path_distance(LATS, LONS)
        self.assertTrue(300_000 < float(x[0]) < 700_000)   # a UTM easting
        self.assertAlmostEqual(float(d[0]), 0.0)
        # 0.01 deg of latitude is ~1,112 m.
        self.assertTrue(1100 < float(d[-1]) < 1125, float(d[-1]))

    def test_configured_beats_the_zone_the_positions_sit_in(self):
        """The positions are in 32N; the setting says 33N; the setting wins."""
        with use_config(projection_crs=ZONE_33N):
            p = dem.resolve_projection(LATS, LONS)
            x, _ = matcher.to_utm(LATS, LONS)
        self.assertEqual(p, {"crs": ZONE_33N, "basis": "configured",
                             "configured": True})
        ref, _ = dem.transformer(dem.WGS84, ZONE_33N).transform(LONS[0], LATS[0])
        self.assertAlmostEqual(float(x[0]), float(ref), places=6)

    def test_coverage_says_configured(self):
        with use_config(projection_crs=ZONE_32N):
            cov = dem.TileSet([]).coverage(LATS, LONS)
        self.assertEqual(cov["projection"],
                         {"crs": ZONE_32N, "basis": "configured"})

    def test_a_different_projection_is_not_served_from_a_stale_cache(self):
        with use_config(projection_crs=ZONE_32N):
            a = matcher.to_utm(LATS, LONS)
        with use_config(projection_crs=ZONE_33N):
            b = matcher.to_utm(LATS, LONS)
        self.assertNotAlmostEqual(float(a[0][0]), float(b[0][0]), places=0)

    def test_segment_ids_are_the_pre_derivation_ids(self):
        """THE PIN: configuring projection_crs keeps every id exactly as it
        was built before derivation -- with the new traversal keys present
        (as load_traversals now writes them) and with them absent."""
        run_lats, run_lons = _run_around(46.5, 7.5)
        with use_config(projection_crs=ZONE_32N):
            t = _traversal_at(46.5, 7.5, run_lats, run_lons, sort_key=1.0)
        self.assertFalse(t["projection_derived"])
        bare = {k: v for k, v in t.items()
                if not k.startswith("projection")}
        expected = _legacy_id(t["place_x"], t["place_y"],
                              round(t["side_bearing_deg"] / matcher.SIDE_TOL_DEG),
                              0)
        for tr in (t, bare):
            [seg] = matcher.build_library([tr])
            self.assertEqual(seg["segment_id"], expected)


class TestTheZoneIsPartOfSegmentIdentity(unittest.TestCase):

    def test_two_runs_a_kilometre_apart_in_one_zone_still_match(self):
        """THE POSITIVE TWIN. Two runs whose medians sit ~1 km apart derive
        the same zone, so a place both cross is one segment."""
        a_lats, a_lons = _run_around(46.5, 7.5)
        b_lats, b_lons = _run_around(46.5, 7.5, north_offset_deg=0.009)
        self.assertGreater(abs(np.median(a_lats) - np.median(b_lats)), 0.008)
        with use_config():
            a = _traversal_at(46.5, 7.5, a_lats, a_lons, sort_key=1.0)
            b = _traversal_at(46.5, 7.5, b_lats, b_lons, sort_key=2.0)
        self.assertEqual(a["projection"], b["projection"])
        self.assertTrue(a["projection_derived"])
        self.assertTrue(matcher.compatible(a, b))
        segs = matcher.build_library([a, b])
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0]["n_efforts"], 2)
        self.assertEqual(segs[0]["projection"], ZONE_32N)

    def test_identical_coordinates_in_different_zones_never_match(self):
        """The failure derivation could introduce: a place in 32N and a place
        ~6 degrees east whose 33N coordinates are the SAME numbers. Position,
        elevation and side all agree; only the frame tells them apart."""
        lat1, lon1 = 46.5, 7.5
        x, y = dem.transformer(dem.WGS84, ZONE_32N).transform(lon1, lat1)
        lon2, lat2 = dem.transformer(ZONE_33N, dem.WGS84).transform(x, y)
        self.assertEqual(dem.utm_zone_at(lat2, lon2), (33, True))
        r1 = _run_around(lat1, lon1)
        r2 = _run_around(lat2, lon2)
        with use_config():
            a = _traversal_at(lat1, lon1, *r1, sort_key=1.0)
            b = _traversal_at(lat2, lon2, *r2, sort_key=2.0)
        self.assertEqual((a["projection"], b["projection"]),
                         (ZONE_32N, ZONE_33N))
        self.assertAlmostEqual(
            math.hypot(a["place_x"] - b["place_x"],
                       a["place_y"] - b["place_y"]), 0.0, places=3)
        self.assertFalse(matcher.compatible(a, b))
        self.assertFalse(matcher.compatible(b, a))
        segs = matcher.build_library([a, b])
        self.assertEqual(len(segs), 2)
        self.assertEqual(len({s["segment_id"] for s in segs}), 2,
                         "identical rounded coordinates must not share an id")
        lib = matcher.build_library([a])
        self.assertIsNone(matcher.match_traversal(b, lib))

    def test_a_derived_id_carries_its_zone(self):
        base = matcher.segment_id_for(500_000.4, 5_150_000.6, 1, 0)
        z32 = matcher.segment_id_for(500_000.4, 5_150_000.6, 1, 0,
                                     projection=ZONE_32N)
        z33 = matcher.segment_id_for(500_000.4, 5_150_000.6, 1, 0,
                                     projection=ZONE_33N)
        self.assertEqual(base, _legacy_id(500_000.4, 5_150_000.6, 1, 0))
        self.assertEqual(len({base, z32, z33}), 3)


class TestARunsProjectionFromTheCorpus(CorpusTestCase):
    """run_projection() reads the run's own stored positions. Asserts the
    LABEL, never a zone: the corpus is someone's real data."""

    def test_unset_is_derived_and_configured_wins(self):
        aid = one(self.con, """
            SELECT activity_id FROM records WHERE lat IS NOT NULL LIMIT 1""",
                  what="positioned record")[0]
        with use_config():
            p = dem.run_projection(self.con, aid)
        self.assertFalse(p["configured"])
        self.assertTrue(p["basis"].startswith(
            "derived from this run's position: UTM zone "), p["basis"])
        with use_config(projection_crs=ZONE_32N):
            q = dem.run_projection(self.con, aid)
        self.assertEqual(q, {"crs": ZONE_32N, "basis": "configured",
                             "configured": True})


class TestTerrainPairingNeedsNoSetting(CorpusTestCase):
    """§6.7's record pairing projects positions; unset, it now derives."""

    def test_matched_terrain_drift_runs_unconfigured(self):
        from trail_factors.analysis import primitives as P
        aid = one(self.con, """
            SELECT activity_id FROM records_enriched
            GROUP BY 1 ORDER BY count(*) DESC, 1 LIMIT 1""",
                  what="run with enriched records")[0]
        with use_config():
            got = P.matched_terrain_drift(self.con, aid)
            zone = dem.run_projection(self.con, aid)["crs"]
        # Deriving the zone gives the same pairs as configuring that zone.
        with use_config(projection_crs=zone):
            same = P.matched_terrain_drift(self.con, aid)
        self.assertEqual((got["n"], got["value"]), (same["n"], same["value"]))


@needs_corpus
class TestTheMcpEnvelopeNeedsNoSetting(unittest.TestCase):
    """With no projection configured a terrain tool ANSWERS: the run's zone
    is derived, so there is no missing setting to refuse over."""

    def test_unset_projection_is_not_a_refusal(self):
        from trail_factors import mcp_server as M
        with use_config():
            env = json.loads(M.matched_terrain_drift(last_n=2))
        self.assertEqual(env["outcome"], "result", env)
        self.assertNotIn("projection_crs is not set", json.dumps(env))


if __name__ == "__main__":
    unittest.main()
