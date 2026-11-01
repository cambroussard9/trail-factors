"""The device boundary: the second altitude channel, measured provenance,
Garmin's self-evaluation, and running dynamics (§9, §14d).

WHY A FILE PER DEVICE RATHER THAN PER MODULE. Every test here exists because
the first barometric-watch file behaved differently from the GPS-altitude
files before it, and the value is in the CONTRAST: a claim about a new watch
that is not checked against the old one is not a claim about hardware. So the
FIT-level tests read one file of each kind.

THE FILES ARE FOUND BY PROPERTY, never by id: the corpus says which activity
was measured as GPS-fed and which as barometric, and the raw file is read only
if it is on disk -- a corpus without its raw directory skips those tests with
the reason. Device model names (fr245_music, fr265_large) are hardware, not
anyone's data.

Every negative has a positive twin.
"""

from __future__ import annotations

import contextlib
import unittest
from pathlib import Path
from unittest import mock

from tests.corpus import CorpusTestCase, corpus_path, corpus_projection, isolated, one
from trail_factors.ingest import fit_file as F
from trail_factors.ingest import load as L
from trail_factors.ingest import validate as V

# --- isolation: never the real athlete state or config ----------------------
# Entered in setUpModule, CLOSED in tearDownModule -- see tests/corpus.py
# isolated() for why this is not unittest.enterModuleContext.
_ISOLATION = contextlib.ExitStack()


def setUpModule():
    if corpus_path() is not None:
        _ISOLATION.enter_context(isolated(projection_crs=corpus_projection()))


def tearDownModule():
    _ISOLATION.close()


def _raw_path(source_file: str) -> Path | None:
    """The FIT a stored `source_file` names, under the configured raw
    directory or the repo, or None when it is not on disk."""
    from trail_factors.paths import RAW_DIR, REPO_ROOT

    p = Path(source_file)
    candidates = [p if p.is_absolute() else REPO_ROOT / p]
    if "raw" in p.parts:
        candidates.append(RAW_DIR.joinpath(*p.parts[p.parts.index("raw") + 1 :]))
    return next((c for c in candidates if c.exists()), None)


# WHICH FILE IS WHICH IS ASKED OF THE CORPUS: the stored provenance is the
# property, the FIT is re-read to check it.
GPS_FED = """a.altitude_source = 'gps' AND a.altitude_source_basis = 'measured'
             AND a.device = 'fr245_music'"""
BAROMETRIC = """a.altitude_source = 'barometric'
                AND a.altitude_source_basis = 'measured'
                AND a.device LIKE 'fr265%'"""


def _fit(con, where: str, what: str) -> tuple[str, str, Path]:
    """(activity_id, device, raw path) of the first matching watch run whose
    raw file is on disk; SkipTest naming what is missing otherwise."""
    rows = con.execute(f"""
        SELECT a.activity_id, a.device, a.source_file FROM activities a
        WHERE a.source = 'garmin_watch' AND a.sport = 'running' AND ({where})
        ORDER BY a.start_time_utc""").fetchall()
    if not rows:
        raise unittest.SkipTest(f"this corpus has no {what}")
    for aid, device, sf in rows:
        path = _raw_path(sf or "")
        if path is not None:
            return aid, device, path
    raise unittest.SkipTest(f"no raw FIT for any {what} is on disk")


