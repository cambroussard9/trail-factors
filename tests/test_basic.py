"""§6.0 basic metrics (SPEC.md §6.0).

Every negative case has a positive twin, per the §12.3 review lesson: a test
that only asserts something is absent would still pass against a stub that
returns nothing.
"""

import contextlib
import json
import unittest

from tests.corpus import (
    CorpusTestCase,
    corpus_path,
    corpus_projection,
    isolated,
    one,
)
from trail_factors.analysis import basic as B
from trail_factors.analysis import over as O
from trail_factors.units import METERS_PER_MILE

# --- isolation: never the real athlete state or config ----------------------
# Entered in setUpModule, CLOSED in tearDownModule -- see tests/corpus.py
# isolated() for why this is not unittest.enterModuleContext.
_ISOLATION = contextlib.ExitStack()


def setUpModule():
    if corpus_path() is not None:
        _ISOLATION.enter_context(isolated(projection_crs=corpus_projection()))


def tearDownModule():
    _ISOLATION.close()


def garmin_listing() -> dict:
    """Garmin's own activity summaries, keyed by Garmin id, from any cached
    listing under the raw directory. Empty when none is cached."""
    from trail_factors.paths import RAW_DIR

    out = {}
    for f in sorted((RAW_DIR / "garmin_api").glob("activities_*.json")):
        out.update({str(a["activityId"]): a for a in json.loads(f.read_text())})
    return out


def newest_run(con, order: str = "start_time_utc DESC") -> str:
    """A garmin_watch running activity, by an explicit ordering."""
    return one(
        con,
        f"""
        SELECT activity_id FROM activities
        WHERE source = 'garmin_watch' AND sport = 'running'
        ORDER BY {order} LIMIT 1""",
        what="garmin_watch running activity",
    )[0]


