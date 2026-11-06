"""Team UX: single-operator by default, wait-for-god only when gods are configured."""
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]
if "bot_chat_agent" not in sys.modules:
    spec = importlib.util.spec_from_file_location("bot_chat_agent", ROOT / "__init__.py")
    bca = importlib.util.module_from_spec(spec)
    sys.modules["bot_chat_agent"] = bca
    spec.loader.exec_module(bca)
else:
    bca = sys.modules["bot_chat_agent"]


class FakeCtx:
    def __init__(self, cfg=None):
        self.cfg = cfg or {}
        self.hooks = {}

    def get_config(self, key, default=None):
        return self.cfg.get(key, default)

    def register_hook(self, name, fn):
        self.hooks[name] = fn


def make_state():
    d = tempfile.mkdtemp()
    p = Path(d) / "state.db"
    with sqlite3.connect(p) as c:
        c.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, parent_session_id TEXT, source TEXT)")
        c.executemany("INSERT INTO sessions VALUES (?,?,?,?)", [
            ("bot", "Bot Chat", None, "desktop"),
            ("sub", "Subagent: x", "bot", "subagent"),
            ("other", "My project", None, "desktop"),
            ("kanban_worker", "Kanban: t1", None, "kanban"),
        ])
    return p


def make_kanban(tasks, links=None):
    d = tempfile.mkdtemp()
    p = Path(d) / "kanban.db"
    with sqlite3.connect(p) as c:
        c.execute(
            "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, assignee TEXT, status TEXT, "
            "result TEXT, started_at INTEGER, session_id TEXT, idempotency_key TEXT)"
        )
        c.execute("CREATE TABLE task_links (parent_id TEXT, child_id TEXT)")
        for row in tasks:
            c.execute(
                "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?)",
                (
                    row["id"], row.get("title"), row.get("assignee"), row.get("status"),
                    row.get("result"), row.get("started_at"), row.get("session_id"),
                    row.get("idempotency_key"),
                ),
            )
        for parent, child in links or []:
            c.execute("INSERT INTO task_links VALUES (?,?)", (parent, child))
    return p


class TeamBase(unittest.TestCase):
    def setUp(self):
        bca._cache.clear()
        bca._questions.clear()
        self.tmp = Path(tempfile.mkdtemp())
        self.ctx = FakeCtx()
        bca.register(self.ctx)
        self.state = make_state()
        bca._state_db = lambda: self.state
        self.ctx.cfg["kanban_db"] = str(self.tmp / "missing-kanban.db")
        self.ctx.cfg["notify_state_path"] = str(self.tmp / "notify.json")

    def call(self, tool, args, sid="bot", turn="t1", **kw):
        return self.ctx.hooks["pre_tool_call"](
            tool_name=tool, args=args, session_id=sid, turn_id=turn, **kw)

    def style(self, sid="bot"):
        return self.ctx.hooks["pre_llm_call"](session_id=sid)


class TestSingleOperator(TeamBase):
    def test_ack_and_no_invented_team(self):
        text = self.style()["context"]
        self.assertIn("On it —", text)
        self.assertIn("Single-operator", text)
        self.assertNotIn("researcher", text.lower())

    def test_spanish_ack(self):
        self.ctx.cfg["language"] = "es"
        text = self.style()["context"]
        self.assertIn("Voy a", text)
        self.assertIn("Un solo operador", text)

    def test_complete_is_not_blocked_without_gods(self):
        board = make_kanban([{"id": "t1", "title": "Notes", "assignee": "researcher", "status": "running"}])
        self.ctx.cfg["kanban_db"] = str(board)
        self.assertIsNone(self.call("kanban_complete", {"task_id": "t1", "summary": "done"}))

    def test_style_off_skips_team_note(self):
        self.ctx.cfg["style"] = False
        self.assertIsNone(self.style())


