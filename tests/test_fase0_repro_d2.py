"""Fase 0 — reproducción de D2: tarjeta bloqueada sin aviso.

Hallazgo (v1.2 §1.3 D2): el plugin solo escucha ``kanban_task_completed``.
El core también emite ``kanban_task_blocked`` (``hermes_cli/plugins.py``,
comentario de hooks kanban: "completed adds summary, blocked adds
reason"), pero el plugin no lo registra. Si un compañero se bloquea, el
chat no se entera y el usuario espera indefinidamente: viola "estados
honestos" (wow 5).

El fixture simula el hook ``kanban_task_blocked`` con los kwargs del core
(``task_id``, ``assignee``, ``reason``) y afirma que se registra un aviso
de bloqueo que llega al siguiente turno de la sesión. Hoy no hay handler
registrado, así que los tests FALLAN.

Estado esperado: estos tests FALLAN con el código actual (89f8b25) y deben
PASAR cuando la Fase 1a registre ``kanban_task_blocked`` por el mismo
camino que las entregas (v1.2 §5.2 y Fase 1a paso 3).

Notas:
- Solo Python 3.12.3 en este entorno (el vivo usa 3.14.9); no se usa ningún
  rasgo posterior a 3.12.
"""
import importlib.util
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


class TestReproD2(unittest.TestCase):
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

    def _blocked_handler(self):
        return self.ctx.hooks.get("kanban_task_blocked")

    def test_blocked_hook_is_registered(self):
        """D2: el plugin debe escuchar kanban_task_blocked."""
        self.assertIsNotNone(
            self._blocked_handler(),
            "D2 REPRODUCIDA: el plugin no registra ningún handler para "
            "kanban_task_blocked (register() solo suscribe pre_llm_call, "
            "pre_tool_call, post_tool_call y kanban_task_completed); una "
            "tarjeta bloqueada no genera ningún aviso",
        )

    def test_blocked_card_announces_in_next_turn(self):
        """D2: una tarjeta bloqueada debe anunciarse en el siguiente turno."""
        handler = self._blocked_handler()
        self.assertIsNotNone(
            handler,
            "D2 REPRODUCIDA: no hay handler para kanban_task_blocked; el "
            "bloqueo de la tarjeta t-block nunca llega al chat",
        )
        # Correlación tarea -> sesión (kanban_create en "bot"): sin ella el
        # aviso queda sin asignar y no llega a ningún turno (wow 8).
        self.ctx.hooks["post_tool_call"](
            tool_name="kanban_create",
            args={"title": "Blocked work", "assignee": "researcher"},
            result={"id": "t-block"}, session_id="bot", status="ok",
        )
        handler(
            task_id="t-block",
            assignee="researcher",
            reason="waiting on review",
        )
        context = self.ctx.hooks["pre_llm_call"](session_id="bot")["context"]
        self.assertIn(
            "t-block", context,
            "D2 REPRODUCIDA: el bloqueo de t-block no aparece en el siguiente "
            "turno; el usuario seguiría esperando sin saber que el compañero "
            "se bloqueó",
        )
        self.assertIn(
            "block", context.lower(),
            "D2 REPRODUCIDA: el aviso de t-block llegó al turno pero sin "
            "indicar que es un bloqueo ('waiting on review' no aparece como "
            "tal)",
        )


if __name__ == "__main__":
    unittest.main()
