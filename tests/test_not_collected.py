"""Per-run context this build does not collect is LABELLED, never defaulted.

run_type, exclude_from_trend, walk_protocol and sun_exposure have no source
in this build (src/trail_factors/ingest/answers.py). Every reader takes the same path:
the
value is null and a sibling field says "not collected". A bare null would read
as "an untyped run" or "a run nobody excluded", which are claims about the
athlete; the label makes it a claim about the build.

AND A run_type FILTER IS REJECTED BY NAME. The parameter stays on every
selection tool so a later version can re-enable it through user-defined run
types without changing a signature. Until then a NON-EMPTY run_type raises
FilterUnavailable naming "no run_type source in this build" -- a filter that
silently matched nothing would read as "you have no such runs". Empty string
and None are "not asked" and work (the positive twin).

The synthetic tests use an in-memory corpus with the cluster and venue
assignment stubbed. The corpus tests read TRAIL_FACTORS_DB (skipped when unset)
under tests.corpus.isolated(), so the athlete-state directory they see is
EMPTY -- the state every reader must handle.
"""

from __future__ import annotations

import contextlib
import datetime
import unittest
from unittest import mock

import duckdb

from tests.corpus import CorpusTestCase, corpus_path, isolated, use_config
from trail_factors.analysis import over as O
from trail_factors.analysis import queries as Q
from trail_factors.analysis import report as REP
from trail_factors.analysis import select as S
from trail_factors.ingest.answers import NO_RUN_TYPE_SOURCE, NOT_COLLECTED, load_answers

T0 = datetime.datetime(2025, 3, 1, 7, 0, 0)  # synthetic
CLUSTER = "CLUSTER_A"  # synthetic cluster name


def corpus(n=4):
    con = duckdb.connect(":memory:")
    con.execute("""CREATE TABLE activities (activity_id VARCHAR,
        run_id VARCHAR, start_time_utc TIMESTAMP, local_tz DOUBLE,
        sport VARCHAR, source VARCHAR, total_distance_m DOUBLE,
        duration_s DOUBLE, moving_time_s DOUBLE, sampling_regime VARCHAR,
        coverage_fraction DOUBLE)""")
    for i in range(n):
        con.execute(
            "INSERT INTO activities VALUES (?, ?, ?, 0.0, 'running', "
            "'garmin_watch', 5000.0, 1800.0, 1700.0, '1hz', 1.0)",
            [f"a{i}", f"run-a{i}", T0 - datetime.timedelta(days=i)],
        )
    return con


def stubs():
    return (
        mock.patch.object(REP, "cluster_of", lambda con, aid: CLUSTER),
        mock.patch(
            "trail_factors.analysis.venues.assign_all", lambda con: {"by_run": {}}
        ),
    )


class TestTheSourceIsEmpty(unittest.TestCase):
    def test_load_answers_is_always_empty(self):
        self.assertEqual(load_answers(), {})

    def test_the_labels_are_the_shared_constants(self):
        self.assertEqual(NOT_COLLECTED, "not collected")
        self.assertEqual(NO_RUN_TYPE_SOURCE, "no run_type source in this build")


class TestARunTypeFilterIsRejectedByName(unittest.TestCase):
    """Every entry point that takes run_type rejects a non-empty one with the
    same words, before reading anything."""

    def assertRejects(self, fn, *args, **kw):
        with self.assertRaises(S.FilterUnavailable) as cm:
            fn(*args, **kw)
        self.assertIn(NO_RUN_TYPE_SOURCE, str(cm.exception))
        self.assertIn("user-defined run types", str(cm.exception))
        return cm.exception

    def test_it_is_a_value_error_so_existing_handlers_still_catch_it(self):
        self.assertTrue(issubclass(S.FilterUnavailable, ValueError))

    def test_check_available_and_validate_filter(self):
        e = self.assertRejects(S.check_available, {"run_type": "easy"})
        self.assertIn("'easy'", str(e))
        self.assertRejects(S.validate_filter, {"run_type": "easy"})

    def test_select_runs_even_without_validation(self):
        con = corpus()
        a, v = stubs()
        with a, v:
            self.assertRejects(S.select_runs, con, {"run_type": "easy"})

    def test_list_runs(self):
        con = corpus()
        a, v = stubs()
        with a, v:
            self.assertRejects(Q.list_runs, con, run_type="easy")

    def test_every_over_primitive(self):
        con = duckdb.connect(":memory:")  # rejected before any read
        calls = [
            (O.drift, ()),
            (O.segment_effort_drift, ()),
            (O.efficiency, ()),
            (O.delta_hr, ()),
            (O.cadence_table, ()),
            (O.grade_table, ()),
            (O.max_hr_over, ()),
            (O.hr_above, (180.0,)),
            (O.time_above_hr, ((170.0,),)),
            (O.zone_by_gait, ()),
            (O.segment_history, ("seg",)),
        ]
        with use_config():
            for fn, args in calls:
                with self.subTest(fn=fn.__name__):
                    self.assertRejects(fn, con, *args, run_type="easy")