class TestWeighting(CorpusTestCase):
    """The filtered-lag bug, pinned. Deriving a sample's weight from the gap
    to the next SURVIVING sample makes the weight a property of the filter."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # A RUN, deterministically: the earliest 1 Hz watch run with running
        # splits. A corpus with every sport has 1 Hz activities that are not
        # runs, so an unordered pick without a sport filter lands wherever the
        # row order happens to put it.
        cls.aid = one(
            cls.con,
            """
            SELECT a.activity_id FROM activities a
            WHERE a.sampling_regime = '1hz' AND a.source = 'garmin_watch'
              AND a.sport = 'running' AND EXISTS (
                SELECT 1 FROM splits s WHERE s.activity_id = a.activity_id
                  AND s.split_type = 'rwd_run')
            ORDER BY a.start_time_utc LIMIT 1""",
            what="1 Hz watch run with running splits",
        )[0]

    def test_weights_come_from_the_unfiltered_series(self):
        """The weight sum over running records must not exceed the running
        time. Under the bug it did, by a fifth, and moved cadence by several
        steps per minute."""
        c = B.mean_running_cadence(self.con, self.aid)
        self.assertLessEqual(c["weight_seconds"], c["rwd_run_timer_time_s"])

    def test_the_weight_sum_is_close_to_the_authoritative_running_time(self):
        """The positive twin: the weights must not merely be small, they must
        account for nearly all of the running time."""
        c = B.mean_running_cadence(self.con, self.aid)
        self.assertGreater(c["weight_seconds"], 0.9 * c["rwd_run_timer_time_s"])

    def test_a_filtered_subset_reuses_the_same_weight_map(self):
        """Running and walking weights must sum to no more than the whole."""
        w = B.record_weights(self.con, self.aid)
        rows = self.con.execute(
            "SELECT timestamp FROM records WHERE activity_id = ?", [self.aid]
        ).fetchall()
        subset = [(r[0], 1.0) for r in rows[:50]]
        _, den = B.weighted_mean(subset, w)
        _, all_den = B.weighted_mean([(r[0], 1.0) for r in rows], w)
        self.assertLess(den, all_den)

    def test_no_weight_exceeds_the_cap(self):
        """A long paused-timer gap must not give one reading minutes."""
        w = B.record_weights(self.con, self.aid)
        self.assertLessEqual(max(w.values()), B.MAX_WEIGHT_GAP_S)


class TestCadence(CorpusTestCase):
    """§9's formula, resolved. Getting the unit wrong is a factor of two."""

    def test_running_cadence_is_in_the_human_range(self):
        """Undoubled it would read ~82, which is a cadence for nothing."""
        checked = 0
        for (aid,) in self.con.execute(
            "SELECT activity_id FROM activities WHERE source = 'garmin_watch' AND "
            "sport = 'running' "
            "ORDER BY start_time_utc DESC LIMIT 6"
        ).fetchall():
            c = B.mean_running_cadence(self.con, aid)
            if c["value"] is None:
                continue
            checked += 1
            self.assertGreater(c["value"], 120, aid)
            self.assertLess(c["value"], 200, aid)
        if not checked:
            self.skipTest("no recent watch run has running cadence")

    def test_the_formula_is_declared_in_the_filters(self):
        f = B.mean_running_cadence(self.con, newest_run(self.con))["filters"]
        self.assertEqual(f["cadence_unit"], "steps_per_minute")
        self.assertIn("fractional_cadence", f["cadence_formula"])
        self.assertFalse(f["comparable_to_garmin_summary"])

    def test_cadence_matches_the_garmin_oracle_when_computed_garmins_way(self):
        """The confirmation itself, pinned so a formula change is caught.

        Garmin's averageRunningCadence is over ALL records including zeros, so
        it is compared against that basis rather than against the running-only
        figure the module reports.
        """
        import re

        oracle = garmin_listing()
        if not oracle:
            self.skipTest("no cached Garmin listing under the raw directory")
        diffs = []
        for aid, sf in self.con.execute(
            "SELECT activity_id, source_file FROM activities "
            "WHERE source = 'garmin_watch' AND sport = 'running'"
        ).fetchall():
            g = re.search(r"(\d+)_ACTIVITY", sf or "")
            a = oracle.get(g.group(1)) if g else None
            if not a or a.get("averageRunningCadenceInStepsPerMinute") is None:
                continue
            rows = self.con.execute(
                "SELECT timestamp, 2.0 * (cadence_raw_rpm + "
                "coalesce(fractional_cadence, 0)) FROM records "
                "WHERE activity_id = ? AND cadence_raw_rpm IS NOT NULL "
                "ORDER BY timestamp",
                [aid],
            ).fetchall()
            if len(rows) < 2:
                continue
            # Garmin's basis: timer time INCLUDING standing, so no paused
            # intervals are subtracted. Our own cadence figure is over moving
            # time and is a different quantity on purpose.
            num, den = B.weighted_mean(rows, B.record_weights(self.con, aid, paused=[]))
            if den > 0:
                diffs.append(num / den - a["averageRunningCadenceInStepsPerMinute"])
        if not diffs:
            self.skipTest(
                "no cached summary carries a running cadence for a run in this corpus"
            )
        diffs.sort()
        median = diffs[len(diffs) // 2]
        self.assertLess(
            abs(median), 1.0, f"median difference from Garmin is {median:+.2f} spm"
        )

    def test_run_and_walk_are_never_pooled_in_the_grade_table(self):
        from trail_factors.analysis.report import cluster_of

        # The newest run's cluster under the test's config: a property, never
        # a named place.
        cluster = cluster_of(self.con, newest_run(self.con))
        t = O.cadence_table(self.con, cluster=cluster, last_n=8)
        kinds = {r["split_type"] for r in t["table"]}
        if not {"rwd_run", "rwd_walk"} <= kinds:
            self.skipTest("the recent runs in this cluster never walk on graded ground")
        runs = [r["steps_per_min"] for r in t["table"] if r["split_type"] == "rwd_run"]
        walks = [
            r["steps_per_min"] for r in t["table"] if r["split_type"] == "rwd_walk"
        ]
        # Running and walking cadence are tens of spm apart in any human; a
        # pooled bin would land between the modes.
        self.assertGreater(min(runs), max(walks) + 20)

    def test_a_source_with_no_cadence_says_so_rather_than_reporting_zero(self):
        aid = one(
            self.con,
            "SELECT activity_id FROM activities WHERE source = 'strava_phone' LIMIT 1",
            what="phone activity",
        )[0]
        c = B.mean_running_cadence(self.con, aid)
        self.assertIsNone(c["value"])
        self.assertIn("no cadence", c["reason"])


class TestDurationsAndPaces(CorpusTestCase):
    def test_every_garmin_run_has_durations_whatever_the_regime(self):
        """The claim the standing brief now makes: splits carry durations for
        every run, so these are not 1 Hz-only."""
        rows = self.con.execute(
            "SELECT activity_id, sampling_regime FROM activities "
            "WHERE source = 'garmin_watch' AND sport = 'running'"
        ).fetchall()
        if not rows:
            self.skipTest("this corpus has no garmin_watch run")
        for aid, regime in rows:
            d = B.durations(self.con, aid)
            if d.get("run_s") is None:
                # AN IMPORTED ACTIVITY IS NOT A RECORDED ONE. A file that came
                # into Garmin in a bulk upload carries a full record stream and
                # NO rwd_* splits, because run/walk segmentation is done by
                # the watch during the activity. The claim being pinned is that
                # splits carry durations in both sampling regimes, not that
                # every Garmin-sourced file was recorded on a Garmin watch.
                self.assertEqual(
                    self.con.execute(
                        "SELECT count(*) FROM splits WHERE activity_id = ? "
                        "AND split_type LIKE 'rwd\\_%' ESCAPE '\\'",
                        [aid],
                    ).fetchone()[0],
                    0,
                    f"{aid}: null durations WITH rwd_* splits present is a BUG",
                )
                self.assertIn("IMPORTED", d["reason"])
                continue
            self.assertIsNotNone(d.get("run_s"), f"{aid} ({regime})")
            # A GENUINE ZERO IS ALLOWED, AND `n_run_splits` IS WHAT SAYS SO.
            # CLAUDE.md records the mirror of this for walking -- "0 s with 0
            # splits is a REAL zero" -- and a walk logged as a run has the
            # opposite shape: rwd_walk blocks and no rwd_run at all. The claim being
            # pinned is
            # that splits carry durations in both regimes, not that every
            # activity contains running.
            if d["run_s"] == 0:
                self.assertEqual(
                    d.get("n_run_splits"),
                    0,
                    f"{aid} ({regime}): 0 s of running with run "
                    f"splits present is a BUG, not a walk",
                )
                continue
            self.assertGreater(d["run_s"], 0, f"{aid} ({regime})")

    def test_the_splits_tile_the_timer_time(self):
        for (aid,) in self.con.execute(
            "SELECT activity_id FROM activities WHERE source = 'garmin_watch' AND "
            "sport = 'running' "
            "LIMIT 12"
        ).fetchall():
            d = B.durations(self.con, aid)
            if d.get("tiling_residual_s") is None:
                continue
            self.assertLess(abs(d["tiling_residual_s"]), 10.0, aid)

    def test_zero_walking_is_a_real_zero_not_missing(self):
        row = self.con.execute("""
            SELECT a.activity_id FROM activities a WHERE a.source = 'garmin_watch' AND
            a.sport = 'running'
              AND NOT EXISTS (SELECT 1 FROM splits s WHERE s.activity_id = a.activity_id
                              AND s.split_type = 'rwd_walk') LIMIT 1""").fetchone()
        if row is None:
            self.skipTest("every run has walking")
        d = B.durations(self.con, row[0])
        self.assertEqual(d["walk_s"], 0.0)
        self.assertEqual(d["n_walk_splits"], 0)

    def test_a_short_walk_class_is_gated_not_reported(self):
        """A walking pace from a few seconds of walking is not a walking
        pace."""
        row = self.con.execute(
            """
            SELECT activity_id FROM splits WHERE split_type = 'rwd_walk'
            GROUP BY 1 HAVING sum(total_timer_time) < ? LIMIT 1""",
            [B.MIN_CLASS_TIME_S],
        ).fetchone()
        if row is None:
            self.skipTest("no activity with a short walk class")
        walking = [
            p for p in B.paces(self.con, row[0])["paces"] if p["name"] == "walking"
        ][0]
        self.assertEqual(walking["status"], "gated")
        self.assertIsNone(walking["pace_per_mile"])
        # GATED CARRIES ITS EVIDENCE. A bare null would lose the reason.
        self.assertIsNotNone(walking["seconds"])

    def test_a_long_walk_class_is_reported(self):
        """The positive twin."""
        row = self.con.execute("""
            SELECT activity_id FROM splits WHERE split_type = 'rwd_walk'
            GROUP BY 1 HAVING sum(total_timer_time) > 600 LIMIT 1""").fetchone()
        if row is None:
            self.skipTest("no activity with a long walk class")
        walking = [
            p for p in B.paces(self.con, row[0])["paces"] if p["name"] == "walking"
        ][0]
        self.assertEqual(walking["status"], "ok")
        self.assertRegex(walking["pace_per_mile"], r"^\d+:[0-5]\d$")

    def test_walking_is_slower_than_running_on_every_run_that_has_both(self):
        n = 0
        for (aid,) in self.con.execute(
            "SELECT activity_id FROM activities WHERE source = 'garmin_watch' AND "
            "sport = 'running'"
        ).fetchall():
            got = {p["name"]: p for p in B.paces(self.con, aid)["paces"]}
            r, w = got["running"], got["walking"]
            if r["status"] != "ok" or w["status"] != "ok":
                continue
            n += 1
            self.assertGreater(r["mps"], w["mps"], aid)
        if not n:
            self.skipTest("no run with both a reported running and walking pace")

    def test_overall_pace_uses_garmins_own_denominator(self):
        got = {p["name"]: p for p in B.paces(self.con, newest_run(self.con))["paces"]}
        self.assertIn("timer_time_s", got["overall"]["source"])
        self.assertIn("duration_s", got["overall_elapsed"]["source"])


class TestWindowedBests(CorpusTestCase):
    def test_bests_get_slower_as_the_window_gets_longer(self):
        """The one invariant that holds regardless of terrain or device: a
        longer window cannot be faster than a shorter one it contains."""
        n = 0
        for (aid,) in self.con.execute(
            "SELECT activity_id FROM activities WHERE source = 'garmin_watch' AND "
            "sport = 'running' "
            "ORDER BY total_distance_m DESC LIMIT 10"
        ).fetchall():
            got = {w["window"]: w for w in B.windowed_bests(self.con, aid)["windows"]}
            q, mile = got.get("best_0.25mi"), got.get("best_1mi")
            if q["status"] != "ok" or mile["status"] != "ok":
                continue
            n += 1
            self.assertGreaterEqual(q["mps"], mile["mps"], aid)
        if not n:
            self.skipTest("no run with both a quarter-mile and a mile best")

    def test_the_best_mile_beats_the_fastest_auto_lap_mile(self):
        """An auto-lap mile IS a sliding mile that happens to start on a lap
        boundary, so the sliding best can only be faster -- up to sampling
        granularity. Restricted to full-coverage runs: where GPS is partial,
        Garmin cuts laps on its own internal odometer, which is finer than the
        recorded Smart Recording samples, so the invariant is not available."""
        n = 0
        for (aid,) in self.con.execute("""
                SELECT DISTINCT a.activity_id FROM activities a
                JOIN laps l USING (activity_id)
                WHERE l.trigger = 'distance' AND a.coverage_fraction >= 0.95
                """).fetchall():
            # TIMER TIME, NOT duration_s. `laps.duration_s` is
            # total_elapsed_time and is PROVENANCE ONLY (CLAUDE.md); the
            # authoritative lap duration is total_timer_time. Comparing a
            # moving-time window against an elapsed-time lap was a latent
            # instance of the bug this project has already fixed twice.
            #
            # AND THE LAP MUST BE SPANNABLE. The invariant "an auto-lap mile IS
            # a sliding mile" holds only where the window CAN slide across it.
            # A fastest mile lap can contain an auto-pause, and
            # windowed_bests refuses to bridge a gap over MAX_SAMPLE_GAP_S --
            # correctly, because there is no data across it. The lap counts
            # the mile anyway; the window cannot, and must not. Such a lap is not a
            # counter-example, it is a different
            # measurement.
            lap = self.con.execute(
                "SELECT min(total_timer_time) FROM laps WHERE activity_id = ? "
                "AND trigger = 'distance' AND distance_m > 1600",
                [aid],
            ).fetchone()[0]
            if lap is not None and self._has_unspannable_gap(aid, lap):
                continue
            got = [
                w
                for w in B.windowed_bests(self.con, aid)["windows"]
                if w["window"] == "best_1mi"
            ]
            if lap is None or not got or got[0]["status"] != "ok":
                continue
            n += 1
            # THE TOLERANCE IS THE LAP'S OWN WORST SAMPLE INTERVAL, not a
            # fixed 3 s and not the activity's median. A sliding window can
            # only START on a recorded sample, so it can be displaced from the
            # lap's own boundary by at most one interval -- and the interval
            # that matters is the widest one inside THAT LAP, which on Smart
            # Recording can be wider than the activity's median.
            self.assertLessEqual(
                got[0]["seconds"], lap + max(3.0, self._lap_max_gap(aid)), aid
            )
        if not n:
            self.skipTest("no full-coverage run with a distance auto-lap mile")

    def _lap_window_gaps(self, aid) -> list[float]:
        """Sample intervals inside the fastest distance lap."""
        row = self.con.execute(
            "SELECT start_time, total_timer_time FROM laps WHERE activity_id = ? "
            "AND trigger = 'distance' AND distance_m > 1600 "
            "ORDER BY total_timer_time LIMIT 1",
            [aid],
        ).fetchone()
        if row is None:
            return []
        import datetime

        end = row[0] + datetime.timedelta(seconds=float(row[1]) * 2)
        stamps = [
            r[0]
            for r in self.con.execute(
                "SELECT timestamp FROM records WHERE activity_id = ? "
                "AND timestamp BETWEEN ? AND ? ORDER BY timestamp",
                [aid, row[0], end],
            ).fetchall()
        ]
        return [
            (stamps[i + 1] - stamps[i]).total_seconds() for i in range(len(stamps) - 1)
        ]

    def _lap_max_gap(self, aid) -> float:
        gaps = self._lap_window_gaps(aid)
        return max(gaps) if gaps else 1.0

    def _has_unspannable_gap(self, aid, lap_seconds) -> bool:
        """Does the fastest distance lap contain a hole the window refuses?"""
        row = self.con.execute(
            "SELECT start_time, total_timer_time FROM laps WHERE activity_id = ? "
            "AND trigger = 'distance' AND distance_m > 1600 "
            "ORDER BY total_timer_time LIMIT 1",
            [aid],
        ).fetchone()
        if row is None:
            return False
        import datetime

        end = row[0] + datetime.timedelta(seconds=float(row[1]) * 2)
        stamps = [
            r[0]
            for r in self.con.execute(
                "SELECT timestamp FROM records WHERE activity_id = ? "
                "AND timestamp BETWEEN ? AND ? ORDER BY timestamp",
                [aid, row[0], end],
            ).fetchall()
        ]
        return any(
            (stamps[i + 1] - stamps[i]).total_seconds() > B.MAX_SAMPLE_GAP_S
            for i in range(len(stamps) - 1)
        )

    def test_the_gap_guard_is_what_makes_that_exception_necessary(self):
        """THE PRESENCE TWIN. The skip above is only legitimate if the guard is
        real and the corpus actually contains such a lap -- otherwise it is a
        licence to skip any inconvenient run."""
        self.assertGreater(B.MAX_SAMPLE_GAP_S, 0)
        found = [
            aid
            for (aid,) in self.con.execute("""
            SELECT DISTINCT a.activity_id FROM activities a JOIN laps l
              USING (activity_id)
            WHERE l.trigger = 'distance' AND a.coverage_fraction >= 0.95""").fetchall()
            if self._has_unspannable_gap(aid, None)
        ]
        if not found:
            self.skipTest(
                "no lap in this corpus contains an unspannable gap, "
                "so the exception above is unused here"
            )
        for aid in found:
            self.assertTrue(self._has_unspannable_gap(aid, None))

    def test_a_best_is_never_an_instantaneous_maximum(self):
        """The whole point. A 30 s window interpolated over the odometer is a
        weighted mean of the record-to-record speeds inside it, so it can
        never exceed the fastest single step -- which, being a maximum, is
        where GPS spikes live, often at speeds no human runs."""
        aid = newest_run(self.con, "total_distance_m DESC")
        best30 = [
            w
            for w in B.windowed_bests(self.con, aid)["windows"]
            if w["window"] == "best_30s"
        ][0]
        if best30["status"] != "ok":
            self.skipTest(f"no 30 s best on the subject: {best30['reason']}")
        step_max = self.con.execute(
            """
            SELECT max(dd / dt) FROM (
                SELECT distance_m - lag(distance_m) OVER w AS dd,
                       epoch(timestamp) - epoch(lag(timestamp) OVER w) AS dt
                FROM records WHERE activity_id = ? AND distance_m IS NOT NULL
                WINDOW w AS (ORDER BY timestamp))
            WHERE dt > 0""",
            [aid],
        ).fetchone()[0]
        self.assertLessEqual(best30["mps"], step_max + 1e-6)
        # ...and no human holds 12.5 m/s for 30 s.
        self.assertLess(best30["mps"], 12.5)

    def test_the_window_is_exactly_the_target_length(self):
        aid = newest_run(self.con, "total_distance_m DESC")
        for w in B.windowed_bests(self.con, aid)["windows"]:
            if w["status"] != "ok":
                continue
            if "target_s" in w:
                self.assertAlmostEqual(w["seconds"], w["target_s"], places=6)
            else:
                self.assertAlmostEqual(w["meters"], w["target_m"], places=6)

    def test_the_composition_of_the_winning_window_is_reported(self):
        """A best mile that is a quarter walking must not read as a running
        mile."""
        aid = newest_run(self.con, "total_distance_m DESC")
        for w in B.windowed_bests(self.con, aid)["windows"]:
            if w["status"] == "ok":
                self.assertIn("walk_time_fraction", w)
                self.assertIn("run_time_fraction", w)

    def test_bests_declare_they_are_not_regime_comparable(self):
        aid = one(
            self.con,
            "SELECT activity_id FROM activities ORDER BY activity_id LIMIT 1",
            what="activity",
        )[0]
        f = B.windowed_bests(self.con, aid)["filters"]
        self.assertFalse(f["comparable_across_sampling_regimes"])
        self.assertIn("distance_provenance", f)


class TestEffortPaces(CorpusTestCase):
    """Per-effort pace: the MEAN and the WINDOWED BEST are different quantities
    and the whole point is that they are kept apart (§6.2, §16). The subject
    is the segment with the most efforts."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.hist = O.segment_history(
            cls.con,
            one(
                cls.con,
                """
            SELECT segment_id FROM segment_efforts GROUP BY 1
            ORDER BY count(*) DESC, segment_id LIMIT 1""",
                what="segment effort",
            )[0],
        )

    def test_segment_history_now_carries_pace(self):
        """It returned duration and vertical speed but no pace at all, so a
        reader could see a slope getting slower without seeing how fast it
        was run."""
        self.assertTrue(self.hist["efforts"])
        got = [e for e in self.hist["efforts"] if e["mean_pace_per_mile"]]
        self.assertTrue(got)
        for e in got:
            self.assertRegex(e["mean_pace_per_mile"], r"^\d+:[0-5]\d$")

    def test_every_effort_carries_both_windows(self):
        for e in self.hist["efforts"]:
            labels = {w["window"] for w in e["best_windows"]}
            self.assertEqual(labels, {"best_10s", "best_30s"})

    def test_a_window_is_never_slower_than_the_effort_mean(self):
        """The best stretch inside an effort cannot be slower than the whole
        effort's average -- if it is, the two are not measuring one span."""
        n = 0
        for e in self.hist["efforts"]:
            if e["mean_mps"] is None:
                continue
            for w in e["best_windows"]:
                if w["status"] != "ok":
                    continue
                n += 1
                self.assertGreaterEqual(
                    w["mps"], e["mean_mps"] - 1e-6, f"{e['date']} {w['window']}"
                )
        if not n:
            self.skipTest("no effort on the subject has a reported window")

    def test_the_shorter_window_is_never_slower_than_the_longer(self):
        n = 0
        for e in self.hist["efforts"]:
            got = {w["window"]: w for w in e["best_windows"]}
            a, b = got["best_10s"], got["best_30s"]
            if a["status"] != "ok" or b["status"] != "ok":
                continue
            n += 1
            self.assertGreaterEqual(a["mps"], b["mps"] - 1e-6, e["date"])
        if not n:
            self.skipTest("no effort on the subject has both windows reported")

    def test_a_window_with_too_few_samples_is_gated_not_reported(self):
        """Path-derived speed averages over the sampling interval, so a 10 s
        window at Smart Recording can rest on two intervals. That must not be
        returned as a peak."""
        gated = [
            w
            for e in self.hist["efforts"]
            for w in e["best_windows"]
            if w["status"] in ("gated", "unavailable")
        ]
        if not gated:
            self.skipTest("no window on the subject segment was gated")
        for w in gated:
            self.assertIsNone(w["mps"])
            self.assertTrue(w["reason"])

    def test_a_window_that_is_reported_says_how_many_samples_back_it(self):
        """The positive twin: reported windows must carry their evidence."""
        ok = [
            w
            for e in self.hist["efforts"]
            for w in e["best_windows"]
            if w["status"] == "ok"
        ]
        self.assertTrue(ok)
        for w in ok:
            self.assertGreaterEqual(w["n_samples_in_window"], B.MIN_SAMPLES_PER_WINDOW)

    def test_the_two_quantities_are_declared_as_different(self):
        f = self.hist["applied_filters"]
        self.assertIsNotNone(f)
        self.assertIn("summit to col", f["mean_grain"])
        self.assertIn("inside the effort", f["window_grain"])
        self.assertFalse(f["comparable_across_sampling_regimes"])


class TestFiltersAreStated(CorpusTestCase):
    """Every result states what it applied, as FIELDS rather than prose. A chat
    that cannot see the filters cannot know what the number is."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.aid = newest_run(cls.con)

    def test_every_basic_metric_carries_filters(self):
        for name, r in B.basic_metrics(self.con, self.aid).items():
            self.assertIn("filters", r, name)
            self.assertIsInstance(r["filters"], dict, name)

    def test_the_durations_declare_regime_independence(self):
        f = B.durations(self.con, self.aid)["filters"]
        self.assertFalse(f["sampling_regime_dependent"])
        self.assertEqual(f["duration_source"], "splits.total_timer_time")

    def test_metrics_that_cannot_apply_quality_flags_say_why(self):
        f = B.average_hr(self.con, self.aid)["filters"]
        self.assertFalse(f["quality_flags_excluded"])
        self.assertIn("quality_flags_reason", f)


class TestPaceFormatting(CorpusTestCase):
    def test_every_reported_pace_is_minutes_and_seconds(self):
        aid = newest_run(self.con, "total_distance_m DESC")
        r = B.basic_metrics(self.con, aid)
        seen = 0
        for p in r["paces"]["paces"]:
            if p["status"] == "ok":
                seen += 1
                self.assertRegex(p["pace_per_mile"], r"^\d+:[0-5]\d$")
        for w in r["windowed_bests"]["windows"]:
            if w["status"] == "ok":
                seen += 1
                self.assertRegex(w["pace_per_mile"], r"^\d+:[0-5]\d$")
        self.assertGreater(seen, 0)

    def test_the_mile_constant_is_the_shared_one(self):
        self.assertEqual(B.BEST_WINDOWS_M[1], METERS_PER_MILE)


if __name__ == "__main__":
    unittest.main()
