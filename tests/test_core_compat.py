"""Compat con el core vivo v0.23.0: sin API de entrega durable (B1), sin pérdida de avisos.

El ctx de v0.23.0 expone ``has_capability`` pero NO ``record_delegation_delivery``,
``record_completion_note`` ni ``read_session`` (verificado en la PC: ver
docs/AUDITORIA-LIVE-CORE.md). El plugin debe detectar la capacidad en runtime y
caer a la ruta de oferta en el siguiente turno, sin excepciones y sin perder nada,
sea cual sea ``delivery.destination``.
"""
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if "bot_chat_agent" not in sys.modules:
    spec = importlib.util.spec_from_file_location("bot_chat_agent", ROOT / "__init__.py")
    bca = importlib.util.module_from_spec(spec)
    sys.modules["bot_chat_agent"] = bca
    spec.loader.exec_module(bca)
else:
    bca = sys.modules["bot_chat_agent"]

import durable_destination as dd  # noqa: E402
import team_flow as tf  # noqa: E402


class V023Ctx:
    """PluginContext de core 0.23.0: sin superficie B1."""

    def __init__(self, cfg=None):
        self.cfg = cfg or {}
        self.hooks = {}

    def get_config(self, key, default=None):
        return self.cfg.get(key, default)

    def register_hook(self, name, fn):
        self.hooks[name] = fn

    def has_capability(self, capability):
        return False


class PartialB1Ctx(V023Ctx):
    """Core intermedio: solo una de las dos mitades de B1."""

    def record_delegation_delivery(self, *a, **k):  # pragma: no cover - nunca debe llamarse
        raise AssertionError("no debe llamarse con B1 incompleto")


class RaisingCtx(V023Ctx):
    @property
    def record_delegation_delivery(self):
        raise RuntimeError("attribute access exploded")


class TestLiveCoreDegradation(unittest.TestCase):
    def _setup(self, destination, ctx_cls=V023Ctx):
        self.tmp = Path(tempfile.mkdtemp())
        self.notify = self.tmp / "team-notify.json"
        self.ctx = ctx_cls({
            "scope": "all",
            "team": {"enabled": True, "assignees": ["BSK"]},
            "notify_state_path": str(self.notify),
            "delivery": {"backend": "sqlite", "destination": destination},
        })
        bca.register(self.ctx)

    def _deliver(self, task_id="t1"):
        tf.remember_create(self.notify, session_id="bot", title="t", assignee="BSK", idempotency="",
                           result={"id": task_id}, gods=["BSK"], backend="sqlite")
        tf.record_delivery(self.notify, task_id=task_id, assignee="BSK", summary="all green: 3 files",
                           gods=["BSK"], backend="sqlite")

    def test_detects_missing_capability_in_every_mode(self):
        for mode in ("offer", "auto", "b1", "nonsense"):
            self._setup(mode)
            info = bca.delivery_path()
            self.assertFalse(info["b1_present"], mode)
            self.assertEqual(info["active_path"], "offer", mode)

    def test_partial_or_exploding_ctx_is_treated_as_no_b1(self):
        for cls in (PartialB1Ctx, RaisingCtx):
            self._setup("auto", cls)
            self.assertEqual(bca.delivery_path()["active_path"], "offer", cls.__name__)

    def test_no_loss_offer_then_confirm(self):
        for mode in ("auto", "b1"):
            self._setup(mode)
            self._deliver("t-" + mode)
            counts = tf.relay_pending(self.notify, ctx=self.ctx, destination=mode, backend="sqlite")
            self.assertEqual(counts["active_path"], "offer")
            self.assertEqual(counts["delivered"], 0)
            self.assertEqual(counts["failed"], 0)
            note = tf.team_note(lang="en", enabled=True, gods=["BSK"], wait_for_god=False,
                                session_id="bot", db_path=None, notify_path=self.notify,
                                delivery_backend="sqlite")
            self.assertIn("3 files", note)
            tf.confirm_turn(self.notify, "bot", None, backend="sqlite")
            again = tf.team_note(lang="en", enabled=True, gods=["BSK"], wait_for_god=False,
                                 session_id="bot", db_path=None, notify_path=self.notify,
                                 delivery_backend="sqlite")
            self.assertNotIn("all green", again, "ya confirmado: no se repite")

    def test_cancelled_turn_re_offers_instead_of_losing(self):
        self._setup("auto")
        self._deliver("t-cancel")
        first = tf.team_note(lang="en", enabled=True, gods=["BSK"], wait_for_god=False,
                             session_id="bot", db_path=None, notify_path=self.notify,
                             delivery_backend="sqlite")
        self.assertIn("3 files", first)
        # el turno se cancela: nunca llega post_llm_call; el stop libera la oferta
        tf.release_session(self.notify, "bot", backend="sqlite")
        second = tf.team_note(lang="en", enabled=True, gods=["BSK"], wait_for_god=False,
                              session_id="bot", db_path=None, notify_path=self.notify,
                              delivery_backend="sqlite")
        self.assertIn("3 files", second, "el aviso reaparece tras un turno cancelado")

    def test_relay_hooks_never_raise_without_b1(self):
        self._setup("b1")
        self._deliver("t-hook")
        for hook, kw in (("post_llm_call", {"session_id": "bot", "turn_id": "x"}),
                         ("agent_loop_stopped", {"session_key": "k", "session_id": "bot"})):
            self.assertIsNone(self.ctx.hooks[hook](**kw))


if __name__ == "__main__":
    unittest.main()
