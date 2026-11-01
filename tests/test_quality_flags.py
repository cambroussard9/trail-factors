"""§15.4 per-record quality flags (SPEC.md §15.4, §12.6).

Thresholds are DERIVED FROM MEASURED DISTRIBUTIONS, so these tests pin both the
mechanism and the derivation. Every negative case has a positive twin.
"""

import unittest
from datetime import datetime, timedelta

from trail_factors.transform import silver as S
from tests.corpus import CorpusTestCase, one

T0 = datetime(2023, 6, 20, 7, 0)                  # synthetic


def track(n=40, step=2.0, cadence=80, split="rwd_run"):
    ts = [T0 + timedelta(seconds=i) for i in range(n)]
    xs = [i * step for i in range(n)]
    ys = [0.0] * n
    sp = [step] * n
    cad = [cadence + (i % 3) for i in range(n)]
    return ts, xs, ys, sp, cad, [split] * n


class TestJitterIsNormalised(unittest.TestCase):
    """Raw jitter is not comparable across sampling regimes; normalised is."""

    def test_a_lateral_spike_is_flagged(self):
        ts, xs, ys, sp, cad, st = track()
        ys[20] = 12.0                      # 6x the 2 m step spacing
        out = S.quality_flags(ts, xs, ys, sp, cad, st)
        self.assertIn("gps_jitter", out[20])

    def test_positive_twin_a_clean_track_is_not_flagged(self):
        out = S.quality_flags(*track())
        self.assertEqual([o for o in out if "gps_jitter" in o], [])

    def test_the_same_deviation_at_wider_spacing_is_NOT_flagged(self):
        """The whole point of normalising: a 12 m excursion is an outlier at
        2 m spacing and ordinary at 20 m spacing. A raw threshold would flag
        most Smart Recording data purely for being sampled less often."""
        ts, xs, ys, sp, cad, st = track(step=20.0)
        ys[20] = 12.0
        out = S.quality_flags(ts, xs, ys, sp, cad, st)
        self.assertNotIn("gps_jitter", out[20])


class TestSpeedDiscontinuity(unittest.TestCase):
    def test_a_physically_impossible_jump_is_flagged(self):
        ts, xs, ys, sp, cad, st = track()
        for i in range(20, 40):
            xs[i] += 8.0                   # 8 m in one second
        out = S.quality_flags(ts, xs, ys, sp, cad, st)
        self.assertTrue(any("speed_discontinuity" in o for o in out))

    def test_positive_twin_a_normal_acceleration_is_not_flagged(self):
        ts, xs, ys, sp, cad, st = track()
        for i in range(20, 40):
            xs[i] += 0.5 * (i - 19)        # gentle build
        out = S.quality_flags(ts, xs, ys, sp, cad, st)
        self.assertEqual([o for o in out if "speed_discontinuity" in o], [])

    def test_the_threshold_is_physical_not_a_percentile(self):
        """3 m/s^2 is 0 to 10.8 km/h in one second."""
        self.assertEqual(S.ACCEL_MAX_MPS2, 3.0)


class TestCadenceFlags(unittest.TestCase):
    def test_zero_cadence_while_moving_is_flagged(self):
        ts, xs, ys, sp, cad, st = track()
        cad[10] = 0
        out = S.quality_flags(ts, xs, ys, sp, cad, st)
        self.assertIn("cadence_dropout", out[10])

    def test_zero_cadence_while_stationary_is_NOT_flagged(self):
        """Standing still legitimately has no cadence."""
        ts, xs, ys, sp, cad, st = track()
        cad[10], sp[10] = 0, 0.0
        out = S.quality_flags(ts, xs, ys, sp, cad, st)
        self.assertNotIn("cadence_dropout", out[10])

    def test_frozen_cadence_while_speed_varies_is_flagged(self):
        ts, xs, ys, sp, cad, st = track()
        for i in range(5, 30):
            cad[i] = 85
            sp[i] = 1.5 + (i % 2)          # speed swings by 1 m/s
        out = S.quality_flags(ts, xs, ys, sp, cad, st)
        self.assertIn("cadence_stuck", out[15])

    def test_positive_twin_steady_running_is_not_cadence_lock(self):
        """Constant cadence at constant speed is what good running looks like."""
        ts, xs, ys, sp, cad, st = track()
        for i in range(5, 30):
            cad[i] = 85                     # speed stays flat at 2.0
        out = S.quality_flags(ts, xs, ys, sp, cad, st)
        self.assertEqual([o for o in out if "cadence_stuck" in o], [])


