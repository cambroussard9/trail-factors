"""§18.2 time in resistance, rpm reconciliation and the load index.

WHAT THESE PIN. Three of the four reads here are DERIVED from a recollection
plus a console readout, so the properties that matter are the ones that would
let a derivation quietly become a measurement: an index that changes when the
athlete happens to log one block instead of two, a reconciliation that clamps
a contradiction into agreement, or a unit appearing on a number that has none.

THE WORDS WATTS, POWER AND kJ ARE FORBIDDEN and a test enforces it. The
console is a rev counter -- its distance is a constant distance per
revolution -- so no absolute calibration exists and a figure in watts would be
a fabrication wearing a unit.

Every log here is SYNTHETIC. The one read that needs real heart rate finds an
indoor ride in the corpus by property.

Every negative has a positive twin.
"""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import duckdb

from tests.corpus import CorpusTestCase, one
from trail_factors.analysis import rides as RD
from trail_factors.ingest import ride_blocks as RB
from trail_factors.ingest.schema import DDL
from trail_factors.paths import REPO_ROOT

RIDE = "2025-01-15T0700Z"  # synthetic run_id


class _Store:
    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        d = Path(self._tmp.name)
        self.path = d / "rb.json"
        self._p = [
            mock.patch.object(RB, "RIDE_BLOCKS", self.path),
            mock.patch.object(RB, "BIKE_CONFIG", d / "bc.json"),
        ]
        for p in self._p:
            p.start()
        return self

    def __exit__(self, *e):
        for p in self._p:
            p.stop()
        self._tmp.cleanup()
        return False


def _save(blocks, avg_rpm=None, dur=50.0, run_id=RIDE):
    mt = {"distance": 25.0, "distance_unit": "km"}
    if avg_rpm is not None:
        mt["avg_rpm"] = avg_rpm
    return RB.save_ride_blocks(
        {
            "run_id": run_id,
            "raw_text": "fixture",
            "blocks": blocks,
            "machine_totals": mt,
        },
        duration_min=dur,
    )


class TestTheIndexIsInvariantToBlockSplitting(unittest.TestCase):
    """The athlete logging one 10-minute block or two 5-minute ones at the
    same setting is a LOGGING choice, not a training difference."""

    def test_two_five_minute_blocks_equal_one_ten_minute_block(self):
        with _Store():
            whole = _save(
                [
                    {"resistance": 6.0, "rpm": [80, 90], "end_min": 10},
                    {"resistance": 7.0, "rpm": [75, 85], "end_min": 20},
                ],
                avg_rpm=82.0,
                dur=20.0,
            )
            a = RD.load_index(whole["blocks"], RD.reconcile_rpm(whole["blocks"], 82.0))
        with _Store():
            two = _save(
                [
                    {"resistance": 6.0, "rpm": [80, 90], "end_min": 5},
                    {"resistance": 6.0, "rpm": [80, 90], "end_min": 10},
                    {"resistance": 7.0, "rpm": [75, 85], "end_min": 20},
                ],
                avg_rpm=82.0,
                dur=20.0,
            )
            b = RD.load_index(two["blocks"], RD.reconcile_rpm(two["blocks"], 82.0))
        self.assertEqual(a["status"], "ok")
        self.assertEqual(b["status"], "ok")
        self.assertAlmostEqual(a["index"], b["index"], places=9)
        self.assertAlmostEqual(a["index_per_min"], b["index_per_min"], places=9)
        # ...and the split really did produce a different number of blocks.
        self.assertNotEqual(len(a["per_block"]), len(b["per_block"]))

    def test_the_reconciled_rpm_itself_is_unchanged_by_the_split(self):
        """THE MECHANISM: t is a weighted-sum solution, so splitting leaves it
        alone. If it moved, the index could not be invariant."""
        blocks_1 = [
            {"start_min": 0.0, "end_min": 10.0, "resistance": 6.0, "rpm": [80, 90]}
        ]
        blocks_2 = [
            {"start_min": 0.0, "end_min": 5.0, "resistance": 6.0, "rpm": [80, 90]},
            {"start_min": 5.0, "end_min": 10.0, "resistance": 6.0, "rpm": [80, 90]},
        ]
        a = RD.reconcile_rpm(blocks_1, 85.0)
        b = RD.reconcile_rpm(blocks_2, 85.0)
        self.assertAlmostEqual(a["t"], b["t"], places=12)
        self.assertAlmostEqual(
            a["blocks"][0]["reconciled_rpm"],
            b["blocks"][0]["reconciled_rpm"],
            places=12,
        )

    def test_a_REAL_difference_does_move_the_index(self):
        """THE TWIN: an index invariant to everything would pass above and
        measure nothing."""
        blocks = [
            {"start_min": 0.0, "end_min": 10.0, "resistance": 6.0, "rpm": [80, 90]}
        ]
        harder = [
            {"start_min": 0.0, "end_min": 10.0, "resistance": 8.0, "rpm": [80, 90]}
        ]
        a = RD.load_index(blocks, RD.reconcile_rpm(blocks, 85.0))
        b = RD.load_index(harder, RD.reconcile_rpm(harder, 85.0))
        self.assertGreater(b["index"], a["index"])


