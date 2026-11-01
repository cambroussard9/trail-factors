"""A selection can span sports for a DOSE, and the default never moves.

Every primitive compares runs with runs, so select_runs stays running-only
unless a caller asks otherwise. A load sum is the exception: a rated ride left
out of a week understates it with one sign. `sport="any"` selects every sport
the watch recorded, one file per session; a named sport the watch never
recorded is refused rather than selecting nothing.

Synthetic, in memory; every id and date is made up.
"""

from __future__ import annotations

import datetime
import unittest
from unittest import mock

import duckdb

from trail_factors.analysis import report as REP
from trail_factors.analysis import select as S

T0 = datetime.datetime(2025, 3, 1, 7, 0, 0)  # synthetic


def corpus():
    """Three watch runs, one watch ride with NO coverage measured, and the
    phone twin of the newest run (which must never be selected)."""
    con = duckdb.connect(":memory:")
    con.execute("""CREATE TABLE activities (activity_id VARCHAR,
        run_id VARCHAR, start_time_utc TIMESTAMP, sport VARCHAR,
        source VARCHAR, sampling_regime VARCHAR, coverage_fraction DOUBLE)""")
    rows = [
        ("a0", "run-a0", 0, "running", "garmin_watch", 1.0),
        ("ride", "run-ride", 1, "cycling", "garmin_watch", None),
        ("a1", "run-a1", 2, "running", "garmin_watch", 1.0),
        ("a2", "run-a2", 3, "running", "garmin_watch", 1.0),
        ("a0-phone", "run-a0", 0, "running", "strava", 1.0),
    ]
    for aid, rid, days, sport, source, cov in rows:
        con.execute(
            "INSERT INTO activities VALUES (?, ?, ?, ?, ?, '1hz', ?)",
            [aid, rid, T0 - datetime.timedelta(days=days), sport, source, cov],
        )
    return con


def stub():
    return mock.patch.object(REP, "cluster_of", lambda con, aid: "CLUSTER_A")


class TestTheDefaultIsUnchanged(unittest.TestCase):
    def test_the_default_is_running_only_with_the_old_rule_and_rows(self):
        con = corpus()
        with stub():
            s = S.select_runs(con, {}, needs_terrain=False)
            explicit = S.select_runs(con, {}, needs_terrain=False, sport="running")
        self.assertEqual([r["activity_id"] for r in s["included"]], ["a0", "a1", "a2"])
        self.assertEqual(s["rule"], "all garmin_watch runs")
        for r in s["included"]:
            self.assertNotIn("sport", r)  # the row shape is the old one
        self.assertEqual(s, explicit)


class TestAnySportIsADose(unittest.TestCase):
    def test_any_includes_the_ride_once_per_session(self):
        con = corpus()
        with stub():
            s = S.select_runs(con, {}, needs_terrain=False, sport=S.SPORT_ANY)
        ids = [r["activity_id"] for r in s["included"]]
        self.assertEqual(ids, ["a0", "ride", "a1", "a2"])
        self.assertNotIn("a0-phone", ids)  # one file per session
        self.assertEqual(
            {r["activity_id"]: r["sport"] for r in s["included"]}["ride"], "cycling"
        )
        self.assertIn("every sport (sport=any)", s["rule"])

    def test_any_composes_with_the_date_filter(self):
        con = corpus()
        day = (T0 - datetime.timedelta(days=1)).date().isoformat()
        with stub():
            s = S.select_runs(
                con,
                {"start_date": day, "end_date": day},
                needs_terrain=False,
                sport=S.SPORT_ANY,
            )
            runs_only = S.select_runs(
                con, {"start_date": day, "end_date": day}, needs_terrain=False
            )
        self.assertEqual([r["activity_id"] for r in s["included"]], ["ride"])
        self.assertEqual(runs_only["included"], [])

    def test_a_named_sport_selects_only_it(self):
        con = corpus()
        with stub():
            s = S.select_runs(con, {}, needs_terrain=False, sport="cycling")
        self.assertEqual([r["activity_id"] for r in s["included"]], ["ride"])
        self.assertIn("sport=cycling", s["rule"])

    def test_a_sport_never_recorded_is_refused_naming_what_was(self):
        con = corpus()
        with stub(), self.assertRaises(ValueError) as cm:
            S.select_runs(con, {}, sport="swimming")
        msg = str(cm.exception)
        self.assertIn("'swimming'", msg)
        self.assertIn("cycling, running", msg)
        self.assertIn("'any'", msg)

    def test_an_unmeasured_coverage_is_named_never_formatted(self):
        """With terrain required, the ride's NULL coverage is an exclusion
        that says it was not measured -- it used to crash the format."""
        con = corpus()
        with stub():
            s = S.select_runs(con, {}, sport=S.SPORT_ANY)
        ex = {e["activity_id"]: e for e in s["excluded"]}
        self.assertIn("not measured", ex["ride"]["reason"])
        self.assertNotIn("ride", [r["activity_id"] for r in s["included"]])


if __name__ == "__main__":
    unittest.main()