class TestWaitForGod(TeamBase):
    def setUp(self):
        super().setUp()
        self.ctx.cfg["team"] = {"enabled": True, "gods": ["Researcher"]}

    def test_blocks_closing_a_god_card_until_done(self):
        board = make_kanban([{
            "id": "t1", "title": "Write the notes", "assignee": "researcher",
            "status": "running", "session_id": "bot",
        }])
        self.ctx.cfg["kanban_db"] = str(board)
        blocked = self.call("kanban_complete", {"task_id": "t1", "summary": "I did it myself"})
        self.assertEqual(blocked["action"], "block")
        self.assertIn("researcher", blocked["message"])
        self.assertIn(bca.MSG["en"]["wait_for_god"], blocked["message"])
        board_done = make_kanban([{
            "id": "t1", "title": "Write the notes", "assignee": "researcher",
            "status": "done", "result": "Notes are in the repo", "session_id": "bot",
        }])
        self.ctx.cfg["kanban_db"] = str(board_done)
        self.assertIsNone(self.call("kanban_complete", {"task_id": "t1", "summary": "already delivered"}))

    def test_parent_waits_for_open_god_descendant(self):
        board = make_kanban(
            [
                {"id": "parent", "title": "Ship", "assignee": "default", "status": "running", "session_id": "bot"},
                {"id": "mid", "title": "Split", "assignee": "default", "status": "running"},
                {"id": "leaf", "title": "Write the notes", "assignee": "researcher", "status": "todo"},
            ],
            [("parent", "mid"), ("mid", "leaf")],
        )
        self.ctx.cfg["kanban_db"] = str(board)
        blocked = self.call("kanban_complete", {"task_id": "parent", "summary": "all done"})
        self.assertEqual(blocked["action"], "block")
        self.assertIn("leaf", blocked["message"])
        done = make_kanban(
            [
                {"id": "parent", "title": "Ship", "assignee": "default", "status": "running"},
                {"id": "mid", "title": "Split", "assignee": "default", "status": "done"},
                {"id": "leaf", "title": "Write the notes", "assignee": "researcher", "status": "done",
                 "result": "Notes are in the repo"},
            ],
            [("parent", "mid"), ("mid", "leaf")],
        )
        self.ctx.cfg["kanban_db"] = str(done)
        self.assertIsNone(self.call("kanban_complete", {"task_id": "parent", "summary": "all done"}))

    def test_subagent_inherits_wait_other_sessions_do_not(self):
        board = make_kanban([{"id": "t1", "assignee": "researcher", "status": "ready", "title": "X"}])
        self.ctx.cfg["kanban_db"] = str(board)
        self.assertIsNotNone(self.call("kanban_complete", {"task_id": "t1"}, sid="sub"))
        self.assertIsNone(self.call("kanban_complete", {"task_id": "t1"}, sid="other"))

    def test_god_worker_may_deliver_its_own_card(self):
        board = make_kanban([{"id": "t1", "assignee": "researcher", "status": "running", "title": "X"}])
        self.ctx.cfg["kanban_db"] = str(board)
        self.ctx.cfg["scope"] = "all"
        with patch.dict(os.environ, {"HERMES_KANBAN_TASK": "t1"}):
            own = self.call("kanban_complete", {"task_id": "t1", "summary": "delivered"}, sid="kanban_worker")
        self.assertIsNone(own)
        with patch.dict(os.environ, {"HERMES_KANBAN_TASK": "t1"}):
            other = self.call("kanban_complete", {"task_id": "t9", "summary": "nope", "assignee": "researcher"},
                              sid="kanban_worker")
        self.assertIsNotNone(other)

    def test_create_subcard_is_allowed_and_idempotent(self):
        first = self.call("kanban_create", {
            "title": "Write the notes", "assignee": "researcher", "parents": ["parent"],
        })
        self.assertEqual(first["action"], "modify")
        key = first["args"]["idempotency_key"]
        self.assertTrue(key.startswith("bca-"))
        second = self.call("kanban_create", {
            "title": "Write the notes", "assignee": "Researcher", "parents": ["parent"],
        })
        self.assertEqual(second["args"]["idempotency_key"], key)
        kept = self.call("kanban_create", {
            "title": "Write the notes", "assignee": "researcher", "idempotency_key": "user-key",
        })
        self.assertIsNone(kept)

    def test_style_names_only_configured_gods_and_open_cards(self):
        board = make_kanban([{
            "id": "t1", "title": "Write the notes", "assignee": "researcher",
            "status": "running", "session_id": "bot",
        }])
        self.ctx.cfg["kanban_db"] = str(board)
        text = self.style()["context"]
        self.assertIn("researcher", text)
        self.assertIn("Write the notes", text)
        self.assertIn("Do not invent", text)
        self.assertNotIn("Single-operator", text)

    def test_delivery_is_announced_once(self):
        self.ctx.hooks["kanban_task_completed"](
            task_id="t9", assignee="researcher", summary="Ship the patch")
        self.ctx.hooks["kanban_task_completed"](
            task_id="t8", assignee="stranger", summary="do not leak this")
        first = self.style()["context"]
        self.assertIn("Ship the patch", first)
        self.assertNotIn("do not leak this", first)
        second = self.style()["context"]
        self.assertNotIn("Ship the patch", second)

    def test_team_disabled_skips_wait(self):
        self.ctx.cfg["team"] = {"enabled": False, "gods": ["researcher"]}
        board = make_kanban([{"id": "t1", "assignee": "researcher", "status": "running", "title": "X"}])
        self.ctx.cfg["kanban_db"] = str(board)
        self.assertIsNone(self.call("kanban_complete", {"task_id": "t1", "summary": "x"}))
        self.assertIn("Single-operator", self.style()["context"])


