"""§6.0 the HR ceiling, and M3's HR-lag recovery gate.

TWO THINGS THAT LOOK UNRELATED AND ARE THE SAME POINT: a maximum selects for
positive measurement error, and a short walk's recovery figure is noise the
instrument cannot see past. Both are cases of a number that can be produced
being worse than a number withheld.

The corpus tests find their subjects by PROPERTY (a run with both short and
long walks, a run whose session summary disagrees with its record stream) and
assert invariants that hold on any athlete's corpus. No declared or measured
heart rate of anyone is pinned here.

Every negative has a positive twin.
"""

from __future__ import annotations

import datetime as dt
import unittest

from trail_factors.analysis import cycles as CY
from trail_factors.analysis.basic import MAX_HR_WINDOW_S, average_hr, max_hr
from trail_factors.analysis.over import max_hr_over
from trail_factors.analysis.zones import HR_LAG_S
from tests.corpus import CorpusTestCase, corpus_path, one, isolated


class TestTheRecoveryGate(unittest.TestCase):
    """A walk must outlast the HR tail to be measurable."""

    def test_the_threshold_is_DERIVED_from_the_lag(self):
        """Not picked. A better lag estimate must re-gate the corpus without
        anyone remembering to change a second number."""
        self.assertEqual(CY.MIN_WALK_FOR_RECOVERY_S,
                         HR_LAG_S + CY.HR_RECOVERY_MARGIN_S)
        self.assertGreater(CY.MIN_WALK_FOR_RECOVERY_S, HR_LAG_S)

    def test_it_is_registered_in_the_digest(self):
        from trail_factors.analysis.versions import calc_params, digest
        params = calc_params()
        self.assertEqual(params["cycles_min_walk_for_recovery_s"],
                         CY.MIN_WALK_FOR_RECOVERY_S)
        self.assertNotEqual(digest(dict(params, cycles_min_walk_for_recovery_s=15.0)),
                            digest(params))


# --- isolation: never the real athlete state or config ----------------------
def setUpModule():
    if corpus_path() is not None:
        unittest.enterModuleContext(isolated())


def _garmin_running(con, where: str = "TRUE", what: str = "a qualifying run"):
    """First garmin_watch running activity (newest first) matching `where`."""
    return one(con, f"""
        SELECT a.activity_id FROM activities a
        WHERE a.source = 'garmin_watch' AND a.sport = 'running' AND ({where})
        ORDER BY a.start_time_utc DESC LIMIT 1""", what=what)[0]


