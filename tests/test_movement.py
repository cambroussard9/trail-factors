"""Tests for movement-ceased detection (SPEC.md §4.3)."""

import unittest
from datetime import datetime, timedelta

from trail_factors.ingest.validate import INFO, WARNING, detect_movement_ceased, movement_checks

BASE = datetime(2023, 6, 20, 7, 0, 0)       # synthetic


def moving(i, t):
    return {"timestamp": t, "enhanced_speed": 2.2, "cadence": 82, "distance": i * 11.0,
            "heart_rate": 150}


def stopped(i, t, dist, drift=0.03):
    return {"timestamp": t, "enhanced_speed": 0.0, "cadence": 0,
            "distance": dist + i * drift, "heart_rate": 140}


def build(n_move=200, n_stop=0, stop_at_end=True, gap=5.0, drift=0.03):
    out, t = [], BASE
    if not stop_at_end:
        for i in range(n_stop):
            out.append(stopped(i, t, 0.0, drift)); t += timedelta(seconds=gap)
    for i in range(n_move):
        out.append(moving(i, t)); t += timedelta(seconds=gap)
    if stop_at_end:
        d = out[-1]["distance"]
        for i in range(n_stop):
            out.append(stopped(i, t, d, drift)); t += timedelta(seconds=gap)
    return out


class MovementTest(unittest.TestCase):
    def test_clean_run_has_no_stops(self):
        m = detect_movement_ceased(build())
        self.assertEqual(m["trailing_s"], 0.0)
        self.assertEqual(m["leading_s"], 0.0)

    def test_trailing_stop_detected(self):
        """A recording left running ~4 minutes after the athlete stopped."""
        m = detect_movement_ceased(build(n_stop=45))    # 45 * 5 s = 225 s
        self.assertGreaterEqual(m["trailing_s"], 200.0)
        self.assertIsNotNone(m["suggested_valid_end"])

    def test_gps_drift_while_stationary_does_not_defeat_detection(self):
        """A real stop drifted several metres over a few minutes; an absolute
        cap on distance missed it."""
        m = detect_movement_ceased(build(n_stop=45, drift=0.17))   # ~7.5 m total
        self.assertGreaterEqual(m["trailing_s"], 200.0)

    def test_short_pause_is_not_a_stop(self):
        m = detect_movement_ceased(build(n_stop=10))    # 50 s
        self.assertEqual(m["trailing_s"], 0.0)

    def test_leading_stop_detected(self):
        m = detect_movement_ceased(build(n_stop=45, stop_at_end=False))
        self.assertGreaterEqual(m["leading_s"], 200.0)

    def test_trailing_stop_is_warning_never_fatal(self):
        f = movement_checks(build(n_stop=45))
        self.assertTrue(any(x.check == "trailing_stop" and x.severity == WARNING for x in f))
        self.assertFalse(any(x.is_fatal for x in f))

    def test_midrun_stop_is_info_not_warning(self):
        """timer_time already excludes paused time, so mid-run does not corrupt."""
        recs = build(n_move=100) + build(n_stop=45)[:45] + build(n_move=100)
        t = BASE
        for r in recs:
            r["timestamp"] = t
            t += timedelta(seconds=5)
        f = movement_checks(recs)
        self.assertFalse(any(x.check == "trailing_stop" for x in f))
        mid = [x for x in f if x.check == "midrun_stop"]
        if mid:
            self.assertEqual(mid[0].severity, INFO)

    def test_hr_is_not_the_trigger(self):
        """HR can barely fall during such a stop; detection must not depend
        on it."""
        recs = build(n_stop=45)
        for r in recs:
            r["heart_rate"] = 150          # HR completely flat
        self.assertGreaterEqual(detect_movement_ceased(recs)["trailing_s"], 200.0)


if __name__ == "__main__":
    unittest.main()


class MissingDistanceTest(unittest.TestCase):
    """A record with no distance must not silently defeat stop detection.

    `(rec.get("distance") or 0)` turned a missing distance into 0, making the
    block's drift enormous and REJECTING a real stop (CLAUDE.md falsy trap).
    """

    def test_stop_still_detected_when_distance_is_missing(self):
        recs = build(n_stop=45)
        for r in recs[-45:]:
            r.pop("distance", None)
        self.assertGreaterEqual(detect_movement_ceased(recs)["trailing_s"], 200.0)

    def test_stop_still_detected_when_only_one_endpoint_lacks_distance(self):
        recs = build(n_stop=45)
        recs[-1].pop("distance", None)
        self.assertGreaterEqual(detect_movement_ceased(recs)["trailing_s"], 200.0)