class TestKanbanLock(TeamBase):
    def test_blocks_db_write_cli_impersonation_and_preapproved(self):
        self.assertEqual(
            self.call("terminal", {"command": "sqlite3 ~/.hermes/kanban.db \"UPDATE tasks SET status='done'\""})["action"],
            "block")
        self.assertIsNotNone(self.call("terminal", {"command": "hermes kanban complete t1 --result done"}))
        self.assertIsNone(self.call("terminal", {"command": "hermes kanban show t1"}))
        self.assertIsNone(self.call("terminal", {"command": "hermes kanban list --status ready"}))
        self.assertIsNotNone(self.call("write_file", {"path": "/tmp/kanban.db", "content": "x"}))
        self.assertIsNotNone(self.call("kanban_create", {
            "title": "Already done", "assignee": "researcher", "status": "done", "result": "shipped",
        }))
        self.assertIsNotNone(self.call("kanban_create", {
            "title": "Spoof", "assignee": "researcher", "as_user": True,
        }))
        spoof = self.call("kanban_create", {"title": "Spoof", "created_by": "user"})
        self.assertEqual(spoof["action"], "block")
        self.assertIn(bca.MSG["en"]["kanban_lock"], spoof["message"])

    def test_lock_covers_subagents_not_other_sessions(self):
        cmd = {"command": "hermes kanban complete t1"}
        self.assertIsNotNone(self.call("terminal", cmd, sid="sub"))
        self.assertIsNone(self.call("terminal", cmd, sid="other"))

    def test_relaxed_keeps_lock_and_drops_wait(self):
        self.ctx.cfg["preset"] = "relaxed"
        self.ctx.cfg["team"] = {"gods": ["researcher"]}
        board = make_kanban([{"id": "t1", "assignee": "researcher", "status": "running", "title": "X"}])
        self.ctx.cfg["kanban_db"] = str(board)
        self.assertIsNone(self.call("kanban_complete", {"task_id": "t1", "summary": "close early"}))
        self.assertIsNotNone(self.call("terminal", {"command": "python3 -c \"import sqlite3; sqlite3.connect('kanban.db')\""}))
        guard = bca._cfg("guard")
        self.assertTrue(guard["kanban_lock"])
        self.assertFalse(guard["wait_for_god"])
        self.assertTrue(guard["secrets"])
        self.assertFalse(guard["deletes"])

    def test_strict_enables_both_new_rules(self):
        self.ctx.cfg = {"preset": "strict", "notify_state_path": str(self.tmp / "n.json"),
                        "kanban_db": str(self.tmp / "missing-kanban.db")}
        guard = bca._cfg("guard")
        self.assertTrue(guard["kanban_lock"])
        self.assertTrue(guard["wait_for_god"])

    def test_missing_board_does_not_crash(self):
        self.ctx.cfg["team"] = {"gods": ["researcher"]}
        self.assertIsNone(self.call("kanban_complete", {"task_id": "t1", "summary": "x"}))
        named = self.call("kanban_complete", {"task_id": "t1", "assignee": "researcher", "summary": "x"})
        self.assertEqual(named["action"], "block")
        text = self.style()["context"]
        self.assertIn("researcher", text)
        self.assertNotIn("Traceback", text)


class TestPresetsUnchangedCore(TeamBase):
    def test_default_still_blocks_original_rules(self):
        self.assertIsNotNone(self.call("terminal", {"command": "rm -rf build"}))
        self.assertIsNotNone(self.call("read_file", {"path": ".env"}))
        self.assertIsNone(self.call("terminal", {"command": "git status"}))
        guard = bca._cfg("guard")
        for key in ("secrets", "deletes", "destructive_git", "protected_push",
                    "external_sends", "expensive_models", "one_question_per_turn"):
            self.assertTrue(guard[key], key)


class TestNotifyFile(TeamBase):
    def test_state_file_has_no_secrets_and_is_json(self):
        self.ctx.cfg["team"] = {"gods": ["researcher"]}
        self.ctx.hooks["kanban_task_completed"](
            task_id="t9", assignee="researcher", summary="Ship the patch")
        raw = Path(self.ctx.cfg["notify_state_path"]).read_text(encoding="utf-8")
        data = json.loads(raw)
        self.assertIn("t9", data["pending"])
        self.assertNotIn("BEGIN ", raw)
        self.assertNotIn("api_key", raw.lower())


if __name__ == "__main__":
    unittest.main()
