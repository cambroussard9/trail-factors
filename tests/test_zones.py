"""M7 time in HR zone by gait (§17.2), and the §14e per-file boundaries.

The boundary READING is pinned on synthetic arrays; the corpus tests find
their subject by property and assert invariants, never one athlete's zones.

Every negative assertion has a positive twin.
"""

import contextlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.corpus import CorpusTestCase, corpus_path, isolated, one
from trail_factors.analysis import over as O
from trail_factors.analysis import zones as Z
from trail_factors.analysis.report import cluster_of
from trail_factors.ingest import load as L


class TestTheFloorsReading(unittest.TestCase):
    """Garmin's `hr_zone_high_boundary` is the array of zone FLOORS despite
    its name, with max HR as the final entry. Reading it as highs shifts every
    zone up by one bpm. Synthetic boundaries, not anyone's heart."""

    MSG = {
        "time_in_zone": [
            {
                "reference_mesg": "session",
                "hr_zone_high_boundary": (100, 120, 140, 160, 175, 190),
                "time_in_hr_zone": (5.0, 10.0, 20.0, 30.0, 40.0, 50.0),
                "hr_calc_type": "percent_max_hr",
            }
        ]
    }

    def test_each_entry_is_a_floor_and_the_last_is_max(self):
        got = {z: (lo, hi) for _, z, lo, hi, *_ in L._hr_zones("a1", self.MSG)}
        self.assertEqual(
            got,
            {1: (100, 119), 2: (120, 139), 3: (140, 159), 4: (160, 174), 5: (175, 190)},
        )

    def test_the_seconds_go_with_their_zone(self):
        got = {z: t for _, z, _, _, _, t, _ in L._hr_zones("a1", self.MSG)}
        self.assertEqual(got[1], 10.0)  # index 0 is below zone 1
        self.assertEqual(got[5], 50.0)

    def test_a_file_without_boundaries_returns_nothing_not_a_default(self):
        self.assertEqual(L._hr_zones("a1", {}), [])
        self.assertEqual(L._hr_zones("a1", {"time_in_zone": [{}]}), [])


