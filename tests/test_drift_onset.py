"""§6.9 drift onset — a view of §6.7, refused until the runs exist.

THE POINT OF MOST OF THESE TESTS IS THAT THE REFUSAL IS REAL. A metric that
returns None because it is unbuilt and a metric that returns None because a
measured gate rejected it are different things, and the second must carry its
evidence. The gate is computed from the live corpus, so these also pin that it
will LIFT ITSELF rather than needing someone to remember.

Whether the gate is open is a fact about one corpus, so nothing here asserts
it either way: the tests assert that the verdict FOLLOWS from the gate's own
counts, whichever way it falls.
"""

import contextlib
import unittest
from unittest import mock

from tests.corpus import (
    CorpusTestCase,
    corpus_path,
    corpus_projection,
    isolated,
    one,
    use_config,
)
from trail_factors.analysis import drift_onset as DO

ONSET_STATUSES = {"onset", "no_onset", "insufficient"}


# --- isolation: never the real athlete state or config ----------------------
# Entered in setUpModule, CLOSED in tearDownModule -- see tests/corpus.py
# isolated() for why this is not unittest.enterModuleContext.
_ISOLATION = contextlib.ExitStack()


def setUpModule():
    if corpus_path() is not None:
        _ISOLATION.enter_context(isolated(projection_crs=corpus_projection()))


def tearDownModule():
    _ISOLATION.close()


def _drift_runs(con):
    """Watch runs with enriched records: the ones §6.7 can pair at all."""
    return [
        aid
        for (aid,) in con.execute("""
        SELECT a.activity_id FROM activities a
        WHERE a.source = 'garmin_watch' AND a.sport = 'running' AND EXISTS (
            SELECT 1 FROM records_enriched e
            WHERE e.activity_id = a.activity_id)
        ORDER BY a.start_time_utc""").fetchall()
    ]


