"""Compact live status panel: header line + optional desktop bar suffix."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_bot_chat_agent import Base, bca  # noqa: E402

import context_budget  # noqa: E402


class TestStatusPanelRender(unittest.TestCase):
    def setUp(self):
        import status_panel as sp
        self.sp = sp

    def test_compact_line_includes_tasks_pending_recalled_and_size(self):
        text = self.sp.render(
            lang="en",
            tasks=[{"title": "Preview render", "status": "working"}],
            pending=["revisa la arena ahora por favor"],
            recalled=[{"title": "Use token auth", "kind": "decision"},
                      {"title": "ffmpeg is the preview renderer", "kind": "lesson"}],
            context_tokens=720,
        )
        self.assertIn("working", text.lower())
        self.assertIn("Preview render", text)
        self.assertIn("pending", text.lower())
        self.assertIn("2", text)  # recalled count
        self.assertIn("token auth", text.lower())
        self.assertIn("720", text)
        self.assertLess(len(text.splitlines()), 4)

    def test_spanish_labels(self):
        text = self.sp.render(
            lang="es",
            tasks=[{"title": "Render", "status": "working"}],
            pending=["arena"],
            recalled=[{"title": "token auth", "kind": "decision"}],
            context_tokens=400,
        )
        self.assertIn("Trabajando", text)
        self.assertIn("Pendiente", text)
        self.assertIn("recordada", text.lower())

    def test_idle_with_nothing_to_show_is_empty(self):
        self.assertEqual(self.sp.render(lang="en"), "")

    def test_disabled_is_empty_even_with_data(self):
        text = self.sp.render(
            lang="en", enabled=False,
            tasks=[{"title": "X", "status": "working"}],
            context_tokens=12,
        )
        self.assertEqual(text, "")


class TestStatusPanelHook(Base):
    def setUp(self):
        super().setUp()
        self.home = Path(tempfile.mkdtemp(prefix="bca-panel-"))
        os.environ["HERMES_HOME"] = str(self.home)

    def test_pre_llm_header_when_memories_are_recalled(self):
        import bot_chat_memory as mem
        store = mem.MemoryStore(self.home / "bot-chat-agent" / "memory.db")
        store.upsert("decision", "Use token auth for the video editor service.",
                     keywords=("token", "video", "editor"))
        history = [{"role": "user", "content": "what auth does the video editor use?"}]
        out = self.ctx.hooks["pre_llm_call"](
            session_id="bot", turn_id="t1", conversation_history=history) or {}
        ctx = out.get("context") or ""
        self.assertIn("[Status]", ctx)
        self.assertIn("token auth", ctx.lower())
        self.assertIn("tok", ctx.lower())

    def test_panel_off_leaves_no_status_header(self):
        self.ctx.cfg["status_panel"] = {"enabled": False}
        import bot_chat_memory as mem
        store = mem.MemoryStore(self.home / "bot-chat-agent" / "memory.db")
        store.upsert("decision", "Use token auth for the video editor service.",
                     keywords=("token", "video", "editor"))
        history = [{"role": "user", "content": "what auth does the video editor use?"}]
        out = self.ctx.hooks["pre_llm_call"](
            session_id="bot", turn_id="t1", conversation_history=history) or {}
        ctx = out.get("context") or ""
        self.assertNotIn("[Status]", ctx)
        self.assertIn("token auth", ctx.lower())  # long-term memory still injected

    def test_idle_turn_does_not_inflate_short_context(self):
        # No tasks, no pending, no recalled memories: no status header
        # (keeps the A0.8 style-only budget intact).
        out = self.ctx.hooks["pre_llm_call"](session_id="bot", turn_id="t0") or {}
        ctx = out.get("context") or ""
        self.assertNotIn("[Status]", ctx)
        self.assertLess(context_budget.estimate_tokens(ctx), 400)


if __name__ == "__main__":
    unittest.main()

class TestStatusSnapshot(unittest.TestCase):
    def setUp(self):
        import status_panel as sp
        self.sp = sp
        self.now = 1_000_000.0

    def test_write_and_read_roundtrip(self):
        home = Path(tempfile.mkdtemp(prefix="bca-status-file-"))
        path = home / "bot-chat-agent" / "status.json"
        payload = self.sp.snapshot(
            lang="es",
            origin="Padre",
            session_id="bot",
            tasks=[{
                "task_id": "t-1", "title": "video editor auth",
                "status": "working", "assignee": "Hefesto",
                "created_at": self.now - 72, "updated_at": self.now - 10,
            }],
            pending=["revisa bien como esta la arena ahora por favor"],
            recalled=[{"title": "Use token auth for the video editor service."}],
            context_tokens=336,
            now=self.now,
            stalled_after_seconds=120,
        )
        self.assertTrue(self.sp.write_snapshot(path, payload))
        got = self.sp.read_snapshot(path)
        self.assertEqual(got["enabled"], True)
        self.assertEqual(got["lang"], "es")
        self.assertEqual(got["active_bot"]["name"], "Padre")
        self.assertTrue(any(b["name"] == "Hefesto" for b in got["bots"]))
        self.assertEqual(len(got["delegations"]), 1)
        self.assertEqual(got["delegations"][0]["from"], "Padre")
        self.assertEqual(got["delegations"][0]["to"], "Hefesto")
        self.assertEqual(got["delegations"][0]["elapsed_seconds"], 72)
        self.assertFalse(got["delegations"][0]["stalled"])

    def test_read_missing_or_corrupt_is_fail_open(self):
        home = Path(tempfile.mkdtemp(prefix="bca-status-bad-"))
        missing = home / "nope" / "status.json"
        self.assertEqual(self.sp.read_snapshot(missing), {})
        bad = home / "status.json"
        bad.write_text("{not-json", encoding="utf-8")
        self.assertEqual(self.sp.read_snapshot(bad), {})
        self.assertFalse(self.sp.write_snapshot(Path("/proc/this-cannot-be-written/status.json"), {"enabled": True}))

    def test_delegation_lines_bilingual_and_done(self):
        waiting = {"from": "Padre", "to": "Hefesto", "state": "waiting", "elapsed_seconds": 40, "stalled": False}
        done = {"from": "Padre", "to": "Hermes", "state": "ready", "elapsed_seconds": 20, "stalled": False}
        stalled = {"from": "Padre", "to": "Hefesto", "state": "working", "elapsed_seconds": 200, "stalled": True}
        self.assertEqual(self.sp.delegation_line(waiting, "en"), "Padre → Hefesto: waiting for reply")
        self.assertEqual(self.sp.delegation_line(waiting, "es"), "Padre → Hefesto: esperando respuesta")
        self.assertEqual(self.sp.delegation_line(done, "en"), "Padre → Hermes: done")
        self.assertEqual(self.sp.delegation_line(done, "es"), "Padre → Hermes: listo")
        self.assertIn("stalled", self.sp.delegation_line(stalled, "en").lower())
        self.assertIn("sin avance", self.sp.delegation_line(stalled, "es").lower())

    def test_finished_delegation_stops_the_clock(self):
        now = 1_000_000.0
        snap = self.sp.snapshot(
            lang="en", origin="Hermes", session_id="s", now=now,
            tasks=[{"task_id": "d1", "title": "refactor", "status": "ready", "assignee": "daedalus",
                    "created_at": now - 1200, "updated_at": now - 1170},
                   {"task_id": "d2", "title": "research", "status": "waiting", "assignee": "athena",
                    "created_at": now - 20, "updated_at": now - 5}])
        by = {d["to"]: d for d in snap["delegations"]}
        self.assertEqual(by["daedalus"]["elapsed_seconds"], 30, "done after 30s, not 20 minutes")
        self.assertEqual(by["athena"]["elapsed_seconds"], 20, "open work keeps counting")

    def test_refresh_live_keeps_the_clock_running_between_turns(self):
        now = 1_000_000.0
        snap = self.sp.snapshot(
            lang="en", origin="Hermes", session_id="s", now=now, stalled_after_seconds=120,
            tasks=[{"task_id": "dm:1", "title": "research", "status": "queued", "assignee": "athena",
                    "created_at": now - 11, "updated_at": now - 11},
                   {"task_id": "dm:2", "title": "refactor", "status": "ready", "assignee": "daedalus",
                    "created_at": now - 900, "updated_at": now - 870}])
        self.assertEqual(snap["delegations"][0]["elapsed_seconds"], 11)
        later = self.sp.refresh_live(snap, now=now + 600)
        by = {d["to"]: d for d in later["delegations"]}
        self.assertEqual(by["athena"]["elapsed_seconds"], 611)
        self.assertTrue(by["athena"]["stalled"], "a reply that never came must show as stalled")
        self.assertEqual(by["daedalus"]["elapsed_seconds"], 30, "finished work keeps its duration")
        self.assertFalse(by["daedalus"]["stalled"])
        other = {b["name"]: b for b in later["bots"] if b.get("role") == "other"}
        self.assertTrue(other["athena"]["stalled"])
        self.assertEqual(snap["delegations"][0]["elapsed_seconds"], 11, "input not mutated")

    def test_refresh_live_is_fail_open(self):
        self.assertEqual(self.sp.refresh_live({}), {})
        self.assertEqual(self.sp.refresh_live(None), None)
        weird = {"tasks": ["x", {"created_at": "bad"}], "delegations": [3], "bots": [None]}
        self.assertIsInstance(self.sp.refresh_live(weird), dict)

    def test_elapsed_and_stalled_threshold(self):
        self.assertEqual(self.sp.elapsed_seconds(self.now - 45, now=self.now), 45)
        self.assertEqual(self.sp.format_elapsed(45, "en"), "45s")
        self.assertEqual(self.sp.format_elapsed(132, "es"), "2m 12s")
        self.assertFalse(self.sp.is_stalled(self.now - 119, now=self.now, threshold=120, status="working"))
        self.assertTrue(self.sp.is_stalled(self.now - 121, now=self.now, threshold=120, status="working"))
        self.assertFalse(self.sp.is_stalled(self.now - 400, now=self.now, threshold=120, status="ready"))

    def test_disabled_snapshot_is_flagged_off(self):
        snap = self.sp.snapshot(enabled=False, origin="Padre", tasks=[{"status": "working", "assignee": "Hefesto"}])
        self.assertEqual(snap["enabled"], False)
        self.assertEqual(snap.get("delegations") or [], [])


class TestStatusWriterHook(Base):
    def setUp(self):
        super().setUp()
        self.home = Path(tempfile.mkdtemp(prefix="bca-status-hook-"))
        os.environ["HERMES_HOME"] = str(self.home)

    def test_pre_llm_writes_status_json(self):
        import bot_chat_memory as mem
        import status_panel as sp
        store = mem.MemoryStore(self.home / "bot-chat-agent" / "memory.db")
        store.upsert("decision", "Use token auth for the video editor service.",
                     keywords=("token", "video", "editor"))
        history = [{"role": "user", "content": "what auth does the video editor use?"}]
        self.ctx.hooks["pre_llm_call"](
            session_id="bot", turn_id="t1", conversation_history=history)
        path = self.home / "bot-chat-agent" / "status.json"
        self.assertTrue(path.is_file(), "Python hook must publish status.json for the Desktop card")
        snap = sp.read_snapshot(path)
        self.assertTrue(snap.get("enabled"))
        titles = " ".join(str(r.get("title") or "") for r in snap.get("recalled") or [])
        self.assertIn("token auth", titles.lower())
        self.assertGreater(int(snap.get("context_tokens") or 0), 0)

    def test_panel_off_writes_disabled_flag(self):
        import status_panel as sp
        self.ctx.cfg["status_panel"] = {"enabled": False}
        self.ctx.hooks["pre_llm_call"](session_id="bot", turn_id="t0")
        snap = sp.read_snapshot(self.home / "bot-chat-agent" / "status.json")
        self.assertEqual(snap.get("enabled"), False)


class TestProfileLabel(unittest.TestCase):
    """A canonical session is always titled "Bot Chat": the card must name the BOT (profile)."""

    def setUp(self):
        import sys as _s
        self.bca = _s.modules["bot_chat_agent"]
        self._env = os.environ.get("HERMES_HOME")
        self._ctx = self.bca._ctx

    def tearDown(self):
        if self._env is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = self._env
        self.bca._ctx = self._ctx

    def _home(self, name, profile_yaml=None):
        root = Path(tempfile.mkdtemp(prefix="bca-prof-"))
        home = (root / "profiles" / name) if name else root
        home.mkdir(parents=True, exist_ok=True)
        if profile_yaml is not None:
            (home / "profile.yaml").write_text(profile_yaml, encoding="utf-8")
        os.environ["HERMES_HOME"] = str(home)
        return home

    def _label(self):
        try:
            import hermes_constants  # noqa: F401  (a real core on the path decides by itself)
            self.skipTest("a real hermes_constants is importable; env override not used")
        except ImportError:
            pass
        return self.bca._profile_label()

    def test_default_profile_is_the_coordinator(self):
        self._home(None)
        self.assertEqual(self._label(), "Hermes")

    def test_named_profile_uses_its_directory_name(self):
        self._home("iris")
        self.assertEqual(self._label(), "iris")

    def test_display_name_from_profile_yaml(self):
        self._home("iris", "display_name: Iris Designer\ndescription: x\n")
        self.assertEqual(self._label(), "Iris Designer")

    def test_session_bot_name_prefers_a_real_title_then_the_profile(self):
        self._home("athena")
        self.assertEqual(self.bca._session_bot_name(""), "athena")
        self.assertEqual(self.bca._session_bot_name("no-such-session"), "athena")

    def test_config_override_wins(self):
        self._home("athena")

        class Ctx:
            def get_config(self, key, default=None):
                return {"bot_name": "Boss"} if key == "status_panel" else default
        self.bca._ctx = Ctx()
        self.assertEqual(self.bca._profile_label(), "Boss")