class TestReconciliation(unittest.TestCase):
    BLOCKS = [
        {"start_min": 0.0, "end_min": 30.0, "resistance": 7.0, "rpm": [90, 95]},
        {"start_min": 30.0, "end_min": 50.0, "resistance": 8.5, "rpm": [80, 90]},
    ]
    CONSOLE = 90.0  # inside the achievable 86-93

    def test_a_solution_inside_the_ranges_reproduces_the_console_total(self):
        r = RD.reconcile_rpm(self.BLOCKS, self.CONSOLE)
        self.assertEqual(r["status"], "ok")
        num = sum(
            b["reconciled_rpm"] * (b["end_min"] - b["start_min"]) for b in r["blocks"]
        )
        den = sum(b["end_min"] - b["start_min"] for b in r["blocks"])
        self.assertAlmostEqual(num / den, self.CONSOLE, places=9)
        for b in r["blocks"]:
            lo, hi = b["rpm_range"]
            self.assertTrue(lo <= b["reconciled_rpm"] <= hi)
            self.assertIn("RECONCILED", b["rpm_provenance"])

    def test_a_total_outside_every_range_is_REFUSED_not_clamped(self):
        """Forcing agreement would hide a misremembered range or a misread
        console, which is the whole reason to reconcile at all."""
        lo_possible, hi_possible = RD.reconcile_rpm(self.BLOCKS, self.CONSOLE)[
            "achievable_range"
        ]
        for impossible in (lo_possible - 20.0, hi_possible + 20.0):
            r = RD.reconcile_rpm(self.BLOCKS, impossible)
            self.assertEqual(r["status"], "no_solution", impossible)
            self.assertIsNone(r["t"])
            self.assertEqual(r["blocks"], [])
            self.assertIn("NOT CLAMPED", r["reason"])
            self.assertIn(f"{lo_possible:.2f}", r["reason"])

    def test_the_index_refuses_too_when_reconciliation_did(self):
        r = RD.reconcile_rpm(self.BLOCKS, 500.0)
        i = RD.load_index(self.BLOCKS, r)
        self.assertEqual(i["status"], "unavailable")
        self.assertIsNone(i["index"])

    def test_a_block_with_no_rpm_makes_the_total_unattributable(self):
        blocks = self.BLOCKS + [
            {"start_min": 50.0, "end_min": 55.0, "resistance": 6.0, "rpm": None}
        ]
        r = RD.reconcile_rpm(blocks, self.CONSOLE)
        self.assertEqual(r["status"], "unavailable")
        self.assertIn("unattributable", r["reason"])

    def test_no_console_average_is_unavailable_not_a_guess(self):
        r = RD.reconcile_rpm(self.BLOCKS, None)
        self.assertEqual(r["status"], "unavailable")
        self.assertIn("nothing to reconcile", r["reason"])


