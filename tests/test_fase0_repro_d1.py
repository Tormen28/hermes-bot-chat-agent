"""Fase 0 — reproducción de D1: aviso sin destinatario.

Hallazgo (v1.2 §1.3 D1): ``record_delivery`` no guarda ``session_id``; el
pendiente es solo ``{assignee, summary}``. ``take_announcements`` entrega
TODOS los pendientes a la primera sesión Bot Chat que arranque un turno.
Un resultado del proyecto A puede anunciarse en el chat del proyecto B:
rompe los wows 3/7 y es un problema de privacidad en modo público (wow 8).

El fixture abre 2 sesiones Bot Chat (proyecto A y proyecto B), entrega un
resultado para A y arranca un turno en B; afirma que B NO recibe el aviso
de A. Hoy B lo recibe, así que el test FALLA.

La correlación tarea -> sesión sí existe en el plugin (``remember_create``
guarda ``session_id`` + ``task_id`` en ``creates``), pero
``record_delivery`` no la usa: ese es el hueco que la Fase 1a debe cerrar
(v1.2 §5.2: destino por sesión + bandeja "sin asignar").

Estado esperado: el primer test FALLA con el código actual (89f8b25) y debe
PASAR cuando la Fase 1a dirija cada aviso a su sesión de origen. El
segundo test es el control positivo (la sesión correcta sí recibe el
aviso) y pasa hoy.

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
    """Dos sesiones Bot Chat distintas: proyecto A (bot-a) y B (bot-b)."""
    d = tempfile.mkdtemp()
    p = Path(d) / "state.db"
    with sqlite3.connect(p) as c:
        c.execute(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, "
            "parent_session_id TEXT, source TEXT)"
        )
        c.executemany(
            "INSERT INTO sessions VALUES (?,?,?,?)",
            [
                ("bot-a", "Bot Chat", None, "desktop"),
                ("bot-b", "Bot Chat", None, "desktop"),
            ],
        )
    return p


class TestReproD1(unittest.TestCase):
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

    def _deliver_for_a(self):
        """El worker entrega un resultado de la tarea del proyecto A.

        Primero la correlación tarea -> sesión (kanban_create en "bot-a"):
        sin ella el aviso queda sin asignar y no llega a ningún turno.
        """
        self.ctx.hooks["post_tool_call"](
            tool_name="kanban_create",
            args={"title": "Project A work", "assignee": "researcher"},
            result={"id": "t-a"}, session_id="bot-a", status="ok",
        )
        self.ctx.hooks["kanban_task_completed"](
            task_id="t-a", assignee="researcher", summary="Result for project A"
        )

    def test_wrong_session_does_not_receive_announcement(self):
        """D1: el turno de B no debe recibir el aviso del proyecto A."""
        self._deliver_for_a()
        context_b = self.ctx.hooks["pre_llm_call"](session_id="bot-b")["context"]
        self.assertNotIn(
            "Result for project A", context_b,
            "D1 REPRODUCIDA: el turno de la sesión B (proyecto B) recibió el "
            "aviso del proyecto A; record_delivery no guarda session_id y "
            "take_announcements entrega todos los pendientes a la primera "
            "sesión que arranque un turno",
        )

    def test_right_session_receives_announcement(self):
        """Control: la sesión A sí debe recibir su propio aviso."""
        self._deliver_for_a()
        context_a = self.ctx.hooks["pre_llm_call"](session_id="bot-a")["context"]
        self.assertIn(
            "Result for project A", context_a,
            "control: la sesión A debe recibir el aviso de su propio proyecto",
        )


if __name__ == "__main__":
    unittest.main()
