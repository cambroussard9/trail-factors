"""§18 stationary-bike block annotations.

THE WATCH RECORDS HR AND NOTHING ELSE for these: no cadence, no speed, no
position, no power, and a constant-zero odometer. So the resistance, the RPM
and the block boundaries exist only in the athlete's memory, and the
properties worth pinning are the ones that would let a recollection be
mistaken for a measurement, or an ambiguous log be silently resolved into a
confident number.

THE LOGS HERE ARE SYNTHETIC. The unit tests key them to a made-up run_id; the
corpus tests re-key the same shapes to an indoor ride found in the corpus by
property, with block boundaries scaled to that ride's own timer time.

NEVER WRITES TO data/athlete/. Every test redirects the module's path
constants at a temporary directory, and one test asserts the real file is
untouched.

Every negative has a positive twin.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.corpus import CorpusTestCase, one
from trail_factors.analysis import rides as RD
from trail_factors.ingest import ride_blocks as RB
from trail_factors.paths import REPO_ROOT

RIDE = "2025-01-15T0700Z"  # synthetic run_id
DUR = 50.0  # synthetic timer time, minutes

# Synthetic log 1: no rpm anywhere, and a complete valid log.
LOG_NO_RPM = {
    "run_id": RIDE,
    "raw_text": "15m @ 6.5\n15m @ 7.5\n20m @ 9.25",
    "blocks": [
        {"resistance": 6.5, "end_min": 15},
        {"resistance": 7.5, "end_min": 30},
        {"resistance": 9.25, "end_min": 50},
    ],
}
# Synthetic log 2: rpm ranges, an event, and the console readout.
LOG_RPM = {
    "run_id": RIDE,
    "raw_text": "0-30 at 7 @ 90-95, then 8.5 @ 85. water stop ~18m. "
    "console 25.0km 80rpm",
    "blocks": [
        {"resistance": 7.0, "rpm": [90, 95], "end_min": 30},
        {"resistance": 8.5, "rpm": 85, "end_min": 50},
    ],
    "events": [{"minute": 18, "text": "water stop"}],
    "machine_totals": {"distance": 25.0, "distance_unit": "km", "avg_rpm": 80},
}


def scaled(log, run_id, dur):
    """The same log re-keyed to a corpus ride and stretched to its length:
    block ends and event minutes keep their fraction of DUR."""
    k = dur / DUR
    out = {
        **log,
        "run_id": run_id,
        "blocks": [{**b, "end_min": b["end_min"] * k} for b in log["blocks"]],
    }
    if "events" in log:
        out["events"] = [{**e, "minute": e["minute"] * k} for e in log["events"]]
    return out


# An indoor ride of at least 30 minutes with HR on its records: the kind of
# activity a ride log exists for. Long enough that every block of the
# synthetic logs spans several 30 s bins.
SUBJECT_SQL = """
    SELECT a.activity_id, a.run_id, a.timer_time_s / 60.0
    FROM activities a
    WHERE a.sub_sport = 'indoor_cycling' AND a.timer_time_s >= 1800
      AND EXISTS (SELECT 1 FROM records r WHERE r.activity_id = a.activity_id
                  AND r.heart_rate IS NOT NULL)
    ORDER BY a.start_time_utc LIMIT 1"""
SUBJECT_WHAT = "indoor_cycling activity of 30+ min with heart rate"


class _RideCorpusTest(CorpusTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.aid, cls.rid, cls.dur = one(cls.con, SUBJECT_SQL, what=SUBJECT_WHAT)


class _Store:
    """Redirect BOTH athlete-state files this module touches."""

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        d = Path(self._tmp.name)
        self.path = d / "ride_blocks.json"
        self._p = [
            mock.patch.object(RB, "RIDE_BLOCKS", self.path),
            mock.patch.object(RB, "BIKE_CONFIG", d / "bike_config.json"),
        ]
        for p in self._p:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._p:
            p.stop()
        self._tmp.cleanup()
        return False

    def raw(self):
        return json.loads(self.path.read_text())


class TestTheCorpusActuallyLooksLikeThis(_RideCorpusTest):
    """The build rests on these being true; they are asserted, not assumed.
    If a corpus's indoor rides start carrying cadence or speed, this fires
    and §18's premise needs revisiting -- that is its job."""

    def test_indoor_rides_carry_hr_and_nothing_else(self):
        n, hr, cad, spd, pos = self.con.execute("""
            SELECT count(*), count(r.heart_rate), count(r.cadence_raw_rpm),
                   count(r.speed_mps), count(r.lat)
            FROM records r JOIN activities a USING (activity_id)
            WHERE a.sub_sport = 'indoor_cycling'""").fetchone()
        self.assertGreater(n, 0)
        # HR ON ESSENTIALLY EVERY RECORD. Asserting ALL of them broke the
        # first time a ride arrived with a handful of HR-less records. The
        # property the build rests on is that HR is the channel these rides
        # have, not that it never drops a sample -- so it is pinned as a
        # fraction, and the exact count is reported rather than frozen.
        self.assertGreater(hr / n, 0.999, f"{n - hr} of {n} records lack HR")
        # THE TWIN IS THE STRICT HALF: the other channels are absent ENTIRELY,
        # and that is what makes the athlete's annotations necessary.
        self.assertEqual((cad, spd, pos), (0, 0, 0))

    def test_the_odometer_is_not_a_measurement(self):
        """Constant (the watch writes a fake 0.0) or absent -- never a
        distance that moves."""
        lo, hi, distinct = self.con.execute(
            """
            SELECT min(r.distance_m), max(r.distance_m),
                   count(DISTINCT r.distance_m)
            FROM records r WHERE r.activity_id = ?""",
            [self.aid],
        ).fetchone()
        self.assertLessEqual(distinct, 1)
        if distinct == 1:
            self.assertEqual((lo, hi), (0.0, 0.0))

    def test_session_series_already_bins_hr_for_one(self):
        from trail_factors.analysis.series import binned_series

        s = binned_series(self.con, self.aid)
        expected = int(self.dur * 2)  # 30 s bins over the timer
        self.assertGreaterEqual(s["n_bins"], expected * 0.9)
        with_hr = sum(1 for b in s["bins"] if b["avg_hr"] is not None)
        self.assertGreaterEqual(with_hr, s["n_bins"] * 0.9)


