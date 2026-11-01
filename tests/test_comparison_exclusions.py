"""Comparison sets follow the subject's sport, never the future, and never a
per-run statement this build does not collect.

THE SPORT BUG: comparison_set hard-coded `sport = 'running'` for the
CANDIDATES without looking at the subject, so a hike's report was measured
against runs.

NOTHING IS EXCLUDED ON THE ATHLETE'S SAY-SO. Per-run trend exclusion has no
source in this build, so the only exclusion kinds are MEASURED ones -- sport
in a comparison set, coverage in a selection -- and both results say so in
words rather than leaving the reader to assume a check ran.

The fixture is a synthetic in-memory corpus with the cluster stubbed: nothing
here reads a corpus database or the athlete-state directory, except the one
corpus check at the end, which reads TRAIL_FACTORS_DB through tests.corpus
(skipped when unset). Every exclusion test has a twin showing the same run IS
a member when nothing excludes it, and the percentile test shows the
exclusion moves the number a reader sees.
"""

import datetime
import inspect
import unittest
from unittest import mock

import duckdb

from trail_factors.analysis import primitives as P
from trail_factors.analysis import report as REP
from trail_factors.analysis import select
from trail_factors.ingest.answers import NOT_COLLECTED
from tests.corpus import connect, isolated, needs_corpus, one

T0 = datetime.datetime(2025, 1, 15, 7, 0, 0)      # synthetic
CLUSTER = "CLUSTER_A"                             # synthetic cluster name

# activity_id -> (sport, days before the subject, metric value)
CORPUS = {
    "subj": ("running", 0, 50.0),
    "r1": ("running", 1, 1.0),
    "r2": ("running", 2, 2.0),
    "r3": ("running", 3, 3.0),
    "r4": ("running", 4, 4.0),
    "odd": ("running", 5, 100.0),   # an outlier nothing may drop
}


def corpus(extra=None):
    rows = dict(CORPUS, **(extra or {}))
    con = duckdb.connect()
    con.execute("""CREATE TABLE activities (activity_id VARCHAR, run_id VARCHAR,
                   start_time_utc TIMESTAMP, sport VARCHAR, source VARCHAR,
                   coverage_fraction DOUBLE, sampling_regime VARCHAR)""")
    for aid, (sport, days, _) in rows.items():
        con.execute("INSERT INTO activities VALUES (?,?,?,?,'garmin_watch',1.0,'1hz')",
                    [aid, f"run-{aid}", T0 - datetime.timedelta(days=days), sport])
    return con, {aid: v for aid, (_, _, v) in rows.items()}


def stubs():
    # The fixture holds no segment tables, so venue assignment (§19) is
    # stubbed to "no run has a venue" -- the cluster fallback these tests ran
    # under before venues existed.
    return (mock.patch.object(REP, "cluster_of", lambda con, aid: CLUSTER),
            mock.patch("trail_factors.analysis.venues.assign_all",
                       lambda con: {"by_run": {}}))


class TestNothingIsExcludedOnSaySoInComparisonSet(unittest.TestCase):
    """The outlier stays: no per-run statement exists that could drop it."""

    def comp(self, extra=None, subject="subj"):
        con, values = corpus(extra)
        a, v = stubs()
        with a, v:
            c = REP.comparison_set(con, subject, f"run-{subject}")
        return c, values

    def test_the_outlier_is_a_member_and_nothing_is_excluded(self):
        c, _ = self.comp()
        self.assertIn("odd", c["members"])
        self.assertEqual(c["excluded"], [])
        self.assertEqual(c["n_excluded_by_reason"], {})

    def test_the_rule_says_why_no_run_type_step_ran(self):
        c, _ = self.comp()
        self.assertIsNone(c["run_type"])
        self.assertEqual(c["run_type_status"], NOT_COLLECTED)
        self.assertIn(f"run_type {NOT_COLLECTED}", c["rule"])
        self.assertIn("say-so", c["rule"])

    def test_the_only_exclusion_kind_is_measured(self):
        """Positive twin: an exclusion DOES still happen, for a measured
        reason -- so 'nothing excluded' above is not a dead code path."""
        c, _ = self.comp({"bike": ("cycling", 1.5, 999.0)})
        self.assertEqual({e["kind"] for e in c["excluded"]}, {"sport"})
        self.assertEqual(set(c["n_excluded_by_reason"]), {"sport"})