class TestSplitTransition(unittest.TestCase):
    def test_samples_just_after_a_transition_are_flagged(self):
        ts, xs, ys, sp, cad, st = track()
        for i in range(20, 40):
            st[i] = "rwd_walk"
        out = S.quality_flags(ts, xs, ys, sp, cad, st)
        for i in (20, 21, 22, 23):
            self.assertIn("split_transition", out[i], i)

    def test_the_window_ends_where_the_measurement_said(self):
        """§14a: cadence was measured back to baseline by 3 s after a
        run/walk transition."""
        ts, xs, ys, sp, cad, st = track()
        for i in range(20, 40):
            st[i] = "rwd_walk"
        out = S.quality_flags(ts, xs, ys, sp, cad, st)
        self.assertNotIn("split_transition", out[25])
        self.assertEqual(S.SPLIT_TRANSITION_S, 3)


class TestNoUnsupportedFlags(unittest.TestCase):
    def test_arm_motion_decoupling_is_not_a_flag(self):
        """§14a measured it and found the trend sign OPPOSITE to the
        hypothesis. Flagging it would encode an unsupported claim as data."""
        self.assertNotIn("arm_motion_decoupling", S.ALL_FLAGS)
        for name in S.ALL_FLAGS:
            self.assertNotIn("arm", name)


class TestAgainstTheCorpus(CorpusTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        one(cls.con, "SELECT 1 FROM records_enriched LIMIT 1",
            what="silver layer (records_enriched is empty)")

    def test_the_column_exists_and_is_populated(self):
        n, flagged = self.con.execute(
            "SELECT count(*), count(*) FILTER (WHERE coalesce(quality_flags,'') <> '') "
            "FROM records_enriched").fetchone()
        self.assertGreater(n, 0)
        self.assertGreater(flagged, 0, "no records flagged -- the detector is dead")

    def test_the_flag_rate_is_a_tail_not_a_majority(self):
        """A detector that flags most of the corpus is measuring the regime,
        not artifacts."""
        n, flagged = self.con.execute(
            "SELECT count(*), count(*) FILTER (WHERE coalesce(quality_flags,'') <> '') "
            "FROM records_enriched").fetchone()
        self.assertLess(flagged / n, 0.20)

    def test_every_stored_flag_is_a_known_name(self):
        for (combo,) in self.con.execute(
                "SELECT DISTINCT quality_flags FROM records_enriched "
                "WHERE coalesce(quality_flags,'') <> ''").fetchall():
            for f in combo.split(","):
                self.assertIn(f, S.ALL_FLAGS)

    def test_both_regimes_flag_at_comparable_rates(self):
        """Normalising jitter is what makes this true; a raw threshold would
        flag Smart Recording several times more often."""
        rates = {}
        for regime, n, fl in self.con.execute("""
            SELECT a.sampling_regime, count(*),
                   count(*) FILTER (WHERE coalesce(e.quality_flags,'') <> '')
            FROM records_enriched e JOIN activities a USING (activity_id)
            GROUP BY 1""").fetchall():
            rates[regime] = fl / n
        if len(rates) < 2:
            self.skipTest("this corpus's enriched records come from one "
                          "sampling regime only")
        self.assertLess(max(rates.values()) / max(min(rates.values()), 1e-9), 4.0,
                        f"regimes flag at very different rates: {rates}")

    def test_record_grain_primitives_declare_that_they_exclude(self):
        from trail_factors.analysis import primitives as P
        self.assertTrue(P.drift_filters()["quality_flags_excluded"])
        aid = self.con.execute(
            "SELECT activity_id FROM records_enriched LIMIT 1").fetchone()[0]
        self.assertTrue(
            P.grade_binned_pace(self.con, aid)["filters"]["quality_flags_excluded"])

    def test_segment_grain_primitives_declare_that_they_do_NOT(self):
        """Stated, not implied -- they read segment_efforts, which aggregates
        records upstream of the flags."""
        from trail_factors.analysis import primitives as P
        self.assertFalse(P.segment_drift_filters()["quality_flags_excluded"])


if __name__ == "__main__":
    unittest.main()
