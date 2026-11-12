"""Fase 0 — reproducción de E5: consumo prematuro del aviso.

Falla (v1.2 §1.2 E5): ``take_announcements()`` se ejecuta dentro de
``team_note()``, llamada desde ``_on_pre_llm_call``: el aviso queda marcado
como anunciado cuando el turno empieza a generar, antes de que el usuario lo
vea. Si el turno se interrumpe después (sin ``post_llm_call``, sin entrega al
usuario), el aviso ya está consumido y no hay retake: ``first_take=1,
retake=0``.

El fixture simula ese turno interrumpido con los hooks reales del plugin:
entrega por ``kanban_task_completed`` -> ``pre_llm_call`` (el turno empieza)
-> interrupción (no pasa nada más) -> siguiente ``pre_llm_call`` (reapertura).
El aviso debe seguir ofreciéndose hasta que el usuario lo vea.

Estado esperado: estos tests FALLAN con el código actual (89f8b25) y deben
PASAR cuando la Fase 1a distinga "ofrecido" de "entregado" (protocolo
pending -> offered -> delivered_in_turn, v1.2 §5.2).

Fase 1a: el segundo test ya no afirma sobre ``team-notify.json`` (que el
backend sqlite no escribe) sino sobre el estado del store: tras solo
``pre_llm_call`` la entrega debe estar ``offered``, no
``delivered_in_turn``. ``_deliver`` establece primero la correlación
tarea -> sesión (``kanban_create`` en la sesión "bot"), porque sin ella el
aviso queda en la bandeja sin asignar y nunca llega a ningún turno (wow 8).
"""
import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]
if "bot_chat_agent" not in sys.modules:
    spec = importlib.util.spec_from_file_location("bot_chat_agent", ROOT / "__init__.py")
    bca = importlib.util.module_from_spec(spec)
    sys.modules["bot_chat_agent"] = bca
    spec.loader.exec_module(bca)
else:
    bca = sys.modules["bot_chat_agent"]

import delivery_store  # noqa: E402  (el __init__ del plugin ya lo puso en sys.path)


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
        c.execute(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, "
            "parent_session_id TEXT, source TEXT)"
        )
        c.executemany(
            "INSERT INTO sessions VALUES (?,?,?,?)",
            [("bot", "Bot Chat", None, "desktop")],
        )
    return p


class TestReproE5(unittest.TestCase):
    def setUp(self):
        bca._cache.clear()
        bca._questions.clear()
        self.tmp = Path(tempfile.mkdtemp())
        self.ctx = FakeCtx()
        bca.register(self.ctx)
        self.state = make_state()
        bca._state_db = lambda: self.state
        self.ctx.cfg["team"] = {"enabled": True, "gods": ["researcher"]}
        self.ctx.cfg["kanban_db"] = str(self.tmp / "missing-kanban.db")
        self.ctx.cfg["notify_state_path"] = str(self.tmp / "notify.json")
        self.notify = Path(self.ctx.cfg["notify_state_path"])

    def _deliver(self):
        """El worker entrega: hook kanban_task_completed -> record_delivery.

        Primero la correlación tarea -> sesión (kanban_create en "bot"):
        sin ella el aviso queda sin asignar y no llega a ningún turno.
        """
        self.ctx.hooks["post_tool_call"](
            tool_name="kanban_create",
            args={"title": "Patch", "assignee": "researcher"},
            result={"id": "t9"}, session_id="bot", status="ok",
        )
        self.ctx.hooks["kanban_task_completed"](
            task_id="t9", assignee="researcher", summary="Ship the patch"
        )

    def test_interrupted_turn_loses_announcement(self):
        """El aviso debe re-ofrecerse si el turno se interrumpió sin entregarlo."""
        self._deliver()
        first = self.ctx.hooks["pre_llm_call"](session_id="bot")["context"]
        self.assertIn("Ship the patch", first,
                      "el aviso debe entrar en el turno que empieza")
        # --- el turno se interrumpe aquí: no hay post_llm_call ni entrega ---
        second = self.ctx.hooks["pre_llm_call"](session_id="bot")["context"]
        self.assertIn(
            "Ship the patch", second,
            "E5 REPRODUCIDA: el turno se interrumpió tras pre_llm_call y el aviso "
            "ya no se ofrece en el siguiente turno; el usuario nunca lo vio y el "
            "resultado se perdió (first_take=1, retake=0).",
        )

    def test_announcement_marked_before_user_sees_it(self):
        """Solo pre_llm_call no debe marcar el aviso como anunciado."""
        self._deliver()
        self.ctx.hooks["pre_llm_call"](session_id="bot")
        # El turno aún no ha generado nada ni entregado nada al usuario:
        # la entrega debe estar 'offered', no 'delivered_in_turn'.
        store = delivery_store.DeliveryStore(
            delivery_store.store_path_for(self.notify))
        rows = store.get("t9")
        self.assertEqual(len(rows), 1, "t9 debe existir una sola vez en el store")
        self.assertEqual(
            rows[0]["status"], "offered",
            "E5 REPRODUCIDA: tras solo pre_llm_call, t9 ya está marcado como "
            f"entregado en el store ({rows[0]['status']}) aunque el usuario "
            "no lo ha visto.",
        )
        self.assertNotEqual(
            rows[0]["status"], "delivered_in_turn",
            "E5 REPRODUCIDA: tras solo pre_llm_call, t9 ya está "
            "delivered_in_turn aunque el turno no generó nada.",
        )


if __name__ == "__main__":
    unittest.main()