class TestTheGateOnRealWalks(CorpusTestCase):
    """The case that motivated the gate: a short walk after a climb.

    THE FIXTURE IS A REAL RUN with both kinds of walk, found by that property.
    A synthetic one would test the arithmetic and not the claim.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        aid = _garmin_running(cls.con, """
            EXISTS (SELECT 1 FROM splits s WHERE s.activity_id = a.activity_id
                    AND s.split_type = 'rwd_walk' AND s.total_timer_time < 20)
            AND EXISTS (SELECT 1 FROM splits s WHERE s.activity_id = a.activity_id
                    AND s.split_type = 'rwd_walk' AND s.total_timer_time >= 120)
            AND EXISTS (SELECT 1 FROM records r WHERE r.activity_id = a.activity_id
                    AND r.heart_rate > 0)""",
            what="run with both a sub-20 s walk and a 120 s+ walk")
        cls.result = CY.cycles(cls.con, aid)

    def _walks(self, lo=0.0, hi=float("inf")):
        return [c for c in self.result["cycles"]
                if c["walk_duration_s"] is not None
                and lo <= c["walk_duration_s"] < hi]

    def test_a_SHORT_walk_reports_shorter_than_hr_lag_not_a_number(self):
        short = self._walks(hi=CY.MIN_WALK_FOR_RECOVERY_S)
        self.assertTrue(short, "the subject run should contain short walks")
        for c in short:
            self.assertIsNone(c["hr_recovered_bpm"], c["walk_duration_s"])
            self.assertEqual(c["hr_recovery_status"], "shorter_than_hr_lag")
            self.assertIn("instrument", c["hr_recovery_reason"].lower())

    def test_the_shortest_walk_specifically(self):
        """The case as reported: a walk of a few seconds, after climbing."""
        c = min(self._walks(), key=lambda x: x["walk_duration_s"])
        self.assertLess(c["walk_duration_s"], CY.MIN_WALK_FOR_RECOVERY_S)
        self.assertIsNone(c["hr_recovered_bpm"])
        self.assertEqual(c["hr_recovery_status"], "shorter_than_hr_lag")

    def test_a_LONG_walk_still_reports_a_figure(self):
        """THE TWIN. A gate that suppressed everything would pass the test
        above and destroy the metric."""
        long = self._walks(lo=90.0)
        self.assertTrue(long, "the subject run should contain a 90 s+ walk")
        for c in long:
            self.assertEqual(c["hr_recovery_status"], "ok")
            self.assertIsNotNone(c["hr_recovered_bpm"])

    def test_the_walk_still_counts_as_a_cycle(self):
        """Only the recovery figure is withheld -- the cycle, its durations and
        its paces are all still reported."""
        short = [c for c in self.result["cycles"]
                 if c["hr_recovery_status"] == "shorter_than_hr_lag"]
        for c in short:
            self.assertIsNotNone(c["walk_duration_s"])
            self.assertIsNotNone(c["run_duration_s"])
        self.assertEqual(self.result["n_complete_cycles"], len(self._walks()))

    def test_the_summary_counts_both_kinds(self):
        """A median over some of the walks is a different claim from one over
        all of them."""
        s = self.result["summary"]
        self.assertGreater(s["n_recovery_shorter_than_hr_lag"], 0)
        self.assertGreater(s["n_recovery_measurable"], 0)
        self.assertEqual(s["n_recovery_measurable"]
                         + s["n_recovery_shorter_than_hr_lag"],
                         self.result["n_complete_cycles"])

    def test_the_gate_is_declared_in_the_filters(self):
        f = self.result["filters"]
        self.assertEqual(f["min_walk_for_recovery_s"], CY.MIN_WALK_FOR_RECOVERY_S)
        self.assertEqual(f["hr_lag_s"], HR_LAG_S)
        self.assertIn("WITHHELD", f["hr_recovery_gate"])


class TestMaxHrPerRun(CorpusTestCase):

    def test_ACCEPTANCE_recorded_ge_30s_ge_average_on_every_run(self):
        checked = 0
        for (aid,) in self.con.execute("SELECT activity_id FROM activities").fetchall():
            m = max_hr(self.con, aid)
            rec = m["max_hr_recorded"]["value"]
            if rec is None:
                continue                      # no HR at all
            w30 = m["max_hr_30s"]["value"]
            if w30 is None:
                continue                      # no 30 s window could fill
            checked += 1
            avg = average_hr(self.con, aid)["value"]
            self.assertGreaterEqual(rec, w30, aid)
            if avg is not None:
                self.assertGreaterEqual(w30, avg, aid)
        self.assertGreater(checked, 0, "expected HR on some activity")

    def test_ACCEPTANCE_the_window_is_not_lower_than_its_floor(self):
        """INDEPENDENT LOWER BOUND. If every unflagged record across a filled
        window reads at least X, that window's time-weighted mean is at least
        X -- so the best window can never fall below the best such X. A lower
        figure means the window logic is wrong, not that the run was easier
        than it was."""
        aid = _garmin_running(self.con, """a.sampling_regime = '1hz' AND EXISTS (
            SELECT 1 FROM records r WHERE r.activity_id = a.activity_id
              AND r.heart_rate > 0)""", what="1 Hz watch run with heart rate")
        flagged = {t for t, in self.con.execute("""
            SELECT timestamp FROM records_enriched WHERE activity_id = ?
              AND coalesce(quality_flags, '') <> ''""", [aid]).fetchall()}
        rows = [r for r in self.con.execute("""
            SELECT timestamp, heart_rate FROM records WHERE activity_id = ?
              AND heart_rate > 0 ORDER BY timestamp""", [aid]).fetchall()
                if r[0] not in flagged]
        floor = None
        for i in range(len(rows)):
            j = i
            while (j + 1 < len(rows) and (rows[j + 1][0] - rows[i][0])
                   .total_seconds() <= MAX_HR_WINDOW_S):
                j += 1
            if (rows[j][0] - rows[i][0]).total_seconds() < MAX_HR_WINDOW_S * 0.8:
                continue
            lo = min(h for _, h in rows[i:j])
            floor = lo if floor is None else max(floor, lo)
        if floor is None:
            self.skipTest("no 30 s window of unflagged records on the subject")
        self.assertGreaterEqual(max_hr(self.con, aid)["max_hr_30s"]["value"],
                                floor - 1e-9)

    def test_the_series_is_RAW_not_lag_shifted(self):
        """The 40 s shift aligns HR to the effort that caused it, which is what
        MATCHING needs. A ceiling wants the peak as recorded."""
        aid = _garmin_running(self.con, """EXISTS (SELECT 1 FROM records r
            WHERE r.activity_id = a.activity_id AND r.heart_rate > 0)""",
            what="watch run with heart rate")
        m = max_hr(self.con, aid)
        self.assertIn("RAW", m["filters"]["hr_series"])
        raw_peak = self.con.execute(
            "SELECT max(heart_rate) FROM records WHERE activity_id = ?",
            [aid]).fetchone()[0]
        self.assertEqual(m["max_hr_recorded"]["value"], float(raw_peak))

    def test_the_window_is_time_weighted_and_full(self):
        aid = _garmin_running(self.con, """EXISTS (SELECT 1 FROM records r
            WHERE r.activity_id = a.activity_id AND r.heart_rate > 0)""",
            what="watch run with heart rate")
        w = max_hr(self.con, aid)["max_hr_30s"]
        if w["value"] is None:
            self.skipTest(f"no window on the subject: {w['reason']}")
        self.assertGreaterEqual(w["window_seconds"], MAX_HR_WINDOW_S * 0.8)
        self.assertLessEqual(w["window_seconds"], MAX_HR_WINDOW_S + 1e-9)
        self.assertIsNotNone(w["flagged_record_fraction"])

    def test_flagged_records_are_excluded_from_the_window(self):
        """A maximum selects for positive measurement error -- the same rule
        the windowed bests follow."""
        aid = one(self.con, """
            SELECT activity_id FROM records_enriched
            WHERE coalesce(quality_flags, '') <> ''
            GROUP BY 1 ORDER BY count(*) DESC LIMIT 1""",
            what="activity with a quality-flagged record")[0]
        m = max_hr(self.con, aid)
        self.assertTrue(m["filters"]["quality_flags_excluded"])
        if m["max_hr_30s"]["value"] is None:
            self.skipTest("the most-flagged activity has no heart rate")
        self.assertIn("excluded", m["max_hr_30s"]["source"])

    def test_a_run_with_no_flags_available_says_so(self):
        """THE TWIN: an unfiltered figure must not claim to be §15.4-clean."""
        aid = one(self.con, """
            SELECT activity_id FROM activities a
            WHERE NOT EXISTS (SELECT 1 FROM records_enriched e
                              WHERE e.activity_id = a.activity_id)
              AND EXISTS (SELECT 1 FROM records r
                          WHERE r.activity_id = a.activity_id
                            AND r.heart_rate IS NOT NULL)
            ORDER BY activity_id LIMIT 1""",
            what="activity with heart rate and no enriched records")[0]
        m = max_hr(self.con, aid)
        self.assertFalse(m["filters"]["quality_flags_excluded"])
        self.assertIn("NOT §15.4-clean", m["filters"]["quality_flags_reason"])

    def test_a_session_record_difference_is_REPORTED_not_resolved(self):
        """A run whose session summary peaks ABOVE its record stream. BOTH ARE
        THE SAME WATCH, same file -- the summary is computed over continuous
        internal sampling while records are written on the Smart Recording
        schedule, so the stream is a decimated view and can only understate.
        Preferring either would hide that."""
        aid, sess, rec = one(self.con, """
            SELECT a.activity_id, a.session_max_hr, max(r.heart_rate)
            FROM activities a JOIN records r USING (activity_id)
            WHERE a.session_max_hr IS NOT NULL AND r.heart_rate > 0
            GROUP BY 1, 2 HAVING a.session_max_hr > max(r.heart_rate)
            ORDER BY 1 LIMIT 1""",
            what="activity whose session max HR exceeds its records")
        m = max_hr(self.con, aid)
        self.assertFalse(m["session_agrees"])
        self.assertIn("SAME WATCH", m["session_note"])
        self.assertIn("DECIMATED", m["session_note"])
        # NEITHER IS OVERWRITTEN.
        self.assertEqual(m["max_hr_recorded"]["value"], float(rec))
        self.assertEqual(m["session_max_hr"], float(sess))

    def test_agreement_is_reported_too(self):
        """THE TWIN: a cross-check that only ever fires on disagreement cannot
        be distinguished from one that never runs."""
        aid = one(self.con, """
            SELECT a.activity_id FROM activities a JOIN records r USING (activity_id)
            WHERE a.session_max_hr IS NOT NULL AND r.heart_rate > 0
            GROUP BY a.activity_id, a.session_max_hr
            HAVING a.session_max_hr = max(r.heart_rate)
            ORDER BY 1 LIMIT 1""",
            what="activity whose session max HR equals its records")[0]
        m = max_hr(self.con, aid)
        self.assertTrue(m["session_agrees"])
        self.assertIsNone(m["session_note"])

    def test_the_difference_is_a_RESOLUTION_property_not_an_ingest_one(self):
        """THE MEASUREMENT BEHIND THE CLAIM. If the record stream were losing
        peaks to ingest, 1 Hz would lose them too and the sign would wander.
        It does neither: 1 Hz agrees exactly, Smart Recording can understate,
        and the summary is NEVER lower -- because skipping a sample can only
        lose a peak, not invent one."""
        rows = self.con.execute("""
            SELECT a.sampling_regime, a.session_max_hr
                   - (SELECT max(heart_rate) FROM records r
                      WHERE r.activity_id = a.activity_id)
            FROM activities a WHERE a.session_max_hr IS NOT NULL""").fetchall()
        by = {}
        for regime, diff in rows:
            if diff is None:
                continue
            by.setdefault(regime, []).append(diff)
        if not by:
            self.skipTest("this corpus has no session max HR beside records")
        self.assertTrue(all(d >= 0 for v in by.values() for d in v),
                        "the session summary must never be LOWER than the "
                        "record stream")
        self.assertTrue(all(d == 0 for d in by.get("1hz", [])),
                        "1 Hz writes every sample, so the two must agree exactly")

    def test_a_run_with_no_heart_rate_is_unavailable_not_zero(self):
        aid = one(self.con, """
            SELECT activity_id FROM activities a
            WHERE NOT EXISTS (SELECT 1 FROM records r
                              WHERE r.activity_id = a.activity_id
                                AND r.heart_rate IS NOT NULL)
            ORDER BY activity_id LIMIT 1""",
            what="activity with no heart rate")[0]
        m = max_hr(self.con, aid)
        self.assertIsNone(m["max_hr_recorded"]["value"])
        self.assertEqual(m["max_hr_recorded"]["status_hint"], "unavailable")


class TestMaxHrOverTheCorpus(CorpusTestCase):
    """"Highest ever recorded" is only usable with the run attached."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.r = max_hr_over(cls.con)
        if not cls.r["per_run"]:
            raise unittest.SkipTest("this corpus has no watch run with HR")

    def test_the_winner_carries_its_circumstances(self):
        """A maximum on a hot or compromised run is a different fact from one
        on a hard effort."""
        for key in ("max_hr_recorded", "max_hr_30s"):
            w = self.r[key]
            for field in ("run_id", "date", "run_type", "run_type_status",
                          "dew_point_c", "exclude_from_trend",
                          "exclude_from_trend_status"):
                self.assertIn(field, w, f"{key}.{field}")

    def test_absent_context_is_not_FALSE_context(self):
        """A run nobody described has not been declared representative -- it
        has not been declared at all. This build collects no per-run context,
        so every run carries null WITH THE LABEL, never a bare null and never
        False."""
        from trail_factors.ingest.answers import NOT_COLLECTED
        self.assertTrue(self.r["per_run"])
        for r in self.r["per_run"]:
            self.assertIsNone(r["exclude_from_trend"])      # None, not False
            self.assertEqual(r["exclude_from_trend_status"], NOT_COLLECTED)
            self.assertIsNone(r["run_type"])
            self.assertEqual(r["run_type_status"], NOT_COLLECTED)
            for gone in ("session_context", "run_type_group"):
                self.assertNotIn(gone, r)

    def test_the_corpus_peak_matches_the_per_run_maximum(self):
        best = max(r["max_hr_recorded"] for r in self.r["per_run"])
        self.assertEqual(self.r["max_hr_recorded"]["value"], best)

    def test_the_two_ceilings_are_ranked_separately(self):
        self.assertIn("same_run", self.r)
        top30 = max(r["max_hr_30s"] for r in self.r["per_run"]
                    if r["max_hr_30s"] is not None)
        self.assertEqual(self.r["max_hr_30s"]["value"], top30)
        self.assertEqual(self.r["same_run"],
                         self.r["max_hr_recorded"]["run_id"]
                         == self.r["max_hr_30s"]["run_id"])

    def _declared(self):
        d = self.r["declared_ceiling"]
        if d["status"] != "ok":
            self.skipTest(f"declared ceiling is {d['status']}: "
                          f"{str(d.get('reason'))[:60]}")
        return d

    def test_the_declared_ceiling_is_compared_and_NOT_corrected(self):
        d = self._declared()
        self.assertTrue(d["declared"])
        self.assertIn("REPORTED, not corrected", d["do_not"])

    def test_the_gaps_are_arithmetic_on_the_figures_beside_them(self):
        """The anchor is the LATEST declared value, and each gap is that minus
        the figure it names -- the record stream's single sample, and the
        watch's session summary. No sign is pinned: whether an anchor sits
        above or below the demonstrated ceiling is a fact about one athlete,
        and it is REPORTED either way."""
        d = self._declared()
        anchor = d["declared"][-1]
        self.assertEqual(d["gap_bpm"], anchor - d["demonstrated_single_sample"])
        if d["demonstrated_session_summary"] is not None:
            self.assertEqual(d["gap_vs_session_summary_bpm"],
                             anchor - d["demonstrated_session_summary"])
        best = max(v for v in (d["demonstrated_single_sample"],
                               d["demonstrated_session_summary"]) if v is not None)
        if anchor > best:
            self.assertIn("shifts every one of them upward", d["note"])
        else:
            self.assertIn("meets or exceeds the declared anchor", d["note"])

    def test_the_session_summary_never_reads_below_the_record_stream(self):
        """The record stream is a decimated view of what the watch sampled:
        skipping a sample can lose a peak, never invent one."""
        d = self._declared()
        if d["demonstrated_session_summary"] is None:
            self.skipTest("no session max HR in the selection")
        self.assertGreaterEqual(d["demonstrated_session_summary"],
                                d["demonstrated_single_sample"])

    def test_a_selection_narrows_it(self):
        dates = sorted(r["date"] for r in self.r["per_run"])
        mid = dates[len(dates) // 2]
        if mid == dates[0]:
            self.skipTest("too few distinct run dates to narrow")
        narrow = max_hr_over(self.con, start_date=mid)
        self.assertLess(narrow["selection"]["n_included"],
                        self.r["selection"]["n_included"])
        self.assertLessEqual(narrow["max_hr_recorded"]["value"],
                             self.r["max_hr_recorded"]["value"])


class TestDeclaredAsOfTheSelection(CorpusTestCase):
    """A dated selection is compared against the max HR declared AS OF ITS
    END, never against a value first declared later. Expectations are read
    from the corpus at test time, so a new file cannot stale them."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # RUNNING FILES ONLY: the selection is running, and Garmin keeps a
        # separate max per sport.
        cls.first = dict(cls.con.execute("""
            SELECT declared_max_hr, min(CAST(start_time_utc AS DATE))
            FROM activities WHERE declared_max_hr IS NOT NULL
              AND sport = 'running' AND source = 'garmin_watch'
            GROUP BY 1""").fetchall())
        if not cls.first:
            raise unittest.SkipTest("this corpus has no declared running max HR")

    def declared(self, **kw):
        return max_hr_over(self.con, **kw)["declared_ceiling"]

    def test_a_value_first_declared_after_end_date_is_not_compared(self):
        latest_first = max(self.first.values())
        end = (latest_first - dt.timedelta(days=1)).isoformat()
        later = {v for v, f in self.first.items() if f >= latest_first}
        if len(self.first) < 2:
            self.skipTest("this corpus declares only one running max HR")
        d = self.declared(end_date=end)
        self.assertFalse(later & set(d["declared"] or []), d["declared"])
        self.assertIn(end, d["declared_as_of"])

    def test_positive_twin_no_end_date_sees_every_declaration(self):
        d = self.declared()
        self.assertEqual(sorted(d["declared"]), sorted(self.first))
        self.assertIn("every file", d["declared_as_of"])

    def test_another_sports_declaration_never_reaches_a_running_selection(self):
        """Garmin keeps a max per sport; a ride's must not sit beside running
        HR."""
        other = {v for v, in self.con.execute("""
            SELECT DISTINCT declared_max_hr FROM activities
            WHERE declared_max_hr IS NOT NULL AND sport <> 'running'
            EXCEPT SELECT declared_max_hr FROM activities
            WHERE sport = 'running'""").fetchall()}
        if not other:
            self.skipTest("this corpus has no value only another sport declared")
        d = self.declared()
        self.assertFalse(other & set(d["declared"]), d["declared"])
        self.assertIn("sport running only", d["declared_as_of"])

    def test_an_end_date_before_any_declaration_is_unavailable_naming_it(self):
        end = (min(self.first.values()) - dt.timedelta(days=1)).isoformat()
        d = self.declared(end_date=end)
        self.assertEqual(d["status"], "unavailable")
        self.assertIn(end, d["reason"])


if __name__ == "__main__":
    unittest.main()