class TestTheForbiddenVocabularyInARead(CorpusTestCase):
    """The full read path, on an indoor ride found in the corpus by property:
    the payload is real HR binned under a synthetic log."""

    def test_no_output_field_or_value_uses_a_power_unit(self):
        aid, rid, dur = one(
            self.con,
            """
            SELECT a.activity_id, a.run_id, a.timer_time_s / 60.0
            FROM activities a
            WHERE a.sub_sport = 'indoor_cycling' AND a.timer_time_s >= 1200
              AND EXISTS (SELECT 1 FROM records r
                          WHERE r.activity_id = a.activity_id
                            AND r.heart_rate IS NOT NULL)
            ORDER BY a.start_time_utc LIMIT 1""",
            what="indoor_cycling activity of 20+ min with heart rate",
        )
        with _Store():
            _save(
                [{"resistance": 6.0, "rpm": [80, 90], "end_min": 20}],
                avg_rpm=85.0,
                dur=dur,
                run_id=rid,
            )
            payload = RD.ride_blocks(self.con, aid)
            payload["time_in_resistance"] = RD.time_in_resistance(self.con)
        self.assertEqual(payload["status"], "ok")
        blob = json.dumps(payload, default=str).lower()
        for word in TestTheForbiddenVocabulary.BANNED:
            self.assertNotIn(word, blob, f"{word!r} must not appear")
        # "power" only as the word inside an explicit denial, never as a field.
        for key in re.findall(r'"([a-z_]+)":', blob):
            self.assertNotIn("power", key)
            self.assertNotIn("watt", key)


class TestTheForbiddenVocabulary(unittest.TestCase):
    """The console is a REV COUNTER. A figure in watts would be a fabrication
    wearing a unit, so the words cannot appear at all."""

    BANNED = ("watt", "kilojoule", "kj", "horsepower")

    def test_the_index_declares_itself_unitless_and_states_its_assumption(self):
        """THE TWIN: the ban is worth nothing if the number arrives bare."""
        blocks = [
            {"start_min": 0.0, "end_min": 10.0, "resistance": 6.0, "rpm": [80, 90]}
        ]
        i = RD.load_index(blocks, RD.reconcile_rpm(blocks, 85.0))
        self.assertIn("index, not a measurement", i["unit"])
        self.assertIn("UNITLESS", i["assumption"])
        self.assertIn("rev counter", i["assumption"])
        self.assertIn("NOT a power estimate", i["assumption"])

    def test_machine_distance_is_stored_but_enters_no_derivation(self):
        with _Store():
            e = _save(
                [{"resistance": 6.0, "rpm": [80, 90], "end_min": 20}],
                avg_rpm=85.0,
                dur=20.0,
            )
            self.assertEqual(e["machine_totals"]["distance"], 25.0)
        src = (REPO_ROOT / "src/trail_factors/analysis/rides.py").read_text()
        body = src[src.index("def reconcile_rpm") :]
        self.assertNotIn('["distance"]', body)
        self.assertNotIn('"distance"', body.split("def time_in_resistance")[0])


class TestTimeInResistance(unittest.TestCase):
    def test_minutes_aggregate_per_value_and_per_band(self):
        # An in-memory database holding one synthetic activity: the read
        # needs only the ride's start time and offset to place it in a week.
        con = duckdb.connect(":memory:")
        try:
            con.execute(DDL)
            con.execute(
                "INSERT INTO activities (activity_id, run_id, file_hash, "
                "source_file, start_time_utc, local_tz, source, "
                "sampling_regime, pipeline_version) VALUES "
                "('a1', ?, 'h1', 'x.fit', TIMESTAMP '2025-01-15 07:00:00', "
                "1.0, 'garmin_watch', '1hz', 1)",
                [RIDE],
            )
            with _Store():
                _save(
                    [
                        {"resistance": 7.0, "rpm": [90, 95], "end_min": 30},
                        {"resistance": 9.25, "rpm": [80, 90], "end_min": 50},
                    ],
                    avg_rpm=90.0,
                )
                t = RD.time_in_resistance(con)
        finally:
            con.close()
        self.assertEqual(t["n_weeks"], 1)
        wk = t["weeks"][0]
        self.assertEqual(wk["by_resistance"]["7.0"], 30.0)
        self.assertEqual(wk["by_resistance"]["9.25"], 20.0)
        # A BAND IS HALF-OPEN AND NAMED BY ITS LOWER EDGE: 9.25 falls in
        # [9.0, 9.5), so it bands to 9.0. Rounding was tried first and is
        # wrong here -- banker's rounding sends a quarter turn down on one
        # side of a band and up on the other, and quarter turns are exactly
        # what a log carries.
        self.assertEqual(wk["by_band"]["7.0"], 30.0)
        self.assertEqual(wk["by_band"]["9.0"], 20.0)
        self.assertIn("half-open", t["band_convention"])
        self.assertEqual(wk["total_min"], 50.0)
        self.assertEqual(
            t["filters"]["assumptions"], "none beyond the blocks being logged correctly"
        )


if __name__ == "__main__":
    unittest.main()
