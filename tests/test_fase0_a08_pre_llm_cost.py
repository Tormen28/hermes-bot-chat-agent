"""Fase 0 (A0.8) — coste del contexto inyectado por ``pre_llm_call``.

Medición (Python 3.12.3; el vivo usa 3.14.9, repetir allí): se llama al hook
real con un HERMES_HOME temporal (sesión 'Bot Chat', kanban.db con tarjetas,
team-notify.json con un aviso) y se mide el ``context`` devuelto.

Método de estimación de tokens: chars/4 (aproximación estándar sin
tokenizer; ``tiktoken`` NO está instalado en este entorno y no se instala
nada de red). Contraste: palabras x 1,33 da cifras coherentes (±5 %).

Resultados medidos el 2026-11-08 (ver ``/tmp/a08_measure.py``):
  STYLE solo:        en 1134 c / ~283 tok · es 1115 c / ~278 tok
  Caso A (sin equipo): en 1210 c / ~302 tok · es 1204 c / ~301 tok
  Caso B (equipo, vacío): en 1327 c / ~331 tok · es 1312 c / ~328 tok
  Caso C (2 tarjetas + 1 aviso): en 1523 c / ~380 tok · es 1429 c / ~357 tok

Re-medido en Fase 1a (2026-11-08, mismo método): A y B sin cambios;
  Caso C: en 1561 c / ~390 tok · es 1530 c / ~382 tok. Sube porque el
  aviso de la semilla no trae correlación tarea -> sesión, así que ya no
  se anuncia en el turno: va a la bandeja sin asignar (línea de bandeja)
  y el resumen llega entre delimitadores con su marco de dato. La
  conclusión no cambia: el presupuesto dinámico de 1500 no se acerca a
  agotarse; el de estilo de 300 está al límite y se supera con la nota
  de equipo.

Lectura frente al presupuesto v1.1 (300 estilo + 1500 dinámicos): el STYLE
solo ya roza los 300 de estilo, y la inyección completa por turno es de
~300–380 tokens en TODOS los casos, en CADA turno (la nota se recalcula
siempre, D5). El presupuesto dinámico de 1500 no se acerca a agotarse; el
de estilo de 300 está al límite y se supera con la nota de equipo.

Dónde cae (core 0f0e96f, ``agent/turn_context.py``): ``_collect_pre_llm_call_context``
(``turn_context.py:904``) dice "their context is injected into the user
message (never the system prompt)"; ``_merge_gateway_notes`` (``~967``) lo
lleva "por el canal de inyección del mensaje de usuario (one-shot)" para
que "el system prompt efímero se mantenga byte-stable". El system prompt
sigue cacheado; el mensaje de usuario cambia cada turno, así que el
prefijo cacheado se rompe en el punto de inyección en cada turno.
El core además limita cada salida de hook a 10.000 caracteres
(``tools/hook_output_spill.py`` DEFAULT_MAX_CHARS = 10_000): el plugin,
con ~1,5 k, está muy por debajo.

Invariante de caché del core (0f0e96f, ``AGENTS.md:20``): "Per-conversation
prompt caching is sacred. Mutating past context, swapping toolsets,
reloading memories or rebuilding the system prompt mid-conversation breaks
the cached prefix and multiplies the user's cost; the ONE exception is
context compression."
"""
import importlib.util
import json
import os
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


def build_home(with_cards=False, with_pending=False):
    home = Path(tempfile.mkdtemp())
    with sqlite3.connect(home / "state.db") as c:
        c.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, "
                  "parent_session_id TEXT, source TEXT)")
        c.execute("INSERT INTO sessions VALUES ('s1','Bot Chat',NULL,'chat')")
    kb = home / "kanban.db"
    with sqlite3.connect(kb) as c:
        c.execute("CREATE TABLE tasks (id TEXT, title TEXT, assignee TEXT, "
                  "status TEXT, result TEXT, started_at TEXT, session_id TEXT, "
                  "idempotency_key TEXT)")
        if with_cards:
            c.executemany(
                "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?)",
                [("t1", "Investigar el fallo de entrega", "BSK", "open", "", "", "s1", ""),
                 ("t2", "Escribir el informe de la Fase 0", "BSK", "working", "", "", "s1", "")])
    notify = home / "team-notify.json"
    if with_pending:
        notify.write_text(json.dumps(
            {"pending": {"t9": {"assignee": "BSK", "summary": "Patch enviado."}},
             "announced": [], "creates": []}), encoding="utf-8")
    return home, kb, notify


def inject(cfg, home):
    # Igual que los tests propios del repo (test_bot_chat_agent.py): se
    # parchea bca._state_db para apuntar a la state.db temporal, porque
    # ese módulo lo deja parcheado a su propia DB y nunca lo restaura.
    os.environ["HERMES_HOME"] = str(home)
    bca._state_db = lambda: home / "state.db"
    bca.register(FakeCtx(cfg=cfg))
    out = bca._on_pre_llm_call(session_id="s1") or {}
    return out.get("context", ""), out


def est_tokens(text):
    return len(text) // 4


class TestPreLlmCost(unittest.TestCase):
    def test_style_under_1200_chars(self):
        for lang in ("en", "es"):
            self.assertLess(len(bca.STYLE[lang].rstrip()), 1200,
                            f"STYLE[{lang}] supera 1200 caracteres")

    def test_case_a_single_operator_under_1300_chars(self):
        home, kb, notify = build_home()
        for lang in ("en", "es"):
            ctx, _ = inject({"language": lang,
                             "team": {"enabled": True, "gods": []},
                             "kanban_db": str(kb),
                             "notify_state_path": str(notify)}, home)
            self.assertLess(len(ctx), 1300, f"A[{lang}] supera 1300 caracteres")
            print(f"A[{lang}]: chars={len(ctx)} tok~{est_tokens(ctx)}")

    def test_case_b_team_empty_under_1400_chars(self):
        home, kb, notify = build_home()
        for lang in ("en", "es"):
            ctx, _ = inject({"language": lang,
                             "team": {"enabled": True, "gods": ["BSK"]},
                             "kanban_db": str(kb),
                             "notify_state_path": str(notify)}, home)
            self.assertLess(len(ctx), 1400, f"B[{lang}] supera 1400 caracteres")
            print(f"B[{lang}]: chars={len(ctx)} tok~{est_tokens(ctx)}")

    def test_case_c_team_busy_under_1700_chars(self):
        home, kb, notify = build_home(with_cards=True, with_pending=True)
        for lang in ("en", "es"):
            ctx, _ = inject({"language": lang,
                             "team": {"enabled": True, "gods": ["BSK"]},
                             "kanban_db": str(kb),
                             "notify_state_path": str(notify)}, home)
            self.assertLess(len(ctx), 1700, f"C[{lang}] supera 1700 caracteres")
            print(f"C[{lang}]: chars={len(ctx)} tok~{est_tokens(ctx)}")

    def test_context_key_not_system_prompt(self):
        # El plugin devuelve {"context": ...}; el core lo inyecta en el
        # mensaje de usuario, nunca en el system prompt (turn_context.py).
        home, kb, notify = build_home()
        _, out = inject({"language": "en",
                         "team": {"enabled": True, "gods": []},
                         "kanban_db": str(kb),
                         "notify_state_path": str(notify)}, home)
        self.assertIn("context", out)
        self.assertNotIn("system_prompt", out)
        self.assertNotIn("system", out)


if __name__ == "__main__":
    unittest.main()
