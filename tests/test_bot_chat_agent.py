"""Tests for bot-chat-agent. Run: python -m pytest tests -q  (or python -m unittest)."""
import importlib.util
import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("bot_chat_agent", ROOT / "__init__.py")
bca = importlib.util.module_from_spec(spec)
sys.modules["bot_chat_agent"] = bca
spec.loader.exec_module(bca)


class FakeCtx:
    def __init__(self, cfg=None):
        self.cfg = cfg or {}
        self.hooks = {}

    def get_config(self, key, default=None):
        return self.cfg.get(key, default)

    def register_hook(self, name, fn):
        self.hooks[name] = fn


def make_db(rows):
    d = tempfile.mkdtemp()
    p = Path(d) / "state.db"
    with sqlite3.connect(p) as c:
        c.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, parent_session_id TEXT, source TEXT)")
        c.executemany("INSERT INTO sessions VALUES (?,?,?,?)", rows)
    return p


class Base(unittest.TestCase):
    def setUp(self):
        bca._cache.clear()
        bca._questions.clear()
        self.ctx = FakeCtx()
        bca.register(self.ctx)
        self.db = make_db([
            ("bot", "Bot Chat", None, "desktop"),
            ("child", None, "bot", "desktop"),
            ("sub", "Subagent: x", "bot", "subagent"),
            ("tool_sub", None, "bot", "tool"),
            ("branch", "Feature work", "bot", "desktop"),
            ("other", "My project", None, "desktop"),
        ])
        bca._state_db = lambda: self.db


class TestDetection(Base):
    def test_canonical_and_child(self):
        self.assertTrue(bca.is_bot_chat("bot"))
        self.assertTrue(bca.is_bot_chat("child"))

    def test_other_sessions_untouched(self):
        for sid in ("other", "sub", "tool_sub", "branch", "missing", "", None):
            self.assertFalse(bca.is_bot_chat(sid))

    def test_custom_title(self):
        self.ctx.cfg["bot_chat_title"] = "My project"
        self.assertTrue(bca.is_bot_chat("other"))
        self.assertFalse(bca.is_bot_chat("bot"))

    def test_db_opened_read_only(self):
        src = (ROOT / "__init__.py").read_text(encoding="utf-8")
        self.assertIn("mode=ro", src)
        self.assertNotRegex(src, r"(?i)auth\.json['\"]\s*\)|open\([^)]*\.env")


class TestStyle(Base):
    def test_injected_only_in_bot_chat(self):
        hook = self.ctx.hooks["pre_llm_call"]
        self.assertIn("[Bot Chat style]", hook(session_id="bot")["context"])
        self.assertIsNone(hook(session_id="other"))

    def test_spanish(self):
        self.ctx.cfg["language"] = "es"
        self.assertIn("[Estilo Bot Chat]", self.ctx.hooks["pre_llm_call"](session_id="bot")["context"])

    def test_style_off(self):
        self.ctx.cfg["style"] = False
        self.assertIsNone(self.ctx.hooks["pre_llm_call"](session_id="bot"))