class TestPositiveTwinEmptyRunTypeWorks(unittest.TestCase):
    """None and '' are "not asked": a caller forwarding every parameter with
    its default must not be rejected."""

    def test_empty_values_pass_the_check(self):
        for v in (None, ""):
            S.check_available({"run_type": v})
            self.assertEqual(S.validate_filter({"run_type": v}), [])
        S.check_available({})

    def test_select_runs_with_an_empty_run_type_selects_everything(self):
        con = corpus()
        a, v = stubs()
        with a, v:
            s = S.select_runs(con, {"run_type": ""})
            t = S.select_runs(con, {})
        self.assertEqual(s["n_included"], 4)
        self.assertEqual(s["included"], t["included"])

    def test_list_runs_with_no_run_type_lists_everything(self):
        con = corpus()
        a, v = stubs()
        with a, v:
            rows = Q.list_runs(con, run_type="")
        self.assertEqual(len(rows), 4)


class TestEveryRowIsLabelled(unittest.TestCase):
    def test_select_runs(self):
        con = corpus()
        a, v = stubs()
        with a, v:
            s = S.select_runs(con, {})
        self.assertTrue(s["included"])
        for r in s["included"]:
            self.assertIsNone(r["run_type"])
            self.assertEqual(r["run_type_status"], NOT_COLLECTED)
        self.assertNotIn("run_type", s["rule"])
        self.assertIn(NOT_COLLECTED, s["trend_exclusion"])

    def test_list_runs(self):
        con = corpus()
        a, v = stubs()
        with a, v:
            rows = Q.list_runs(con)
        self.assertTrue(rows)
        for r in rows:
            self.assertIsNone(r["run_type"])
            self.assertEqual(r["run_type_status"], NOT_COLLECTED)

    def test_comparison_set(self):
        con = corpus()
        a, v = stubs()
        with a, v:
            c = REP.comparison_set(con, "a0", "run-a0")
        self.assertIsNone(c["run_type"])
        self.assertEqual(c["run_type_status"], NOT_COLLECTED)
        self.assertIn(f"run_type {NOT_COLLECTED}", c["rule"])
        self.assertEqual(sorted(c["members"]), ["a1", "a2", "a3"])

    def test_the_comparison_rule_is_the_live_not_collected_path(self):
        """The rule text is where run_type's absence reaches a reader: no
        run_type step and no exclusion on the athlete's say-so. The old
        per-run reader had no caller and is gone, so nothing can route a
        run_type around this sentence."""
        con = corpus()
        a, v = stubs()
        with a, v:
            c = REP.comparison_set(con, "a0", "run-a0")
        self.assertIn(
            f"run_type {NOT_COLLECTED} in this build, so no run_type step", c["rule"]
        )
        self.assertIn(
            "no run excluded from the trend on the athlete's say-so", c["rule"]
        )
        self.assertNotIn("same run_type", c["rule"])
        self.assertFalse(hasattr(REP, "run_type_of"))

    def test_the_walk_protocol_gate_is_a_labelled_constant(self):
        from trail_factors.analysis import cycles as CY

        g = CY._walk_protocol_gate()
        self.assertIsNone(g["walk_protocol"])
        self.assertEqual(g["walk_protocol_status"], NOT_COLLECTED)
        self.assertIs(g["poolable_with_other_runs"], False)
        self.assertIn("not collected", g["pooling_note"])
        self.assertFalse(hasattr(CY, "STRUCTURED_PROTOCOLS"))

    def test_sun_exposure(self):
        s = REP._sun_exposure()
        self.assertEqual(s["status"], NOT_COLLECTED)
        self.assertIsNone(s["value"])


