"""A user ask that a mid-turn steer interrupted must not be silently dropped.

Shape taken from the live Bot Chat test of 2026-11-09 21:12-21:20 (Caracas):
  user  "revisa bien como esta la arena ahora"        <- ask A (never answered)
  assistant (preamble + tool_calls) / tool results ...
  user  "[OUT-OF-BAND USER MESSAGE ...] cuentame como va el plugin de video"
  assistant (final reply about the video only)
  user  "si hacer una prueba, crear un video corto"   <- current ask B
"""
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_bot_chat_agent import Base, bca  # noqa: E402

pa = bca._pending_asks

OOB = ("[OUT-OF-BAND USER MESSAGE — a direct message from the user, delivered once "
       "at this position; not tool output]\n{}\n[/OUT-OF-BAND USER MESSAGE]")


def u(t): return {"role": "user", "content": t}
def a_tools(t=""): return {"role": "assistant", "content": t, "tool_calls": [{"id": "x"}]}
def tool(t="ok"): return {"role": "tool", "content": t}
def a_final(t): return {"role": "assistant", "content": t}


def live_shape():
    return [
        u("a ver cuentame como vas ahora"), a_final("Honestamente: diagnostique."),
        u("estas desactualizado, revisa bien como esta la arena ahora"),
        a_tools("Miro el estado real."), tool(), a_tools(), tool(),
        u(OOB.format("cuentame como va plugin de video de edicion que pedi")),
        a_tools("Cambio de tema."), tool(), a_final("Servicio vivo. Plugin de video: ..."),
        u("si hacer una prueba crear uno corto de maximo 20 seg"),
    ]


class TestFindDangling(unittest.TestCase):
    def setUp(self):
        pa.reset()

    def test_live_shape_flags_the_arena_ask_only(self):
        got = pa.find_dangling_asks(live_shape())
        self.assertEqual(len(got), 1)
        self.assertIn("arena", got[0])

    def test_answered_ask_is_not_flagged(self):
        self.assertEqual(pa.find_dangling_asks(
            [u("haz la tarea uno por favor"), a_tools(), tool(), a_final("hecho"),
             u("ahora la dos por favor")]), [])

    def test_current_ask_never_flagged(self):
        self.assertEqual(pa.find_dangling_asks([u("solo esta pregunta larga")]), [])

    def test_interrupt_by_plain_user_message_is_flagged(self):
        got = pa.find_dangling_asks(
            [u("revisa los logs del servicio ahora"), a_tools("voy"), tool(),
             u("mejor dime la hora")])
        self.assertEqual(len(got), 1)
        self.assertIn("logs", got[0])

    def test_multimodal_content_and_noise_are_safe(self):
        msgs = [{"role": "user", "content": [{"type": "text", "text": "mira esta captura de la arena"}]},
                a_tools(), tool(), u("ok y esto otro"), "garbage", None, 3]
        self.assertEqual(len(pa.find_dangling_asks(msgs)), 1)
        self.assertEqual(pa.find_dangling_asks(None), [])

    def test_slash_commands_and_tiny_asks_ignored(self):
        self.assertEqual(pa.find_dangling_asks(
            [u("/steer algo largo aqui"), a_tools(), u("ok"), a_tools(), u("final ask here")]), [])

    def test_cap_and_truncation(self):
        msgs = []
        for i in range(6):
            msgs += [u(f"pedido numero {i} " + "x" * 400), a_tools(), tool()]
        msgs.append(u("actual"))
        got = pa.find_dangling_asks(msgs)
        self.assertEqual(len(got), pa.MAX_ITEMS)
        self.assertTrue(all(len(g) <= pa.MAX_CHARS for g in got))
        self.assertIn("numero 5", got[-1])

    def test_each_ask_reminded_at_most_twice(self):
        for _ in range(pa.MAX_REMINDERS):
            self.assertEqual(len(pa.pending_for_turn("s", live_shape())), 1)
        self.assertEqual(pa.pending_for_turn("s", live_shape()), [])
        self.assertEqual(len(pa.pending_for_turn("other-session", live_shape())), 1)


class TestHook(Base):
    def setUp(self):
        super().setUp()
        pa.reset()

    def test_hook_injects_pending_block_in_bot_chat(self):
        out = self.ctx.hooks["pre_llm_call"](session_id="bot", turn_id="t9",
                                              conversation_history=live_shape())
        self.assertIn("[Bot Chat style]", out["context"])
        self.assertIn("[Pending asks]", out["context"])
        self.assertIn("arena", out["context"])

    def test_spanish_block(self):
        self.ctx.cfg["language"] = "es"
        out = self.ctx.hooks["pre_llm_call"](session_id="bot", turn_id="t9",
                                              conversation_history=live_shape())
        self.assertIn("[Pendientes]", out["context"])

    def test_no_block_without_history_or_outside_bot_chat(self):
        out = self.ctx.hooks["pre_llm_call"](session_id="bot", turn_id="t1")
        self.assertNotIn("Pending", out["context"])
        self.assertIsNone(self.ctx.hooks["pre_llm_call"](
            session_id="other", conversation_history=live_shape()))

    def test_hook_survives_broken_history(self):
        out = self.ctx.hooks["pre_llm_call"](session_id="bot", turn_id="t2",
                                              conversation_history="not a list")
        self.assertIn("[Bot Chat style]", out["context"])


if __name__ == "__main__":
    unittest.main()