class TestGuard(Base):
    def call(self, tool, args, sid="bot", turn="t1"):
        return self.ctx.hooks["pre_tool_call"](tool_name=tool, args=args, session_id=sid, turn_id=turn)

    def assertBlocked(self, tool, args, rule):
        r = self.call(tool, args)
        self.assertIsNotNone(r, f"{tool} {args} should be blocked")
        self.assertEqual(r["action"], "block")
        self.assertIn(bca.MSG["en"][rule], r["message"])

    def test_secrets(self):
        self.assertBlocked("read_file", {"path": "/home/u/project/.env"}, "secrets")
        self.assertBlocked("read_file", {"path": "C:\\x\\auth.json"}, "secrets")
        self.assertBlocked("patch", {"target_file": ".env.local"}, "secrets")
        self.assertBlocked("terminal", {"command": "cat ~/.ssh/id_rsa"}, "secrets")
        self.assertBlocked("terminal", {"command": "cat .env; echo ok"}, "secrets")
        self.assertBlocked("terminal", {"command": "printenv"}, "secrets")
        self.assertBlocked("terminal", {"command": "env | sort"}, "secrets")
        self.assertIsNone(self.call("read_file", {"path": ".env.example"}))
        self.assertIsNone(self.call("read_file", {"path": ".env.template"}))
        self.assertIsNone(self.call("terminal", {"command": "python -m venv env"}))
        self.assertIsNone(self.call("terminal", {"command": "source env/bin/activate"}))
        self.assertIsNone(self.call("terminal", {"command": "/usr/bin/env pytest"}))
        self.assertIsNone(self.call("terminal", {"command": "set -e"}))

    def test_deletes(self):
        for c in ("rm -rf build", "Remove-Item x -Recurse", "del foo.txt", "DROP TABLE users",
                  "find . -name '*.tmp' -delete", "echo ok && rm foo.txt"):
            self.assertBlocked("terminal", {"command": c}, "deletes")
        for safe in ("grep -ri pattern src/", 'git commit -m "ajuste del modulo"', 'git commit -m "rm unused import"'):
            self.assertIsNone(self.call("terminal", {"command": safe}), safe)

    def test_destructive_git(self):
        for c in ("git push --force origin dev", "git reset --hard HEAD~1", "git clean -fd",
                  "git branch -D x", "git -C repo reset --hard HEAD", "git stash clear"):
            self.assertBlocked("terminal", {"command": c}, "destructive_git")

    def test_protected_push(self):
        self.assertBlocked("terminal", {"command": "git push origin main"}, "protected_push")
        self.assertBlocked("terminal", {"command": "git push origin HEAD:master"}, "protected_push")
        self.assertBlocked("terminal", {"command": "git push origin refs/heads/main"}, "protected_push")
        self.assertBlocked("terminal", {"command": "gh pr merge 12"}, "protected_push")
        self.assertIsNone(self.call("terminal", {"command": "git push origin feature/x"}))
        self.assertIsNone(self.call("terminal", {"command": "git push origin feature/main"}))

    def test_custom_protected_branches(self):
        self.ctx.cfg["protected_branches"] = ["release"]
        self.assertBlocked("terminal", {"command": "git push origin release"}, "protected_push")
        self.assertIsNone(self.call("terminal", {"command": "git push origin main"}))

    def test_external_sends(self):
        self.assertBlocked("send_message", {"text": "hi"}, "external_sends")
        self.assertBlocked("react_to_message", {}, "external_sends")
        self.assertBlocked("mcp_slack_send_message", {}, "external_sends")
        self.assertIsNone(self.call("mcp_slack_read_channel", {}))

    def test_expensive_models(self):
        self.assertBlocked("delegate_task", {"goal": "x", "model": "gemini-3-pro"}, "expensive_models")
        self.assertIsNone(self.call("delegate_task", {"goal": "x", "model": "gemini-3-flash"}))

    def test_one_question_per_turn(self):
        self.assertIsNone(self.call("clarify", {"question": "a?"}))
        self.assertBlocked("clarify", {"question": "b?"}, "one_question_per_turn")
        self.assertIsNone(self.call("clarify", {"question": "c?"}, turn="t2"))

    def test_safe_calls_pass(self):
        for tool, args in (("read_file", {"path": "src/app.py"}), ("terminal", {"command": "git status"}),
                           ("write_file", {"path": "notes.md", "content": "x"}),
                           ("terminal", {"command": "npm test"})):
            self.assertIsNone(self.call(tool, args), f"{tool} {args}")

    def test_scope_bot_chat_only(self):
        self.assertIsNone(self.call("terminal", {"command": "rm -rf build"}, sid="other"))
        self.ctx.cfg["scope"] = "all"
        self.assertIsNotNone(self.call("terminal", {"command": "rm -rf build"}, sid="other"))

    def test_rule_toggle(self):
        self.ctx.cfg["guard"] = {"deletes": False}
        self.assertIsNone(self.call("terminal", {"command": "rm -rf build"}))
        self.assertIsNotNone(self.call("terminal", {"command": "git push origin main"}))

    def test_never_approves(self):
        src = (ROOT / "__init__.py").read_text(encoding="utf-8")
        self.assertNotIn('"approve"', src)

    def test_spanish_message(self):
        self.ctx.cfg["language"] = "es"
        self.assertIn("Bloqueado", self.call("send_message", {})["message"])


class TestDesktopSdkOnly(unittest.TestCase):
    """Catalog rule 8: desktop plugins must stay inside the SDK."""
    def test_no_dom_reach(self):
        js = (ROOT / "desktop" / "plugin.js").read_text(encoding="utf-8")
        for bad in ("document.", "MutationObserver", "data-slot", "window.", "querySelector", "__hermesDesktop"):
            self.assertNotIn(bad, js, bad)
        self.assertIn("from '@hermes/plugin-sdk'", js)


class TestPackaging(unittest.TestCase):
    def test_no_bytecode_or_secrets(self):
        bad = [p for p in ROOT.rglob("*") if p.suffix in {".pyc", ".pyo"} or p.name in {".env", "auth.json"}]
        self.assertEqual(bad, [])

    def test_manifest(self):
        y = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
        self.assertRegex(y, r'requires_hermes:\s*">=0\.22\.5"')
        for h in ("pre_llm_call", "pre_tool_call"):
            self.assertIn(h, y)


if __name__ == "__main__":
    unittest.main()
