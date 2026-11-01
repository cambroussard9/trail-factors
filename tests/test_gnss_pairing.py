"""A mid-run GNSS loss leaves NULLs, never a stale reading -- and the
altitude-provenance comparison ignores those records.

THE PREMISE TESTED HERE WAS FALSE. It was claimed that records
are paired with the last gps_metadata frame before them, so a GNSS loss would
leave records carrying an old reading. The pairing CLEARS the pending frame
once a record takes it, so a record gets a reading only if a frame was
written since the previous record. This pins that, on a synthetic frame
stream (fit_file.pair_frames is the same function read_messages uses).

THE FIXTURE: 1 Hz records for 200 s, a gps_metadata frame before every
record EXCEPT for t in [60, 120) -- a 60 s mid-run GNSS gap.

THE RESIDUAL CASE (a recorded project decision): a RECORDING gap, where
the watch writes no record but GNSS frames keep arriving. The record after it
pairs with the newest frame from inside the gap, whose age is unknowable --
so over 60 s the provenance comparison treats it as MISSING.
"""

import datetime as dt
import unittest

from trail_factors.ingest import fit_file as ff

T0 = dt.datetime(2023, 6, 20, 7, 0, 0)       # synthetic
GAP = range(60, 120)


def frames(gap=GAP, stale_device=False):
    out = []
    for t in range(200):
        if t not in gap:
            out.append(("gps_metadata", {"enhanced_altitude": 1500.0 + 0.1 * t}))
        elif stale_device:
            # A device that KEEPS WRITING frames repeating its last fix: the
            # pairing cannot see that, and says so in the docs, not here.
            out.append(("gps_metadata", {"enhanced_altitude": 1505.9}))
        out.append(("record", {"timestamp": T0 + dt.timedelta(seconds=t),
                               # barometric channel, smoother and offset
                               "enhanced_altitude": 1501.0 + 0.1 * t}))
    return out


class TestAGnssGapLeavesNulls(unittest.TestCase):
    def setUp(self):
        self.recs = ff.pair_frames(frames())["record"]

    def test_exactly_the_records_inside_the_gap_have_no_gnss(self):
        none = [i for i, r in enumerate(self.recs)
                if r[ff.GNSS_ALTITUDE_KEY] is None]
        self.assertEqual(none, list(GAP))

    def test_no_record_inside_the_gap_carries_an_old_reading(self):
        last_before = self.recs[GAP.start - 1][ff.GNSS_ALTITUDE_KEY]
        self.assertTrue(all(self.recs[i][ff.GNSS_ALTITUDE_KEY] != last_before
                            for i in GAP))

    def test_positive_twin_records_outside_the_gap_are_paired_exactly(self):
        for i in (0, GAP.start - 1, GAP.stop, 199):
            self.assertAlmostEqual(self.recs[i][ff.GNSS_ALTITUDE_KEY],
                                   1500.0 + 0.1 * i)

    def test_the_provenance_comparison_ignores_them(self):
        prov = ff.altitude_provenance(self.recs)
        self.assertEqual(prov["n_pairs"], 200 - len(GAP))
        whole = ff.altitude_provenance(ff.pair_frames(frames(gap=()))["record"])
        self.assertEqual(whole["n_pairs"], 200)


def recording_gap_frames(gap_s):
    """1 Hz records for t < 60 and from 60 + gap_s for another 100 s; GNSS
    frames every second THROUGHOUT, including inside the recording gap."""
    out = []
    times = list(range(60)) + list(range(60 + gap_s, 160 + gap_s))
    for t in range(160 + gap_s):
        out.append(("gps_metadata", {"enhanced_altitude": 1500.0 + 0.1 * t}))
        if t in times:
            out.append(("record", {"timestamp": T0 + dt.timedelta(seconds=t),
                                   "enhanced_altitude": 1501.0 + 0.1 * t}))
    return out


class TestARecordingGapMakesTheNextReadingMissing(unittest.TestCase):
    def test_the_record_after_a_long_recording_gap_is_excluded(self):
        recs = ff.pair_frames(recording_gap_frames(90))["record"]
        # The pairing still STORES it: it is what the file says.
        self.assertIsNotNone(recs[60][ff.GNSS_ALTITUDE_KEY])
        prov = ff.altitude_provenance(recs)
        self.assertEqual(prov["n_excluded_after_recording_gap"], 1)
        self.assertEqual(prov["n_pairs"], len(recs) - 1)
        self.assertEqual(prov["recording_gap_threshold_s"], 60.0)

    def test_twin_a_gap_under_the_threshold_excludes_nothing(self):
        recs = ff.pair_frames(recording_gap_frames(30))["record"]
        prov = ff.altitude_provenance(recs)
        self.assertEqual(prov["n_excluded_after_recording_gap"], 0)
        self.assertEqual(prov["n_pairs"], len(recs))

    def test_the_threshold_is_in_the_digest(self):
        from trail_factors.analysis.versions import calc_params
        self.assertEqual(calc_params()["gnss_unknown_age_after_gap_s"],
                         ff.GNSS_UNKNOWN_AGE_AFTER_GAP_S)


if __name__ == "__main__":
    unittest.main()
