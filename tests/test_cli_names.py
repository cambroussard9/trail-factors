"""Every global name a CLI module uses must actually resolve.

THE BUG THIS EXISTS FOR. `sync` crashed with a `NameError` on a helper it
never imported, in the middle of a real sync — after downloading and ingesting a new
activity,
in the block that only runs WHEN THERE ARE NEW ACTIVITIES. Nothing caught it:
the module imports fine, every test passed, and the path is exercised only on
the days it matters.

That is the "check that cannot fire" family (§4.2) from the other side — a code
path with no test rather than a test with no teeth. The module-level fix is a
one-line import; the durable fix is this, which reads every name each CLI
module loads at global scope and asserts it is defined somewhere it could come
from.

WHY AST AND NOT A SMOKE RUN. Running `sync` in a test would hit the network,
the database and the Garmin rate limiter. This needs none of them: an
undefined global is a static property of the module.
"""

from __future__ import annotations

import ast
import builtins
import importlib
import unittest

from trail_factors.paths import REPO_ROOT

CLI_DIR = REPO_ROOT / "src" / "trail_factors" / "cli"


def _global_loads(tree: ast.AST) -> set[str]:
    """Names read at global scope or inside a function, minus locals.

    Deliberately conservative: anything bound ANYWHERE in the function -- a
    parameter, an assignment, a comprehension target, an import, an except
    alias, or a binding in a NESTED function -- is treated as local.

    THE NESTED CASE IS WHY THIS WALKS TOP-LEVEL FUNCTIONS ONLY. Analysing
    every FunctionDef in isolation reported `stops` and `ref_order` in
    terrain.py as undefined: both are enclosing-scope locals read by a nested
    helper, which is a closure and not a bug. The first version of this
    docstring claimed the analysis "can only cause a miss, never a false
    alarm", and that claim was wrong in exactly this case -- so the scope is
    now the whole top-level function including everything nested inside it.
    """
    used: set[str] = set()
    top = [
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    nested = {
        id(sub)
        for n in top
        for sub in ast.walk(n)
        if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and sub is not n
    }
    for node in top:
        if id(node) in nested:
            continue  # analysed as part of its parent
        bound = {a.arg for a in node.args.args + node.args.kwonlyargs}
        if node.args.vararg:
            bound.add(node.args.vararg.arg)
        if node.args.kwarg:
            bound.add(node.args.kwarg.arg)
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, (ast.Store, ast.Del)):
                bound.add(sub.id)
            elif isinstance(sub, (ast.Import, ast.ImportFrom)):
                for alias in sub.names:
                    bound.add((alias.asname or alias.name).split(".")[0])
            elif isinstance(sub, ast.ExceptHandler) and sub.name:
                bound.add(sub.name)
            elif isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                bound.add(sub.name)
            elif isinstance(sub, ast.arg):
                bound.add(sub.arg)
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                if sub.id not in bound:
                    used.add(sub.id)
    return used


class TestEveryCliNameResolves(unittest.TestCase):
    def test_no_cli_module_uses_an_undefined_global(self):
        problems = []
        for path in sorted(CLI_DIR.glob("*.py")):
            module = importlib.import_module(
                f"trail_factors.cli.{path.stem}"
                if path.stem != "__init__"
                else "trail_factors.cli"
            )
            defined = set(vars(module)) | set(vars(builtins))
            for name in sorted(_global_loads(ast.parse(path.read_text()))):
                if name not in defined:
                    problems.append(f"{path.relative_to(REPO_ROOT)}: {name}")
        self.assertEqual(problems, [], f"undefined global(s) in the CLI: {problems}")

    def test_the_check_would_have_caught_the_sync_bug(self):
        """THE PRESENCE TWIN. A guard that cannot fire is worth nothing, so
        this reconstructs the exact shape of the bug -- a name used inside a
        function, never imported -- and asserts the analyser reports it."""
        source = (
            "import os\n"
            "def sync():\n"
            "    missing = missing_names(con, limit=3)\n"
            "    return missing\n"
        )
        used = _global_loads(ast.parse(source))
        self.assertIn("missing_names", used)
        self.assertIn("con", used)
        # ...and does NOT report the ones that are genuinely bound.
        self.assertNotIn("missing", used)
        self.assertNotIn("os", used)

    def test_the_names_sync_reaches_are_importable(self):
        """The guard found `outstanding_confirmations` on the line after the
        first crash, in the same block, and a live sync confirmed it by
        crashing there once the first was fixed. The block that prints a
        rejected file's next step reaches `advice_lines` the same way: only
        on a sync that has something to report."""
        from trail_factors.cli import report

        self.assertTrue(hasattr(report, "outstanding_confirmations"))
        self.assertTrue(hasattr(report, "advice_lines"))

    def test_a_CLOSURE_is_not_reported_as_undefined(self):
        """THE NEGATIVE TWIN for the analyser itself. terrain.py reads `stops`
        and `ref_order` from an enclosing scope inside a nested helper; an
        analyser that flagged those would be deleted within a week."""
        source = (
            "def outer(con):\n"
            "    stops = con.execute('...')\n"
            "    def inner(a, b):\n"
            "        for s0, s1 in stops:\n"
            "            pass\n"
            "    return inner\n"
        )
        self.assertNotIn("stops", _global_loads(ast.parse(source)))


if __name__ == "__main__":
    unittest.main()
