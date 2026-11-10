"""Generic short-term + long-term memory for Bot Chat (no external vault)."""
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
import pending_asks as pa  # noqa: E402


def _tmp_store_path() -> Path:
    return Path(tempfile.mkdtemp(prefix="bca-mem-")) / "memory.db"


def u(t): return {"role": "user", "content": t}
def a_final(t): return {"role": "assistant", "content": t}
def a_tools(t=""): return {"role": "assistant", "content": t, "tool_calls": [{"id": "x"}]}
def tool(t="ok"): return {"role": "tool", "content": t}


def long_session(n: int) -> list[dict]:
    msgs = []
    for i in range(n):
        msgs.append(u(f"please review item {i} of the plugin work and give a short status"))
        msgs.append(a_final(f"On it — item {i} is in progress, no blocker found."))
    return msgs


class TestMemoryStore(unittest.TestCase):
    def setUp(self):
        import bot_chat_memory as mem
        self.mem = mem
        self.store = mem.MemoryStore(_tmp_store_path())

    def test_upsert_and_retrieve_by_relevance(self):
        self.store.upsert("decision", "Use the plugin local store for long-term notes.",
                          session_id="s1", keywords=("plugin", "store", "notes"))
        self.store.upsert("lesson", "A mid-turn steer must not drop the earlier ask.",
                          session_id="s1", keywords=("steer", "ask"))
        hits = self.store.retrieve("where should long-term notes be stored?",
                                   budget_tokens=200)
        texts = " ".join(h["text"] for h in hits)
        self.assertIn("plugin local store", texts)
        self.assertNotIn("mid-turn steer", texts)

    def test_retrieve_respects_token_budget(self):
        for i in range(20):
            self.store.upsert(
                "project_state",
                f"Project state {i}: the video editor token flow is documented " + ("word " * 40),
                keywords=("video", "editor", "token"),
            )
        hits = self.store.retrieve("video editor token", budget_tokens=80)
        cost = context_budget.estimate_tokens(
            "\n".join(h["text"] for h in hits))
        self.assertLessEqual(cost, 80)
        self.assertGreaterEqual(len(hits), 1)

    def test_unrelated_query_returns_nothing(self):
        self.store.upsert("decision", "Always use ffmpeg for the local preview.",
                          keywords=("ffmpeg", "preview"))
        self.assertEqual(self.store.retrieve("what is the weather in Caracas?",
                                            budget_tokens=200), [])

    def test_corrupt_store_fails_open(self):
        path = _tmp_store_path()
        path.write_text("this is not sqlite", encoding="utf-8")
        store = self.mem.MemoryStore(path)
        self.assertEqual(store.retrieve("anything at all here", budget_tokens=100), [])
        self.assertIsNone(store.upsert("lesson", "should not explode", session_id="s"))


