"""list_runs defaults to running (A3), reports the LOCAL date beside the
run_id (A5), and labels run_type as not collected.

Rides and a hike sat in the run listing beside the runs. The default is now
running; `sport="any"` widens to everything and any other value names one
sport. In-memory synthetic fixture: nothing reads a corpus database or the
athlete-state directory.
"""

import datetime
import inspect
import unittest
from unittest import mock

import duckdb

from trail_factors.analysis import queries as Q
from trail_factors.analysis import report as REP
from trail_factors.analysis.select import FilterUnavailable
from trail_factors.ingest.answers import NO_RUN_TYPE_SOURCE, NOT_COLLECTED

# Synthetic. run_id, start (naive UTC), local_tz, sport
ACTS = [
    ("run-a", datetime.datetime(2025, 1, 17, 21, 0), -3.0, "running"),
    ("ride-b", datetime.datetime(2025, 1, 16, 15, 0), -3.0, "cycling"),
    ("hike-c", datetime.datetime(2025, 1, 15, 15, 0), -3.0, "hiking"),
    # 01:30 UTC at UTC-3 is 22:30 the PREVIOUS local day.
    ("run-late", datetime.datetime(2025, 1, 11, 1, 30), -3.0, "running"),
]


def corpus():
    con = duckdb.connect()
    con.execute("""CREATE TABLE activities (activity_id VARCHAR, run_id VARCHAR,
        start_time_utc TIMESTAMP, local_tz DOUBLE, sport VARCHAR, source VARCHAR,
        total_distance_m DOUBLE, duration_s DOUBLE, moving_time_s DOUBLE,
        sampling_regime VARCHAR, coverage_fraction DOUBLE)""")
    con.execute("""CREATE TABLE runs (run_id VARCHAR, start_time_utc TIMESTAMP,
        local_start_display VARCHAR, local_tz DOUBLE, sport VARCHAR)""")
    for rid, start, tz, sport in ACTS:
        con.execute(
            "INSERT INTO activities VALUES (?,?,?,?,?,'garmin_watch',"
            "5000,1800,1700,'1hz',1.0)",
            [f"a-{rid}", rid, start, tz, sport],
        )
        con.execute(
            "INSERT INTO runs VALUES (?,?,?,?,?)",
            [rid, start, f"display {rid}", tz, sport],
        )
    return con


def stub_cluster():
    """cluster_of reads positions this fixture does not have."""
    return mock.patch.object(REP, "cluster_of", lambda con, aid: "CLUSTER_A")


class TestListRuns(unittest.TestCase):
    def rows(self, **kw):
        with stub_cluster():
            return Q.list_runs(corpus(), **kw)

    def ids(self, **kw):
        return [r["run_id"] for r in self.rows(**kw)]

    def test_the_default_is_running_only(self):
        self.assertEqual(self.ids(), ["run-a", "run-late"])

    def test_any_widens_to_every_sport(self):
        self.assertEqual(
            self.ids(sport="any"), ["run-a", "ride-b", "hike-c", "run-late"]
        )

    def test_a_named_sport_selects_only_it(self):
        self.assertEqual(self.ids(sport="cycling"), ["ride-b"])

    def test_a_misspelt_sport_returns_nothing_not_everything(self):
        self.assertEqual(self.ids(sport="runing"), [])

    def test_local_date_is_the_local_day_not_the_utc_day(self):
        rows = {r["run_id"]: r for r in self.rows()}
        self.assertEqual(rows["run-late"]["local_date"], "2025-01-10")
        self.assertEqual(rows["run-a"]["local_date"], "2025-01-17")


class TestRunTypeIsNotCollected(unittest.TestCase):
    """This build has no run_type source. A null alone would read as "an
    untyped run"; the label says the value was never collected."""

    def test_every_row_carries_null_and_the_label(self):
        with stub_cluster():
            rows = Q.list_runs(corpus(), sport="any")
        self.assertEqual(len(rows), len(ACTS))
        for r in rows:
            self.assertIsNone(r["run_type"], r["run_id"])
            self.assertEqual(r["run_type_status"], NOT_COLLECTED, r["run_id"])

    def test_a_run_type_filter_is_rejected_by_name(self):
        """An empty listing would read as "no runs of that type" -- a claim
        about the athlete this build cannot make."""
        con = corpus()
        with stub_cluster(), self.assertRaises(FilterUnavailable) as cm:
            Q.list_runs(con, run_type="continuous")
        self.assertIn(NO_RUN_TYPE_SOURCE, str(cm.exception))
        self.assertIn("run_type", str(cm.exception))

    def test_positive_twin_an_empty_run_type_is_not_asked(self):
        """A caller forwarding every parameter with its default must not be
        rejected: "" and None both mean the filter was not given."""
        for empty in ("", None):
            with stub_cluster():
                self.assertEqual(
                    [r["run_id"] for r in Q.list_runs(corpus(), run_type=empty)],
                    ["run-a", "run-late"],
                    repr(empty),
                )


class TestOneLocalDateRule(unittest.TestCase):
    def test_list_runs_uses_the_shared_rule(self):
        self.assertIn("local_date_of(", inspect.getsource(Q.list_runs))


if __name__ == "__main__":
    unittest.main()