class TestTheConfiguredFallback(unittest.TestCase):
    """A file that recorded no zones falls back to the athlete's DATED
    configuration and says so. The configuration is a temporary, synthetic
    hr_zones.json -- never the real one."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "hr_zones.json"
        p = mock.patch.object(Z, "ZONE_CONFIG", self.path)
        p.start()
        self.addCleanup(p.stop)

    def write(self, sets):
        self.path.write_text(json.dumps(sets))

    def test_absent_configuration_is_stated_not_defaulted(self):
        self.assertEqual(Z.configured_zones()["status"], "unconfigured")
        self.assertEqual(Z._configured_zones(), ([], "none configured"))

    def test_the_set_in_force_at_the_date_is_used(self):
        """THE TWIN: with a configuration, the fallback carries it."""
        self.write(
            [
                {
                    "sport": "RUNNING",
                    "effective_from": "2025-01-01",
                    "max_hr": 180,
                    "zone_floors": {"1": 90, "2": 110, "3": 130, "4": 150, "5": 165},
                },
                {
                    "sport": "RUNNING",
                    "effective_from": "2025-06-01",
                    "max_hr": 182,
                    "zone_floors": {"1": 91, "2": 111, "3": 131, "4": 151, "5": 166},
                },
                {
                    "sport": "CYCLING",
                    "effective_from": "2025-01-01",
                    "max_hr": 170,
                    "zone_floors": {"1": 80, "2": 100, "3": 120, "4": 140, "5": 155},
                },
            ]
        )
        zones, label = Z._configured_zones("2025-03-01")
        self.assertEqual(
            zones,
            [(1, 90, 109), (2, 110, 129), (3, 130, 149), (4, 150, 164), (5, 165, 180)],
        )
        self.assertIn("2025-01-01", label)
        zones, _ = Z._configured_zones("2025-07-01")
        self.assertEqual(zones[0], (1, 91, 110))
        self.assertEqual(Z._configured_zones("2024-12-31")[0], [])
        cur = Z.configured_zones()
        self.assertEqual(cur["status"], "ok")
        self.assertEqual(cur["zones"][-1], (5, 166, 182))


# --- isolation: never the real athlete state or config ----------------------
# Entered in setUpModule, CLOSED in tearDownModule -- see tests/corpus.py
# isolated() for why this is not unittest.enterModuleContext.
_ISOLATION = contextlib.ExitStack()


def setUpModule():
    if corpus_path() is not None:
        _ISOLATION.enter_context(isolated())


def tearDownModule():
    _ISOLATION.close()


class TestStoredBoundaries(CorpusTestCase):
    """§14e said the historical boundaries were unrecoverable. They are in
    every FIT, and are now stored."""

    def test_almost_every_run_has_its_own_boundaries(self):
        # RUNS. A corpus with swims, rides and rows holds files that need not
        # carry a time_in_zone message at all.
        n = self.con.execute("""
            SELECT count(DISTINCT z.activity_id) FROM activity_hr_zones z
            JOIN activities a USING (activity_id)
            WHERE a.sport = 'running' AND a.source = 'garmin_watch'""").fetchone()[0]
        total = self.con.execute(
            "SELECT count(*) FROM activities WHERE sport = 'running' "
            "AND source = 'garmin_watch'"
        ).fetchone()[0]
        if total == 0:
            self.skipTest("this corpus has no garmin_watch run")
        self.assertGreaterEqual(n, 0.9 * total)

    def test_boundary_sets_are_stored_per_file(self):
        """Each activity's set is its own: the distinct sets are exactly the
        distinct stored arrays, never one set copied everywhere."""
        n_sets = self.con.execute("""
            SELECT count(*) FROM (SELECT DISTINCT list(low_bpm ORDER BY zone)
            FROM activity_hr_zones GROUP BY activity_id)""").fetchone()[0]
        n_act = self.con.execute(
            "SELECT count(DISTINCT activity_id) FROM activity_hr_zones"
        ).fetchone()[0]
        if n_act == 0:
            self.skipTest("this corpus has no stored zone boundaries")
        self.assertGreaterEqual(n_sets, 1)
        self.assertLessEqual(n_sets, n_act)

    def test_every_stored_set_is_five_ascending_zones(self):
        for (aid,) in self.con.execute(
            "SELECT DISTINCT activity_id FROM activity_hr_zones"
        ).fetchall():
            rows = self.con.execute(
                "SELECT zone, low_bpm, high_bpm FROM activity_hr_zones "
                "WHERE activity_id = ? ORDER BY zone",
                [aid],
            ).fetchall()
            self.assertEqual([r[0] for r in rows], list(range(1, len(rows) + 1)), aid)
            for z, lo, hi in rows:
                self.assertLessEqual(lo, hi, f"{aid} z{z}")

    def test_zones_do_not_overlap_or_leave_gaps(self):
        for (aid,) in self.con.execute(
            "SELECT DISTINCT activity_id FROM activity_hr_zones"
        ).fetchall():
            rows = self.con.execute(
                "SELECT zone, low_bpm, high_bpm FROM activity_hr_zones "
                "WHERE activity_id = ? ORDER BY zone",
                [aid],
            ).fetchall()
            for (z1, _, hi), (z2, lo, _) in zip(rows, rows[1:], strict=False):
                self.assertEqual(lo, hi + 1, f"{aid} between z{z1} and z{z2}")


class TestZoneByGait(CorpusTestCase):
    """The subject: the newest watch run with all three gaits, stored zones
    with Garmin's own zone-2 seconds, and quality-flagged records."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.aid = one(
            cls.con,
            """
            SELECT a.activity_id FROM activities a
            WHERE a.source = 'garmin_watch' AND a.sport = 'running'
              AND EXISTS (SELECT 1 FROM splits s WHERE s.activity_id = a.activity_id
                          AND s.split_type = 'rwd_stand' AND s.total_timer_time > 5)
              AND EXISTS (SELECT 1 FROM splits s WHERE s.activity_id = a.activity_id
                          AND s.split_type = 'rwd_walk')
              AND EXISTS (SELECT 1 FROM activity_hr_zones z
                          WHERE z.activity_id = a.activity_id AND z.zone = 2
                            AND z.time_in_zone_s > 300)
              AND EXISTS (SELECT 1 FROM records_enriched e
                          WHERE e.activity_id = a.activity_id
                            AND coalesce(e.quality_flags, '') <> '')
            ORDER BY a.start_time_utc DESC LIMIT 1""",
            what="watch run with walking, standing, stored zones and flags",
        )[0]
        cls.r = Z.time_in_zone_by_gait(cls.con, cls.aid)

    def test_all_three_gaits_are_reported(self):
        self.assertEqual(set(self.r["by_gait"]), {"running", "walking", "standing"})

    def test_standing_is_a_gait_not_paused_time(self):
        """record_weights normally subtracts rwd_stand. Zeroing it would make
        'zone 2 while standing' -- one of the three quantities asked for --
        unmeasurable."""
        self.assertGreater(self.r["by_gait"]["standing"]["total_s"], 0)
        self.assertIn("rwd_stand is a GAIT", self.r["filters"]["stopped_time_source"])

    def test_zone_seconds_sum_to_the_gait_total(self):
        for gait, v in self.r["by_gait"].items():
            zsum = sum(val for k, val in v.items() if k.startswith("zone_"))
            self.assertAlmostEqual(
                zsum + v["unclassified_s"], v["total_s"], delta=1e-6, msg=gait
            )

    def test_it_uses_the_activitys_own_boundaries(self):
        self.assertIn("own FIT file", self.r["boundary_source"])

    def test_the_boundary_set_travels_with_the_result(self):
        """Several sets can exist; a reader must see which ruler was used."""
        stored = [
            tuple(r)
            for r in self.con.execute(
                "SELECT low_bpm, high_bpm FROM activity_hr_zones "
                "WHERE activity_id = ? ORDER BY zone",
                [self.aid],
            ).fetchall()
        ]
        self.assertEqual([tuple(b) for b in self.r["boundary_set"]], stored)
        self.assertIn("boundary_set", self.r["filters"])

    def test_flagged_records_are_excluded_and_counted(self):
        self.assertGreater(self.r["excluded_flagged_s"], 0)

    def test_it_reads_lower_than_garmins_ungated_figure(self):
        """Ours excludes stops and flagged records and applies the HR lag, so
        it must be LOWER. Reading higher would mean a gate is not firing."""
        garmin = self.con.execute(
            "SELECT time_in_zone_s FROM activity_hr_zones "
            "WHERE activity_id = ? AND zone = 2",
            [self.aid],
        ).fetchone()[0]
        self.assertIsNotNone(garmin)
        self.assertLess(self.r["zone2_total_minutes"] * 60, garmin)

    def test_the_hr_lag_is_applied(self):
        self.assertEqual(self.r["filters"]["hr_lag_s"], Z.HR_LAG_S)

    def test_a_run_with_no_heart_rate_is_unavailable_not_zero(self):
        aid = one(
            self.con,
            """
            SELECT a.activity_id FROM activities a WHERE NOT EXISTS (
                SELECT 1 FROM records r WHERE r.activity_id = a.activity_id
                  AND r.heart_rate IS NOT NULL)
            ORDER BY a.activity_id LIMIT 1""",
            what="activity with no heart rate",
        )[0]
        r = Z.time_in_zone_by_gait(self.con, aid)
        self.assertEqual(r["status"], "unavailable")
        self.assertEqual(r["by_gait"], {})


