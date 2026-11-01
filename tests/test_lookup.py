"""Account-history lookups (§10.1) — separate from the corpus, and kept so.

THE BOUNDARY IS THE POINT. These summaries exist to choose which files to
download; they are never ingested, never joined to a run, and never read by
anything that computes. Two tests below assert that structurally rather than
trusting a convention, because a convention is what the next module forgets.

Every negative has a positive twin.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from trail_factors import lookup as L
from trail_factors.paths import REPO_ROOT
from tests.isolation import git_ignores

SRC = REPO_ROOT / "src" / "trail_factors"


def _api_activity(**over) -> dict:
    """An API summary shaped like the real thing, INCLUDING the fields this
    module must drop. Every value is SYNTHETIC: an arbitrary id, a made-up
    effort, a point in the Alps and a placeholder device id."""
    return {
        "activityId": 10000000001,
        "activityName": "Morning Run",
        "startTimeLocal": "2020-01-04 08:00:00",
        "activityType": {"typeKey": "running", "typeId": 1},
        "distance": 5150.0, "duration": 1530.0,
        "maxHR": 178.0, "averageHR": 165.0,
        # THE ONES THAT MUST NOT SURVIVE.
        "startLatitude": 46.512345, "startLongitude": 7.498765,
        "hasPolyline": True, "deviceId": 4000000001,
        **over,
    }


class _Store:
    """Redirect the lookup file. data/lookup/ must never see a fixture."""

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        d = Path(self._tmp.name)
        self._p = [mock.patch.object(L, "HISTORY_FILE", d / "summaries.json"),
                   mock.patch.object(L, "LOOKUP_DIR", d)]
        for p in self._p:
            p.start()
        return d

    def __exit__(self, *exc):
        for p in self._p:
            p.stop()
        self._tmp.cleanup()
        return False


class TestTheProjectionDropsEverythingElse(unittest.TestCase):
    """NO COORDINATES, BY CONSTRUCTION. The API returns startLatitude and
    startLongitude on every activity; projecting at the boundary means the
    stored file cannot hold one even by accident."""

    def test_only_the_seven_fields_survive(self):
        row = L.project(_api_activity())
        self.assertEqual(set(row), set(L.SUMMARY_FIELDS))

    def test_no_coordinate_can_reach_the_file(self):
        row = L.project(_api_activity())
        blob = json.dumps(row).lower()
        for banned in ("latitude", "longitude", "polyline", "lat", "lon"):
            self.assertNotIn(banned, blob, banned)
        for v in row.values():
            # The fixture's start point is a coordinate at 6 dp. Nothing
            # numeric here may be in coordinate range at coordinate precision.
            if isinstance(v, float):
                self.assertFalse(-180.0 < v < 180.0 and abs(v) > 0.0001
                                 and v != int(v) and abs(v) < 200
                                 and str(v).count(".") and len(str(v).split(".")[1]) > 3,
                                 f"{v} looks like a coordinate")

    def test_the_fields_that_ARE_kept_are_kept(self):
        """THE TWIN: a projection that dropped everything would pass above."""
        row = L.project(_api_activity())
        self.assertEqual(row["activity_id"], "10000000001")
        self.assertEqual(row["activity_type"], "running")
        self.assertEqual(row["max_hr"], 178.0)
        self.assertEqual(row["avg_hr"], 165.0)
        self.assertEqual(row["distance_m"], 5150.0)
        self.assertEqual(row["duration_s"], 1530.0)
        self.assertEqual(row["start_local"], "2020-01-04 08:00:00")


class TestTheBoundaryIsStructural(unittest.TestCase):

    def test_nothing_that_computes_imports_the_lookup(self):
        """It is a lookup, not data. If analysis or ingest ever reads it, a
        single-sample maximum has entered the corpus's reasoning through the
        side door."""
        offenders = []
        for path in list((SRC / "analysis").rglob("*.py")) + \
                    list((SRC / "ingest").rglob("*.py")) + \
                    list((SRC / "transform").rglob("*.py")):
            body = path.read_text()
            if "trail_factors.lookup" in body or "from trail_factors import lookup" in body:
                offenders.append(str(path))
        self.assertEqual(offenders, [])

    def test_the_CLI_may_read_it(self):
        """THE TWIN: the boundary test must be capable of failing, and the CLI
        is where the lookup is legitimately used."""
        cli = (SRC / "cli" / "garmin.py").read_text()
        self.assertIn("trail_factors.lookup", cli)

    def test_it_lives_outside_the_corpus_directories(self):
        from trail_factors.paths import ATHLETE_DIR, DATA_DIR, LOOKUP_DIR, RAW_DIR
        self.assertEqual(LOOKUP_DIR.parent, DATA_DIR)
        self.assertNotEqual(LOOKUP_DIR, RAW_DIR)
        self.assertNotEqual(LOOKUP_DIR, ATHLETE_DIR)
        self.assertNotIn(str(RAW_DIR), str(LOOKUP_DIR))

    def test_it_is_gitignored(self):
        """Regenerable from Garmin, so git is not its backup. Asked of git
        itself: `data/` ignores the lookup without naming it."""
        self.assertTrue(git_ignores("data/lookup/summaries.json"))
        # THE TWIN: the check can fail -- a source file is not ignored.
        self.assertFalse(git_ignores("src/trail_factors/lookup.py"))


class TestTheSweep(unittest.TestCase):

    def _api(self, pages):
        api = mock.MagicMock()
        api.count_activities.return_value = sum(len(p) for p in pages)
        api.get_activities.side_effect = list(pages) + [[]]
        return api

    def test_it_never_downloads_a_file(self):
        """LIST CALLS ONLY. The download endpoint is the heavy one and the one
        that draws throttling."""
        with _Store():
            api = self._api([[_api_activity()]])
            L.sweep(api, page_size=200, progress=lambda *_: None)
        api.download_activity.assert_not_called()

    def test_it_stores_the_projection_not_the_response(self):
        with _Store():
            L.sweep(self._api([[_api_activity()]]), progress=lambda *_: None)
            stored = L.load_history()
        row = stored["10000000001"]
        self.assertEqual(set(row), set(L.SUMMARY_FIELDS))
        self.assertNotIn("startLatitude", json.dumps(stored))

    def test_a_second_sweep_MERGES_rather_than_replacing(self):
        with _Store():
            L.sweep(self._api([[_api_activity()]]), progress=lambda *_: None)
            L.sweep(self._api([[_api_activity(activityId=999, maxHR=150.0)]]),
                    progress=lambda *_: None)
            stored = L.load_history()
        self.assertIn("10000000001", stored)
        self.assertIn("999", stored)

    def test_a_rate_limit_keeps_what_was_fetched(self):
        """Never retry in a loop: repeated attempts extend Garmin's limit."""
        from garminconnect import GarminConnectTooManyRequestsError
        with _Store():
            api = mock.MagicMock()
            api.count_activities.return_value = 400
            api.get_activities.side_effect = [
                [_api_activity()],
                GarminConnectTooManyRequestsError("429"),
            ]
            with mock.patch("trail_factors.ingest.garmin_api.record_failure"):
                L.sweep(api, page_size=1, progress=lambda *_: None)
            stored = L.load_history()
        self.assertIn("10000000001", stored)
        self.assertEqual(api.get_activities.call_count, 2)

    def test_the_file_says_what_it_is(self):
        with _Store():
            L.sweep(self._api([[_api_activity()]]), progress=lambda *_: None)
            meta = L.load_history()["_meta"]
        self.assertIn("NEVER ingested", meta["what_this_is"])
        self.assertIn("spike candidate", meta["what_this_is"])

    def test_the_write_is_atomic(self):
        src = (SRC / "lookup.py").read_text()
        self.assertIn("os.replace", src)
        self.assertNotIn("HISTORY_FILE.write_text", src)


