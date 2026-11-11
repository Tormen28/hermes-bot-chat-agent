"""Catalog rule 9 (public surfaces only), enforced as a test.

Every import of a Hermes core module made by the plugin's runtime code must be on the short
allow-list below (all three are in the core's published plugin API,
``hermes_cli/data/plugin_api.json``). When a core checkout is available
(``HERMES_AGENT_CHECKOUT`` or the running venv), the core's own admission lint
(``hermes_cli.plugin_validate_private_imports``) must also report nothing.
"""
import ast
import os
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]

CORE_TOP_LEVEL = {"hermes_constants", "hermes_cli", "hermes_state", "agent", "tools", "gateway",
                  "cron", "run_agent", "hermes_agent", "hermes_platform"}
ALLOWED = {
    "hermes_constants",        # get_hermes_home, get_default_hermes_root, profile_name_for_home
    "agent.context_engine",    # ContextEngine base class (documented plugin extension point)
    "tools.registry",          # registry: the public surface to call core tools
}
LOCAL = {p.stem for p in ROOT.glob("*.py")}


def _runtime_files():
    for p in ROOT.rglob("*.py"):
        rel = p.relative_to(ROOT)
        if rel.parts[0] in {"tests", "tools", "demo", ".git"}:
            continue
        yield p


def _core_imports(path):
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            mods = [node.module]
        else:
            continue
        for m in mods:
            if m.split(".")[0] in CORE_TOP_LEVEL and m.split(".")[0] not in LOCAL:
                yield node.lineno, m


class TestPublicSurfaces(unittest.TestCase):
    def test_runtime_code_imports_only_allowed_core_modules(self):
        bad = []
        for path in _runtime_files():
            for line, mod in _core_imports(path):
                if mod not in ALLOWED:
                    bad.append(f"{path.relative_to(ROOT)}:{line} {mod}")
        self.assertEqual(bad, [], "private core imports: " + "; ".join(bad))

    def test_core_admission_lint_is_clean_when_available(self):
        core = os.environ.get("HERMES_AGENT_CHECKOUT", "")
        if core and Path(core).is_dir() and core not in sys.path:
            sys.path.insert(0, core)
        try:
            from hermes_cli import plugin_validate_private_imports as lint
        except Exception as exc:  # noqa: BLE001
            self.skipTest(f"core admission lint not importable here: {exc}")
        self.assertEqual(lint.private_import_findings(ROOT), [])
        self.assertEqual(lint.private_attribute_findings(ROOT), [])


if __name__ == "__main__":
    unittest.main()