class TestSportInComparisonSet(unittest.TestCase):
    """The subject's sport decides, not a hard-coded 'running'."""

    def comp(self, extra, subject="subj"):
        con, values = corpus(extra)
        a, v = stubs()
        with a, v:
            return REP.comparison_set(con, subject, f"run-{subject}"), values

    def test_a_ride_does_not_enter_a_runs_set(self):
        c, _ = self.comp({"bike": ("cycling", 1.5, 999.0)})
        self.assertNotIn("bike", c["members"])
        self.assertEqual([(e["activity_id"], e["kind"]) for e in c["excluded"]],
                         [("bike", "sport")])
        self.assertIn("cycling", c["excluded"][0]["reason"])

    def test_a_hike_is_not_measured_against_runs(self):
        """The original shape: a hike as the SUBJECT got the runs' set."""
        c, values = self.comp({"hike": ("hiking", -1, 7.0),
                               "hike2": ("hiking", 6, 8.0)}, subject="hike")
        self.assertEqual(c["members"], ["hike2"])
        self.assertEqual(c["sport"], "hiking")
        self.assertTrue(all(e["kind"] == "sport" for e in c["excluded"]))
        self.assertEqual(c["n_excluded_by_reason"], {"sport": 6})

    def test_positive_twin_a_same_sport_activity_is_a_member(self):
        c, _ = self.comp({"run6": ("running", 1.5, 5.0)})
        self.assertIn("run6", c["members"])

    def test_the_percentile_changes(self):
        """Before the fix a hike's population was the RUNS' values."""
        c, values = self.comp({"hike": ("hiking", -1, 7.0),
                               "hike2": ("hiking", 6, 8.0),
                               "hike3": ("hiking", 7, 9.0),
                               "hike4": ("hiking", 8, 10.0)}, subject="hike")
        runs = [values[a] for a in ("r1", "r2", "r3", "r4", "odd")]
        hikes = [values[m] for m in c["members"]]
        self.assertEqual(sorted(hikes), [8.0, 9.0, 10.0])
        self.assertNotEqual(P.percentile_of(values["hike"], runs),
                            P.percentile_of(values["hike"], hikes))


class TestNoLookAhead(unittest.TestCase):
    """A run is measured against runs BEFORE it, never after. The candidate
    query had no date bound, so re-running an old report compared it with
    runs that happened later."""

    LATER = {"later1": ("running", -1, 999.0), "later2": ("running", -3, 999.0),
             "later_odd": ("running", -2, 999.0)}

    def comp(self, extra=None):
        con, values = corpus(dict(self.LATER, **(extra or {})))
        a, v = stubs()
        with a, v:
            return REP.comparison_set(con, "subj", "run-subj"), values

    def test_later_runs_never_enter_the_set(self):
        c, _ = self.comp()
        self.assertEqual(sorted(c["members"]), ["odd", "r1", "r2", "r3", "r4"])
        for later in self.LATER:
            self.assertNotIn(later, c["members"])

    def test_the_window_reflects_the_bound(self):
        c, _ = self.comp()
        w = c["window"]
        self.assertEqual(w["subject_start_utc"], str(T0))
        self.assertLess(w["latest_member_utc"], w["subject_start_utc"])
        self.assertIn("<", w["bound"])
        self.assertLessEqual(c["date_range"][1], str(T0)[:10])

    def test_a_later_exclusion_is_not_reported_either(self):
        """Excluded entries come from the same bounded candidates: a LATER
        ride is neither a member nor a reported exclusion."""
        c, _ = self.comp({"later_bike": ("cycling", -1.5, 999.0)})
        self.assertNotIn("later_bike",
                         [e["activity_id"] for e in c["excluded"]])
        self.assertNotIn("later_bike", c["members"])

    def test_the_percentile_ignores_the_future(self):
        """Three later 999s would have made the subject's 50 look low."""
        c, values = self.comp()
        pop = [values[m] for m in c["members"]]
        self.assertNotIn(999.0, pop)
        self.assertGreater(P.percentile_of(values["subj"], pop), 50)

    def test_positive_twin_the_newest_run_still_has_a_full_history(self):
        """The bound only removes the future; an up-to-date report keeps every
        earlier run it had."""
        con, _ = corpus()
        a, v = stubs()
        with a, v:
            c = REP.comparison_set(con, "subj", "run-subj")
        self.assertEqual(sorted(c["members"]), ["odd", "r1", "r2", "r3", "r4"])


