"""Every remedy string must name something that EXISTS, and say it truthfully.

THE BUGS THIS EXISTS FOR, both found in an athlete-state writer (since
removed; the rules outlive it and apply to every writer that remains).

1. POSITION. An already-stored refusal said "Pass supersede=true" and never
   said WHERE. `supersede` is an ARGUMENT beside the payload; a chat
   assembling JSON put it inside the payload, the validator answered "unknown
   field(s): supersede", and the correction was impossible across three
   attempts with nothing written. Every named thing existed -- the remedy was
   ambiguous about where, which no existence check catches.

2. RETRYABILITY. The same rejection carried `retryable: false` while its own
   remedy said to call again. `retryable` defaults to False, so it was an
   omission rather than a decision, and a caller reading the field would stop.

A remedy is an INSTRUCTION TO A MACHINE that cannot inspect the interface. If
it names a flag that does not exist, or a flag that exists somewhere other than
where it implies, the caller has no way to find out except by failing.

Every negative has a positive twin.
"""

from __future__ import annotations

import ast
import inspect
import re
import unittest
from pathlib import Path

import trail_factors.mcp_server as M
from trail_factors.paths import REPO_ROOT

SRC = REPO_ROOT / "src" / "trail_factors"
REMEDY_RE = re.compile(r'(?:remedy=|"remedy":\s*|"fix":\s*)((?:f?"[^"]*"\s*)+)')


def _remedy_strings(path: Path) -> list[str]:
    """Every remedy/fix string literal in a module, concatenated."""
    out = []
    for m in REMEDY_RE.finditer(path.read_text()):
        parts = re.findall(r'f?"([^"]*)"', m.group(1))
        out.append("".join(parts))
    return [s for s in out if s.strip()]


def _mcp_tools() -> set[str]:
    return {n for n, v in vars(M).items()
            if inspect.isfunction(v) and not n.startswith("_")}


def _mcp_params() -> set[str]:
    params = set()
    for n in _mcp_tools():
        try:
            params |= set(inspect.signature(getattr(M, n)).parameters)
        except (TypeError, ValueError):
            pass
    return params


def _cli_commands() -> set[str]:
    body = (SRC / "cli" / "main.py").read_text()
    return set(re.findall(r'args\[0\] == "([a-z-]+)"', body)) | \
           set(re.findall(r'"([a-z-]+)"', body.split("DB_COMMANDS")[1].split("}")[0]))


class TestEveryNamedThingExists(unittest.TestCase):

    def setUp(self):
        self.remedies = []
        for path in sorted(SRC.rglob("*.py")):
            for s in _remedy_strings(path):
                self.remedies.append((path.relative_to(REPO_ROOT), s))

    def test_the_sweep_actually_found_remedies(self):
        """A sweep over nothing passes everything."""
        self.assertGreaterEqual(len(self.remedies), 10, self.remedies)

    def test_every_named_FLAG_is_a_real_parameter(self):
        """`foo=true` in a remedy must be a parameter of some tool."""
        params, problems = _mcp_params(), []
        for path, s in self.remedies:
            for flag in re.findall(r'\b([a-z_]{3,})=(?:true|True|<)', s):
                if flag not in params:
                    problems.append(f"{path}: remedy names {flag}= which is "
                                    f"not a parameter of any MCP tool")
        self.assertEqual(problems, [])

    def test_every_named_CALLABLE_exists(self):
        """`foo()` in a remedy must be a tool or a real function."""
        tools, problems = _mcp_tools(), []
        for path, s in self.remedies:
            # (?<!\.) -- a METHOD call inside an f-string, e.g.
            # `{d.isoformat()}`, names no interface and is not a claim.
            for name in re.findall(r'(?<!\.)\b([a-z_]{4,})\(\)', s):
                if name not in tools and not any(
                        (SRC / p).exists() and f"def {name}(" in (SRC / p).read_text()
                        for p in ("mcp_server.py", "analysis/queries.py")):
                    problems.append(f"{path}: remedy names {name}() which "
                                    f"does not exist")
        self.assertEqual(problems, [])

    def test_every_named_TOOL_exists(self):
        """A bare tool name, e.g. "check the run reference with list_runs"."""
        tools, problems = _mcp_tools(), []
        for path, s in self.remedies:
            for name in re.findall(r'\bwith ([a-z_]{4,})\b', s):
                if "_" in name and name not in tools:
                    problems.append(f"{path}: remedy names tool {name!r} "
                                    f"which is not an MCP tool")
        self.assertEqual(problems, [])

    def test_every_named_CLI_COMMAND_dispatches(self):
        cmds, problems = _cli_commands(), []
        for path, s in self.remedies:
            for cmd in re.findall(r'uv run trail-factors ([a-z-]+)', s):
                if cmd not in cmds:
                    problems.append(f"{path}: remedy names CLI command "
                                    f"{cmd!r} which does not dispatch")
        self.assertEqual(problems, [])

    def test_the_sweep_CAN_fail(self):
        """THE POSITIVE TWIN. A checker that passes everything is worth
        nothing, so reconstruct the exact bug shape and require a catch."""
        fake = "call again with definitely_not_a_parameter=true"
        found = re.findall(r'\b([a-z_]{3,})=(?:true|True|<)', fake)
        self.assertIn("definitely_not_a_parameter", found)
        self.assertNotIn("definitely_not_a_parameter", _mcp_params())


