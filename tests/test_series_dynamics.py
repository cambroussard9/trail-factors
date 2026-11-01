"""§17.1b-bis running dynamics per 30 s bin.

WHAT THESE PIN. The four fields are only meaningful with their gait gate and
their per-field n attached, so the properties that matter are the ones that
would let a wrong answer look right: walking dynamics pooled into a running
mean, a 5-sample mean presented as a 30-sample one, an absence rendered as
0.0, or a §15.4-unfiltered figure passed off as a filtered one.

THREE ABSENCES, ALL DISTINGUISHABLE, NEVER 0.0 -- the same rule as the
float(None) fix: decide what the absence MEANS at that site and say it.

Every negative has a positive twin.

The hand-computed checks run on a synthetic in-memory fixture. The corpus
checks read the database named by TRAIL_FACTORS_DB (skipped when unset) and find
their subject by a PROPERTY -- "a watch run whose records carry all four
dynamics fields", "a watch activity with none" -- so they hold on any
athlete's corpus and skip, naming what is missing, when a corpus lacks one.
"""

from __future__ import annotations

import datetime as dt
import unittest

import duckdb

from tests.corpus import CorpusTestCase, one
from trail_factors.analysis.series import binned_series

T0 = dt.datetime(2025, 1, 15, 7, 0, 0)  # synthetic

# A watch running activity whose records carry all four dynamics fields and
# which has rwd_run segmentation: the subject of the "values" checks.
DYNAMICS_RUN = """
    SELECT a.activity_id FROM activities a
    WHERE a.source = 'garmin_watch' AND a.sport = 'running'
      AND EXISTS (SELECT 1 FROM records r WHERE r.activity_id = a.activity_id
                    AND r.stance_time IS NOT NULL)
      AND EXISTS (SELECT 1 FROM records r WHERE r.activity_id = a.activity_id
                    AND r.step_length IS NOT NULL)
      AND EXISTS (SELECT 1 FROM records r WHERE r.activity_id = a.activity_id
                    AND r.vertical_oscillation IS NOT NULL)
      AND EXISTS (SELECT 1 FROM records r WHERE r.activity_id = a.activity_id
                    AND r.vertical_ratio IS NOT NULL)
      AND EXISTS (SELECT 1 FROM splits s WHERE s.activity_id = a.activity_id
                    AND s.split_type = 'rwd_run')
    ORDER BY a.start_time_utc LIMIT 1"""

# ...and one that ALSO holds a non-running stretch of at least two bins, so
# at least one whole 30 s bin contains no rwd_run record.
DYNAMICS_RUN_WITH_A_LONG_WALK = """
    SELECT a.activity_id FROM activities a
    WHERE a.source = 'garmin_watch' AND a.sport = 'running'
      AND EXISTS (SELECT 1 FROM records r WHERE r.activity_id = a.activity_id
                    AND r.stance_time IS NOT NULL)
      AND EXISTS (SELECT 1 FROM splits s WHERE s.activity_id = a.activity_id
                    AND s.split_type = 'rwd_run')
      AND EXISTS (SELECT 1 FROM splits s WHERE s.activity_id = a.activity_id
                    AND s.split_type IN ('rwd_walk', 'rwd_stand')
                    AND date_diff('second', s.start_time, s.end_time) >= 60)
    ORDER BY a.start_time_utc LIMIT 1"""

# A watch activity whose device wrote no running dynamics at all.
NO_DYNAMICS_POSITIONED = """
    SELECT a.activity_id FROM activities a
    WHERE a.source = 'garmin_watch' AND NOT EXISTS (
        SELECT 1 FROM records r WHERE r.activity_id = a.activity_id
          AND r.stance_time IS NOT NULL)
      AND EXISTS (SELECT 1 FROM records r2
                  WHERE r2.activity_id = a.activity_id
                    AND r2.lat IS NOT NULL)
    ORDER BY a.start_time_utc LIMIT 1"""