class TestShortTerm(unittest.TestCase):
    def setUp(self):
        import bot_chat_memory as mem
        self.mem = mem
        self.store = mem.MemoryStore(_tmp_store_path())

    def test_summary_stays_under_budget_on_long_session(self):
        msgs = long_session(80)
        text = self.mem.update_summary(
            self.store, "s1", msgs, recent_n=8, budget_tokens=120)
        self.assertTrue(text.strip())
        self.assertLessEqual(context_budget.estimate_tokens(text), 120)
        # Earlier turns are represented; the very first item should appear
        # or the summary must mention older work, not only the last 8.
        self.assertNotIn("item 79", text)  # 79 is inside the recent window

    def test_recent_window_excludes_tool_payloads(self):
        msgs = [
            u("check the arena status now please"),
            a_tools("looking"),
            tool("HUGE TOOL OUTPUT " * 200),
            a_final("Arena is stable."),
            u("thanks, next step?"),
        ]
        recent = self.mem.recent_utterances(msgs, n=8)
        blob = " ".join(recent)
        self.assertIn("arena status", blob)
        self.assertIn("Arena is stable", blob)
        self.assertNotIn("HUGE TOOL OUTPUT", blob)

    def test_bounded_view_does_not_grow_with_session_length(self):
        small = self.mem.bounded_view(long_session(10), session_id="s1",
                                      lang="en", store=self.store)
        large = self.mem.bounded_view(long_session(200), session_id="s2",
                                      lang="en", store=self.store)
        self.assertGreater(context_budget.estimate_tokens(small), 0)
        self.assertLessEqual(
            context_budget.estimate_tokens(large),
            self.mem.DEFAULTS["summary_budget_tokens"]
            + self.mem.DEFAULTS["long_term_budget_tokens"]
            + 400,  # recent window cap
        )
        # Growth of the bounded view must stay far below linear history growth.
        ratio = context_budget.estimate_tokens(large) / max(
            1, context_budget.estimate_tokens(small))
        self.assertLess(ratio, 3.0)

    def test_pending_asks_included_in_short_term_render(self):
        pa.reset()
        msgs = [
            u("revisa bien como esta la arena ahora por favor"),
            a_tools(), tool(),
            u("cuentame como va el plugin de video"),
        ]
        pending = pa.render(pa.find_dangling_asks(msgs), "en")
        text = self.mem.render_for_turn(
            self.store, "s1", msgs, query="plugin de video",
            lang="en", pending_text=pending)
        self.assertIn("arena", text.lower())
        self.assertIn("Pending asks", text)


class TestLongTermExtract(unittest.TestCase):
    def setUp(self):
        import bot_chat_memory as mem
        self.mem = mem
        self.store = mem.MemoryStore(_tmp_store_path())

    def test_extracts_decision_and_lesson(self):
        items = self.mem.extract_memories(
            "should we keep the vault?",
            "Decision: store long-term notes in the plugin local store. "
            "Lesson learned: never depend on an external vault.",
        )
        kinds = {k for k, _ in items}
        texts = " ".join(t for _, t in items)
        self.assertIn("decision", kinds)
        self.assertIn("lesson", kinds)
        self.assertIn("plugin local store", texts)
        self.assertIn("external vault", texts)

    def test_extracts_spanish_cues(self):
        items = self.mem.extract_memories(
            "¿qué queda?",
            "Decidimos usar solo el almacén local del plugin. "
            "Lección: no atar la memoria a un vault personal.",
        )
        kinds = {k for k, _ in items}
        self.assertIn("decision", kinds)
        self.assertIn("lesson", kinds)

    def test_remember_turn_writes_and_retrieves(self):
        n = self.mem.remember_turn(
            self.store, "s1",
            messages=[u("where do we keep notes?")],
            final_response="Decision: use the plugin local store under HERMES_HOME.",
        )
        self.assertGreaterEqual(n, 1)
        hits = self.store.retrieve("plugin local store", budget_tokens=200)
        self.assertTrue(any("HERMES_HOME" in h["text"] or "local store" in h["text"]
                            for h in hits))

    def test_secret_like_text_is_not_stored(self):
        self.mem.remember_turn(
            self.store, "s1",
            final_response="Decision: the api key is sk-secret-ABCDEFG123456.",
        )
        hits = self.store.retrieve("api key decision", budget_tokens=200)
        blob = " ".join(h["text"] for h in hits)
        self.assertNotIn("sk-secret", blob)

    def test_entries_are_visible_across_sessions(self):
        self.mem.remember_turn(
            self.store, "alpha",
            final_response="Decision: ffmpeg is the only preview renderer.",
        )
        hits = self.store.retrieve("ffmpeg preview renderer", budget_tokens=200)
        self.assertTrue(any("ffmpeg" in h["text"] for h in hits))

    def test_max_entries_are_pruned(self):
        for i in range(12):
            self.store.upsert("lesson", f"Lesson number {i} about unique topic {i}.",
                              keywords=("topic", str(i)))
        self.store.prune(max_entries=5)
        self.assertLessEqual(self.store.count(), 5)