class TestARemedyThatSaysCallAgainIsRetryable(unittest.TestCase):
    """THE SECOND BUG, GENERALISED. A refusal said 'call again with
    supersede=true' under retryable: false. The field and the sentence
    contradicted each other, and the field is the machine-readable one."""

    RETRY_WORDS = ("call again", "resend", "ask again", "try again")

    def _handlers(self):
        """Each `except X:` block in _call, with its remedy and retryable."""
        src = (SRC / "mcp_server.py").read_text()
        tree = ast.parse(src)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "_call")
        out = []
        for h in ast.walk(fn):
            if not isinstance(h, ast.ExceptHandler):
                continue
            seg = ast.get_source_segment(src, h) or ""
            name = getattr(h.type, "id", None) or getattr(
                getattr(h.type, "attr", None), "__str__", lambda: "?")()
            rem = " ".join(re.findall(r'f?"([^"]*)"', seg))
            retryable = "retryable=True" in seg
            out.append((name, rem, retryable, seg))
        return out

    def test_every_call_again_remedy_is_marked_retryable(self):
        problems = []
        for name, rem, retryable, _ in self._handlers():
            says = [w for w in self.RETRY_WORDS if w in rem.lower()]
            if says and not retryable:
                problems.append(
                    f"except {name}: remedy says {says} but retryable is not "
                    f"True -- the sentence and the field disagree")
        self.assertEqual(problems, [], problems)

    def test_the_sweep_sees_a_call_again_remedy(self):
        """THE PRESENCE TWIN: the rule above is vacuous unless some handler
        actually says to call again. The lock wait does, and must be marked."""
        by_name = {n: (rem, retry) for n, rem, retry, _ in self._handlers()}
        self.assertIn("DatabaseBusy", by_name)
        rem, retry = by_name["DatabaseBusy"]
        self.assertIn("call again", rem.lower())
        self.assertTrue(retry)

    def test_a_corrigible_rejection_is_retryable(self):
        """RETRYABLE_MEANS: "is there a next call that can succeed?" Omitting
        run_type, or fixing the listed ride-block entries, is such a call --
        and nothing was written, so it is safe to make."""
        by_name = {n: (rem, retry) for n, rem, retry, _ in self._handlers()}
        for name, says in (("FilterUnavailable", "omit run_type"),
                           ("RideBlockError", "resend")):
            self.assertIn(name, by_name)
            rem, retry = by_name[name]
            self.assertTrue(retry, f"{name} must be retryable")
            self.assertIn(says, rem)

    def test_a_genuinely_non_retryable_rejection_stays_non_retryable(self):
        """THE TWIN: not everything is retryable, and marking everything so
        would be the same failure in the other direction."""
        by_name = {n: r for n, _, r, _ in self._handlers()}
        self.assertFalse(by_name.get("ReadBackFailed", True),
                         "a read-back failure must NOT invite a resend -- the "
                         "entry is already in the file")


