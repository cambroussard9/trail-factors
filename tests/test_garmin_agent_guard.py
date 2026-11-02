"""Coding agents never contact Garmin through the CLI (cli/main.garmin_refusal).

No test here can reach Garmin: the guard is exercised as a function, and the
one test that goes through main() replaces every Garmin-reaching command and
the Garmin client with stubs that fail the test if they are ever called.
"""

import importlib
import os
import re
import sys
import unittest
from unittest import mock

from trail_factors.paths import REPO_ROOT

# The module, not the `main` function that trail_factors.cli re-exports.
CLI = importlib.import_module("trail_factors.cli.main")

AGENT = {"CLAUDECODE": "1"}
NO_MARKERS = {m: "" for m in CLI.AGENT_ENV_MARKERS}

CASES = [
    ["sync"],
    ["garmin-login"],
    ["garmin-download", "10000000001"],
    ["garmin-download-all"],
    ["garmin-list", "history"],
    ["coverage-report"],
    ["validate", "--refresh"],
]


class TestAnAgentSessionIsRefused(unittest.TestCase):
    def test_every_garmin_command_is_refused_with_the_instruction(self):
        with mock.patch.dict(os.environ, {**NO_MARKERS, **AGENT}):
            for args in CASES:
                msg = CLI.garmin_refusal(args)
                self.assertIsNotNone(msg, args)
                self.assertIn("Nothing was sent", msg)
                self.assertIn("ask the user", msg)
                self.assertIn("CLAUDECODE", msg)

    def test_the_other_marker_counts_too(self):
        with mock.patch.dict(os.environ, {**NO_MARKERS, "AI_AGENT": "claude"}):
            self.assertIsNotNone(CLI.garmin_refusal(["sync"]))

    def test_main_refuses_before_dispatching_anything(self):
        def boom(*a, **k):
            raise AssertionError("a Garmin path was reached")

        patches = [
            mock.patch.object(CLI, name, boom)
            for name in (
                "sync",
                "garmin_login",
                "garmin_download",
                "garmin_download_all",
                "garmin_history",
                "coverage_report",
                "validate_cmd",
            )
        ] + [mock.patch("trail_factors.ingest.garmin_api.Garmin", boom)]
        for p in patches:
            p.start()
        try:
            with mock.patch.dict(os.environ, {**NO_MARKERS, **AGENT}):
                for args in CASES:
                    with mock.patch.object(sys, "argv", ["trail-factors", *args]):
                        with self.assertRaises(SystemExit) as cm:
                            CLI.main()
                    self.assertIn("Refusing", str(cm.exception), args)
        finally:
            for p in patches:
                p.stop()


class TestEverythingElseIsUntouched(unittest.TestCase):
    """The positive twins: a guard that refused everything would pass the
    tests above."""

    def test_a_human_terminal_is_not_refused(self):
        with mock.patch.dict(os.environ, NO_MARKERS):
            for args in CASES:
                self.assertIsNone(CLI.garmin_refusal(args), args)

    def test_offline_commands_run_in_an_agent_session(self):
        with mock.patch.dict(os.environ, {**NO_MARKERS, **AGENT}):
            for args in (
                ["demo"],
                ["report"],
                ["validate"],
                ["ingest-all"],
                ["confirm-file", "x", "--end", "t"],
                ["rejected-files"],
                [],
            ):
                self.assertIsNone(CLI.garmin_refusal(args), args)


class TestTheListIsComplete(unittest.TestCase):
    """A new CLI function that logs in to Garmin must be mapped to a refused
    command, or this fails: the guard is only as good as its list."""

    CALLERS = {
        "garmin_login": "garmin-login",
        "garmin_history": "garmin-list",
        "garmin_download": "garmin-download",
        "garmin_download_all": "garmin-download-all",
        "coverage_report": "coverage-report",
        "validate_cmd": "validate --refresh",
    }

    def test_every_function_that_reaches_garmin_is_known(self):
        found = set()
        for path in (REPO_ROOT / "src" / "trail_factors" / "cli").glob("*.py"):
            fn = None
            for line in path.read_text().splitlines():
                m = re.match(r"def (\w+)\(", line)
                if m:
                    fn = m.group(1)
                if fn and re.search(r"\b(client|interactive_login)\(", line):
                    found.add(fn)
        self.assertEqual(found, set(self.CALLERS), found)
        for cmd in self.CALLERS.values():
            name = cmd.split()[0]
            self.assertTrue(name in CLI.GARMIN_COMMANDS or name == "validate", cmd)
        # sync reaches Garmin through garmin_download_all.
        self.assertIn("sync", CLI.GARMIN_COMMANDS)


if __name__ == "__main__":
    unittest.main()