# Chosen so the time-weighted mean is hand-checkable. THE FIXTURE IS
# CONTIGUOUS 1 Hz FOR THIS REASON: record_weights gives each record the moving
# seconds to the NEXT record, capped at MAX_WEIGHT_GAP_S = 20 s. An earlier
# version put three running records at t0..t0+2 and the next record 28 s
# later, so the third running record carried 20 s of weight and dominated its
# own mean -- the arithmetic in the test was wrong, not the code. With every
# second populated, every weight is exactly 1.0 s and the weighted mean is the
# plain mean.
RUN_STANCE = [240.0, 250.0, 260.0]  # synthetic
RUN_STEP = [1000.0, 1010.0, 1020.0]
RUN_VO = [70.0, 74.0, 78.0]
# DELIBERATELY NOT VO/STEP. 100 * mean(VO)/mean(STEP) is 7.33; these average
# to 11.2, so a recomputed ratio cannot masquerade as the device's own.
RUN_VR = [11.0, 11.2, 11.4]
WALK_SENTINEL = 999.0


def _fixture(
    con, *, enriched: bool, flag_first: bool = False, dynamics: bool = True
) -> str:
    """One synthetic activity: bin 0 running, bin 1 walking.

    Built rather than borrowed so the expected value is arithmetic, not a
    second implementation of the thing under test.
    """
    aid = "fix"
    con.execute(
        "CREATE OR REPLACE TABLE activities (activity_id VARCHAR, "
        "valid_end_utc TIMESTAMP, sampling_regime VARCHAR, "
        "coverage_fraction DOUBLE)"
    )
    con.execute("INSERT INTO activities VALUES (?, NULL, '1hz', 1.0)", [aid])
    con.execute("""CREATE OR REPLACE TABLE records (
        activity_id VARCHAR, timestamp TIMESTAMP, elapsed_s DOUBLE,
        distance_m DOUBLE, heart_rate INTEGER, cadence_raw_rpm DOUBLE,
        fractional_cadence DOUBLE, stance_time DOUBLE, step_length DOUBLE,
        vertical_oscillation DOUBLE, vertical_ratio DOUBLE)""")
    # CONTIGUOUS 1 Hz, 60 s: bin 0 is t0..t0+29, bin 1 is t0+30..t0+59.
    # Records 0-2 are the only RUNNING ones; everything after is walking and
    # carries a sentinel, because the FR265 writes dynamics while walking too
    # and they must never reach a running mean.
    for i in range(60):
        running = i < 3
        con.execute(
            "INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [
                aid,
                T0 + dt.timedelta(seconds=i),
                float(i),
                float(i * 3),
                150 if running else 120,
                80.0 if running else 60.0,
                0.0,
                (RUN_STANCE[i] if running else WALK_SENTINEL) if dynamics else None,
                (RUN_STEP[i] if running else WALK_SENTINEL) if dynamics else None,
                (RUN_VO[i] if running else WALK_SENTINEL) if dynamics else None,
                (RUN_VR[i] if running else WALK_SENTINEL) if dynamics else None,
            ],
        )
    con.execute("""CREATE OR REPLACE TABLE splits (
        activity_id VARCHAR, split_type VARCHAR, start_time TIMESTAMP,
        end_time TIMESTAMP, total_timer_time DOUBLE)""")
    con.execute(
        "INSERT INTO splits VALUES (?,'rwd_run',?,?,3.0)",
        [aid, T0, T0 + dt.timedelta(seconds=2)],
    )
    con.execute(
        "INSERT INTO splits VALUES (?,'rwd_walk',?,?,57.0)",
        [aid, T0 + dt.timedelta(seconds=3), T0 + dt.timedelta(seconds=59)],
    )
    con.execute(
        "CREATE OR REPLACE TABLE stops (activity_id VARCHAR, "
        "start_utc TIMESTAMP, end_utc TIMESTAMP)"
    )
    con.execute("""CREATE OR REPLACE TABLE records_enriched (
        activity_id VARCHAR, timestamp TIMESTAMP, altitude_dem_m DOUBLE,
        quality_flags VARCHAR)""")
    if enriched:
        for i in range(3):
            con.execute(
                "INSERT INTO records_enriched VALUES (?,?,?,?)",
                [
                    aid,
                    T0 + dt.timedelta(seconds=i),
                    200.0,
                    "gps_jitter" if (flag_first and i == 0) else "",
                ],
            )
    return aid