class TestNothingIsExcludedOnSaySoInSelectRuns(unittest.TestCase):
    def sel(self, filt=None, **kw):
        con, _ = corpus()
        a, v = stubs()
        with a, v:
            return select.select_runs(con, dict({"cluster": CLUSTER},
                                                **(filt or {})), **kw)

    def test_every_run_is_included_and_the_note_says_why(self):
        s = self.sel()
        self.assertIn("odd", [r["activity_id"] for r in s["included"]])
        self.assertEqual(s["excluded"], [])
        self.assertEqual(s["trend_exclusion"], select.TREND_EXCLUSION_NOTE)
        self.assertIn("not collected", s["trend_exclusion"])

    def test_every_included_row_labels_run_type_not_collected(self):
        s = self.sel()
        self.assertTrue(s["included"])
        for r in s["included"]:
            self.assertIsNone(r["run_type"])
            self.assertEqual(r["run_type_status"], NOT_COLLECTED)

    def test_coverage_is_still_a_measured_exclusion(self):
        """The positive twin: an exclusion still happens when it is MEASURED,
        and it is the only kind a selection can carry."""
        con, _ = corpus()
        con.execute("UPDATE activities SET coverage_fraction = 0.1 "
                    "WHERE activity_id = 'odd'")
        a, v = stubs()
        with a, v:
            s = select.select_runs(con, {"cluster": CLUSTER})
        self.assertEqual([(e["activity_id"], e["kind"]) for e in s["excluded"]],
                         [("odd", "coverage")])
        with a, v:
            s = select.select_runs(con, {"cluster": CLUSTER},
                                   needs_terrain=False)
        self.assertIn("odd", [r["activity_id"] for r in s["included"]])

    def test_the_switch_that_kept_excluded_runs_is_gone(self):
        """With no say-so exclusion there is nothing for ceiling and dose
        tools to opt out of, so the keyword no longer exists -- a caller
        still passing it fails loudly rather than being silently ignored."""
        self.assertNotIn("honour_trend_exclusion",
                         inspect.signature(select.select_runs).parameters)
        with self.assertRaises(TypeError):
            self.sel(honour_trend_exclusion=False)


class TestNoToolExcludesOnSaySo(unittest.TestCase):
    """On the corpus, read-only, with an EMPTY athlete-state directory: every
    watch run with heart rate reaches the ceiling and dose tools, and no tool
    -- ceiling, dose or trend -- reports an exclusion of a kind that is not
    measured."""

    @needs_corpus
    def test_only_measured_exclusion_kinds_appear(self):
        from trail_factors.analysis import over
        con = connect()
        try:
            rid = one(con, """
                SELECT a.run_id FROM activities a
                WHERE a.sport = 'running' AND a.source = 'garmin_watch'
                  AND EXISTS (SELECT 1 FROM records r
                              WHERE r.activity_id = a.activity_id
                                AND r.heart_rate IS NOT NULL)
                ORDER BY a.start_time_utc LIMIT 1""",
                what="watch running activity with heart rate")[0]
            with isolated():
                m = over.max_hr_over(con)
                row = next(r for r in m["per_run"] if r["run_id"] == rid)
                self.assertIsNone(row["exclude_from_trend"])
                self.assertEqual(row["exclude_from_trend_status"],
                                 NOT_COLLECTED)
                for fn in (over.max_hr_over, over.hr_above,
                           over.time_above_hr, over.zone_by_gait,
                           over.cadence_table):
                    kinds = {e.get("kind") for e in fn(con)["excluded"]
                             if e.get("kind") is not None}
                    self.assertLessEqual(kinds, {"coverage"}, fn.__name__)
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main()