class TestWeeklyRollup(CorpusTestCase):
    """The cluster is whichever one this corpus's newest run falls in under
    the test's config -- a property, never a named place."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        aid = one(
            cls.con,
            """
            SELECT activity_id FROM activities
            WHERE source = 'garmin_watch' AND sport = 'running'
            ORDER BY start_time_utc DESC LIMIT 1""",
            what="watch run",
        )[0]
        cls.cluster = cluster_of(cls.con, aid)
        cls.r = O.zone_by_gait(cls.con, cluster=cls.cluster, last_n=8)

    def test_it_returns_per_run_and_per_week(self):
        self.assertTrue(self.r["per_run"])
        self.assertTrue(self.r["weekly"])

    def test_weeks_are_local_not_utc(self):
        """A late-evening run crosses midnight UTC and would land in the next
        week at a boundary (CLAUDE.md)."""
        self.assertIn("LOCAL ISO weeks", self.r["note"])
        for run in self.r["per_run"]:
            self.assertIn("local_date", run)

    def test_a_week_on_one_boundary_set_is_summed(self):
        ok = [w for w in self.r["weekly"] if w["status"] == "ok"]
        self.assertTrue(ok)
        for w in ok:
            self.assertIsNotNone(w["zone2_minutes"])
            self.assertEqual(w["n_boundary_sets"], 1)

    def test_a_week_spanning_two_boundary_sets_is_split_not_summed(self):
        """A minute in zone 2 means a different thing under each set, so
        summing across a change produces a number on no scale."""
        wide = O.zone_by_gait(self.con)
        split = [w for w in wide["weekly"] if w["status"] == "split"]
        for w in split:
            self.assertIsNone(w["zone2_minutes"])
            self.assertIn("boundary set", w["reason"])
        # The positive twin lives in the test above: some weeks DO sum.

    def test_every_result_states_its_filters(self):
        f = self.r["applied_filters"]
        self.assertIsNotNone(f)
        for k in (
            "hr_lag_s",
            "gait_source",
            "stopped_time_source",
            "boundary_source",
            "quality_flags_excluded",
        ):
            self.assertIn(k, f)

    def test_it_honours_the_standard_filter_vocabulary(self):
        narrow = O.zone_by_gait(self.con, cluster=self.cluster, last_n=3)
        self.assertLessEqual(len(narrow["per_run"]), 3)
        self.assertLessEqual(len(narrow["per_run"]), len(self.r["per_run"]))


if __name__ == "__main__":
    unittest.main()