class _Files(CorpusTestCase):
    """One GPS-fed file and one barometric file, from the corpus's own
    provenance labels."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.gps_aid, cls.gps_model, cls.GPS = _fit(
            cls.con, GPS_FED, "GPS-fed fr245_music run"
        )
        cls.baro_aid, cls.baro_model, cls.BARO = _fit(
            cls.con, BAROMETRIC, "barometric fr265 run"
        )

    def stored(self, aid, cols):
        return self.con.execute(
            f"SELECT {cols} FROM activities WHERE activity_id = ?", [aid]
        ).fetchone()


class TestTheGnssChannelPairing(_Files):
    """§9: `gps_metadata` carries no timestamp, so the join is FILE ORDER.

    The rule -- each record takes the last gps_metadata frame written BEFORE it
    -- is not a preference. It is established on a GPS-fed watch, where both
    channels are demonstrably ONE series, so the correct pairing is the one
    that reproduces the record's own altitude.
    """

    def test_the_rule_reproduces_the_gps_fed_watchs_own_altitude(self):
        recs = F.read_messages(self.GPS)["record"]
        paired = [r for r in recs if r.get(F.GNSS_ALTITUDE_KEY) is not None]
        self.assertTrue(paired)
        exact = sum(
            1
            for r in paired
            if abs(r[F.GNSS_ALTITUDE_KEY] - r["enhanced_altitude"]) < 1e-9
        )
        self.assertGreaterEqual(
            exact / len(paired),
            F.ALTITUDE_IDENTICAL_MIN,
            "the preceding-frame rule should reproduce a GPS-fed watch's own altitude",
        )

    def test_the_OTHER_pairing_does_not(self):
        """THE DISCRIMINATOR. Without this, 'the join works' is untested: a
        test that passes under both rules says nothing about either."""
        import fitdecode

        seq = []
        with fitdecode.FitReader(self.GPS) as fit:
            for fr in fit:
                if fr.frame_type != fitdecode.FIT_FRAME_DATA:
                    continue
                if fr.name in ("record", "gps_metadata"):
                    seq.append(
                        (
                            fr.name,
                            fr.get_value("enhanced_altitude")
                            if fr.has_field("enhanced_altitude")
                            else None,
                        )
                    )
        hits = n = 0
        for i, (name, alt) in enumerate(seq):
            if name != "record" or alt is None:
                continue
            n += 1
            nxt = next((v for nm, v in seq[i + 1 :] if nm == "gps_metadata"), None)
            if nxt is not None and abs(nxt - alt) < 1e-9:
                hits += 1
        self.assertGreater(n, 0)
        self.assertLess(
            hits / n,
            0.5,
            "the FOLLOWING frame should NOT reproduce the record's "
            "altitude; if it does, the pairing argument is wrong",
        )

    def test_the_barometric_watchs_two_channels_are_genuinely_different(self):
        recs = F.read_messages(self.BARO)["record"]
        paired = [r for r in recs if r.get(F.GNSS_ALTITUDE_KEY) is not None]
        self.assertTrue(paired)
        exact = sum(
            1
            for r in paired
            if abs(r[F.GNSS_ALTITUDE_KEY] - r["enhanced_altitude"]) < 1e-9
        )
        self.assertLess(exact / len(paired), F.ALTITUDE_IDENTICAL_MIN)

    def test_the_first_record_has_no_preceding_fix_and_says_so(self):
        """An honest None, not a borrowed value from the record after it."""
        for path in (self.GPS, self.BARO):
            recs = F.read_messages(path)["record"]
            self.assertIsNone(recs[0][F.GNSS_ALTITUDE_KEY], path.name)
            self.assertTrue(
                any(r[F.GNSS_ALTITUDE_KEY] is not None for r in recs), path.name
            )


class TestProvenanceIsMeasuredNotLookedUp(_Files):
    """The device map alone is what failed: a watch reports a model string
    (`fr265_large`) the map may not know, and every record would have read
    `unknown`. Re-measuring the raw file must reproduce the stored label."""

    def prov(self, path):
        return F.altitude_provenance(
            F.coalesce_records(F.read_messages(path)["record"])
        )

    def test_the_gps_fed_watch_measures_as_gps(self):
        prov = self.prov(self.GPS)
        self.assertEqual(prov["source"], "gps")
        self.assertGreaterEqual(prov["identical_fraction"], F.ALTITUDE_IDENTICAL_MIN)
        # Ingest measured the same file: the two must agree (closely, not
        # exactly -- a confirmed recording window trims records before the
        # stored measurement is taken).
        stored = self.stored(self.gps_aid, "altitude_identical_frac")[0]
        self.assertAlmostEqual(prov["identical_fraction"], stored, delta=0.02)

    def test_the_barometric_watch_measures_as_barometric(self):
        prov = self.prov(self.BARO)
        self.assertEqual(prov["source"], "barometric")
        self.assertLess(prov["identical_fraction"], F.ALTITUDE_IDENTICAL_MIN)
        # THE DIRECTION IS THE TEST, not the magnitude: a barometer is a
        # low-pass channel and GNSS altitude is the noisiest thing either
        # watch records.
        self.assertLess(prov["sd_record_m"], prov["sd_gnss_m"])

    def test_the_measurement_would_survive_the_map_being_wrong(self):
        """The point of measuring. Delete the map entry and the answer stands."""
        without = {
            k: v
            for k, v in F.ALTITUDE_SOURCE_BY_DEVICE.items()
            if not k.startswith("fr265")
        }
        with mock.patch.object(F, "ALTITUDE_SOURCE_BY_DEVICE", without):
            self.assertEqual(
                F.altitude_source("fr265_large", "garmin_watch"), "unknown"
            )
            self.assertEqual(self.prov(self.BARO)["source"], "barometric")

    def test_the_map_still_agrees_where_it_has_an_opinion(self):
        """The cross-check must be live: a map that disagreed with every
        measurement would be worthless as a check."""
        for path, model in ((self.GPS, self.gps_model), (self.BARO, self.baro_model)):
            self.assertEqual(
                self.prov(path)["source"],
                F.altitude_source(model, "garmin_watch"),
                model,
            )


class TestTooFewPairs(unittest.TestCase):
    def test_too_few_pairs_is_not_an_answer(self):
        prov = F.altitude_provenance(
            [{"enhanced_altitude": 200.0, F.GNSS_ALTITUDE_KEY: 200.0}] * 10
        )
        self.assertEqual(prov["source"], "unknown")
        self.assertIn("fewer than", prov["reason"])

    def test_enough_identical_pairs_is_gps(self):
        """THE TWIN: the same synthetic channel, past the pair floor."""
        n = F.ALTITUDE_MIN_PAIRS + 5
        recs = [
            {"enhanced_altitude": 200.0 + i, F.GNSS_ALTITUDE_KEY: 200.0 + i}
            for i in range(n)
        ]
        self.assertEqual(F.altitude_provenance(recs)["source"], "gps")


class TestProvenanceAsStored(CorpusTestCase):
    """What the corpus says after ingest -- including the runs where the
    measurement cannot be made at all."""

    def test_the_basis_distinguishes_measured_from_fallen_back(self):
        rows = dict(
            self.con.execute("""
            SELECT altitude_source_basis, count(*) FROM activities
            WHERE source = 'garmin_watch' GROUP BY 1""").fetchall()
        )
        if not rows:
            self.skipTest("this corpus has no watch activity")
        self.assertLessEqual(set(rows), {"measured", "device_map"})
        self.assertGreater(rows.get("measured", 0), 0)

    def test_an_UNMEASURABLE_run_is_not_a_DISAGREEMENT(self):
        """THE BUG THIS PINS. Indoor runs have no position, no GNSS channel and
        no altitude, so provenance cannot be measured -- and they were being
        stored as `unknown` with agrees=False, which says the data contradicts
        the device map. It cannot: there is no data."""
        from trail_factors.ingest.fit_file import ALTITUDE_MIN_PAIRS

        rows = self.con.execute("""
            SELECT a.activity_id, a.altitude_source, a.altitude_source_agrees,
                   (SELECT count(altitude_gnss_m) FROM records r
                    WHERE r.activity_id = a.activity_id)
            FROM activities a
            WHERE a.altitude_source_basis = 'device_map'""").fetchall()
        if not rows:
            self.skipTest("every activity in this corpus was measured")
        for aid, src, agrees, paired in rows:
            # THE RULE IS "TOO FEW PAIRED RECORDS", not "no position": an
            # older file whose gps_metadata is sparse can fall short of
            # ALTITUDE_MIN_PAIRS while still carrying most of its position.
            # Asserting zero coverage would pin a proxy for the rule.
            self.assertLess(
                paired,
                ALTITUDE_MIN_PAIRS,
                f"{aid}: a run with enough paired records must be MEASURED",
            )
            self.assertIsNone(agrees, aid)
            self.assertNotEqual(src, "unknown", aid)

    def test_a_MEASURED_run_carries_its_statistics(self):
        """THE POSITIVE TWIN: the fallback path must not be swallowing runs
        that could have been measured. A barometric label is the barometric
        signature: diverging from GNSS and SMOOTHER than it (§9.2)."""
        from trail_factors.ingest.fit_file import ALTITUDE_IDENTICAL_MIN

        row = one(
            self.con,
            """
            SELECT altitude_divergence_m, altitude_identical_frac,
                   altitude_sd_record_m, altitude_sd_gnss_m
            FROM activities a WHERE """
            + BAROMETRIC
            + """
            ORDER BY start_time_utc LIMIT 1""",
            what="measured barometric run",
        )
        for v in row:
            self.assertIsNotNone(v)
        self.assertLess(row[1], ALTITUDE_IDENTICAL_MIN)
        self.assertLess(row[2], row[3])

    def test_the_gnss_channel_reached_the_records_table(self):
        """THE PREMISE "ONLY THE FIRST RECORD IS UNPAIRED" WAS FALSE, and was
        pinned as a count. A watch that starts recording before it has a fix
        writes no gps_metadata until it does, so a file can open with a
        leading block of unpaired records.

        WHAT MATTERS IS THAT A GAP IS HANDLED, and that is what is asserted:
          * the channel reached the records table at all;
          * an unpaired record can only PRECEDE the first GNSS frame -- the
            join takes the last frame written BEFORE a record -- so the
            unpaired records are a LEADING block, never scattered;
          * an unpaired record is NULL, never a coerced value, and keeps its
            device altitude, so no elevation is lost;
          * provenance falls back by its own rule: `measured` when enough
            records paired, `device_map` when they did not;
          * a leading gap without position is FLAGGED (gps_coverage).
        """
        from trail_factors.ingest.fit_file import ALTITUDE_MIN_PAIRS
        from trail_factors.ingest.validate import COVERAGE_WARN

        aids = [
            a
            for (a,) in self.con.execute(
                "SELECT activity_id FROM activities WHERE device LIKE 'fr265%' "
                "ORDER BY activity_id"
            ).fetchall()
        ]
        if not aids:
            self.skipTest("this corpus has no fr265 activity")
        for aid in aids:
            n_total, n_gnss = self.con.execute(
                """
                SELECT count(*), count(altitude_gnss_m) FROM records
                WHERE activity_id = ?""",
                [aid],
            ).fetchone()
            if n_gnss == 0:
                # AN INDOOR ACTIVITY HAS NO gps_metadata TO PAIR WITH, and
                # nothing unpaired about it. The pairing claim is about
                # POSITIONED files, and this check is what keeps the branch
                # from becoming a way to skip a real failure: no position is
                # REQUIRED for the exemption.
                self.assertEqual(
                    self.con.execute(
                        "SELECT count(lat) FROM records WHERE activity_id = ?", [aid]
                    ).fetchone()[0],
                    0,
                    f"{aid}: no GNSS altitude but it DOES carry position",
                )
                continue
            first_paired, last_unpaired, n_unpaired_no_device, n_unpaired_no_pos = (
                self.con.execute(
                    """
                SELECT min(timestamp) FILTER (WHERE altitude_gnss_m IS NOT NULL),
                       max(timestamp) FILTER (WHERE altitude_gnss_m IS NULL),
                       count(*) FILTER (WHERE altitude_gnss_m IS NULL
                                        AND altitude_device_m IS NULL),
                       count(*) FILTER (WHERE altitude_gnss_m IS NULL
                                        AND lat IS NULL)
                FROM records WHERE activity_id = ?""",
                    [aid],
                ).fetchone()
            )
            if last_unpaired is not None:
                self.assertLess(
                    last_unpaired,
                    first_paired,
                    f"{aid}: an unpaired record AFTER the first "
                    f"GNSS frame means the join is not file-order",
                )
            self.assertEqual(
                n_unpaired_no_device,
                0,
                f"{aid}: a GNSS gap must not cost the device altitude",
            )
            basis, cov = self.con.execute(
                "SELECT altitude_source_basis, coverage_fraction FROM activities "
                "WHERE activity_id = ?",
                [aid],
            ).fetchone()
            self.assertEqual(
                basis, "measured" if n_gnss >= ALTITUDE_MIN_PAIRS else "device_map", aid
            )
            if n_unpaired_no_pos and cov < COVERAGE_WARN:
                flags = {
                    r[0]
                    for r in self.con.execute(
                        "SELECT check_name FROM validation_results "
                        "WHERE activity_id = ?",
                        [aid],
                    ).fetchall()
                }
                self.assertIn(
                    "gps_coverage",
                    flags,
                    f"{aid}: a leading no-fix block must be flagged",
                )

    def test_a_leading_gap_run_reads_its_provenance_correctly(self):
        """The barometric run with the LONGEST leading block of unpaired
        records. Its barometer is still measured against GNSS, and the
        barometric signature holds: diverging from GNSS and SMOOTHER than it
        (§9.2)."""
        from trail_factors.ingest.fit_file import ALTITUDE_IDENTICAL_MIN

        row = one(
            self.con,
            """
            SELECT a.altitude_source, a.altitude_source_basis,
                   a.altitude_source_device, a.altitude_source_agrees,
                   a.altitude_identical_frac, a.altitude_sd_record_m,
                   a.altitude_sd_gnss_m
            FROM activities a JOIN records r USING (activity_id)
            WHERE a.device LIKE 'fr265%' AND a.altitude_source_basis = 'measured'
              AND a.altitude_source = 'barometric'
            GROUP BY ALL
            HAVING count(*) FILTER (WHERE r.altitude_gnss_m IS NULL) > 1
            ORDER BY count(*) FILTER (WHERE r.altitude_gnss_m IS NULL) DESC
            LIMIT 1""",
            what="barometric run with a leading unpaired block",
        )
        src, basis, dev, agrees, ident, sd_rec, sd_gnss = row
        self.assertEqual(
            (src, basis, dev, agrees), ("barometric", "measured", "barometric", True)
        )
        self.assertLess(ident, ALTITUDE_IDENTICAL_MIN)
        self.assertLess(sd_rec, sd_gnss)

    def test_the_older_watch_also_gained_the_channel(self):
        """It was always in those files; nothing read it. A fix that only
        reached the new watch would leave the corpus half-instrumented."""
        one(
            self.con,
            "SELECT 1 FROM activities WHERE device = 'fr245_music' "
            "AND coverage_fraction > 0 LIMIT 1",
            what="positioned fr245_music activity",
        )
        n = self.con.execute("""
            SELECT count(altitude_gnss_m) FROM records r JOIN activities a
              USING (activity_id) WHERE a.device = 'fr245_music'""").fetchone()[0]
        self.assertGreater(n, 0)


class TestGarminSelfEvaluation(unittest.TestCase):
    """The watch's own post-run rating: decoded with its scale, and the
    only RPE that prices a session load."""

    def test_the_encoding_is_decoded(self):
        out = L._self_evaluation({"workout_rpe": 40, "workout_feel": 75})
        self.assertEqual(out["garmin_rpe"], 4.0)
        self.assertEqual(out["garmin_feel"], 4)
        self.assertIn("DIRECTION UNVERIFIED", out["garmin_selfeval_scale"])

    def test_every_point_of_the_five_point_scale_decodes(self):
        got = [
            L._self_evaluation({"workout_feel": v})["garmin_feel"]
            for v in (0, 25, 50, 75, 100)
        ]
        self.assertEqual(got, [1, 2, 3, 4, 5])

    def test_an_off_grid_feel_is_NULL_rather_than_rounded(self):
        """A value off the documented steps means the encoding changed, and a
        rounded guess would hide that under a plausible number."""
        self.assertIsNone(L._self_evaluation({"workout_feel": 60})["garmin_feel"])

    def test_a_run_that_was_never_asked_carries_nothing(self):
        out = L._self_evaluation({})
        self.assertIsNone(out["garmin_rpe"])
        self.assertIsNone(out["garmin_feel"])
        self.assertIsNone(out["garmin_selfeval_scale"])


class TestGarminSelfEvaluationAsStored(CorpusTestCase):
    def test_stored_values_are_on_the_decoded_scales(self):
        rows = self.con.execute("""
            SELECT source, garmin_rpe, garmin_feel, garmin_selfeval_scale
            FROM activities
            WHERE garmin_rpe IS NOT NULL OR garmin_feel IS NOT NULL""").fetchall()
        if not rows:
            self.skipTest("no activity in this corpus carries a self-evaluation")
        for source, rpe, feel, scale in rows:
            # Only the watch asks; a phone file never carries one.
            self.assertEqual(source, "garmin_watch")
            if rpe is not None:
                self.assertGreaterEqual(rpe, 0.0)
                self.assertLessEqual(rpe, 10.0)
            if feel is not None:
                self.assertIn(feel, (1, 2, 3, 4, 5))
            self.assertIsNotNone(scale, "the scale travels with every value")

    def test_the_report_shows_it_and_prices_the_load_from_it(self):
        from trail_factors.analysis.report import build, render

        aid, rpe, timer_s = one(
            self.con,
            """
            SELECT activity_id, garmin_rpe, timer_time_s FROM activities
            WHERE garmin_rpe IS NOT NULL AND timer_time_s IS NOT NULL
              AND source = 'garmin_watch' AND sport = 'running'
            ORDER BY start_time_utc DESC LIMIT 1""",
            what="watch run with a Garmin RPE",
        )
        rep = build(self.con, aid)
        gse = rep["athlete"]["garmin_self_evaluation"]
        self.assertEqual(gse["status"], "ok")
        self.assertEqual(gse["garmin_rpe"], rpe)
        self.assertIsNotNone(gse["scale"], "the scale travels with the value")
        self.assertNotIn("do_not_pool", gse)
        sl = rep["session_load"]
        self.assertEqual(sl["status"], "ok")
        self.assertAlmostEqual(sl["foster_load_au"], rpe * timer_s / 60.0)
        text = render(rep)
        self.assertIn("GARMIN SELF-EVALUATION", text)
        self.assertIn("SESSION LOAD", text)
        self.assertIn("feel not used", text)

    def test_an_unrated_run_says_unavailable_not_zero(self):
        """THE TWIN: a watch that never asked has no rating, and a 0/10 would
        be a fabricated answer."""
        from trail_factors.analysis.report import build

        aid = one(
            self.con,
            """
            SELECT activity_id FROM activities
            WHERE garmin_rpe IS NULL AND garmin_feel IS NULL
              AND source = 'garmin_watch' AND sport = 'running'
              AND coverage_fraction > 0
            ORDER BY start_time_utc DESC LIMIT 1""",
            what="positioned watch run with no self-evaluation",
        )[0]
        rep = build(self.con, aid)
        gse = rep["athlete"]["garmin_self_evaluation"]
        self.assertEqual(gse["status"], "unavailable")
        self.assertIsNone(gse["garmin_rpe"])
        self.assertIn("did not ask", gse["reason"])
        # ...and the load is unavailable with a reason, never priced at 0.
        self.assertEqual(rep["session_load"]["status"], "unavailable")
        self.assertIsNone(rep["session_load"]["foster_load_au"])


class TestRunningDynamics(CorpusTestCase):
    """Present and populated where the watch records them; absent, with a
    reason, where it does not (§7, §17.2)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.dyn = one(
            cls.con,
            """
            SELECT r.activity_id FROM records r
            JOIN activities a USING (activity_id)
            WHERE a.source = 'garmin_watch' AND a.sport = 'running'
              AND r.vertical_oscillation IS NOT NULL
            GROUP BY 1 ORDER BY count(*) DESC, 1 LIMIT 1""",
            what="watch run with running-dynamics records",
        )[0]

    def test_the_four_channels_are_stored(self):
        row = self.con.execute(
            """
            SELECT count(vertical_oscillation), count(vertical_ratio),
                   count(stance_time), count(step_length)
            FROM records WHERE activity_id = ?""",
            [self.dyn],
        ).fetchone()
        for i, name in enumerate(
            ("vertical_oscillation", "vertical_ratio", "stance_time", "step_length")
        ):
            self.assertGreater(row[i], 0, name)

    def test_the_accessory_only_channels_are_NULL(self):
        """§7 predicted this: stance_time_balance and stance_time_percent need
        a chest strap or pod. Null is the correct answer, not zero."""
        n = self.con.execute(
            """
            SELECT count(*) FROM records WHERE activity_id = ?
              AND stance_time_pct = 0""",
            [self.dyn],
        ).fetchone()[0]
        self.assertEqual(n, 0, "an absent accessory channel must be NULL, never 0")

    def test_basic_metrics_reports_them_with_UNITS(self):
        from trail_factors.analysis.basic import running_dynamics

        rd = running_dynamics(self.con, self.dyn)
        self.assertEqual(rd["vertical_oscillation_mm"]["unit"], "mm")
        self.assertEqual(rd["stance_time_ms"]["unit"], "ms")
        self.assertEqual(rd["vertical_ratio_pct"]["unit"], "percent")
        self.assertEqual(rd["step_length_mm"]["unit"], "mm")
        # These are millimetres, so a value of tens is centimetres of bounce
        # -- a reader who assumes cm is out by 10x. The bounds are any human
        # runner's, wide on purpose.
        if rd["vertical_oscillation_mm"]["value"] is None:
            self.skipTest("the subject's dynamics fall outside its rwd_run time")
        self.assertGreater(rd["vertical_oscillation_mm"]["value"], 20)
        self.assertLess(rd["vertical_oscillation_mm"]["value"], 200)
        self.assertGreater(rd["stance_time_ms"]["value"], 100)
        self.assertLess(rd["stance_time_ms"]["value"], 600)

    def test_a_run_without_them_is_unavailable_with_a_reason(self):
        from trail_factors.analysis.basic import running_dynamics

        aid = one(
            self.con,
            """
            SELECT a.activity_id FROM activities a
            WHERE a.source = 'garmin_watch' AND a.sport = 'running'
              AND a.coverage_fraction > 0
              AND NOT EXISTS (SELECT 1 FROM records r
                              WHERE r.activity_id = a.activity_id
                                AND r.vertical_oscillation IS NOT NULL)
            ORDER BY a.start_time_utc DESC LIMIT 1""",
            what="positioned watch run without running dynamics",
        )[0]
        rd = running_dynamics(self.con, aid)
        for key in ("vertical_oscillation_mm", "stance_time_ms"):
            self.assertIsNone(rd[key]["value"])
            self.assertEqual(rd[key]["status_hint"], "unavailable")
            self.assertIn("records no", rd[key]["reason"])

    def test_the_grade_table_carries_them_per_bin(self):
        from trail_factors.analysis.primitives import grade_binned_pace

        aid = one(
            self.con,
            """
            SELECT r.activity_id FROM records r
            JOIN activities a USING (activity_id)
            WHERE a.source = 'garmin_watch' AND a.sport = 'running'
              AND r.vertical_oscillation IS NOT NULL
              AND EXISTS (SELECT 1 FROM records_enriched e
                          WHERE e.activity_id = r.activity_id)
            GROUP BY 1 ORDER BY count(*) DESC, 1 LIMIT 1""",
            what="DEM-covered watch run with running dynamics",
        )[0]
        bins = grade_binned_pace(self.con, aid)
        if not bins["bins"]:
            self.skipTest("the subject produced no grade bins")
        with_rd = [b for b in bins["bins"] if b["vertical_oscillation_mm"] is not None]
        self.assertTrue(with_rd, "a run with dynamics should carry them per bin")
        self.assertEqual(bins["filters"]["running_dynamics_units"]["stance_time"], "ms")

    def test_a_grade_table_without_them_is_null_there_not_zero(self):
        from trail_factors.analysis.primitives import grade_binned_pace

        aid = one(
            self.con,
            """
            SELECT a.activity_id FROM activities a
            WHERE a.source = 'garmin_watch' AND a.sport = 'running'
              AND EXISTS (SELECT 1 FROM records_enriched e
                          WHERE e.activity_id = a.activity_id)
              AND NOT EXISTS (SELECT 1 FROM records r
                              WHERE r.activity_id = a.activity_id
                                AND r.vertical_oscillation IS NOT NULL)
            ORDER BY a.start_time_utc DESC LIMIT 1""",
            what="DEM-covered watch run without running dynamics",
        )[0]
        for b in grade_binned_pace(self.con, aid)["bins"]:
            self.assertIsNone(b["vertical_oscillation_mm"])
            self.assertIsNone(b["stance_time_ms"])


