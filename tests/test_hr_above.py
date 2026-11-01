"""§14e-ter: HR excursions at record grain, and time at or above a ladder.

WHAT THESE PIN. The tool exists to decide whether the demonstrated ceiling is
DEMONSTRATED or merely the highest thing recorded, so the properties that
matter are the ones that would let a wrong answer look right: a spike pooled
with a sustained bout, a flagged record silently dropped, a lag-shifted series
standing in for the raw one, or the unresolved run quietly excluded.

THE THRESHOLDS ARE CONFIG, and every number here is read off the corpus under
test: the default threshold sits a little under THIS corpus's own peak, and
the "unresolved" run is whichever run carries that peak. Nothing is one
athlete's heart.

Every negative has a positive twin.
"""

from __future__ import annotations

import contextlib
import unittest

from tests.corpus import CorpusTestCase, corpus_path, isolated, one, use_config
from trail_factors import config as C
from trail_factors.analysis import over as O

# --- isolation: never the real athlete state or config ----------------------
# Entered in setUpModule, CLOSED in tearDownModule -- see tests/corpus.py
# isolated() for why this is not unittest.enterModuleContext.
_ISOLATION = contextlib.ExitStack()


def setUpModule():
    if corpus_path() is not None:
        _ISOLATION.enter_context(isolated())


def tearDownModule():
    _ISOLATION.close()


class _Corpus(CorpusTestCase):
    """The corpus's own peak, the run that carries it, and a default
    threshold MARGIN bpm under it -- the band the ceiling question lives in."""

    MARGIN = 10.0

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        peak, run_id = one(
            cls.con,
            """
            SELECT max(r.heart_rate), arg_max(a.run_id, r.heart_rate)
            FROM records r JOIN activities a USING (activity_id)
            WHERE a.source = 'garmin_watch' AND a.sport = 'running'
              AND r.heart_rate > 0
            HAVING max(r.heart_rate) IS NOT NULL""",
            what="garmin_watch running activity with heart rate",
        )
        cls.peak, cls.peak_run = float(peak), run_id
        cls.threshold = cls.peak - cls.MARGIN

    @classmethod
    def above(cls, threshold=None, **config):
        with use_config(**{"hr_above_default_bpm": cls.threshold, **config}):
            return O.hr_above(cls.con, threshold)


class TestExcursions(_Corpus):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.r = cls.above()

    def test_the_default_threshold_is_the_configured_one(self):
        self.assertEqual(self.r["threshold_bpm"], self.threshold)
        self.assertEqual(self.r["applied_filters"]["threshold_bpm"], self.threshold)
        self.assertFalse(
            hasattr(O, "HR_ABOVE_DEFAULT"),
            "the default is config, never a module constant",
        )

    def test_an_unset_config_falls_back_to_the_documented_default(self):
        with use_config():
            r = O.hr_above(self.con)
        self.assertEqual(r["threshold_bpm"], C.AthleteConfig().hr_above_default_bpm)

    def test_an_explicit_threshold_wins_over_the_config(self):
        r = self.above(self.peak - 1.0)
        self.assertEqual(r["threshold_bpm"], self.peak - 1.0)

    def test_it_returns_excursions_not_records(self):
        """A sustained bout must not arrive as one row per record."""
        self.assertTrue(self.r["excursions"])
        total_records = sum(e["n_records"] for e in self.r["excursions"])
        self.assertLessEqual(self.r["n_excursions"], total_records)
        if total_records == self.r["n_excursions"]:
            self.skipTest("every excursion in this corpus is a single record")
        longest = max(self.r["excursions"], key=lambda e: e["n_records"])
        self.assertGreater(longest["n_records"], 1)

    def test_every_record_in_an_excursion_is_at_or_above_the_threshold(self):
        for e in self.r["excursions"]:
            self.assertGreaterEqual(e["peak_hr"], self.threshold)
            self.assertGreaterEqual(e["mean_hr"], self.threshold)

    def test_ordered_by_peak_descending(self):
        peaks = [e["peak_hr"] for e in self.r["excursions"]]
        self.assertEqual(peaks, sorted(peaks, reverse=True))

    def test_a_higher_threshold_returns_a_subset(self):
        """THE TWIN for the threshold actually being applied."""
        high = self.above(self.threshold + self.MARGIN / 2)
        self.assertLessEqual(
            sum(e["n_records"] for e in high["excursions"]),
            sum(e["n_records"] for e in self.r["excursions"]),
        )
        for e in high["excursions"]:
            self.assertGreaterEqual(e["peak_hr"], self.threshold + self.MARGIN / 2)
        # ...and above the corpus peak there is nothing at all.
        self.assertEqual(self.above(self.peak + 1.0)["n_excursions"], 0)

    def test_a_lower_threshold_returns_a_superset(self):
        low = self.above(self.threshold - self.MARGIN / 2)
        self.assertGreaterEqual(
            sum(e["n_records"] for e in low["excursions"]),
            sum(e["n_records"] for e in self.r["excursions"]),
        )
        self.assertGreaterEqual(low["n_runs_with_any"], self.r["n_runs_with_any"])

    def test_each_excursion_carries_what_was_happening(self):
        """The same peak while climbing and on the flat are different facts."""
        for e in self.r["excursions"]:
            for field in (
                "start_offset",
                "span_s",
                "n_records",
                "n_flagged",
                "sampling_regime",
                "peak_at_utc",
                "mean_hr",
                "peak_window_mean_hr",
                "pace_basis",
                "median_cadence_spm",
                "median_grade_pct",
            ):
                self.assertIn(field, e)

    def test_pace_is_present_and_says_which_source_it_used(self):
        with_pace = [e for e in self.r["excursions"] if e["mps"] is not None]
        if not with_pace:
            self.skipTest("no excursion in this corpus has a distance to pace")
        for e in with_pace:
            self.assertIn(
                e["pace_basis"],
                (
                    "records_enriched.path_distance_m",
                    "records.distance_m (device odometer)",
                ),
            )
            self.assertIsNotNone(e["pace_per_mile"])

    def test_grade_is_absent_rather_than_zero_where_no_DEM_covers_the_run(self):
        """Absent and flat are different."""
        no_grade = [e for e in self.r["excursions"] if not e["grade_available"]]
        if not no_grade:
            self.skipTest("every excursion is on DEM-covered ground")
        for e in no_grade:
            self.assertIsNone(e["median_grade_pct"])

    def test_the_sampling_regime_travels_with_every_excursion(self):
        """A 4 s Smart-Recording excursion and a 1 Hz one are not the same
        resolution, and the number of records alone does not say which."""
        for e in self.r["excursions"]:
            self.assertIn(e["sampling_regime"], ("1hz", "smart_recording", "unknown"))
            self.assertGreater(e["median_sample_gap_s"], 0)