class TestTheRoundTrip(unittest.TestCase):
    def test_a_block_set_saves_and_reads_back(self):
        with _Store() as s:
            e = RB.save_ride_blocks(dict(LOG_RPM), duration_min=DUR)
            self.assertEqual(e["revision"], 0)
            back = RB.load_ride_blocks()[RIDE]
            self.assertEqual(len(back["blocks"]), 2)
            self.assertEqual(back["raw_text"], LOG_RPM["raw_text"])
            self.assertEqual(
                back["machine_totals"],
                {"distance": 25.0, "distance_unit": "km", "avg_rpm": 80.0},
            )
            self.assertEqual(s.raw()[RIDE]["blocks"][0]["resistance"], 7.0)

    def test_an_end_anchored_input_derives_the_intervals(self):
        """Amendment 1: (resistance, rpm?, end_min) in, [start, end) out."""
        with _Store():
            e = RB.save_ride_blocks(dict(LOG_NO_RPM), duration_min=DUR)
        spans = [(b["start_min"], b["end_min"]) for b in e["blocks"]]
        self.assertEqual(spans, [(0.0, 15.0), (15.0, 30.0), (30.0, 50.0)])
        self.assertEqual([b["resistance"] for b in e["blocks"]], [6.5, 7.5, 9.25])

    def test_start_min_may_not_be_supplied(self):
        """One way to state a boundary means no way for two to disagree."""
        with _Store():
            with self.assertRaises(RB.RideBlockError) as ctx:
                RB.save_ride_blocks(
                    {
                        **LOG_NO_RPM,
                        "blocks": [{"resistance": 6.5, "start_min": 0, "end_min": 15}],
                    },
                    duration_min=DUR,
                )
            self.assertIn("DERIVED", str(ctx.exception))

    def test_a_log_with_no_rpm_anywhere_round_trips(self):
        """Amendment 1: absent is absent, and that is a COMPLETE valid log."""
        with _Store():
            e = RB.save_ride_blocks(dict(LOG_NO_RPM), duration_min=DUR)
            back = RB.load_ride_blocks()[RIDE]
        for b in e["blocks"] + back["blocks"]:
            self.assertIsNone(b["rpm"])
            self.assertIsNone(b["rpm_stated_as"])

    def test_a_single_rpm_becomes_a_range_and_a_range_is_never_collapsed(self):
        with _Store():
            e = RB.save_ride_blocks(dict(LOG_RPM), duration_min=DUR)
        self.assertEqual(e["blocks"][0]["rpm"], [90.0, 95.0])  # kept as stated
        self.assertEqual(e["blocks"][1]["rpm"], [85.0, 85.0])  # single -> pair
        # NEVER a midpoint: 92.5 would invent precision the memory lacks.
        self.assertNotIn(92.5, e["blocks"][0]["rpm"])