class TestTheCadenceFormulaHoldsOnTheNewDevice(_Files):
    """§9's formula was settled by PARITY on GPS-fed-watch max values. Parity
    can only discriminate on an ODD max, so on a file whose max is even the
    AVERAGE has to do it instead -- against Garmin's own summary, read from a
    cached listing when one is on disk."""

    def _oracle(self):
        import json
        import re

        from trail_factors.paths import RAW_DIR

        gid = re.search(r"(\d+)_ACTIVITY", self.BARO.name)
        listing = {}
        for f in sorted((RAW_DIR / "garmin_api").glob("activities_*.json")):
            listing.update({str(a["activityId"]): a for a in json.loads(f.read_text())})
        a = listing.get(gid.group(1)) if gid else None
        if not a or a.get("averageRunningCadenceInStepsPerMinute") is None:
            self.skipTest("no cached Garmin summary for the barometric file")
        return a

    def _candidates(self):
        recs = F.coalesce_records(F.read_messages(self.BARO)["record"])
        cad = [
            (r.get("cadence"), r.get("fractional_cadence"))
            for r in recs
            if r.get("cadence") is not None
        ]
        a = [(c + (f if f is not None else 0.0)) * 2 for c, f in cad]
        b = [c * 2 + (f if f is not None else 0.0) for c, f in cad]
        return a, b

    def test_the_average_settles_it(self):
        api = self._oracle()["averageRunningCadenceInStepsPerMinute"]
        a, b = self._candidates()
        mean_a = sum(a) / len(a)
        mean_b = sum(b) / len(b)
        self.assertLess(abs(mean_a - api), 0.05)
        self.assertLess(
            abs(mean_a - api),
            abs(mean_b - api),
            "the stored formula must fit Garmin's average better than the alternative",
        )

    def test_the_stored_formula_is_the_one_that_matched(self):
        from trail_factors.analysis.basic import CADENCE_STEPS_PER_STRIDE, _cadence_expr

        self.assertEqual(CADENCE_STEPS_PER_STRIDE, 2)
        self.assertIn("fractional_cadence", _cadence_expr())