class TestTheUnresolvedRunIsMarkedNotDropped(_Corpus):
    """§14e-bis: a run whose peak's provenance is open is configured as
    `unresolved_peak_run`. It is MARKED, never excluded, and every total is
    reported a second time without it. Unset, nothing is split."""

    def test_unset_nothing_is_marked_and_the_split_says_so(self):
        r = self.above(unresolved_peak_run=None)
        w = r["without_unresolved_peak"]
        self.assertEqual(w["status"], "not_configured")
        self.assertIsNone(w["run_excluded"])
        self.assertTrue(w["reason"])
        self.assertFalse(any(e["unresolved_provenance"] for e in r["excursions"]))
        self.assertIn("none configured", r["applied_filters"]["unresolved_peak"])

    def test_set_it_is_present_and_its_excursions_are_marked(self):
        """THE TWIN: the corpus peak's own run, configured as unresolved."""
        r = self.above(unresolved_peak_run=self.peak_run)
        self.assertIn(self.peak_run, {e["run_id"] for e in r["excursions"]})
        for e in r["excursions"]:
            self.assertEqual(e["unresolved_provenance"], e["run_id"] == self.peak_run)
        self.assertIn(self.peak_run, r["applied_filters"]["unresolved_peak"])

    def test_set_the_same_figures_are_reported_without_it(self):
        r = self.above(unresolved_peak_run=self.peak_run)
        w = r["without_unresolved_peak"]
        self.assertEqual(w["status"], "ok")
        self.assertEqual(w["run_excluded"], self.peak_run)
        self.assertEqual(
            w["n_excursions"],
            sum(1 for e in r["excursions"] if e["run_id"] != self.peak_run),
        )
        self.assertLess(w["n_excursions"], r["n_excursions"])
        # PIN THE PROPERTY, NOT THE FIGURE: the without-figure is real, no
        # higher than the with-figure, and comes from another run.
        if w["peak_hr"] is not None:
            self.assertLessEqual(w["peak_hr"], r["excursions"][0]["peak_hr"])
            self.assertIn(
                w["peak_hr"],
                {e["peak_hr"] for e in r["excursions"] if e["run_id"] != self.peak_run},
            )

    def test_a_configured_run_absent_from_the_selection_drops_nothing(self):
        r = self.above(unresolved_peak_run="2000-01-01T0000Z")
        self.assertFalse(any(e["unresolved_provenance"] for e in r["excursions"]))
        self.assertEqual(
            r["without_unresolved_peak"]["n_excursions"], r["n_excursions"]
        )

    def test_the_ladder_reports_both_ways_too(self):
        with use_config(unresolved_peak_run=None):
            t = O.time_above_hr(self.con)
        self.assertEqual(
            t["corpus_without_unresolved_peak"]["status"], "not_configured"
        )
        self.assertFalse(any(r["unresolved_provenance"] for r in t["per_run"]))

        with use_config(unresolved_peak_run=self.peak_run):
            t = O.time_above_hr(self.con)
        w = t["corpus_without_unresolved_peak"]
        self.assertEqual(w["status"], "ok")
        self.assertEqual(w["run_excluded"], self.peak_run)
        for th in t["thresholds_bpm"]:
            self.assertLessEqual(w["totals"][th]["seconds"], t["corpus"][th]["seconds"])


