"""session_series: a bin with heart-rate records keeps avg_hr even when it
has fewer than two distance records; only distance-derived fields go null.

In-memory database. Bin 0 (0-29 s) is normal; bin 1 (30-59 s) has HR on every
record but a distance on only ONE; bin 3 has no records at all.

The same fixture also carries DEM elevation, which is what pins the ROUTE
FINGERPRINT GATE: elevation indexed by distance identifies a known trail, so
it is emitted only when the athlete config says `route_fingerprint_accepted`.
Both settings are tested, each the other's twin.
"""

import datetime as dt
import unittest

import duckdb

from trail_factors.analysis.series import binned_series
from trail_factors.ingest.schema import DDL
from tests.corpus import use_config

T0 = dt.datetime(2025, 1, 15, 7, 0, 0)        # synthetic
DEM_M = 500.0                                  # synthetic elevation


def _fixture():
    """One synthetic activity, DEM-covered, in a fresh in-memory database."""
    con = duckdb.connect(":memory:")
    con.execute(DDL)
    con.execute(
        "INSERT INTO activities (activity_id, run_id, file_hash, source_file,"
        " start_time_utc, source, sampling_regime, pipeline_version, sport)"
        " VALUES ('a1','r1','h1','x.fit',?,'garmin_watch','1hz',1,'running')",
        [T0])
    recs = []
    for t in range(0, 60):
        has_d = t < 30 or t == 45           # bin 1: ONE distance record
        recs.append(("a1", T0 + dt.timedelta(seconds=t), float(t),
                     3.0 * t if has_d else None, 140 + (t >= 30) * 10))
    recs.append(("a1", T0 + dt.timedelta(seconds=120), 120.0, 360.0, 150))
    con.executemany(
        "INSERT INTO records (activity_id, timestamp, elapsed_s, "
        "distance_m, heart_rate) VALUES (?,?,?,?,?)", recs)
    con.executemany(
        "INSERT INTO records_enriched (activity_id, timestamp, elapsed_s, "
        "altitude_dem_m, terrain_usable, quality_flags, calc_version) "
        "VALUES (?,?,?,?,TRUE,'',1)",
        [(r[0], r[1], r[2], DEM_M + r[2] / 10.0) for r in recs])
    return con


class TestNullDistanceBin(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        con = _fixture()
        try:
            cls.bins = {b["bin"]: b for b in binned_series(
                con, "a1", include_dynamics=False)["bins"]}
        finally:
            con.close()

    def test_hr_survives_a_bin_with_one_distance_record(self):
        b = self.bins[1]
        self.assertAlmostEqual(b["avg_hr"], 150.0, places=6)
        self.assertEqual(b["n_records"], 30)
        self.assertGreater(b["coverage"], 0.9)

    def test_only_distance_derived_fields_are_null_and_say_why(self):
        b = self.bins[1]
        for f in ("distance_start_m", "distance_end_m", "mps", "pace_per_mile"):
            self.assertIsNone(b[f], f)
        self.assertIn("1 record(s)", b["distance_reason"])

    def test_positive_twin_a_normal_bin_has_distance_and_pace(self):
        b = self.bins[0]
        self.assertIsNotNone(b["mps"])
        self.assertAlmostEqual(b["avg_hr"], 140.0, places=6)
        self.assertNotIn("distance_reason", b)

    def test_a_bin_with_no_records_is_null_throughout(self):
        b = self.bins[3]
        self.assertIsNone(b["avg_hr"])
        self.assertEqual(b["n_records"], 0)


class TestTheRouteFingerprintIsGatedOnConfig(unittest.TestCase):
    """Elevation by distance is a route fingerprint. It is emitted only when
    the config accepts it, and the payload says which case applies."""

    def series(self, accepted: bool) -> dict:
        con = _fixture()
        try:
            with use_config(route_fingerprint_accepted=accepted):
                return binned_series(con, "a1", include_dynamics=False)
        finally:
            con.close()

    def test_not_accepted_withholds_elevation_from_every_bin_and_says_so(self):
        r = self.series(False)
        self.assertTrue(r["elevation_available"])      # the DEM covers it...
        self.assertFalse(r["elevation_emitted"])       # ...and it is withheld
        self.assertTrue(r["elevation_withheld_reason"])
        self.assertIn("route_fingerprint_accepted",
                      r["elevation_withheld_reason"])
        self.assertTrue(all(b["elevation_m"] is None for b in r["bins"]))
        self.assertIs(r["filters"]["route_fingerprint_accepted"], False)
        self.assertIs(r["filters"]["position_emitted"], False)

    def test_positive_twin_accepted_emits_elevation(self):
        r = self.series(True)
        self.assertTrue(r["elevation_emitted"])
        self.assertIsNone(r["elevation_withheld_reason"])
        elev = [b["elevation_m"] for b in r["bins"] if b["elevation_m"] is not None]
        self.assertTrue(elev)
        for e in elev:
            self.assertGreaterEqual(e, DEM_M)
        self.assertIs(r["filters"]["route_fingerprint_accepted"], True)
        # Position is never emitted, whatever the fingerprint setting.
        self.assertIs(r["filters"]["position_emitted"], False)

    def test_the_gate_changes_only_elevation(self):
        strip = lambda bins: [{k: v for k, v in b.items() if k != "elevation_m"}
                              for b in bins]
        self.assertEqual(strip(self.series(False)["bins"]),
                         strip(self.series(True)["bins"]))


if __name__ == "__main__":
    unittest.main()