class TestTheSplitPartitionCheck(CorpusTestCase):
    """A watch can write an `interval_active` split spanning the WHOLE RUN on
    top of the rwd_* partition, so a naive sum double-counts. The subject is a
    file that carries one."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.aid, _, path = _fit(
            cls.con,
            """EXISTS (SELECT 1 FROM splits s
            WHERE s.activity_id = a.activity_id
              AND s.split_type = 'interval_active')""",
            "watch run with an interval_active split",
        )
        cls.m = F.read_messages(path)

    def findings(self, **session):
        sess = dict(F.session_of(self.m), **session)
        return V.bronze_checks(F.coalesce_records(self.m["record"]), sess, self.m)

    def test_the_whole_run_split_raises_no_partition_finding(self):
        self.assertNotIn(
            "split_distance_consistency",
            [f.check for f in self.findings()],
            "a file with nothing wrong with its partition must not flag",
        )

    def test_the_check_still_fires_on_a_REAL_inconsistency(self):
        """THE POSITIVE TWIN. Excluding non-partition splits must not disable
        the check -- a partition that genuinely does not sum must still flag."""
        total = F.session_of(self.m).get("total_distance")
        if total is None:
            self.skipTest("the subject's session carries no total distance")
        found = self.findings(total_distance=total * 3 + 10_000.0)
        self.assertIn("split_distance_consistency", [f.check for f in found])

    def test_the_message_names_what_it_excluded(self):
        total = F.session_of(self.m).get("total_distance")
        if total is None:
            self.skipTest("the subject's session carries no total distance")
        msg = [
            f.message
            for f in self.findings(total_distance=total * 3 + 10_000.0)
            if f.check == "split_distance_consistency"
        ][0]
        self.assertIn("interval_active", msg)

    def test_a_fresh_read_agrees_with_what_ingest_stored(self):
        """The check set as a whole is live: whatever bronze finds on a fresh
        read of the file, ingest recorded too."""
        stored = {
            r[0]
            for r in self.con.execute(
                "SELECT check_name FROM validation_results WHERE activity_id = ?",
                [self.aid],
            ).fetchall()
        }
        self.assertLessEqual({f.check for f in self.findings()}, stored)


if __name__ == "__main__":
    unittest.main()
