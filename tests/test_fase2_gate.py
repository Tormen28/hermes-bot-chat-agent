"""Fase 2 gate (rubric docs/fase2-rubric.md v1.0, level A, offline).

Corpus fijo: 8 restricciones críticas, 20 hechos, 3 ediciones manuales.
Puerta: presupuesto respetado, 100 % de restricciones críticas y
≥ 95 % de hechos en el contexto reconstruido, 100 % de ediciones
manuales respetadas, nota incremental y reconstrucción tras cancelación,
compactación y reapertura.
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

import context_budget  # noqa: E402
import context_engine as _ce  # noqa: E402
import context_sections  # noqa: E402
import project_card  # noqa: E402

CONSTRAINTS = [
    "Nunca borrar la rama main sin aprobación explícita.",
    "Todo resultado debe tener un ID persistido antes de anunciarse.",
    "Ningún secreto sale del chat en ningún mensaje.",
    "Las tarjetas bloqueadas se anuncian en menos de 1 s.",
    "El trabajo de fondo nunca se ejecuta dentro del plugin.",
    "La ficha de proyecto manda sobre cualquier texto derivado.",
    "Cada entrega llega solo a su sesión de origen.",
    "Sin doble despacho: una tarjeta, un trabajador.",
]
OBJECTIVE = "Entregar el plugin Bot Chat con entregas fiables entre procesos."
DECISIONS = [
    "Almacén SQLite con leases, no JSON con lock.",
    "El lease es el mecanismo; el hook solo acelera.",
    "Destino conmutable oferta/B1 según el core.",
    "La nota de equipo es incremental desde la Fase 2.",
]
PENDING = [
    "Medir la puerta de 10.000 eventos en Python 3.14.9.",
    "Revisar el diff de B1+B5 con Gustavo.",
    "Documentar context.engine: bot-chat para el engine.",
    "Probar la reconstrucción tras compactación.",
]
RESULT_IDS = ["t-101", "t-102", "t-103"]


class FakeCtx:
    def __init__(self, cfg=None):
        self.cfg = cfg or {}
        self.hooks = {}

    def get_config(self, key, default=None):
        return self.cfg.get(key, default)

    def register_hook(self, name, fn):
        self.hooks[name] = fn


class FakeCompressor:
    """Stand-in for the core ContextCompressor (records what it gets)."""

    def __init__(self):
        self.calls = []
        self.compression_count = 0
        self.last_prompt_tokens = 0
        self.last_completion_tokens = 0
        self.last_total_tokens = 0

    def update_from_response(self, usage):
        self.last_prompt_tokens = int(usage.get("prompt_tokens") or 0)
        self.last_completion_tokens = int(usage.get("completion_tokens") or 0)
        self.last_total_tokens = int(usage.get("total_tokens") or 0)

    def should_compress(self, prompt_tokens=None):
        return True

    def compress(self, messages, current_tokens=None, focus_topic=None,
                 force=False, memory_context=""):
        self.calls.append({"memory_context": memory_context, "n": len(messages)})
        self.compression_count += 1
        return [{"role": "user", "content": "resumen compactado"}]


def build_home(cards=True):
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
        if cards:
            c.executemany(
                "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?)",
                [("t-card-1", "Card Alpha", "BSK", "open", "", "", "s1", ""),
                 ("t-card-2", "Card Beta", "JEV", "working", "", "", "s1", ""),
                 ("t-card-3", "Card Gamma", "BSK", "open", "", "", "s1", "")])
    notify = home / "bot-chat-agent" / "team-notify.json"
    notify.parent.mkdir(parents=True, exist_ok=True)
    return home, kb, notify


def make_card(notify):
    return project_card.edit_card(
        project_card.card_path_for(notify), author="gustavo",
        objective=OBJECTIVE, add_constraints=list(CONSTRAINTS),
        add_decisions=list(DECISIONS), add_pending=list(PENDING),
        add_result_ids=list(RESULT_IDS))


def seed_deliveries(notify, n=3, prefix="result"):
    # Task ids ARE the card's result IDs (plan §6: detail by ID).
    for i in range(n):
        bca.team_flow.record_delivery(
            notify, task_id=RESULT_IDS[i], assignee="BSK",
            summary=f"{prefix}-{chr(97 + i)}", gods=["BSK", "JEV"],
            session_id="s1")
    # One unassigned delivery: the tray fact.
    bca.team_flow.record_delivery(
        notify, task_id="t-299", assignee="BSK", summary="result-tray",
        gods=["BSK", "JEV"], session_id="")


def seed_effects(notify):
    store = bca._context_store()
    store.record_tool_effect("s1", "delete_file", "old.tmp")
    store.record_tool_effect("s1", "send_message", "aviso al canal")
    store.record_tool_effect("s1", "kanban_create", "t-card-9")


FACTS = (
    [OBJECTIVE] + DECISIONS + PENDING
    + ["Card Alpha", "Card Beta", "Card Gamma"]
    + ["result-a", "result-b", "result-c"]
    + ["sin sesión conocida", "BSK, JEV"]
    + RESULT_IDS
)
assert len(FACTS) == 20, len(FACTS)


def coverage(text):
    missing_c = [c for c in CONSTRAINTS if c not in text]
    missing_f = [f for f in FACTS if f not in text]
    return len(CONSTRAINTS) - len(missing_c), len(FACTS) - len(missing_f), missing_c, missing_f


class TestFase2Gate(unittest.TestCase):
    def setUp(self):
        self.home, self.kb, self.notify = build_home()
        os.environ["HERMES_HOME"] = str(self.home)
        bca._state_db = lambda: self.home / "state.db"
        bca.register(FakeCtx(cfg={
            "language": "en",
            "team": {"enabled": True, "gods": ["BSK", "JEV"]},
            "kanban_db": str(self.kb),
            "notify_state_path": str(self.notify),
        }))
        _ce._ENGINE_SESSIONS.clear()

    def tearDown(self):
        _ce._ENGINE_SESSIONS.clear()

    def _full_state(self):
        make_card(self.notify)
        seed_deliveries(self.notify)
        seed_effects(self.notify)

    # ---------------------------------------------------------- budgets
    def test_stable_section_within_300_tokens(self):
        for lang in ("en", "es"):
            self.assertLessEqual(
                context_budget.estimate_tokens(bca.STYLE[lang].rstrip()), 300,
                f"STYLE[{lang}] supera el presupuesto estable")

    def test_heavy_scenario_within_budgets(self):
        self._full_state()
        for i in range(30):
            bca.team_flow.record_delivery(
                self.notify, task_id=f"t-30{i}", assignee="JEV",
                summary=f"extra-{i}", gods=["BSK", "JEV"], session_id="s1")
        built = bca._build_turn_context("s1", turn_id="turn-1")
        check = context_budget.check_budgets(built["stable"], built["dynamic"])
        self.assertTrue(check["ok"], f"presupuesto roto: {check}")
        self.assertEqual(built["dropped"], [], f"se recortó: {built['dropped']}")
        n_c, n_f, missing_c, missing_f = coverage(built["dynamic"])
        self.assertEqual(n_c, 8, f"restricciones fuera: {missing_c}")
        self.assertGreaterEqual(n_f, 19, f"hechos fuera: {missing_f}")

    def test_over_budget_trims_low_priority_never_constraints(self):
        self._full_state()
        long_summary = "x" * 240
        for i in range(30):
            bca.team_flow.record_delivery(
                self.notify, task_id=f"t-40{i}", assignee="JEV",
                summary=long_summary, gods=["BSK", "JEV"], session_id="s1")
        built = bca._build_turn_context("s1", turn_id="turn-1")
        self.assertLessEqual(built["tokens"], 1500, "dinámica sobre presupuesto")
        self.assertTrue(built["dropped"], "nada recortado con sobrecarga")
        for c in CONSTRAINTS:
            self.assertIn(c, built["dynamic"], "restricción recortada")

    def test_estimator_chars4_without_tiktoken(self):
        self.assertEqual(context_budget.estimator_name(), "chars/4")
        self.assertEqual(context_budget.estimate_tokens("abcd"), 1)
        self.assertEqual(context_budget.estimate_tokens("abcdefgh"), 2)
        self.assertEqual(context_budget.estimate_tokens(""), 0)

    # ------------------------------------------------------ incremental note
    def test_no_change_turn_is_one_short_line(self):
        self._full_state()
        first = bca._build_turn_context("s1", turn_id="turn-1")
        self.assertTrue(first["full"], "primer turno debe ser completo")
        bca._on_post_llm_call(session_id="s1", turn_id="turn-1")
        second = bca._build_turn_context("s1", turn_id="turn-2")
        self.assertFalse(second["full"], "segundo turno debe ser delta")
        self.assertLessEqual(second["tokens"], 15,
                             f"turno sin cambios no es corto: {second['dynamic']!r}")
        self.assertIn("No changes", second["dynamic"])

    def test_delta_only_what_changed(self):
        self._full_state()
        bca._build_turn_context("s1", turn_id="turn-1")
        bca._on_post_llm_call(session_id="s1", turn_id="turn-1")
        with sqlite3.connect(self.kb) as c:
            c.execute("UPDATE tasks SET status='done' WHERE id='t-card-1'")
        bca.team_flow.record_delivery(
            self.notify, task_id="t-210", assignee="JEV", summary="result-new",
            gods=["BSK", "JEV"], session_id="s1")
        second = bca._build_turn_context("s1", turn_id="turn-2")
        self.assertIn("result-new", second["dynamic"])
        self.assertIn("t-card-1", second["dynamic"], "cambio de tarjeta ausente")
        self.assertNotIn("result-a", second["dynamic"],
                         "aviso ya confirmado reaparece en el delta")
        self.assertNotIn("Card Beta", second["dynamic"],
                         "tarjeta sin cambio reaparece en el delta")

    def test_interrupted_turn_reoffers_unconfirmed(self):
        self._full_state()
        bca._build_turn_context("s1", turn_id="turn-1")
        # No post_llm_call: turno interrumpido, nada confirmado.
        bca._on_agent_loop_stopped(session_id="s1")
        second = bca._build_turn_context("s1", turn_id="turn-2")
        self.assertTrue(second["full"], "tras cancelar debe reconstruir completo")
        for i in ("a", "b", "c"):
            self.assertIn(f"result-{i}", second["dynamic"],
                          "aviso no confirmado perdido tras cancelar")

    # ------------------------------------------------------ reconstruction
    def test_reconstruction_after_cancel(self):
        self._full_state()
        bca._build_turn_context("s1", turn_id="turn-1")
        bca._on_agent_loop_stopped(session_id="s1")
        rebuilt = bca._build_turn_context("s1", turn_id="turn-2")
        n_c, n_f, missing_c, missing_f = coverage(rebuilt["dynamic"])
        self.assertEqual(n_c, 8, f"restricciones fuera tras cancelar: {missing_c}")
        self.assertGreaterEqual(n_f, 19, f"hechos fuera tras cancelar: {missing_f}")

    def test_reconstruction_after_reopen(self):
        self._full_state()
        bca._build_turn_context("s1", turn_id="turn-1")
        bca._on_post_llm_call(session_id="s1", turn_id="turn-1")
        # Proceso nuevo: otra conexión al mismo store, sin estado en memoria.
        store2 = bca._context_store()
        rebuilt = context_sections.render_dynamic(
            lang="en", gods=["BSK", "JEV"], wait_for_god=True,
            session_id="s1", db_path=self.kb, notify_path=self.notify,
            card=bca._load_project_card(),
            announcements=bca.team_flow.offer_for_session(
                self.notify, "s1", turn_id="turn-9"),
            unassigned_count=bca.team_flow.unassigned_count(self.notify),
            store=store2, force_full=True)
        n_c, n_f, missing_c, missing_f = coverage(rebuilt["text"])
        self.assertEqual(n_c, 8, f"restricciones fuera tras reabrir: {missing_c}")
        self.assertGreaterEqual(n_f, 19, f"hechos fuera tras reabrir: {missing_f}")

    def test_compaction_protects_constraints_and_ids(self):
        self._full_state()
        fake = FakeCompressor()
        engine = _ce.BotChatContextEngine(
            build_context=lambda sid: bca._build_turn_context(
                sid, turn_id=f"engine:{sid}"),
            protected_block=bca._protected_block,
            clear_fingerprint=bca._clear_engine_fingerprint)
        engine._delegate = fake
        engine.on_session_start("s1")
        bca._build_turn_context("s1", turn_id="turn-1")
        out = engine.compress([{"role": "user", "content": "largo"}],
                              memory_context="")
        self.assertEqual(out, [{"role": "user", "content": "resumen compactado"}])
        self.assertEqual(len(fake.calls), 1)
        block = fake.calls[0]["memory_context"]
        for c in CONSTRAINTS:
            self.assertIn(c, block, "restricción fuera del bloque protegido")
        for rid in RESULT_IDS:
            self.assertIn(rid, block, "ID de resultado fuera del bloque")
        self.assertIn("delete_file", block, "log de efectos fuera del bloque")
        # Tras compactar, la huella se borra: el turno siguiente reconstruye.
        self.assertEqual(bca._context_store().load_context_state("s1"), "")
        rebuilt = bca._build_turn_context("s1", turn_id="turn-2")
        self.assertTrue(rebuilt["full"])
        n_c, n_f, missing_c, missing_f = coverage(rebuilt["dynamic"])
        self.assertEqual(n_c, 8, f"restricciones fuera tras compactar: {missing_c}")
        self.assertGreaterEqual(n_f, 19, f"hechos fuera tras compactar: {missing_f}")

    # ------------------------------------------------------ project card
    def test_manual_edits_win_and_are_versioned(self):
        card = make_card(self.notify)
        self.assertEqual(card.version, 1)
        before = bca._build_turn_context("s1", turn_id="turn-1")
        self.assertIn(CONSTRAINTS[0], before["dynamic"])
        # Tres ediciones manuales: cambiar una restricción, el objetivo y
        # añadir una restricción (y quitar otra).
        project_card.edit_card(
            project_card.card_path_for(self.notify), author="gustavo",
            remove_constraints=[CONSTRAINTS[0]],
            add_constraints=["Nunca anunciar un resultado sin su ID."],
            objective="Entregar Bot Chat con cero entregas perdidas.")
        project_card.edit_card(
            project_card.card_path_for(self.notify), author="gustavo",
            add_constraints=["La nota incremental nunca reescribe la ficha."])
        card2 = project_card.load_card(
            project_card.card_path_for(self.notify),
            store=bca._context_store())
        self.assertEqual(card2.version, 3)
        self.assertEqual(len(bca._context_store().card_versions("")), 3)
        after = bca._build_turn_context("s1", turn_id="turn-2")
        self.assertIn("Nunca anunciar un resultado sin su ID.", after["dynamic"])
        self.assertIn("La nota incremental nunca reescribe la ficha.", after["dynamic"])
        self.assertIn("cero entregas perdidas", after["dynamic"])
        self.assertNotIn(CONSTRAINTS[0], after["dynamic"],
                         "texto derivado/edición vieja sigue presente")
        # 7 original constraints left + 2 new ones = 9 in the card now.
        for c in CONSTRAINTS[1:]:
            self.assertIn(c, after["dynamic"], f"restricción perdida: {c}")

    def test_derived_content_never_overwrites_the_card(self):
        card = make_card(self.notify)
        path = project_card.card_path_for(self.notify)
        digest_before = path.read_bytes()
        # Renderizar y proteger no tocan el archivo.
        bca._build_turn_context("s1", turn_id="turn-1")
        bca._protected_block("s1")
        self.assertEqual(path.read_bytes(), digest_before)
        self.assertEqual(len(bca._context_store().card_versions("")), 1)
        # Una edición manual en el editor sí versiona.
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace(OBJECTIVE, OBJECTIVE + " (revisado)"),
                        encoding="utf-8")
        reloaded = project_card.load_card(path, store=bca._context_store())
        self.assertEqual(reloaded.version, 2)
        self.assertIn("(revisado)", reloaded.objective)

    # ------------------------------------------------------ engine
    def test_engine_select_context_bounded_no_duplicate(self):
        self._full_state()
        engine = _ce.BotChatContextEngine(
            build_context=lambda sid: bca._build_turn_context(
                sid, turn_id=f"engine:{sid}"),
            protected_block=bca._protected_block,
            clear_fingerprint=bca._clear_engine_fingerprint)
        engine.on_session_start("s1")
        self.assertTrue(_ce.is_engine_active("s1"))
        self.assertIsNone(bca._on_pre_llm_call(session_id="s1", turn_id="t1"),
                          "el hook debe ceder ante el engine activo")
        request = [{"role": "system", "content": "sys"},
                   {"role": "user", "content": "hola"}]
        out = engine.select_context(request)
        self.assertIsNotNone(out, "select_context no devolvió nada")
        self.assertEqual(len(out), 3)
        appended = out[-1]["content"]
        check = context_budget.check_budgets("", appended)
        self.assertLessEqual(check["dynamic_tokens"], 1800,
                             "contexto del engine sobre presupuesto")
        self.assertIn("[Bot Chat style]", appended)
        # Segunda llamada con el estilo ya presente: no duplica.
        self.assertIsNone(engine.select_context(out))
        engine.on_session_end("s1", [])
        self.assertFalse(_ce.is_engine_active("s1"))
        self.assertIsNotNone(bca._on_pre_llm_call(session_id="s1", turn_id="t2"),
                             "tras cerrar sesión el hook debe volver")

    def test_engine_clone_is_independent(self):
        engine = _ce.BotChatContextEngine(model="m1")
        engine.last_prompt_tokens = 111
        clone = engine.clone_for_agent()
        self.assertIsNot(clone, engine)
        self.assertIsNone(clone._delegate)
        self.assertEqual(clone.last_prompt_tokens, 111)
        clone.last_prompt_tokens = 999
        clone._model = "m2"
        self.assertEqual(engine.last_prompt_tokens, 111)
        self.assertEqual(engine._model, "m1")

    def test_engine_update_from_response_forwards(self):
        fake = FakeCompressor()
        engine = _ce.BotChatContextEngine()
        engine._delegate = fake
        engine.update_from_response({"prompt_tokens": 10, "completion_tokens": 5,
                                     "total_tokens": 15})
        self.assertEqual(engine.last_total_tokens, 15)
        self.assertEqual(fake.last_prompt_tokens, 10)

    def test_tool_effects_recorded_by_hook(self):
        store = bca._context_store()
        bca._on_post_tool_call(tool_name="delete_file",
                               args={"path": "old.tmp"}, session_id="s1",
                               status="ok")
        bca._on_post_tool_call(tool_name="read_file",
                               args={"path": "a.txt"}, session_id="s1",
                               status="ok")
        effects = store.tool_effects("s1")
        self.assertEqual([e["tool_name"] for e in effects], ["delete_file"])
        block = bca._protected_block("s1")
        self.assertIn("delete_file", block)


if __name__ == "__main__":
    unittest.main()