class TestFlagsAreReportedNotApplied(_Corpus):
    def test_flagged_records_are_counted_and_kept(self):
        """At the top of the range a §15.4 flag may mark a real artifact or a
        real spike, and the flag alone cannot decide which -- so the athlete
        judges, which needs the record present."""
        r = self.above()
        self.assertFalse(r["applied_filters"]["quality_flags_excluded"])
        flagged = [e for e in r["excursions"] if e["n_flagged"] > 0]
        if not flagged:
            r = self.above(self.threshold - 3 * self.MARGIN)
            flagged = [e for e in r["excursions"] if e["n_flagged"] > 0]
        if not flagged:
            self.skipTest("no flagged record in any excursion of this corpus")
        for e in flagged:
            self.assertTrue(e["flags"], "the flag NAMES must travel too")
            self.assertLessEqual(e["n_flagged"], e["n_records"])


class TestTheSeriesIsRaw(_Corpus):
    def test_peaks_match_the_raw_records_not_a_shifted_series(self):
        r = self.above()
        for e in r["excursions"]:
            aid_peak = self.con.execute(
                """
                SELECT max(r.heart_rate) FROM records r
                JOIN activities a USING (activity_id)
                WHERE a.run_id = ?""",
                [e["run_id"]],
            ).fetchone()[0]
            self.assertLessEqual(e["peak_hr"], float(aid_peak))
        self.assertIn("RAW", r["applied_filters"]["hr_series"])
        self.assertIn("NEVER hr_shifted", r["applied_filters"]["hr_series"])

    def test_the_corpus_peak_appears_as_an_excursion_peak(self):
        """THE POSITIVE TWIN: the highest excursion peak must equal the highest
        single record in the selection, or the excursion logic is dropping it."""
        r = self.above()
        self.assertEqual(max(e["peak_hr"] for e in r["excursions"]), self.peak)


class TestTheLadder(_Corpus):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # The top rung is ABOVE the corpus peak, so every run has a real zero.
        cls.ladder = (
            cls.threshold - 10.0,
            cls.threshold - 5.0,
            cls.threshold,
            cls.peak + 1.0,
        )
        with use_config(time_above_ladder_bpm=cls.ladder):
            cls.t = O.time_above_hr(cls.con)

    def test_the_ladder_is_the_configured_one(self):
        self.assertEqual(self.t["thresholds_bpm"], list(self.ladder))
        self.assertFalse(hasattr(O, "TIME_ABOVE_LADDER"))
        with use_config():
            t = O.time_above_hr(self.con)
        self.assertEqual(
            t["thresholds_bpm"], sorted(C.AthleteConfig().time_above_ladder_bpm)
        )

    def test_an_empty_ladder_is_refused(self):
        with use_config(time_above_ladder_bpm=()):
            with self.assertRaises(ValueError):
                O.time_above_hr(self.con)

    def test_the_ladder_is_monotonic_per_run_and_corpus_wide(self):
        """Time at or above a higher rung cannot exceed time at or above a
        lower one. A failure here means the weighting is being recomputed per
        threshold."""
        th = self.t["thresholds_bpm"]
        for r in self.t["per_run"]:
            s = r["seconds_at_or_above"]
            for lo, hi in zip(th, th[1:], strict=False):
                self.assertGreaterEqual(s[lo], s[hi], r["run_id"])
        c = self.t["corpus"]
        for lo, hi in zip(th, th[1:], strict=False):
            self.assertGreaterEqual(c[lo]["seconds"], c[hi]["seconds"])

    def test_it_answers_repeatedly_or_twice(self):
        """The count of RUNS is the point, not the seconds: one run with six
        excursions and six runs with one each are different facts."""
        c = self.t["corpus"]
        th = self.t["thresholds_bpm"]
        for t in th:
            self.assertIn("n_runs", c[t])
        self.assertGreaterEqual(c[th[0]]["n_runs"], c[th[-1]]["n_runs"])
        self.assertGreater(c[th[0]]["n_runs"], 0)

    def test_a_run_with_no_time_in_the_band_reports_zero_not_absence(self):
        top = self.t["thresholds_bpm"][-1]
        self.assertTrue(self.t["per_run"])
        for r in self.t["per_run"]:
            self.assertEqual(r["seconds_at_or_above"][top], 0)
        self.assertEqual(self.t["corpus"][top]["n_runs"], 0)

    def test_the_time_basis_is_declared_as_a_dose(self):
        f = self.t["applied_filters"]
        self.assertIn("DOSE", f["time_basis"])
        self.assertIn("not a rate", f["time_basis"])


if __name__ == "__main__":
    unittest.main()
