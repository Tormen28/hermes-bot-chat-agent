"""Tests for bot-chat-agent. Run: python -m pytest tests -q  (or python -m unittest)."""
import atexit
import importlib.util
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]


def _clean_pycache() -> None:
    for pycache in ROOT.rglob("__pycache__"):
        shutil.rmtree(pycache, ignore_errors=True)


_clean_pycache()
atexit.register(_clean_pycache)
spec = importlib.util.spec_from_file_location("bot_chat_agent", ROOT / "__init__.py")
bca = importlib.util.module_from_spec(spec)
sys.modules["bot_chat_agent"] = bca
spec.loader.exec_module(bca)


class FakeCtx:
    def __init__(self, cfg=None):
        self.cfg = cfg or {}
        self.hooks = {}
        self.commands = {}

    def get_config(self, key, default=None):
        return self.cfg.get(key, default)

    def register_hook(self, name, fn):
        self.hooks[name] = fn

    def register_command(self, name, handler, description="", args_hint="", **k):
        clean = str(name or "").lower().strip().lstrip("/")
        if clean:
            self.commands[clean] = handler


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
        bca._reset_language_state()
        self.ctx = FakeCtx()
        bca.register(self.ctx)
        self.db = make_db([
            ("bot", "Bot Chat", None, "desktop"),
            ("child", None, "bot", "desktop"),
            ("sub", "Subagent: x", "bot", "subagent"),
            ("sub_nested", "Subagent: y", "sub", "subagent"),
            ("sub_of_child", "Subagent: z", "child", "subagent"),
            ("tool_sub", None, "bot", "tool"),
            ("branch", "Feature work", "bot", "desktop"),
            ("other", "My project", None, "desktop"),
            ("other_sub", "Subagent: o", "other", "subagent"),
            ("kanban_sub", "Subagent: k", "bot", "kanban"),
        ])
        bca._state_db = lambda: self.db


class TestDetection(Base):
    def test_canonical_and_child(self):
        self.assertTrue(bca.is_bot_chat("bot"))
        self.assertTrue(bca.is_bot_chat("child"))

    def test_other_sessions_untouched(self):
        for sid in ("other", "sub", "sub_nested", "sub_of_child", "tool_sub",
                    "other_sub", "kanban_sub", "branch", "missing", "", None):
            self.assertFalse(bca.is_bot_chat(sid), f"{sid} should be False without include_subagents")

    def test_subagents_of_bot_chat_inherit_when_enabled(self):
        for sid in ("sub", "sub_nested", "sub_of_child", "tool_sub"):
            self.assertTrue(bca.is_bot_chat(sid, include_subagents=True), f"{sid} should inherit Bot Chat")
        for sid in ("other", "other_sub", "kanban_sub", "branch", "missing", "", None):
            self.assertFalse(bca.is_bot_chat(sid, include_subagents=True), f"{sid} should NOT inherit Bot Chat")

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
                  "git branch -D x", "git -C repo reset --hard", "git -C repo reset --hard HEAD",
                  "git --no-pager -C r push origin x --force", "git stash clear"):
            self.assertBlocked("terminal", {"command": c}, "destructive_git")

    def test_protected_push(self):
        self.assertBlocked("terminal", {"command": "git push origin main"}, "protected_push")
        self.assertBlocked("terminal", {"command": "git push origin HEAD:master"}, "protected_push")
        self.assertBlocked("terminal", {"command": "git push origin refs/heads/main"}, "protected_push")
        self.assertBlocked("terminal", {"command": "git --no-pager -C r push origin main"}, "protected_push")
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

    def test_scope_bot_chat_and_subagents(self):
        # Canonical Bot Chat and its subagent children are guarded
        for sid in ("bot", "child", "sub", "sub_nested", "sub_of_child", "tool_sub"):
            self.assertIsNotNone(self.call("terminal", {"command": "rm -rf build"}, sid=sid), sid)
        # Unspawned child passing parent_session_id in hook kwargs is also guarded
        r = self.ctx.hooks["pre_tool_call"](
            tool_name="terminal", args={"command": "rm -rf build"},
            session_id="unindexed_sub", parent_session_id="bot", turn_id="t1")
        self.assertIsNotNone(r)
        # Non-Bot-Chat sessions and their subagents are untouched unless scope == "all"
        for sid in ("other", "other_sub", "branch", "kanban_sub"):
            self.assertIsNone(self.call("terminal", {"command": "rm -rf build"}, sid=sid), sid)
        self.ctx.cfg["scope"] = "all"
        self.assertIsNotNone(self.call("terminal", {"command": "rm -rf build"}, sid="other"))
        self.assertIsNotNone(self.call("terminal", {"command": "rm -rf build"}, sid="other_sub"))

    def test_rule_toggle(self):
        self.ctx.cfg["guard"] = {"deletes": False}
        self.assertIsNone(self.call("terminal", {"command": "rm -rf build"}))
        self.assertIsNotNone(self.call("terminal", {"command": "git push origin main"}))

    def test_presets_strict_default_relaxed(self):
        # Default preset blocks everything
        self.assertIsNotNone(self.call("terminal", {"command": "rm -rf build"}))
        self.assertIsNotNone(self.call("send_message", {"text": "hi"}))
        self.assertIsNotNone(self.call("delegate_task", {"goal": "x", "model": "gemini-3-pro"}))

        # Relaxed preset keeps secrets, destructive_git, protected_push ON, turns off deletes/sends/expensive/one_question
        self.ctx.cfg["preset"] = "relaxed"
        self.assertIsNotNone(self.call("read_file", {"path": ".env"}))
        self.assertIsNotNone(self.call("terminal", {"command": "git reset --hard HEAD"}))
        self.assertIsNotNone(self.call("terminal", {"command": "git push origin main"}))
        self.assertIsNone(self.call("terminal", {"command": "rm -rf build"}))
        self.assertIsNone(self.call("send_message", {"text": "hi"}))
        self.assertIsNone(self.call("delegate_task", {"goal": "x", "model": "gemini-3-pro"}))
        self.assertIsNone(self.call("clarify", {"question": "q1?"}))
        self.assertIsNone(self.call("clarify", {"question": "q2?"}))

        # Explicit per-rule override on top of relaxed preset
        self.ctx.cfg["guard"] = {"deletes": True}
        self.assertIsNotNone(self.call("terminal", {"command": "rm -rf build"}))
        self.assertIsNone(self.call("send_message", {"text": "hi"}))

        # Strict preset blocks all 7 rules
        self.ctx.cfg = {"preset": "strict"}
        for k, v in bca._cfg("guard").items():
            self.assertTrue(v, k)

        # Unknown preset falls back safely to default (all True)
        self.ctx.cfg = {"preset": "custom_unknown"}
        for k, v in bca._cfg("guard").items():
            self.assertTrue(v, k)

    def test_never_approves(self):
        src = (ROOT / "__init__.py").read_text(encoding="utf-8")
        self.assertNotIn('"approve"', src)

    def test_spanish_message(self):
        self.ctx.cfg["language"] = "es"
        self.assertIn("Bloqueado", self.call("send_message", {})["message"])

    def test_git_option_prefix_returns_quickly(self):
        """A long run of `-C` tokens must not catastrophically backtrack."""
        cmd = "git " + "-C " * 60 + "x"
        limit = 0.1  # well under 100 ms; the bounded pattern finishes in well under 1 ms

        started = time.perf_counter()
        self.assertIsNone(self.call("terminal", {"command": cmd}))
        self.assertLess(time.perf_counter() - started, limit)

        started = time.perf_counter()
        self.assertFalse(bca._protected_push(cmd))
        self.assertLess(time.perf_counter() - started, limit)


