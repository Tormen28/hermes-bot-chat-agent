"""R26 (disclosure audit) as a real test: the audit passes on the plugin and FAILS on a tampered copy."""
import importlib.util
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / "stability" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


audit = _load("r26_audit")


def _copy_plugin():
    dst = Path(tempfile.mkdtemp(prefix="bca-r26-copy-")) / "plugin"
    shutil.copytree(ROOT, dst, ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache", "tools"))
    (dst / "tools").mkdir()
    return dst


class TestR26(unittest.TestCase):
    def test_static_audit_clean_on_the_plugin(self):
        self.assertEqual(audit.static_audit(ROOT), [])

    def test_runtime_audit_clean_and_really_exercises_the_plugin(self):
        res = audit.runtime_audit(ROOT)
        self.assertEqual(res["network_events"], [])
        self.assertEqual(res["process_events"], [])
        self.assertEqual(res["secret_opens"], [])
        self.assertEqual(res["writes_outside_temp"], [])
        self.assertEqual(res["read_write_violations"], [])
        self.assertIn("hooks", res["exercised"])
        self.assertGreater(res["events_seen"], 20, "the audit must observe real activity")
        modes = {(c["db"], c["mode"]) for c in res["sqlite_connections"]}
        self.assertIn(("state.db", "ro"), modes)
        self.assertNotIn(("state.db", "rw"), modes)
        self.assertNotIn(("kanban.db", "rw"), modes)

    def test_static_audit_detects_tampering(self):
        bad = _copy_plugin()
        (bad / "evil.py").write_text(
            "import subprocess\nimport requests\nimport os\n"
            "def f():\n    os.system('id')\n    open('.env').read()\n", encoding="utf-8")
        problems = audit.static_audit(bad)
        joined = "\n".join(problems)
        self.assertIn("imports subprocess", joined)
        self.assertIn("imports requests", joined)
        self.assertIn("os.system", joined)
        self.assertIn(".env", joined)

    def test_runtime_audit_detects_a_network_call(self):
        bad = _copy_plugin()
        init = bad / "__init__.py"
        init.write_text(init.read_text(encoding="utf-8") + (
            "\n\n_orig_register = register\n"
            "def register(ctx):\n"
            "    import socket\n"
            "    try:\n        socket.getaddrinfo('example.invalid', 80)\n    except OSError:\n        pass\n"
            "    return _orig_register(ctx)\n"), encoding="utf-8")
        res = audit.runtime_audit(bad)
        self.assertTrue(res["network_events"], "a network lookup during register() must be caught")


if __name__ == "__main__":
    unittest.main()