class TestRejections(unittest.TestCase):
    def test_a_block_past_the_activity_duration_is_rejected_NAMING_the_bound(self):
        with _Store():
            with self.assertRaises(RB.RideBlockError) as ctx:
                RB.save_ride_blocks(
                    {**LOG_NO_RPM, "blocks": [{"resistance": 6.5, "end_min": 90}]},
                    duration_min=DUR,
                )
            msg = str(ctx.exception)
            self.assertIn("90", msg)
            self.assertIn("50", msg)  # the BOUND is named
            self.assertIn("timer time", msg)

    def test_contradictory_blocks_are_rejected_naming_BOTH_entries(self):
        """Amendment 2: ambiguity is refused, never resolved."""
        with _Store():
            with self.assertRaises(RB.RideBlockError) as ctx:
                RB.save_ride_blocks(
                    {
                        **LOG_NO_RPM,
                        "blocks": [
                            {"resistance": 7.0, "end_min": 35},
                            {"resistance": 8.5, "end_min": 30},
                        ],
                    },  # goes backwards
                    duration_min=DUR,
                )
            msg = str(ctx.exception)
            self.assertIn("block 1", msg)  # the offending entry
            self.assertIn("30", msg)  # ...and its value
            self.assertIn("35", msg)  # ...and what it collides with
            self.assertIn("overlap", msg.lower())

    def test_resistance_outside_the_scale_is_rejected(self):
        with _Store():
            with self.assertRaises(RB.RideBlockError) as ctx:
                RB.save_ride_blocks(
                    {**LOG_NO_RPM, "blocks": [{"resistance": 65, "end_min": 15}]},
                    duration_min=DUR,
                )
            self.assertIn("65", str(ctx.exception))

    def test_the_ceiling_says_it_is_DECLARED_not_measured(self):
        """Until the athlete measures where the knob stops, the ceiling is a
        declared logging-error detector, said to be declared."""
        with _Store():
            ceiling, source = RB.resistance_max()
            self.assertEqual(source, "declared_editorial_not_measured")
            with self.assertRaises(RB.RideBlockError) as ctx:
                RB.save_ride_blocks(
                    {
                        **LOG_NO_RPM,
                        "blocks": [{"resistance": ceiling + 1, "end_min": 15}],
                    },
                    duration_min=DUR,
                )
            self.assertIn("declared editorial", str(ctx.exception))

    def test_the_payload_names_WHERE_the_ceiling_lives(self):
        """A report pointed at data/athlete/bike_config.json, which does not
        exist -- the number is a constant in the module. A claim about where a
        value lives must be checkable, so it is a field, not prose."""
        from pathlib import Path as _P

        with _Store():
            self.assertFalse(_P(str(RB.BIKE_CONFIG)).exists())
            e = RB.save_ride_blocks(dict(LOG_NO_RPM), duration_min=DUR)
        where = e["resistance_scale"]["max_defined_in"]
        self.assertIn("ride_blocks.py", where)
        self.assertIn("RESISTANCE_MAX_DECLARED", where)
        # ...and that constant is genuinely there.
        self.assertIn(
            "RESISTANCE_MAX_DECLARED",
            (REPO_ROOT / "src/trail_factors/ingest/ride_blocks.py").read_text(),
        )

    def test_the_config_file_is_NOT_created_by_a_save(self):
        """data/athlete/ is athlete-SUPPLIED state. Filing a declared default
        there would record a guess as though the athlete had said it."""
        with _Store() as s:
            RB.save_ride_blocks(dict(LOG_NO_RPM), duration_min=DUR)
            self.assertFalse((s.path.parent / "bike_config.json").exists())

    def test_no_constant_claims_which_values_were_logged(self):
        """`observed` is a claim about data, and a constant listing logged
        values goes stale with the next ride. The set belongs to the stored
        blocks; the module carries no such list, and a save does not stamp
        one onto the entry."""
        self.assertFalse(hasattr(RB, "RESISTANCE_VALUES_LOGGED"))
        with _Store():
            e = RB.save_ride_blocks(dict(LOG_NO_RPM), duration_min=DUR)
        self.assertNotIn("values_logged_to_date", e["resistance_scale"])

    def test_a_measured_ceiling_overrides_and_relabels(self):
        """THE TWIN: when the athlete measures it, the label must change."""
        with _Store() as s:
            (s.path.parent / "bike_config.json").write_text(
                json.dumps({"resistance_max_turns": 30.0})
            )
            self.assertEqual(RB.resistance_max(), (30.0, "measured_by_athlete"))
            e = RB.save_ride_blocks(
                {**LOG_NO_RPM, "blocks": [{"resistance": 25.0, "end_min": 15}]},
                duration_min=DUR,
            )
            self.assertEqual(e["resistance_scale"]["max_source"], "measured_by_athlete")

    def test_quarter_turn_resolution_is_NOT_enforced(self):
        """The knob is a 360-degree swivel read by eye. Refusing a 6.3 would
        be hostile to the way the log is actually kept."""
        with _Store():
            e = RB.save_ride_blocks(
                {**LOG_NO_RPM, "blocks": [{"resistance": 6.3, "end_min": 15}]},
                duration_min=DUR,
            )
            self.assertEqual(e["blocks"][0]["resistance"], 6.3)
            self.assertFalse(e["resistance_scale"]["resolution_enforced"])

    def test_raw_text_is_required(self):
        with _Store():
            with self.assertRaises(RB.RideBlockError) as ctx:
                RB.save_ride_blocks(
                    {k: v for k, v in LOG_NO_RPM.items() if k != "raw_text"},
                    duration_min=DUR,
                )
            self.assertIn("raw_text", str(ctx.exception))

    def test_a_distance_without_its_unit_is_refused(self):
        """A bare number whose unit is assumed is the decimal-minutes bug."""
        with _Store():
            with self.assertRaises(RB.RideBlockError) as ctx:
                RB.save_ride_blocks(
                    {**LOG_NO_RPM, "machine_totals": {"distance": 25.0}},
                    duration_min=DUR,
                )
            self.assertIn("distance_unit", str(ctx.exception))