class TestARunWithDynamicsReturnsValuesWithPlausibleN(CorpusTestCase):
    """Test 1, on the corpus."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.aid = one(
            cls.con, DYNAMICS_RUN, what="watch run carrying all four dynamics fields"
        )[0]
        cls.r = binned_series(cls.con, cls.aid)

    def test_the_payload_says_dynamics_are_available(self):
        self.assertTrue(self.r["dynamics_available"])
        self.assertTrue(all(self.r["dynamics_fields_available"].values()))
        self.assertIsNone(self.r["dynamics_reason"])

    def test_bins_carry_all_four_fields_with_their_units(self):
        want = {
            "stance_time_ms": "ms",
            "step_length_mm": "mm",
            "vertical_oscillation_mm": "mm",
            "vertical_ratio_pct": "percent",
        }
        for b in self.r["bins"]:
            self.assertEqual(set(b["dynamics"]), set(want))
            for key, unit in want.items():
                self.assertEqual(b["dynamics"][key]["unit"], unit)

    def test_the_values_are_physiologically_plausible(self):
        # Generic human ranges, not one athlete's values.
        live = [b for b in self.r["bins"] if b["dynamics_reason"] is None]
        self.assertTrue(live, "a run with rwd_run must have a running bin")
        for b in live:
            d = b["dynamics"]
            if d["stance_time_ms"]["value"] is not None:
                self.assertTrue(150 < d["stance_time_ms"]["value"] < 500)
            if d["step_length_mm"]["value"] is not None:
                self.assertTrue(400 < d["step_length_mm"]["value"] < 2000)
            if d["vertical_ratio_pct"]["value"] is not None:
                self.assertTrue(2 < d["vertical_ratio_pct"]["value"] < 25)

    def test_n_never_exceeds_the_bins_running_seconds(self):
        """At 1 Hz a bin cannot hold more running records than running
        seconds. An n above it would mean records were double-counted -- the
        FR265 split-partition failure, which is why the query uses EXISTS."""
        for b in self.r["bins"]:
            for f in b["dynamics"].values():
                self.assertLessEqual(f["n"], b["run_s"] + 1, b["bin"])


class TestHandComputedBins(unittest.TestCase):
    """Test 1, on the synthetic fixture: the expected value is arithmetic."""

    def test_a_hand_computed_bin_matches_EXACTLY(self):
        con = duckdb.connect(":memory:")
        try:
            aid = _fixture(con, enriched=False)
            r = binned_series(con, aid)
            d = r["bins"][0]["dynamics"]
            self.assertEqual(d["stance_time_ms"]["n"], 3)
            self.assertAlmostEqual(
                d["stance_time_ms"]["value"], sum(RUN_STANCE) / 3, places=9
            )
            self.assertAlmostEqual(
                d["step_length_mm"]["value"], sum(RUN_STEP) / 3, places=9
            )
            self.assertAlmostEqual(
                d["vertical_oscillation_mm"]["value"], sum(RUN_VO) / 3, places=9
            )
            self.assertAlmostEqual(
                d["vertical_ratio_pct"]["value"], sum(RUN_VR) / 3, places=9
            )
        finally:
            con.close()

    def test_vertical_ratio_is_the_DEVICE_field_not_a_recomputation(self):
        """Recomputing VO/step length would give a different number here by
        construction: the fixture's ratio is NOT its oscillation over its
        step length."""
        con = duckdb.connect(":memory:")
        try:
            aid = _fixture(con, enriched=False)
            d = binned_series(con, aid)["bins"][0]["dynamics"]
            derived = 100.0 * (sum(RUN_VO) / 3) / (sum(RUN_STEP) / 3)
            self.assertAlmostEqual(
                d["vertical_ratio_pct"]["value"], sum(RUN_VR) / 3, places=9
            )
            self.assertNotAlmostEqual(
                d["vertical_ratio_pct"]["value"], derived, places=3
            )
        finally:
            con.close()

    def test_walking_records_never_reach_a_running_mean(self):
        """The fixture's walking records carry 999.0. If the gait gate leaked,
        bin 1 would report it."""
        con = duckdb.connect(":memory:")
        try:
            aid = _fixture(con, enriched=False)
            r = binned_series(con, aid)
            b1 = r["bins"][1]["dynamics"]
            for f in b1.values():
                self.assertIsNone(f["value"])
                self.assertEqual(f["n"], 0)
            for b in r["bins"]:
                for f in b["dynamics"].values():
                    self.assertNotEqual(f["value"], WALK_SENTINEL)
        finally:
            con.close()


class TestTheTwoAbsencesAreDifferENTFacts(CorpusTestCase):
    """Test 2: positive and negative twin in one test."""

    def test_no_device_channel_and_no_running_in_bin_differ(self):
        con = self.con
        # NEGATIVE: an activity whose device wrote no dynamics at all.
        pre = one(
            con,
            NO_DYNAMICS_POSITIONED,
            what="positioned watch activity without dynamics",
        )[0]
        a = binned_series(con, pre)
        self.assertFalse(a["dynamics_available"])
        self.assertIsNotNone(a["dynamics_reason"])
        self.assertIn("FR245", a["dynamics_reason"])
        reasons_a = {b["dynamics_reason"] for b in a["bins"]}
        self.assertEqual(reasons_a, {"no_device_channel"})
        for b in a["bins"]:
            for f in b["dynamics"].values():
                self.assertIsNone(f["value"])
                self.assertEqual(f["n"], 0)

        # POSITIVE: an activity whose device DOES record them, with bins
        # holding no running.
        withdyn = one(
            con,
            DYNAMICS_RUN_WITH_A_LONG_WALK,
            what="dynamics run with a walk or stand of 60 s+",
        )[0]
        b_ = binned_series(con, withdyn)
        self.assertTrue(b_["dynamics_available"])
        self.assertIsNone(b_["dynamics_reason"])
        walkers = [x for x in b_["bins"] if x["dynamics_reason"] == "no_running_in_bin"]
        self.assertTrue(walkers, "expected at least one non-running bin")
        for x in walkers:
            for f in x["dynamics"].values():
                self.assertIsNone(f["value"])
                self.assertEqual(f["n"], 0)

        # THE TWO REASONS ARE NOT THE SAME STRING. That is the whole test.
        self.assertNotEqual(reasons_a.pop(), "no_running_in_bin")

    def test_a_running_activity_with_NO_rwd_run_splits_reads_no_running(self):
        """A WALK logged under the running sport: rwd_* splits exist and tile
        the timer with zero rwd_run, while the device still writes dynamics
        records. Without the gait gate those WALKING dynamics would be
        reported as running dynamics."""
        aid = one(
            self.con,
            """
            SELECT a.activity_id FROM activities a
            WHERE a.source = 'garmin_watch' AND a.sport = 'running'
              AND EXISTS (SELECT 1 FROM records r
                          WHERE r.activity_id = a.activity_id
                            AND r.stance_time IS NOT NULL)
              AND EXISTS (SELECT 1 FROM splits s
                          WHERE s.activity_id = a.activity_id
                            AND s.split_type LIKE 'rwd\\_%' ESCAPE '\\')
              AND NOT EXISTS (SELECT 1 FROM splits s
                              WHERE s.activity_id = a.activity_id
                                AND s.split_type = 'rwd_run')
            ORDER BY a.start_time_utc LIMIT 1""",
            what="running-sport watch activity with dynamics and no rwd_run",
        )
        r = binned_series(self.con, aid[0])
        self.assertTrue(r["dynamics_available"])  # the device records them
        self.assertEqual(
            {b["dynamics_reason"] for b in r["bins"]}, {"no_running_in_bin"}
        )  # ...and none is running


class TestNeverZeroForAnAbsence(unittest.TestCase):
    """Test 3: reintroduce a 0.0 coercion and a test must fail."""

    def _bins_with_coercion(self, which: str):
        """The sectioning as it would be if an absence were rendered 0.0."""
        con = duckdb.connect(":memory:")
        try:
            aid = _fixture(con, enriched=False, dynamics=(which != "no_device_channel"))
            r = binned_series(con, aid)
            coerced = []
            for b in r["bins"]:
                d = {k: dict(v) for k, v in b["dynamics"].items()}
                if b["dynamics_reason"] == which:
                    for f in d.values():
                        f["value"] = 0.0  # <-- the reintroduced bug
                coerced.append({**b, "dynamics": d})
            return coerced
        finally:
            con.close()

    def test_a_zero_for_no_running_in_bin_is_caught(self):
        bins = self._bins_with_coercion("no_running_in_bin")
        offenders = [
            b
            for b in bins
            if b["dynamics_reason"] == "no_running_in_bin"
            and any(f["value"] == 0.0 for f in b["dynamics"].values())
        ]
        self.assertTrue(offenders, "the fixture must produce the case")
        with self.assertRaises(AssertionError):
            for b in bins:
                if b["dynamics_reason"] is not None:
                    for f in b["dynamics"].values():
                        self.assertIsNone(f["value"])

    def test_a_zero_for_no_device_channel_is_caught(self):
        bins = self._bins_with_coercion("no_device_channel")
        with self.assertRaises(AssertionError):
            for b in bins:
                if b["dynamics_reason"] is not None:
                    for f in b["dynamics"].values():
                        self.assertIsNone(f["value"])

    def test_the_REAL_implementation_passes_that_same_assertion(self):
        """THE TWIN: the assertion above must be one the shipped code meets."""
        con = duckdb.connect(":memory:")
        try:
            for dynamics in (True, False):
                aid = _fixture(con, enriched=False, dynamics=dynamics)
                for b in binned_series(con, aid)["bins"]:
                    if b["dynamics_reason"] is not None:
                        for f in b["dynamics"].values():
                            self.assertIsNone(f["value"])
                            self.assertEqual(f["n"], 0)
        finally:
            con.close()


class TestFlagsAreAppliedWhereTheyExistAndLabelledWhereTheyDoNot(unittest.TestCase):
    """Test 4: both assertions, one test."""

    def test_unenriched_says_so_and_enriched_excludes_the_flagged_record(self):
        # (a) NO records_enriched: every record included, and the payload says
        #     the figure is NOT §15.4-clean.
        con = duckdb.connect(":memory:")
        try:
            aid = _fixture(con, enriched=False)
            bare = binned_series(con, aid)
        finally:
            con.close()
        self.assertFalse(bare["flags_available"])
        self.assertFalse(bare["filters"]["dynamics_flags_excluded"])
        self.assertIn("NOT §15.4-clean", bare["filters"]["dynamics_flags_reason"])
        d_bare = bare["bins"][0]["dynamics"]
        self.assertEqual(d_bare["stance_time_ms"]["n"], 3)
        self.assertAlmostEqual(
            d_bare["stance_time_ms"]["value"], sum(RUN_STANCE) / 3, places=9
        )

        # (b) WITH records_enriched and the first record flagged: it is
        #     excluded, n drops, and the value MOVES.
        con = duckdb.connect(":memory:")
        try:
            aid = _fixture(con, enriched=True, flag_first=True)
            filt = binned_series(con, aid)
        finally:
            con.close()
        self.assertTrue(filt["flags_available"])
        self.assertTrue(filt["filters"]["dynamics_flags_excluded"])
        d_filt = filt["bins"][0]["dynamics"]
        self.assertEqual(d_filt["stance_time_ms"]["n"], 2)
        self.assertAlmostEqual(
            d_filt["stance_time_ms"]["value"], sum(RUN_STANCE[1:]) / 2, places=9
        )
        self.assertNotAlmostEqual(
            d_filt["stance_time_ms"]["value"],
            d_bare["stance_time_ms"]["value"],
            places=3,
        )

    def test_enriched_with_NO_flag_keeps_every_record(self):
        """THE TWIN: an exclusion that fired on unflagged records would pass
        the test above for the wrong reason."""
        con = duckdb.connect(":memory:")
        try:
            aid = _fixture(con, enriched=True, flag_first=False)
            r = binned_series(con, aid)
        finally:
            con.close()
        self.assertTrue(r["flags_available"])
        self.assertEqual(r["bins"][0]["dynamics"]["stance_time_ms"]["n"], 3)


class TestExistingConsumersAreUnbroken(CorpusTestCase):
    """Test 5."""

    KEYS = (
        "bin",
        "elapsed_start_s",
        "elapsed_end_s",
        "run_s",
        "walk_s",
        "stand_s",
        "distance_start_m",
        "distance_end_m",
        "mps",
        "pace_per_mile",
        "avg_hr",
        "steps_per_min",
        "elevation_m",
        "coverage",
        "n_records",
    )

    def test_a_non_running_and_a_no_dynamics_activity_keep_every_field(self):
        con = self.con
        other = one(
            con,
            """
            SELECT activity_id FROM activities
            WHERE source = 'garmin_watch' AND sport <> 'running'
              AND activity_id IN (SELECT activity_id FROM records)
            ORDER BY start_time_utc LIMIT 1""",
            what="non-running watch activity with records",
        )[0]
        pre = one(
            con,
            """
            SELECT a.activity_id FROM activities a
            WHERE a.source = 'garmin_watch' AND NOT EXISTS (
                SELECT 1 FROM records r WHERE r.activity_id = a.activity_id
                  AND r.stance_time IS NOT NULL)
              AND a.activity_id IN (SELECT activity_id FROM records)
            ORDER BY a.start_time_utc LIMIT 1""",
            what="watch activity without dynamics",
        )[0]
        for aid in (other, pre):
            r = binned_series(con, aid)
            self.assertTrue(r["bins"])
            for b in r["bins"]:
                for k in self.KEYS:
                    self.assertIn(k, b, f"{aid} bin {b['bin']} lost {k}")
                # ...plus the new block, on EVERY bin.
                self.assertIn("dynamics", b)
                self.assertIn("dynamics_reason", b)
            for k in ("bins", "n_bins", "bin_s", "elevation_available", "filters"):
                self.assertIn(k, r)

    def test_steps_per_min_is_unchanged_and_its_convention_is_declared(self):
        """The new fields are rwd_run-only; steps_per_min is over ALL records
        with cadence. Different record sets in the same bin, so the payload
        says so rather than letting a reader assume one gait."""
        aid = one(
            self.con, DYNAMICS_RUN, what="watch run carrying all four dynamics fields"
        )[0]
        r = binned_series(self.con, aid)
        conv = r["filters"]["gait_conventions"]
        self.assertIn("ALL records", conv["steps_per_min"])
        self.assertIn("rwd_run", conv["stance_time_ms"])
        self.assertNotEqual(conv["steps_per_min"], conv["stance_time_ms"])

    def test_n_is_declared_per_field(self):
        aid = one(
            self.con, DYNAMICS_RUN, what="watch run carrying all four dynamics fields"
        )[0]
        f = binned_series(self.con, aid)["filters"]
        self.assertTrue(f["dynamics_n_is_per_field"])
        self.assertIn("independently", f["dynamics_n_note"])


if __name__ == "__main__":
    unittest.main()