class TestDesktopSdkOnly(unittest.TestCase):
    """Catalog rule 8: desktop plugins must stay inside the SDK."""
    def test_no_dom_reach(self):
        js = (ROOT / "desktop" / "plugin.js").read_text(encoding="utf-8")
        for bad in ("document.", "MutationObserver", "data-slot", "window.", "querySelector", "__hermesDesktop"):
            self.assertNotIn(bad, js, bad)
        self.assertIn("from '@hermes/plugin-sdk'", js)

    def test_status_bar_phases_present(self):
        js = (ROOT / "desktop" / "plugin.js").read_text(encoding="utf-8")
        for expected in ("Working…", "Waiting for your OK", "Esperando tu OK", "classifyPhase", "shortError",
                         "waiting_ok", "working", "error", "Waiting on", "Esperando a", "Done ·", "Listo ·",
                         "waiting_team", "applyTeamEvent"):
            self.assertIn(expected, js, expected)

    def test_status_bar_phase_logic_via_node(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed")
        js = (ROOT / "desktop" / "plugin.js").read_text(encoding="utf-8")
        # Strip the two SDK/React import lines so pure helper functions can run in plain Node
        stripped = "\n".join(
            line for line in js.splitlines()
            if not line.strip().startswith("import ")
        ).replace("export function ", "function ").replace("export default {", "const _plugin = {")
        harness = """
const assert = require('assert');
function atom(v) { return { get: () => v, set: (n) => { v = n; } }; }
const COMPOSER_AREAS = { top: 'top' };
const host = {};
function useValue(a) { return a.get(); }
function jsx(t, p) { return { t, p }; }
function jsxs(t, p) { return { t, p }; }
""" + stripped + """
assert.deepStrictEqual(classifyPhase('en', { inBotChat: false, busy: true }), { active: false, phase: 'idle', blocked: false, text: '' });
assert.deepStrictEqual(classifyPhase('en', { inBotChat: true, busy: true, activity: '' }), { active: true, phase: 'working', blocked: false, text: 'Working… · Thinking…' });
assert.deepStrictEqual(classifyPhase('en', { inBotChat: true, busy: true, activity: 'Reading files' }), { active: true, phase: 'working', blocked: false, text: 'Working… · Reading files' });
assert.deepStrictEqual(classifyPhase('en', { inBotChat: true, busy: true, blocked: true, detail: 'this deletes files or data.' }), { active: true, phase: 'waiting_ok', blocked: true, text: 'Waiting for your OK · this deletes files or data.' });
assert.deepStrictEqual(classifyPhase('es', { inBotChat: true, busy: false, blocked: true, detail: '' }), { active: true, phase: 'waiting_ok', blocked: true, text: 'Esperando tu OK' });
assert.deepStrictEqual(classifyPhase('en', { inBotChat: true, busy: false, error: 'Connection reset by peer\\nstack trace' }), { active: true, phase: 'error', blocked: false, text: 'Error: Connection reset by peer' });
assert.strictEqual(shortError('[bot-chat-agent] Blocked: x\\nline2', 20), 'Blocked: x');
assert.deepStrictEqual(classifyPhase('en', { inBotChat: true, busy: true, teamWait: 'researcher' }), { active: true, phase: 'waiting_team', blocked: false, text: 'Waiting on researcher' });
assert.deepStrictEqual(classifyPhase('es', { inBotChat: true, busy: false, teamWait: 'investigator' }), { active: true, phase: 'waiting_team', blocked: false, text: 'Esperando a investigator' });
assert.deepStrictEqual(classifyPhase('en', { inBotChat: true, busy: false, notice: 'Ship the patch' }), { active: true, phase: 'done', blocked: false, text: 'Done · Ship the patch' });
assert.strictEqual(classifyPhase('en', { inBotChat: true, blocked: true, detail: '', teamWait: 'researcher' }).phase, 'waiting_ok');
assert.strictEqual(classifyPhase('en', { inBotChat: false, notice: 'Ship the patch' }).phase, 'idle');
const created = applyTeamEvent({ teamWait: '', notice: 'old' }, 'tool', { name: 'kanban_create', assignee: 'researcher' });
assert.strictEqual(created.teamWait, 'researcher');
assert.strictEqual(created.notice, '');
const done = applyTeamEvent({ teamWait: 'researcher', notice: '' }, 'kanban_task_completed', { task_id: 't-1', assignee: 'researcher', summary: 'Ship the patch', session_id: 's1' });
assert.strictEqual(done.teamWait, '');
assert.deepStrictEqual(done.ready, { s1: ['t-1'] });
assert.strictEqual(done.notice, '');
// Fase 3: completed feeds the ready badge (dedup by task id), not a Done notice.
assert.deepStrictEqual(classifyPhase('es', { inBotChat: true, busy: false, ready: { s1: ['t-1'] }, focusedSession: 's1' }), { active: true, phase: 'ready', blocked: false, text: '1 resultado listo' });
assert.deepStrictEqual(classifyPhase('es', { inBotChat: true, busy: true, ready: { s1: ['t-1'] }, focusedSession: 's1', activity: 'Reading files' }), { active: true, phase: 'working', blocked: false, text: 'Cargando… · Reading files' });
const queued = applyTeamEvent({ teamWait: '', notice: '' }, 'kanban_task_claimed', { task_id: 't-2', assignee: 'researcher' });
assert.strictEqual(queued.queued, 'researcher');
assert.deepStrictEqual(classifyPhase('en', { inBotChat: true, busy: false, queued: 'researcher' }), { active: true, phase: 'queued', blocked: false, text: 'Queued · researcher' });
console.log('NODE_OK');
"""
        # Run from a temp file: the whole desktop/plugin.js inline exceeds the Windows
        # command-line limit (WinError 206) when passed with `node -e`.
        import tempfile
        with tempfile.TemporaryDirectory(prefix="bca-node-") as td:
            script = Path(td) / "phase_logic.cjs"
            script.write_text(harness, encoding="utf-8")
            proc = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("NODE_OK", proc.stdout)


class TestPackaging(unittest.TestCase):
    def test_no_bytecode_or_secrets(self):
        for pycache in ROOT.rglob("__pycache__"):
            shutil.rmtree(pycache, ignore_errors=True)
        bad = [p for p in ROOT.rglob("*") if p.suffix in {".pyc", ".pyo"} or p.name in {".env", "auth.json"}]
        self.assertEqual(bad, [])

    def test_manifest(self):
        y = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
        self.assertRegex(y, r'requires_hermes:\s*">=0\.22\.5"')
        for h in ("pre_llm_call", "pre_tool_call", "post_tool_call", "kanban_task_completed"):
            self.assertIn(h, y)
        self.assertRegex(y, r"version:\s*0\.3\.0\b")


if __name__ == "__main__":
    unittest.main()
