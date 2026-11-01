"""Garmin credential files live OUTSIDE the repo, and the password is never ours.

No network: `Garmin` is replaced by a stub that fails the test if it is ever
constructed on a path that should have been refused.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from trail_factors.ingest import garmin_api as G
from trail_factors.paths import REPO_ROOT


def _no_garmin(*a, **k):
    raise AssertionError("Garmin was contacted on a refused path")


class TestCredentialPathsInsideTheRepoAreRefused(unittest.TestCase):
    def setUp(self):
        # Paths under the repo that do not exist: nothing may create them.
        self.tok = REPO_ROOT / "config" / "garmin-tokens-must-not-exist"
        self.auth = REPO_ROOT / "config" / "auth-state-must-not-exist.json"

    def _patched(self, tokenstore, auth_state):
        return [mock.patch.object(G, "TOKENSTORE", str(tokenstore)),
                mock.patch.object(G, "AUTH_STATE", Path(auth_state)),
                mock.patch.object(G, "Garmin", _no_garmin)]

    def _run(self, tokenstore, auth_state, fn):
        patches = self._patched(tokenstore, auth_state)
        for p in patches:
            p.start()
        try:
            with self.assertRaises(G.CredentialPathError) as cm:
                fn()
        finally:
            for p in patches:
                p.stop()
        msg = str(cm.exception)
        self.assertIn("Nothing was sent", msg)
        self.assertNotIn(str(Path.home()), msg)
        self.assertFalse(self.tok.exists())
        self.assertFalse(self.auth.exists())
        return msg

    def test_a_tokenstore_inside_the_repo_is_refused_everywhere(self):
        with tempfile.TemporaryDirectory() as d:
            auth = Path(d) / "auth.json"
            for fn in (G.client, G.check_backoff, lambda: G.record_failure("x"),
                       lambda: G.interactive_login("a@example.com", "pw")):
                msg = self._run(self.tok, auth, fn)
                self.assertIn("TRAIL_FACTORS_GARMIN_TOKENSTORE", msg)
            self.assertFalse(auth.exists())

    def test_an_auth_state_inside_the_repo_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            msg = self._run(Path(d) / "tokens", self.auth,
                            lambda: G.record_failure("x"))
        self.assertIn("TRAIL_FACTORS_GARMIN_AUTH_STATE", msg)


class TestOutsideTheRepoTheBackoffWorks(unittest.TestCase):
    """The positive twin: a refusal test alone would pass against a module
    that refused everything."""

    def test_record_check_clear_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            auth = Path(d) / "nested" / "auth.json"     # parent does not exist
            with mock.patch.object(G, "TOKENSTORE", str(Path(d) / "tokens")), \
                    mock.patch.object(G, "AUTH_STATE", auth):
                G.check_backoff()                        # nothing recorded yet
                G.record_failure("429 rate limited")
                self.assertEqual(json.loads(auth.read_text())["error"],
                                 "429 rate limited")
                self.assertEqual([p.name for p in auth.parent.iterdir()],
                                 ["auth.json"])          # no temp file left
                with self.assertRaises(G.AuthBackoff) as cm:
                    G.check_backoff()
                self.assertNotIn(str(Path.home()), str(cm.exception))
                G.clear_failure()
                G.check_backoff()
                self.assertFalse(auth.exists())

    def test_empty_credentials_are_refused_locally(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(G, "TOKENSTORE", str(Path(d) / "tokens")), \
                mock.patch.object(G, "AUTH_STATE", Path(d) / "auth.json"), \
                mock.patch.object(G, "Garmin", _no_garmin):
            for email, pw in (("", "pw"), ("a@example.com", ""), ("a@example.com", "  ")):
                with self.assertRaises(SystemExit):
                    G.interactive_login(email, pw)
            self.assertFalse((Path(d) / "auth.json").exists())

    def test_the_password_is_never_written(self):
        """A failed login records the error -- never the password."""
        class Boom:
            def __init__(self, **kw):
                pass

            def login(self, tokenstore=None):
                raise RuntimeError("bad credentials")

        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(G, "TOKENSTORE", str(Path(d) / "tokens")), \
                mock.patch.object(G, "AUTH_STATE", Path(d) / "auth.json"), \
                mock.patch.object(G, "Garmin", Boom):
            with self.assertRaises(RuntimeError):
                G.interactive_login("a@example.com", "s3cret-pw-123")
            written = "".join(p.read_text() for p in Path(d).rglob("*")
                              if p.is_file())
        self.assertIn("bad credentials", written)
        self.assertNotIn("s3cret-pw-123", written)


if __name__ == "__main__":
    unittest.main()