class TestRawTextSurvivesSupersede(unittest.TestCase):
    """Amendment 1: the text is the record; the blocks are an interpretation."""

    def test_raw_text_is_byte_identical_through_revisions(self):
        original = LOG_RPM["raw_text"]
        digest = hashlib.sha256(original.encode()).hexdigest()
        with _Store() as s:
            RB.save_ride_blocks(dict(LOG_RPM), duration_min=DUR)
            corrected = {
                **LOG_RPM,
                "blocks": [
                    {"resistance": 7.0, "rpm": [90, 95], "end_min": 25},
                    {"resistance": 8.5, "rpm": 85, "end_min": 50},
                ],
            }
            e = RB.save_ride_blocks(corrected, duration_min=DUR, supersede=True)
            self.assertEqual(e["revision"], 1)
            self.assertEqual(hashlib.sha256(e["raw_text"].encode()).hexdigest(), digest)
            old = s.raw()[RIDE]["superseded"][0]
            self.assertEqual(
                hashlib.sha256(old["raw_text"].encode()).hexdigest(), digest
            )
            self.assertEqual(old["blocks"][0]["end_min"], 30.0)  # kept as was

    def test_a_second_save_without_supersede_is_refused(self):
        with _Store():
            RB.save_ride_blocks(dict(LOG_NO_RPM), duration_min=DUR)
            with self.assertRaises(RB.RideBlockError) as ctx:
                RB.save_ride_blocks(dict(LOG_NO_RPM), duration_min=DUR)
            msg = str(ctx.exception)
            self.assertIn("already has a ride log", msg)
            self.assertIn("ARGUMENT BESIDE", msg)  # position, not just name


