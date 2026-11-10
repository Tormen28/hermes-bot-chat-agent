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