# --- the corpus, with an EMPTY athlete-state directory -----------------------
# Entered in setUpModule, CLOSED in tearDownModule -- see tests/corpus.py
# isolated() for why this is not unittest.enterModuleContext.
_ISOLATION = contextlib.ExitStack()


def setUpModule():
    if corpus_path() is not None:
        _ISOLATION.enter_context(isolated())


def tearDownModule():
    _ISOLATION.close()


class TestOnTheCorpus(CorpusTestCase):
    def test_max_hr_over_labels_every_run(self):
        r = O.max_hr_over(self.con, last_n=5)
        if not r["per_run"]:
            self.skipTest("this corpus has no watch run with HR")
        for row in r["per_run"]:
            self.assertIsNone(row["run_type"])
            self.assertEqual(row["run_type_status"], NOT_COLLECTED)
            self.assertIsNone(row["exclude_from_trend"])
            self.assertEqual(row["exclude_from_trend_status"], NOT_COLLECTED)

    def test_time_above_hr_labels_every_run(self):
        r = O.time_above_hr(self.con, last_n=3)
        if not r["per_run"]:
            self.skipTest("this corpus has no watch run with HR")
        for row in r["per_run"]:
            self.assertIsNone(row["run_type"])
            self.assertEqual(row["run_type_status"], NOT_COLLECTED)

    def test_run_walk_cycles_on_a_run_that_walks(self):
        from trail_factors.analysis import cycles as CY

        aid = self.con.execute("""
            SELECT activity_id FROM splits WHERE split_type = 'rwd_walk'
            GROUP BY 1 ORDER BY 1 LIMIT 1""").fetchone()
        if aid is None:
            self.skipTest("this corpus has no run with a walk split")
        c = CY.cycles(self.con, aid[0])
        self.assertIsNone(c["walk_protocol"])
        self.assertEqual(c["walk_protocol_status"], NOT_COLLECTED)
        self.assertIs(c["poolable_with_other_runs"], False)

    def test_the_report_labels_everything_and_asks_nothing(self):
        aid = self.con.execute("""
            SELECT activity_id FROM activities
            WHERE source = 'garmin_watch' AND sport = 'running'
            ORDER BY start_time_utc DESC LIMIT 1""").fetchone()
        if aid is None:
            self.skipTest("this corpus has no watch run")
        rep = REP.build(self.con, aid[0])
        cs = rep["comparison"]
        self.assertIsNone(cs["run_type"])
        self.assertEqual(cs["run_type_status"], NOT_COLLECTED)
        cyc = rep["run_walk_cycles"]
        if "walk_protocol_status" in cyc:
            self.assertEqual(cyc["walk_protocol_status"], NOT_COLLECTED)
        wbgt = rep["conditions"]["estimated_wbgt_c"]
        if "sun_exposure" in wbgt:
            self.assertEqual(wbgt["sun_exposure"]["status"], NOT_COLLECTED)
        self.assertIn("session_load", rep)
        self.assertEqual(
            set(rep["athlete"]),
            {"garmin_self_evaluation", "notes", "notes_status", "notes_projection"},
        )
        text = REP.render(rep).lower()
        for asked in ("needs a", "please answer", "missing answer"):
            self.assertNotIn(asked, text)

    def test_a_run_type_selection_on_the_corpus_is_rejected(self):
        with self.assertRaises(S.FilterUnavailable):
            O.max_hr_over(self.con, run_type="easy")
        # Positive twin on the same corpus.
        self.assertGreaterEqual(
            O.max_hr_over(self.con, run_type="", last_n=1)["selection"]["n_included"], 0
        )


if __name__ == "__main__":
    unittest.main()