class TestTheRead(_RideCorpusTest):
    def save(self, log):
        return RB.save_ride_blocks(
            scaled(log, self.rid, self.dur), duration_min=self.dur
        )

    def test_an_unannotated_ride_is_unavailable_with_a_reason(self):
        """NOT an empty list, which reads as a ride with no blocks in it."""
        with _Store():
            r = RD.ride_blocks(self.con, self.aid)
        self.assertEqual(r["status"], "unavailable")
        self.assertIsNone(r["blocks"])
        self.assertIn("no ride log has been saved", r["reason"])
        self.assertIn("NOT a ride without blocks", r["reason"])

    def test_per_block_hr_comes_from_the_existing_bins(self):
        with _Store():
            self.save(LOG_NO_RPM)
            r = RD.ride_blocks(self.con, self.aid)
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["n_blocks"], 3)
        for b in r["blocks"]:
            self.assertIsNotNone(b["mean_hr"])
            self.assertGreater(b["n_bins_with_hr"], 5)
            self.assertLessEqual(b["min_hr"], b["mean_hr"])
            self.assertGreaterEqual(b["max_hr"], b["mean_hr"])
            self.assertIsNotNone(b["hr_end_minus_start"])

    def test_a_block_containing_an_off_bike_event_is_FLAGGED(self):
        """The clock keeps running through a water-bottle break, so the break
        lands inside a block and its HR is recovery, not work."""
        with _Store():
            self.save(LOG_RPM)
            r = RD.ride_blocks(self.con, self.aid)
        flagged = [b for b in r["blocks"] if b["contains_event"]]
        self.assertEqual(len(flagged), 1)
        self.assertEqual(flagged[0]["events"][0]["text"], "water stop")
        clean = [b for b in r["blocks"] if not b["contains_event"]]
        self.assertTrue(clean, "THE TWIN: not every block is flagged")

    def test_provenance_travels_with_the_numbers(self):
        with _Store():
            self.save(LOG_RPM)
            r = RD.ride_blocks(self.con, self.aid)
        self.assertEqual(r["provenance"]["blocks"], "athlete_recalled")
        self.assertEqual(r["provenance"]["machine_totals"], "machine_reported")
        self.assertIn("authoritative", r["provenance"]["heart_rate"])
        self.assertIn("recalled", r["blocks"][0]["hr_provenance"])
        self.assertIn("NOTHING", r["filters"]["derived_from_resistance"])


class TestTheLongitudinalCut(_RideCorpusTest):
    def save(self, log):
        return RB.save_ride_blocks(
            scaled(log, self.rid, self.dur), duration_min=self.dur
        )

    def test_one_matching_block_returns_n_1_and_refuses_a_trend(self):
        with _Store():
            self.save(LOG_NO_RPM)
            h = RD.resistance_history(self.con, resistance=6.5)
        self.assertEqual(h["n_groups"], 1)
        g = h["groups"][0]
        self.assertEqual(g["n_blocks"], 1)
        self.assertEqual(g["n_sessions"], 1)
        self.assertIsNone(g["trend"])
        self.assertEqual(g["trend_status"], "insufficient")
        self.assertIn("1 block", g["trend_reason"])

    def test_effective_n_is_blocks_not_hr_records(self):
        with _Store():
            self.save(LOG_NO_RPM)
            h = RD.resistance_history(self.con)
        self.assertEqual(h["filters"]["effective_n"], "BLOCKS, never HR records")
        self.assertIn("pseudo-replication", h["filters"]["effective_n_note"])
        for g in h["groups"]:
            # n must be small: a block is ONE observation, not 600.
            self.assertLess(g["n_blocks"], 10)

    def test_a_block_with_no_rpm_never_merges_with_one_that_has_a_range(self):
        with _Store():
            self.save(LOG_NO_RPM)
            h = RD.resistance_history(self.con)
            keys = {
                (g["resistance"], None if g["rpm"] is None else tuple(g["rpm"]))
                for g in h["groups"]
            }
        self.assertTrue(all(k[1] is None for k in keys))
        self.assertEqual(len(keys), 3)


class TestTheRealAthleteFileIsUntouched(unittest.TestCase):
    def test_no_test_writes_into_data_athlete(self):
        """The real file is never READ here, only stat-ed: its contents are
        the athlete's, and this test needs only to know it did not change."""
        real = RB.RIDE_BLOCKS

        def stamp():
            return (
                (real.stat().st_size, real.stat().st_mtime_ns)
                if real.exists()
                else None
            )

        before = stamp()
        with _Store() as s:
            self.assertNotEqual(RB.RIDE_BLOCKS, real)
            RB.save_ride_blocks(dict(LOG_RPM), duration_min=DUR)
            self.assertTrue(s.path.exists(), "THE TWIN: the save did land")
        self.assertEqual(stamp(), before)


if __name__ == "__main__":
    unittest.main()