class TestHooksAndConfig(Base):
    def setUp(self):
        super().setUp()
        import bot_chat_memory as mem
        self.mem = mem
        self.home = Path(tempfile.mkdtemp(prefix="bca-mem-hook-"))
        os.environ["HERMES_HOME"] = str(self.home)
        pa.reset()

    def test_pre_llm_injects_relevant_long_term_and_summary(self):
        store = self.mem.MemoryStore(self.home / "bot-chat-agent" / "memory.db")
        store.upsert("decision", "Use token auth for the video editor service.",
                     keywords=("token", "video", "editor"))
        self.mem.update_summary(store, "bot", long_session(20),
                                recent_n=8, budget_tokens=200)
        history = long_session(20) + [u("what auth does the video editor use?")]
        out = self.ctx.hooks["pre_llm_call"](
            session_id="bot", turn_id="t1", conversation_history=history) or {}
        ctx = out.get("context") or ""
        self.assertIn("[Bot Chat style]", ctx)
        self.assertIn("token auth", ctx.lower())
        self.assertIn("Session summary", ctx)

    def test_memory_off_leaves_context_untouched(self):
        self.ctx.cfg["memory"] = {"enabled": False}
        store = self.mem.MemoryStore(self.home / "bot-chat-agent" / "memory.db")
        store.upsert("decision", "Use token auth for the video editor service.",
                     keywords=("token", "video", "editor"))
        history = [u("what auth does the video editor use?")]
        out = self.ctx.hooks["pre_llm_call"](
            session_id="bot", turn_id="t1", conversation_history=history) or {}
        ctx = out.get("context") or ""
        self.assertIn("[Bot Chat style]", ctx)
        self.assertNotIn("token auth", ctx.lower())
        self.assertNotIn("Session summary", ctx)
        self.assertNotIn("Long-term memory", ctx)

    def test_post_llm_call_v023_shape_writes_memory(self):
        history = [u("where do notes live?")]
        self.ctx.hooks["pre_llm_call"](
            session_id="bot", turn_id="t2", conversation_history=history)
        self.ctx.hooks["post_llm_call"](
            session_id="bot", turn_id="t2",
            conversation_history=history,
            final_response="Decision: keep notes in the plugin local store.",
        )
        store = self.mem.MemoryStore(self.home / "bot-chat-agent" / "memory.db")
        hits = store.retrieve("plugin local store", budget_tokens=200)
        self.assertTrue(any("local store" in h["text"] for h in hits))

    def test_post_llm_call_without_response_does_not_raise(self):
        self.ctx.hooks["post_llm_call"](session_id="bot", turn_id="t3")

    def test_injected_memory_stays_bounded_on_long_session(self):
        history = long_session(80) + [u("remind me of the plugin work so far")]
        out = self.ctx.hooks["pre_llm_call"](
            session_id="bot", turn_id="t4", conversation_history=history) or {}
        ctx = out.get("context") or ""
        # Style (~300) + dynamic note + summary (400) + ltm (400) should stay
        # well under the core hook spill cap of 10_000 chars.
        self.assertLess(len(ctx), 8000)
        self.assertLessEqual(
            context_budget.estimate_tokens(ctx), 2000)

    def test_bad_store_does_not_break_the_turn(self):
        target = self.home / "bot-chat-agent"
        target.mkdir(parents=True, exist_ok=True)
        (target / "memory.db").mkdir()  # path is a directory, not a db
        out = self.ctx.hooks["pre_llm_call"](
            session_id="bot", turn_id="t5",
            conversation_history=long_session(12) + [u("how is the plugin?")])
        self.assertIsNotNone(out)
        self.assertIn("[Bot Chat style]", out["context"])


if __name__ == "__main__":
    unittest.main()
