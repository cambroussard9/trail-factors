"""session_series: running dynamics per bin are OPT-IN (include_dynamics).

The per-bin dynamics block is the bulk of the payload, so the default omits
it from every bin and says so. Read-only against the corpus named by
TRAIL_FACTORS_DB; the subject is any watch run that carries running-dynamics
records, found by that property.

The route-fingerprint gate is checked here on a real DEM-covered run as well
(its hand-checkable version is in tests/test_series_null_distance.py).
"""

import json
import unittest

from trail_factors import mcp_server as M
from tests.corpus import CorpusTestCase, one, use_config

DYNAMICS_RUN = """
    SELECT a.run_id FROM activities a
    WHERE a.source = 'garmin_watch' AND EXISTS (
        SELECT 1 FROM records r WHERE r.activity_id = a.activity_id
          AND r.stance_time IS NOT NULL)
    ORDER BY a.start_time_utc LIMIT 1"""

DEM_RUN = """
    SELECT a.run_id FROM activities a
    WHERE a.source = 'garmin_watch' AND a.sport = 'running' AND EXISTS (
        SELECT 1 FROM records_enriched e WHERE e.activity_id = a.activity_id
          AND e.altitude_dem_m IS NOT NULL)
    ORDER BY a.start_time_utc LIMIT 1"""


class TestDynamicsAreOptIn(CorpusTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        run = one(cls.con, DYNAMICS_RUN,
                  what="watch run with running-dynamics records")[0]
        cls.raw_default = M.session_series(run=run)
        cls.raw_full = M.session_series(run=run, include_dynamics=True)
        cls.default = json.loads(cls.raw_default)["payload"]
        cls.full = json.loads(cls.raw_full)["payload"]

    def test_the_default_omits_them_from_every_bin_and_says_so(self):
        self.assertFalse(self.default["dynamics_included"])
        self.assertIn("include_dynamics=true",
                      self.default["dynamics_included_note"])
        self.assertFalse([b for b in self.default["bins"] if "dynamics" in b])

    def test_availability_is_still_reported_without_them(self):
        self.assertTrue(self.full["dynamics_available"])
        self.assertEqual(self.default["dynamics_available"],
                         self.full["dynamics_available"])

    def test_positive_twin_opt_in_carries_them_on_every_bin(self):
        self.assertTrue(self.full["dynamics_included"])
        self.assertTrue(self.full["bins"])
        self.assertTrue(all("dynamics" in b for b in self.full["bins"]))

    def test_everything_else_is_identical(self):
        strip = lambda b: {k: v for k, v in b.items()
                           if k not in ("dynamics", "dynamics_reason")}
        self.assertEqual([strip(b) for b in self.default["bins"]],
                         [strip(b) for b in self.full["bins"]])

    def test_the_default_response_is_smaller(self):
        self.assertLess(len(self.raw_default), len(self.raw_full) * 0.8)

    def test_the_null_distance_bin_case_is_documented(self):
        doc = M.session_series.__doc__
        self.assertIn("NULL-DISTANCE BINS", doc)
        self.assertIn("DISTANCE-\n    DERIVED fields null", doc)


class TestTheRouteFingerprintGateOnARealRun(CorpusTestCase):
    """Both config settings on one DEM-covered run, each the other's twin."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.run_id = one(cls.con, DEM_RUN, what="DEM-covered watch running run")[0]

    def payload(self, accepted: bool) -> dict:
        with use_config(route_fingerprint_accepted=accepted):
            env = json.loads(M.session_series(run=self.run_id))
        self.assertEqual(env["outcome"], "result", env.get("text"))
        return env["payload"]

    def test_not_accepted_withholds_every_bins_elevation(self):
        p = self.payload(False)
        self.assertTrue(p["elevation_available"])
        self.assertFalse(p["elevation_emitted"])
        self.assertTrue(p["elevation_withheld_reason"])
        self.assertTrue(all(b["elevation_m"] is None for b in p["bins"]))
        self.assertIs(p["filters"]["route_fingerprint_accepted"], False)
        self.assertIs(p["filters"]["position_emitted"], False)

    def test_positive_twin_accepted_emits_it(self):
        p = self.payload(True)
        self.assertTrue(p["elevation_emitted"])
        self.assertTrue(any(b["elevation_m"] is not None for b in p["bins"]))
        self.assertIs(p["filters"]["route_fingerprint_accepted"], True)
        self.assertIs(p["filters"]["position_emitted"], False)


if __name__ == "__main__":
    unittest.main()