class TestGate(CorpusTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.gate = DO.gate_status(cls.con)

    def test_the_verdict_follows_from_the_counts(self):
        """Too few onsets is ALWAYS a refusal, whatever else is true."""
        g = self.gate
        self.assertEqual(
            g["n_with_onset"],
            sum(1 for c in g["cohort"] if c["onset_status"] == "onset"),
        )
        if g["n_with_onset"] < DO.MIN_ONSET_RUNS:
            self.assertFalse(g["reportable"])
            self.assertIsNone(g["cv"])
        if g["reportable"]:
            self.assertIsNotNone(g["cv"])
            self.assertLessEqual(g["cv"], DO.MAX_ONSET_CV)

    def test_the_refusal_states_what_would_lift_it(self):
        """Not 'unavailable' -- a reader must be able to act on it."""
        g = self.gate
        if g["reportable"]:
            self.skipTest("the gate is open on this corpus")
        r = g["reason"]
        self.assertTrue(r)
        if g["n_with_onset"] < DO.MIN_ONSET_RUNS:
            self.assertIn(DO.REQUIRED_SAMPLING_REGIME, r)
            self.assertIn(str(DO.MIN_ONSET_RUNS), r)
            # The structural fact that makes the wait longer than a naive run
            # count suggests.
            self.assertIn(f"{DO.MIN_ONSET_DURATION_MIN:.0f} minutes", r)

    def test_the_gate_lifts_itself(self):
        """THE POSITIVE TWIN for the refusal: lower the bar to what this
        corpus already has and the same code answers. A gate that could only
        ever refuse would pass every test above."""
        n = self.gate["n_with_onset"]
        if n == 0:
            self.skipTest("no run in this corpus produces an onset")
        with (
            mock.patch.object(DO, "MIN_ONSET_RUNS", 1),
            mock.patch.object(DO, "MAX_ONSET_CV", float("inf")),
            mock.patch.object(DO, "DEW_POINT_BAND_C", float("inf")),
        ):
            g = DO.gate_status(self.con)
        self.assertTrue(g["reportable"])
        self.assertIsNotNone(g["cv"])

    def test_the_gate_is_measured_from_the_corpus_not_hardcoded(self):
        """It counts real runs, so it changes when the corpus does.

        The denominator is the PICKED activity per run_id, not every 1 Hz
        file: see the dual-recording test below for why those differ.
        """
        expected = self.con.execute("""
            WITH picked AS (
                SELECT run_id, sampling_regime, row_number() OVER (
                    PARTITION BY run_id ORDER BY
                    CASE source WHEN 'garmin_watch' THEN 0 ELSE 1 END,
                    activity_id) AS pick
                FROM activities WHERE sport = 'running')
            SELECT count(*) FROM picked
            WHERE pick = 1 AND sampling_regime = '1hz'""").fetchone()[0]
        self.assertEqual(self.gate["n_1hz_runs"], expected)

    def test_a_run_whose_only_1hz_file_is_the_phone_is_not_counted(self):
        """THE DUAL-RECORDING TRAP, pinned.

        A run can have a smart_recording watch file and a 1 Hz PHONE file.
        Counting it as a 1 Hz run would promise HR-drift analysis on a file
        with no heart rate at all, which the naive count
        (`count(DISTINCT run_id) WHERE sampling_regime = '1hz'`) does.
        """
        naive = self.con.execute(
            "SELECT count(DISTINCT run_id) FROM activities "
            "WHERE sampling_regime = '1hz'"
        ).fetchone()[0]
        mixed = self.con.execute("""
            SELECT run_id FROM activities WHERE sampling_regime = '1hz'
              AND source <> 'garmin_watch'
              AND run_id IN (SELECT run_id FROM activities
                             WHERE source = 'garmin_watch' AND sport = 'running'
                               AND sampling_regime <> '1hz')""").fetchall()
        if not mixed:
            self.skipTest("no mixed-regime dual recording in the corpus")
        self.assertLess(self.gate["n_1hz_runs"], naive)
        counted = {c["run_id"] for c in self.gate["cohort"]}
        for (run_id,) in mixed:
            self.assertNotIn(run_id, counted)

    def test_the_cohort_is_listed_rather_than_summarised_away(self):
        if self.gate["n_1hz_runs"] == 0:
            self.skipTest("this corpus has no 1 Hz run")
        self.assertTrue(self.gate["cohort"])
        for c in self.gate["cohort"]:
            self.assertIn("onset_status", c)
            self.assertIn(c["onset_status"], ONSET_STATUSES)
            self.assertIn("dew_point_c", c)

    def test_one_activity_per_run_id(self):
        """Dual recording: the phone copy must not be counted as a second run."""
        ids = [c["run_id"] for c in self.gate["cohort"]]
        self.assertEqual(len(ids), len(set(ids)))


class TestCurve(CorpusTestCase):
    """The subject is the LONGEST 1 Hz watch run with enriched records: the
    one most likely to carry enough pairs to bin."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.aid = one(
            cls.con,
            """
            SELECT a.activity_id FROM activities a
            WHERE a.sampling_regime = '1hz' AND a.source = 'garmin_watch'
              AND a.sport = 'running' AND EXISTS (
                SELECT 1 FROM records_enriched e
                WHERE e.activity_id = a.activity_id)
            ORDER BY a.timer_time_s DESC LIMIT 1""",
            what="1 Hz watch run with enriched records",
        )[0]
        cls.c = DO.curve(cls.con, cls.aid)

    def test_dew_point_travels_with_the_value(self):
        """§8: onset is uninterpretable without it."""
        self.assertIn("dew_point_c", self.c)
        self.assertTrue(self.c["filters"]["dew_point_required"])

    def test_ties_are_counted_not_dropped(self):
        """A pair with equal heart rates is evidence FOR the null."""
        self.assertTrue(self.c["filters"]["ties_counted"])
        if not self.c["bins"]:
            self.skipTest("the subject run produced no bins")
        for b in self.c["bins"]:
            self.assertIn("n_zero", b)
            self.assertLessEqual(b["n_zero"], b["n_pairs"])

    def test_the_independent_unit_is_a_passage_not_a_pair(self):
        """Pseudo-replication is the failure that would turn this refusal into
        a false finding: one bin can hold hundreds of pairs drawn from two
        passages."""
        self.assertEqual(self.c["filters"]["independence_unit"], "passage")
        for b in self.c["bins"]:
            self.assertLessEqual(b["n_passages"], b["n_pairs"])
        crowded = [b for b in self.c["bins"] if b["n_pairs"] > 100]
        if not crowded:
            self.skipTest("no bin on the subject holds over 100 pairs")
        for b in crowded:
            self.assertLess(b["n_passages"], b["n_pairs"])

    def test_a_bin_below_the_passage_floor_is_reported_not_dropped(self):
        unusable = [b for b in self.c["bins"] if not b["usable"]]
        if not unusable:
            self.skipTest("every bin on the subject clears the passage floor")
        for b in unusable:
            self.assertIn("median_bpm_per_hour", b)
            self.assertIn("n_pairs", b)
            self.assertLess(b["n_passages"], DO.MIN_PASSAGES_PER_BIN)

    def test_it_inherits_every_one_of_6_7s_filters(self):
        from trail_factors.analysis import primitives as P

        base = P.drift_filters()
        for k, v in base.items():
            # `grain` and `note` are deliberately overridden: this IS a
            # different grain (pairs binned by time into run), and saying
            # otherwise would misdescribe what the number is.
            if k in ("note", "grain"):
                continue
            self.assertEqual(self.c["filters"][k], v, k)

    def test_elapsed_time_is_measured_from_the_activity_start(self):
        """Not from the first positioned record: on a run whose GPS fixes late
        that would shift every bin by up to an hour."""
        self.assertIn("elapsed time from activity start", self.c["filters"]["bin_by"])

    def test_an_unset_projection_derives_the_runs_own_zone(self):
        """Pairing is in metres. With no projection configured the run is
        projected in the UTM zone of its own positions -- and the curve is
        the SAME curve as configuring that zone explicitly, so derivation
        changes where the frame comes from, never the answer."""
        from trail_factors.transform import dem

        with use_config(projection_crs=None):
            p = dem.run_projection(self.con, self.aid)
            derived = DO.curve(self.con, self.aid)
        self.assertFalse(p["configured"])
        self.assertIn("derived from this run's position", p["basis"])
        with use_config(projection_crs=p["crs"]):
            configured = DO.curve(self.con, self.aid)
        for key in ("bins", "onset_min", "onset_status", "n_usable_bins"):
            self.assertEqual(derived[key], configured[key], key)


class TestOnsetCategories(CorpusTestCase):
    """`no_onset` is a COUNTED CATEGORY. A design that returned nothing for a
    flat curve would make drift look universal."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.curves = {aid: DO.curve(cls.con, aid) for aid in _drift_runs(cls.con)}
        if not cls.curves:
            raise unittest.SkipTest(
                "this corpus has no watch run with enriched records"
            )

    def test_every_run_lands_in_a_named_category(self):
        seen = {c["onset_status"] for c in self.curves.values()}
        self.assertTrue(seen)
        self.assertLessEqual(seen, ONSET_STATUSES)

    def test_no_onset_is_not_a_sentinel_number(self):
        """Never coded as a large value -- a sentinel would be averaged."""
        for aid, c in self.curves.items():
            if c["onset_status"] != "onset":
                self.assertIsNone(c["onset_min"], aid)

    def test_an_onset_run_carries_a_real_number(self):
        """The positive twin."""
        onsets = {
            aid: c for aid, c in self.curves.items() if c["onset_status"] == "onset"
        }
        if not onsets:
            self.skipTest("no run in this corpus produces an onset")
        for aid, c in onsets.items():
            self.assertIsInstance(c["onset_min"], float, aid)
            self.assertGreaterEqual(c["onset_min"], DO.ONSET_BIN_S / 60.0, aid)

    def test_no_onset_is_earlier_than_6_7s_own_floor(self):
        """§6.7 requires 600 s separation, so an onset before 10 minutes would
        be the filter rather than the athlete."""
        for aid, c in self.curves.items():
            if c["onset_min"] is not None:
                self.assertGreaterEqual(c["onset_min"], 10.0, aid)

    def test_a_run_too_short_for_three_bins_is_insufficient(self):
        """The structural fact the gate's reason cites, pinned on the runs
        themselves: under MIN_ONSET_DURATION_MIN nothing produces an onset."""
        for aid, c in self.curves.items():
            if (
                c.get("duration_min") is not None
                and c["duration_min"] < DO.MIN_ONSET_DURATION_MIN
            ):
                self.assertNotEqual(c["onset_status"], "onset", aid)


class TestReportIntegration(CorpusTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from trail_factors.analysis import report as REP

        aid = one(
            cls.con,
            """
            SELECT activity_id FROM activities
            WHERE source = 'garmin_watch' AND sport = 'running'
            ORDER BY start_time_utc DESC LIMIT 1""",
            what="watch run",
        )[0]
        cls.rep = REP.build(cls.con, aid)
        cls.gate = DO.gate_status(cls.con)

    def test_the_report_shows_drift_onset_as_refused_not_unavailable(self):
        """§15.4: refused and unavailable are different and must not merge.
        `unavailable` says an input is missing; `refused` says the analysis
        class is invalid here."""
        got = [p for p in self.rep["primitives"] if p["name"] == "drift_onset"]
        self.assertEqual(len(got), 1)
        if self.gate["reportable"]:
            self.skipTest("the gate is open on this corpus")
        self.assertEqual(got[0]["status"], "refused")
        self.assertIsNone(got[0]["value"])
        self.assertTrue(got[0]["reason"])

    def test_a_refused_primitive_is_never_notable(self):
        """§15.4: a refusal is not a finding."""
        for item in self.rep["notable"]:
            self.assertNotIn("drift_onset", item["text"])


if __name__ == "__main__":
    unittest.main()