class TestTheCeilingReport(unittest.TestCase):

    # SYNTHETIC: made-up dates and efforts, shaped to exercise the rules
    # (one isolated top, a 5 km band of three ranked by pace, a long run
    # outside the band, and a swim with no HR).
    ROWS = {
        "a": {"activity_id": "a", "start_local": "2020-01-04 08:00:00",
              "activity_type": "running", "duration_s": 1530.0,
              "distance_m": 5150.0, "max_hr": 178.0, "avg_hr": 165.0},
        "b": {"activity_id": "b", "start_local": "2020-02-08 09:30:00",
              "activity_type": "running", "duration_s": 3800.0,
              "distance_m": 9000.0, "max_hr": 185.0, "avg_hr": 162.0},
        "c": {"activity_id": "c", "start_local": "2020-03-07 07:00:00",
              "activity_type": "running", "duration_s": 1450.0,
              "distance_m": 5000.0, "max_hr": 176.0, "avg_hr": 160.0},
        "d": {"activity_id": "d", "start_local": "2020-04-04 07:00:00",
              "activity_type": "lap_swimming", "duration_s": 900.0,
              "distance_m": 380.0, "max_hr": None, "avg_hr": None},
        "e": {"activity_id": "e", "start_local": "2020-05-02 07:00:00",
              "activity_type": "running", "duration_s": 1800.0,
              "distance_m": 5100.0, "max_hr": 150.0, "avg_hr": 140.0},
    }

    def report(self, **kw):
        return L.ceiling_report(dict(self.ROWS), **kw)

    def test_the_highest_carries_its_date_and_activity(self):
        h = self.report()["highest_max_hr"]
        self.assertEqual(h["max_hr"], 185.0)
        self.assertEqual(h["date"], "2020-02-08")
        self.assertEqual(h["activity_id"], "b")

    def test_it_says_whether_the_peak_is_already_held(self):
        self.assertTrue(self.report(corpus_ids={"b"})["highest_max_hr"]["in_corpus"])
        self.assertFalse(self.report(corpus_ids=set())["highest_max_hr"]["in_corpus"])

    def test_every_figure_is_labelled_a_single_sample(self):
        r = self.report()
        self.assertIn("SINGLE-SAMPLE", r["figures_are"])
        self.assertIn("cadence lock", r["figures_are"])
        self.assertIn("nothing here is ingested", r["is_not"])

    def test_the_distribution_says_whether_the_top_is_ISOLATED(self):
        """An isolated high reading and a repeated one are different facts, and
        the whole reason to look at a distribution rather than a maximum."""
        d = self.report()["distribution"]
        self.assertEqual(d["n_within_5_of_max"], 1)
        self.assertTrue(d["top_is_isolated"])

    def test_a_CLUSTERED_top_is_reported_as_clustered(self):
        """THE TWIN."""
        rows = dict(self.ROWS)
        rows["f"] = {**self.ROWS["c"], "activity_id": "f", "max_hr": 183.0}
        d = L.ceiling_report(rows)["distribution"]
        self.assertEqual(d["n_within_5_of_max"], 2)
        self.assertFalse(d["top_is_isolated"])

    def test_the_5k_band_excludes_everything_outside_it(self):
        band = self.report()["five_k_band"]
        ids = {f["activity_id"] for f in band["fastest"]}
        self.assertEqual(ids, {"a", "c", "e"})
        self.assertNotIn("b", ids)          # 9 km
        self.assertNotIn("d", ids)          # a swim, and 380 m

    def test_the_shortlist_is_ranked_by_PACE_not_by_maxHR(self):
        """Ranking by maxHR would sort by the figure that cannot be trusted
        yet. A maximal effort is where a real ceiling shows, so rank by pace."""
        fastest = self.report()["five_k_band"]["fastest"]
        self.assertEqual([f["activity_id"] for f in fastest], ["c", "a", "e"])
        self.assertLess(fastest[0]["max_hr"], fastest[1]["max_hr"])

    def test_each_candidate_carries_the_command_to_fetch_it(self):
        f = self.report()["five_k_band"]["fastest"][0]
        self.assertIn("garmin-download", f["download"])
        self.assertIn(f["activity_id"], f["download"])

    def test_activities_without_HR_are_excluded_not_zeroed(self):
        r = self.report()
        self.assertEqual(r["n_activities_with_hr"], 4)
        self.assertEqual(r["n_activities_total"], 5)
        self.assertNotIn("d", {t["activity_type"] for t in r["by_activity_type"]})

    def test_an_empty_store_refuses_rather_than_reporting_zero(self):
        r = L.ceiling_report({})
        self.assertEqual(r["status"], "unavailable")
        self.assertIn("garmin-list history", r["reason"])


if __name__ == "__main__":
    unittest.main()