class TestASupersedeRemedySaysWHERETheFlagGoes(unittest.TestCase):
    """The reported bug was pure position: the flag existed, the message did
    not say it was an argument, and the caller put it in the payload."""

    def _supersede_strings(self):
        """Caller-facing text ONLY: remedy strings and raised messages.

        NOT comments and NOT docstrings. The first version of this scanned
        every line and flagged a comment that QUOTED the old wording plus a
        docstring sentence describing behaviour -- neither is an instruction a
        caller receives, and a checker that cries about comments is one that
        gets deleted. The surface that matters is what comes back from a
        refused call.
        """
        out = []
        for path in sorted(SRC.rglob("*.py")):
            body = path.read_text()
            texts = [(0, s) for s in _remedy_strings(path)]
            try:
                tree = ast.parse(body)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
                    seg = ast.get_source_segment(body, node) or ""
                    texts.append((node.lineno,
                                  " ".join(re.findall(r'f?"([^"]*)"', seg))))
            for line_no, s in texts:
                if re.search(r'supersede=[tT]rue', s) and "wrong place" not in s:
                    out.append((path.relative_to(REPO_ROOT), line_no, s))
        return out

    def test_every_instruction_to_pass_supersede_names_its_position(self):
        problems = []
        for path, line_no, ctx in self._supersede_strings():
            if "MISPLACED_FIELDS" in ctx and "wrong place" in ctx:
                continue
            if not re.search(r'ARGUMENT|argument|BESIDE|beside', ctx):
                problems.append(f"{path}:{line_no} tells a caller to pass "
                                f"supersede without saying WHERE")
        self.assertEqual(problems, [], problems)

    def test_there_ARE_such_instructions_to_check(self):
        """THE TWIN. One writer remains, so one refusal tells a caller to pass
        supersede -- the ride-block store's."""
        found = self._supersede_strings()
        self.assertGreaterEqual(len(found), 1)
        self.assertIn("src/trail_factors/ingest/ride_blocks.py",
                      {str(path) for path, _, _ in found})

    def test_the_position_check_CAN_fail(self):
        """A supersede instruction with no position must be caught."""
        bare = "already stored. Pass supersede=true to replace it."
        self.assertTrue(re.search(r'supersede=[tT]rue', bare))
        self.assertIsNone(re.search(r'ARGUMENT|argument|BESIDE|beside', bare))


class TestARejectedFileNamesARealNextStep(unittest.TestCase):
    """A rejected file's next step is an instruction to a person at a
    terminal, or to a chat relaying it. Every CLI command it names must
    dispatch, or the athlete's only route back to the run is a dead end."""

    def _texts(self) -> list[str]:
        from trail_factors.cli import athlete as A
        out = [M.REJECTED_FILES_CMD, M.CONFIRM_IS_CLI_ONLY,
               *A.NO_WINDOW_STEP.values()]
        # A window CAN fix a vehicle tail and CANNOT fix corrupt timestamps:
        # one advice of each kind. The file does not exist, so the window
        # suggestion fails -- and must not cost the advice.
        fake = A.RAW_GARMIN / "10000000001_ACTIVITY.fit"
        for checks in (["pace_sustained_impossible"], ["timestamp_integrity"]):
            adv = A.rejection_advice(fake, "synthetic", checks)
            self.assertIsNotNone(adv["suggest_error"])
            out += [v for v in (adv["next_step"], adv["command"], adv["then"],
                                adv["list_command"]) if v]
        return out

    def _named(self) -> set[str]:
        named = set()
        for s in self._texts():
            named |= set(re.findall(r'uv run trail-factors ([a-z-]+)', s))
            named |= set(re.findall(r'`([a-z]+-[a-z-]+)\b', s))
        return named

    def test_every_named_command_dispatches(self):
        missing = self._named() - _cli_commands()
        self.assertEqual(missing, set(), f"next steps name {missing}")

    def test_the_loop_commands_are_all_named(self):
        """THE PRESENCE TWIN: confirm, re-sync, and list what is outstanding."""
        self.assertLessEqual({"confirm-file", "sync", "rejected-files"},
                             self._named())

    def test_every_rejection_reason_has_words(self):
        from trail_factors.cli import athlete as A
        for check, reason in A.REJECTION_REASONS.items():
            self.assertTrue(reason.strip(), check)


if __name__ == "__main__":
    unittest.main()
