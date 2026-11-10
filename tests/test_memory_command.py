"""Slash command /memoria (alias /memory): list, forget, edit, clear."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_bot_chat_agent import Base, bca, FakeCtx  # noqa: E402


def _tmp():
    return Path(tempfile.mkdtemp(prefix="bca-memcmd-")) / "memory.db"


class TestMemoryCommand(unittest.TestCase):
    def setUp(self):
        import bot_chat_memory as mem
        import memory_command as cmd
        self.mem = mem
        self.cmd = cmd
        self.store = mem.MemoryStore(_tmp())
        self.sid = "s1"
        self.d1 = self.store.upsert(
            "decision", "Use the plugin local store under HERMES_HOME.",
            session_id=self.sid, keywords=("plugin", "store"))
        self.l1 = self.store.upsert(
            "lesson", "A mid-turn steer must not drop the earlier ask.",
            session_id=self.sid, keywords=("steer", "ask"))
        self.assertTrue(self.d1 and self.l1)
        self.store.save_summary(self.sid, "- U: review the plugin\n- A: On it.", 4)
        self.store.save_pending(self.sid, ["revisa bien como esta la arena ahora"])

    def handle(self, raw, lang="en", session_id=None):
        return self.cmd.handle(
            raw, store=self.store, session_id=session_id or self.sid, lang=lang)

    def test_list_groups_by_type_and_shows_summary_and_pending(self):
        out = self.handle("")
        self.assertIn("decision", out.lower())
        self.assertIn("lesson", out.lower())
        self.assertIn("plugin local store", out)
        self.assertIn("mid-turn steer", out)
        self.assertIn("review the plugin", out)
        self.assertIn("arena", out)
        # ids are visible so the user can forget or edit
        self.assertIn(self.d1[:8], out)

    def test_spanish_labels_for_memoria_language(self):
        out = self.handle("listar", lang="es")
        self.assertIn("Decisión", out)
        self.assertIn("Lección", out)
        self.assertIn("Resumen", out)
        self.assertIn("Pendientes", out)

    def test_forget_by_short_id(self):
        out = self.handle(f"forget {self.d1[:8]}")
        self.assertIn("Forgot", out)
        self.assertIsNone(self.store.get(self.d1[:8]))
        left = self.store.list_all()
        self.assertEqual(len(left), 1)
        self.assertIn("steer", left[0]["text"])

    def test_edit_by_id(self):
        out = self.handle(f"edit {self.l1[:8]} Always close the earlier ask first.")
        self.assertIn("Updated", out)
        row = self.store.get(self.l1[:8])
        self.assertIn("Always close the earlier ask first", row["text"])
        # id stays stable so the user can keep referring to it
        self.assertTrue(row["id"].startswith(self.l1[:8]))

    def test_clear_requires_confirmation(self):
        out = self.handle("clear")
        self.assertIn("confirm", out.lower())
        self.assertEqual(self.store.count(), 2)
        out = self.handle("clear confirm")
        self.assertIn("Cleared", out)
        self.assertEqual(self.store.count(), 0)

    def test_clear_spanish_confirm_alias(self):
        self.handle("borrar", lang="es")
        self.assertEqual(self.store.count(), 2)
        out = self.handle("borrar confirmar", lang="es")
        self.assertIn("Borradas", out)
        self.assertEqual(self.store.count(), 0)

    def test_unknown_id_and_corrupt_store_fail_open(self):
        out = self.handle("forget deadbeef")
        self.assertIn("not found", out.lower())
        path = _tmp()
        path.write_text("not-a-db", encoding="utf-8")
        broken = self.mem.MemoryStore(path)
        text = self.cmd.handle("list", store=broken, session_id="s", lang="en")
        self.assertTrue(text)
        self.assertNotIn("Traceback", text)

    def test_help_mentions_both_names(self):
        out = self.handle("help")
        self.assertIn("/memoria", out)
        self.assertIn("/memory", out)
        self.assertIn("forget", out)
        self.assertIn("clear", out)


class TestCommandRegistration(Base):
    def test_register_exposes_memoria_and_memory_aliases(self):
        self.assertIn("memoria", self.ctx.commands)
        self.assertIn("memory", self.ctx.commands)
        self.assertTrue(callable(self.ctx.commands["memoria"]))
        self.assertTrue(callable(self.ctx.commands["memory"]))

    def test_hook_path_lists_and_forgets(self):
        home = Path(tempfile.mkdtemp(prefix="bca-memcmd-hook-"))
        os.environ["HERMES_HOME"] = str(home)
        import bot_chat_memory as mem
        store = mem.MemoryStore(home / "bot-chat-agent" / "memory.db")
        mid = store.upsert("decision", "Use token auth for the video editor.",
                           keywords=("token", "video"))
        fn = self.ctx.commands["memoria"]
        listed = fn(raw_args="", session_id="bot")
        self.assertIn("token auth", listed)
        self.assertIn(mid[:8], listed)
        forgot = fn(raw_args=f"olvidar {mid[:8]}", session_id="bot")
        self.assertIn("olvid", forgot.lower() + "forgot")
        self.assertIsNone(store.get(mid[:8]))

    def test_register_without_command_api_does_not_raise(self):
        class Bare:
            def __init__(self):
                self.cfg = {}
                self.hooks = {}
            def get_config(self, key, default=None):
                return default
            def register_hook(self, name, fn):
                self.hooks[name] = fn
        class Noisy(Bare):
            def register_command(self, *a, **k):
                raise RuntimeError("core too old")
        bca.register(Bare())
        bca.register(Noisy())  # must not raise


if __name__ == "__main__":
    unittest.main()
