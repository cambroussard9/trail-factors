"""A sync that rejects a file must say so (§15.4).

THE BUG. The sync job ran its steps as `fn()` and discarded every return
value, `ingest_all`'s included. So a file REJECTED during a sync was reported
nowhere: the job state said `done`, the new activity was simply absent from
the corpus, and the only trace was a `validation_results` row with
activity_id NULL that nothing read. Files accumulated that way unnoticed,
real runs among them.

SCOPED TO THIS SYNC. `ingest_all` walks every raw file, so its `rejected`
list carries the historical ones too. A sync that re-raised every old
rejection every time is a sync nobody reads; those live in `rejected-files`,
which persists on purpose.

Every negative has a positive twin.
"""

from __future__ import annotations

import unittest

from trail_factors.paths import REPO_ROOT

# SYNTHETIC Garmin file ids: one downloaded by this sync, one from an
# earlier one.
NEW_ID = "10000000002"
OLD_ID = "10000000003"


class TestSyncKeepsTheIngestResult(unittest.TestCase):
    """Structural, over the job body AND the rejection builder it calls: the
    work function runs in a thread inside a job, so this reads what it does
    rather than running a sync. sync_rejections is exercised directly in
    tests/test_mcp.py (TestSyncRejectionsNameTheNextStep)."""

    @classmethod
    def setUpClass(cls):
        src = (REPO_ROOT / "src" / "trail_factors" / "mcp_server.py").read_text()
        job = src[src.index("def work(progress):") :]
        job = job[: job.index("job_id = start(")]
        helper = src[src.index("REJECTED_FILES_CMD =") :]
        helper = helper[: helper.index("@mcp.tool()")]
        cls.body = job + helper

    def test_the_return_value_is_captured_not_discarded(self):
        self.assertIn('ingest_result = ran["outputs"].get("ingest")', self.body)
        self.assertNotRegex(
            self.body, r"\n\s+fn\(\)\n", "fn() with its result discarded is the bug"
        )

    def test_a_rejection_goes_in_problems_by_GARMIN_ID(self):
        self.assertIn('"problems": problems', self.body)
        self.assertIn('"garmin_id": gid', self.body)
        self.assertIn("did not ingest", self.body)

    def test_the_validation_MESSAGE_travels_with_it(self):
        """A rejection without its reason is unactionable."""
        self.assertIn('"message": str(msg)', self.body)

    def test_a_sync_with_problems_is_NOT_a_clean_done(self):
        self.assertIn('"clean": not problems', self.body)

    def test_n_ingested_is_reported(self):
        self.assertIn('"n_ingested":', self.body)
        self.assertIn('.get("ok", [])', self.body)

    def test_it_is_scoped_to_THIS_syncs_files(self):
        """No corpus-wide scan: `rejected-files` is the persistent view."""
        self.assertIn("if new_ids and gid not in new_ids", self.body)
        self.assertIn("rejected-files", self.body)

    def test_both_rejected_AND_failed_are_covered(self):
        self.assertIn('for kind in ("rejected", "failed")', self.body)


class TestTheFilterLogicItself(unittest.TestCase):
    """The scoping rule, exercised rather than read."""

    def _filter(self, ingest_result, new_ids):
        """The same expression the job body uses."""
        problems, rejected = [], []
        for kind in ("rejected", "failed"):
            for name, msg in (ingest_result or {}).get(kind, []):
                gid = str(name).split("_")[0]
                if new_ids and gid not in new_ids:
                    continue
                rejected.append({"garmin_id": gid, "kind": kind, "message": str(msg)})
                problems.append(f"{kind.upper()}: garmin {gid} did not ingest")
        return problems, rejected

    def test_a_rejection_in_this_sync_is_reported(self):
        res = {
            "ok": ["a.fit"],
            "rejected": [(f"{NEW_ID}_ACTIVITY.fit", "pace impossible")],
            "failed": [],
        }
        problems, rejected = self._filter(res, [NEW_ID])
        self.assertEqual(len(problems), 1)
        self.assertIn(NEW_ID, problems[0])
        self.assertEqual(rejected[0]["message"], "pace impossible")

    def test_an_OLD_rejection_is_not_re_raised(self):
        """THE TWIN: historical rejections must not resurface every sync."""
        res = {"ok": [], "rejected": [(f"{OLD_ID}_ACTIVITY.fit", "junk")], "failed": []}
        problems, _ = self._filter(res, [NEW_ID])
        self.assertEqual(problems, [])

    def test_a_clean_sync_has_no_problems(self):
        """THE TWIN for the whole feature."""
        problems, rejected = self._filter(
            {"ok": ["a.fit", "b.fit"], "rejected": [], "failed": []}, ["1"]
        )
        self.assertEqual(problems, [])
        self.assertEqual(rejected, [])

    def test_a_FAILED_file_is_reported_too(self):
        res = {
            "ok": [],
            "rejected": [],
            "failed": [(f"{NEW_ID}_ACTIVITY.fit", "TypeError: boom")],
        }
        problems, rejected = self._filter(res, [NEW_ID])
        self.assertEqual(len(problems), 1)
        self.assertIn("FAILED", problems[0])
        self.assertEqual(rejected[0]["kind"], "failed")


class TestIngestAllStillReturnsWhatSyncReads(unittest.TestCase):
    """The contract between the two. If ingest_all's keys change, sync goes
    quiet again rather than failing loudly -- so the keys are pinned."""

    def test_the_four_keys_exist(self):
        src = (REPO_ROOT / "src" / "trail_factors" / "cli" / "ingest.py").read_text()
        body = src[src.index("def ingest_all") :]
        self.assertIn(
            'return {"ok": ok, "rejected": rejected, '
            '"failed": failed, "warned": warned}',
            body,
        )


if __name__ == "__main__":
    unittest.main()
