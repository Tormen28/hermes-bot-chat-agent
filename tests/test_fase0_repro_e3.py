"""Fase 0 — reproducción de E3: errores de entrega tragados en silencio.

Falla (v1.2 §1.2 E3): ``_on_kanban_completed`` (``__init__.py``) captura la
excepción de ``record_delivery`` y la registra solo en ``logger.debug``.
Una entrega fallida desaparece sin dejar rastro visible: viola "un timeout
deja un estado recuperable y explicado" y hace imposible contabilizar el
fallo (la Fase 1b exige 0 ``PermissionError`` sin manejar, y un error que
solo existe en debug es un error sin manejar).

El fixture provoca un fallo real de escritura en ``record_delivery`` y
afirma que queda visible (nivel warning o superior). Hoy solo hay un
``debug``, así que el test FALLA.

La ruta no escribible es determinista incluso como root: el padre de
``team-notify.json`` es un ARCHIVO, no un directorio, así que
``_save_state`` levanta ``FileExistsError`` (subclase de ``OSError``) en
``mkdir``.

Estado esperado: este test FALLA con el código actual (89f8b25) y debe
PASAR cuando la Fase 1a registre los fallos de entrega en warning (o los
contabilice) en vez de tragarlos en debug.

Notas:
- Solo Python 3.12.3 en este entorno (el vivo usa 3.14.9); no se usa ningún
  rasgo posterior a 3.12.
"""
import importlib.util
import logging
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


class TestReproE3(unittest.TestCase):
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
        # Padre de team-notify.json = un archivo: mkdir levanta FileExistsError.
        blocker = self.tmp / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        self.ctx.cfg["notify_state_path"] = str(blocker / "team-notify.json")
        self.notify = Path(self.ctx.cfg["notify_state_path"])

    def test_failed_delivery_is_visible(self):
        """E3: un fallo de record_delivery debe quedar en warning o superior."""
        records: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(self, record):  # noqa: N802 - firma de logging.Handler
                records.append(record)

        handler = _Capture(level=logging.DEBUG)
        logger = logging.getLogger("bot_chat_agent")
        logger.addHandler(handler)
        try:
            self.ctx.hooks["kanban_task_completed"](
                task_id="t1", assignee="researcher", summary="Ship the patch"
            )
        finally:
            logger.removeHandler(handler)

        # Primero: el fixture provocó de verdad el fallo (si esto falla, el
        # problema es el fixture, no el plugin).
        self.assertFalse(
            self.notify.exists(),
            "el fixture no provocó el fallo de escritura: la entrega quedó "
            "registrada pese a la ruta no escribible",
        )
        visible = [r for r in records if r.levelno >= logging.WARNING]
        self.assertTrue(
            visible,
            "E3 REPRODUCIDA: record_delivery falló (la entrega t1 no quedó "
            "registrada) y el único rastro es un logger.debug en "
            "_on_kanban_completed ('delivery note skipped'); ningún registro "
            "de nivel warning o superior, ninguna contabilidad del fallo. "
            f"Registros capturados: {[(r.levelname, r.getMessage()) for r in records]}",
        )


if __name__ == "__main__":
    unittest.main()
