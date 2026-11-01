"""Tests for run pairing (SPEC.md §4, §9).

Pairing is the first component with a QUIET failure mode: too tight a
tolerance splits one run into two, too loose merges two runs into one, and
neither raises. These use synthetic activity dicts so they do not depend on
the contents of data/raw/.

    uv run python -m unittest discover tests -v
"""

import unittest
from datetime import datetime, timedelta

import duckdb

from trail_factors.ingest.load import refresh_run_start, resolve_run
from trail_factors.ingest.schema import DDL

BASE = datetime(2023, 6, 20, 7, 0, 49)  # synthetic


def make_activity(activity_id, source, start, duration_s=3600.0, sport="running"):
    """A synthetic activities row in production column order."""
    return dict(
        activity_id=activity_id,
        run_id=None,
        file_hash="hash-" + activity_id,
        source_file=f"{source}.fit",
        start_time_utc=start,
        local_tz=1.0,
        sport=sport,
        sub_sport="generic",
        device="test",
        total_distance_m=7000.0,
        total_ascent_m=200.0,
        duration_s=duration_s,
        source=source,
        sampling_regime="1hz",
        median_gap_s=1.0,
        coverage_fraction=1.0,
        gps_contiguous=True,
        pipeline_version=1,
    )


class PairingTest(unittest.TestCase):
    def setUp(self):
        self.con = duckdb.connect(":memory:")
        self.con.execute(DDL)

    def tearDown(self):
        self.con.close()

    def attach(self, activity):
        """Mirror the ingest write path: resolve, insert, recompute run start."""
        run_id = resolve_run(self.con, activity)
        activity["run_id"] = run_id
        # Insert by name so a new nullable column does not break this helper.
        self.con.execute(
            f"INSERT INTO activities ({','.join(activity)}) VALUES "
            f"({','.join(['?'] * len(activity))})",
            list(activity.values()),
        )
        refresh_run_start(self.con, run_id)
        return run_id

    def run_count(self):
        return self.con.execute("SELECT count(*) FROM runs").fetchone()[0]

    def run_start(self, run_id):
        return self.con.execute(
            "SELECT start_time_utc FROM runs WHERE run_id = ?", [run_id]
        ).fetchone()[0]

    # --- a typical pair: the phone starts 2 s before the watch ---

    def test_both_ingest_orders_converge(self):
        """Same run_id AND same start_time_utc regardless of ingest order."""
        watch = ("watch", "garmin_watch", BASE, 3600.0)
        phone = ("phone", "strava_phone", BASE - timedelta(seconds=2), 3599.0)

        results = []
        for order in ([watch, phone], [phone, watch]):
            self.setUp()
            ids = {self.attach(make_activity(*a)) for a in order}
            self.assertEqual(len(ids), 1, "the pair must share one run")
            run_id = ids.pop()
            results.append((run_id, self.run_start(run_id), self.run_count()))
            self.tearDown()

        self.assertEqual(results[0], results[1], "ingest order must not matter")
        self.assertEqual(results[0][0], "2023-06-20T0700Z")
        # min() across sources: the phone started 2 s earlier
        self.assertEqual(results[0][1], BASE - timedelta(seconds=2))
        self.assertEqual(results[0][2], 1)

    # --- guards against merging things that are not the same run ---

    def test_two_files_from_same_source_never_merge(self):
        """A run holds at most one file per source, even at identical times."""
        self.attach(make_activity("a", "garmin_watch", BASE))
        self.attach(make_activity("b", "garmin_watch", BASE))
        self.assertEqual(self.run_count(), 2)

    def test_run_then_hike_four_hours_apart_stay_separate(self):
        """Same sport, same day, far apart in time: two outings (§14)."""
        self.attach(make_activity("run", "garmin_watch", BASE))
        self.attach(make_activity("hike", "garmin_watch", BASE + timedelta(hours=4)))
        self.assertEqual(self.run_count(), 2)

    def test_different_sports_do_not_merge(self):
        """Overlapping in time but a different sport is a different outing."""
        self.attach(make_activity("r", "garmin_watch", BASE, sport="running"))
        self.attach(make_activity("c", "strava_phone", BASE, sport="cycling"))
        self.assertEqual(self.run_count(), 2)

    def test_non_overlapping_ranges_do_not_merge(self):
        """Within the start tolerance but not overlapping: still separate.

        Two 60 s activities 150 s apart start within the 180 s window, so
        tolerance alone would merge them. The overlap requirement is what
        keeps them apart.
        """
        self.attach(make_activity("a", "garmin_watch", BASE, duration_s=60.0))
        self.attach(
            make_activity(
                "b", "strava_phone", BASE + timedelta(seconds=150), duration_s=60.0
            )
        )
        self.assertEqual(self.run_count(), 2)

    # --- idempotency ---

    def test_reingest_does_not_create_a_second_run(self):
        """Re-ingest deletes the activity row then re-attaches it (§12 m1)."""
        first = self.attach(make_activity("watch", "garmin_watch", BASE))
        self.con.execute("DELETE FROM activities WHERE activity_id = 'watch'")
        second = self.attach(make_activity("watch", "garmin_watch", BASE))
        self.assertEqual(first, second)
        self.assertEqual(self.run_count(), 1)


if __name__ == "__main__":
    unittest.main()
