"""Fase 4 — puerta de avance (v1.2 §4, revisión manual en docs/fase4-review.md).

Una versión pública y personal: un solo paquete, modo individual en
instalaciones nuevas y equipo opcional; un nuevo default NO desactiva el
equipo de una instalación existente.

Criterios de la puerta:

- ``team.gods`` -> ``team.assignees``: la compatibilidad se define desde
  cero. Solo ``assignees`` o solo ``gods`` funciona; si las dos claves
  existen y difieren, error explicativo (nunca una elección silenciosa).
- El default nuevo es individual (sin clave ``team`` no hay equipo), pero
  una instalación existente con compañeros configurados sigue en equipo:
  ``enabled`` ausente se deriva de que haya compañeros listados.
- Convivencia con Panteón, JEV, Tormen y BSK (los cuatro perfiles de la
  configuración real): asignación, espera de la guarda y entrega por
  sesión, sin inventar compañeros ni cruzar sesiones.
- Autorizaciones intactas: la guarda sigue bloqueando con la configuración
  rota, y un conflicto de configuración falla cerrado (bloqueo explicativo)
  en vez de desactivar el equipo o la guarda en silencio.
- Degradación declarada si faltan hooks del core: sin ``post_llm_call``
  no se confirma nada (el lease re-ofrece, at-least-once); sin
  ``agent_loop_stopped`` solo queda el lease; sin ``kanban_task_blocked``
  no se inventa ningún aviso de bloqueado (la tarjeta sigue visible en
  el tablero).
- Piso de versión del core: ``requires_hermes`` nombra la versión que
  tiene los hooks que se usan (``post_llm_call``, ``agent_loop_stopped``,
  ``kanban_task_blocked``), verificada contra el tag de esa versión en
  el checkout vivo del core; los tres hooks existen en el vivo
  ``2bd378ed`` (v0.23.0).

Solo Linux/fork y Python 3.12.3 en este entorno (el vivo usa 3.14.9);
no se usa ningún rasgo posterior a 3.12.
"""
import importlib.util
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
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

import delivery_store  # noqa: E402
import team_config  # noqa: E402
import team_flow as tf  # noqa: E402

CORE_CHECKOUT = Path(os.environ.get(
    "HERMES_AGENT_CHECKOUT",
    Path.home() / "workspace/hermes-work/hermes-agent"))
CORE_REV = "2bd378ed"
REQUIRED_HOOKS = ("post_llm_call", "agent_loop_stopped", "kanban_task_blocked")
FOUR_PROFILES = ["Panteón", "JEV", "Tormen", "BSK"]


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
        c.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, parent_session_id TEXT, source TEXT)")
        c.executemany("INSERT INTO sessions VALUES (?,?,?,?)", [
            ("bot", "Bot Chat", None, "desktop"),
            ("bot2", "Bot Chat", None, "desktop"),
        ])
    return p


def make_kanban(tasks, links=None):
    d = tempfile.mkdtemp()
    p = Path(d) / "kanban.db"
    with sqlite3.connect(p) as c:
        c.execute(
            "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, assignee TEXT, status TEXT, "
            "result TEXT, started_at INTEGER, session_id TEXT, idempotency_key TEXT)"
        )
        c.execute("CREATE TABLE task_links (parent_id TEXT, child_id TEXT)")
        for row in tasks:
            c.execute(
                "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?)",
                (
                    row["id"], row.get("title"), row.get("assignee"), row.get("status"),
                    row.get("result"), row.get("started_at"), row.get("session_id"),
                    row.get("idempotency_key"),
                ),
            )
        for parent, child in links or []:
            c.execute("INSERT INTO task_links VALUES (?,?)", (parent, child))
    return p


class TeamBase(unittest.TestCase):
    def setUp(self):
        bca._cache.clear()
        bca._questions.clear()
        self.tmp = Path(tempfile.mkdtemp())
        self.ctx = FakeCtx()
        bca.register(self.ctx)
        self.state = make_state()
        bca._state_db = lambda: self.state
        self.ctx.cfg["kanban_db"] = str(self.tmp / "missing-kanban.db")
        self.ctx.cfg["notify_state_path"] = str(self.tmp / "notify.json")
        self.notify = Path(self.ctx.cfg["notify_state_path"])

    def _store(self):
        return delivery_store.DeliveryStore(
            delivery_store.store_path_for(self.notify))

    def _pre(self, session="bot", turn=""):
        return self.ctx.hooks["pre_llm_call"](
            session_id=session, turn_id=turn)["context"]

    def _post(self, session="bot", turn=""):
        return self.ctx.hooks["post_llm_call"](session_id=session, turn_id=turn)

    def _tool(self, name, args, session="bot"):
        return self.ctx.hooks["pre_tool_call"](
            tool_name=name, args=args, session_id=session)

    def _allowed(self, res):
        """None o un `modify` (p. ej. solo la idempotency key) = permitido.

        Un `modify` que solo añade la clave de idempotencia es el camino
        de "permitir" de evaluate, no un bloqueo.
        """
        return res is None or res.get("action") == "modify"

    def _create_and_complete(self, task_id, assignee, session="bot", summary=None):
        self.ctx.hooks["post_tool_call"](
            tool_name="kanban_create",
            args={"title": f"Work {task_id}", "assignee": assignee},
            result={"id": task_id}, session_id=session, status="ok",
        )
        self.ctx.hooks["kanban_task_completed"](
            task_id=task_id, assignee=assignee,
            summary=summary or f"result for {task_id}",
        )


class TestTeamConfigResolution(unittest.TestCase):
    """team.gods -> team.assignees: compatibilidad definida desde cero."""

    def test_assignees_only(self):
        r = team_config.resolve_team({"assignees": ["BSK", "JEV"]})
        self.assertTrue(r["enabled"])
        self.assertEqual(r["assignees"], ["BSK", "JEV"])
        self.assertFalse(r["legacy_gods"])

    def test_gods_only_still_works_as_legacy(self):
        r = team_config.resolve_team({"gods": ["BSK"]})
        self.assertTrue(r["enabled"])
        self.assertEqual(r["assignees"], ["BSK"])
        self.assertTrue(r["legacy_gods"])

    def test_both_keys_equal_is_ok(self):
        r = team_config.resolve_team(
            {"assignees": ["BSK", "JEV"], "gods": ["JEV", "BSK"]})
        self.assertTrue(r["enabled"])
        self.assertEqual(sorted(r["assignees"]), ["BSK", "JEV"])
        self.assertFalse(r["legacy_gods"])

    def test_both_keys_differ_raises_explanatory_error(self):
        with self.assertRaises(team_config.TeamConfigError) as cm:
            team_config.resolve_team(
                {"assignees": ["JEV"], "gods": ["BSK"]})
        msg = str(cm.exception)
        self.assertIn("team.assignees", msg)
        self.assertIn("team.gods", msg)
        self.assertIn("JEV", msg)
        self.assertIn("BSK", msg)

    def test_empty_assignees_vs_gods_differ_raises(self):
        with self.assertRaises(team_config.TeamConfigError):
            team_config.resolve_team({"assignees": [], "gods": ["BSK"]})

    def test_no_team_key_is_individual(self):
        r = team_config.resolve_team({})
        self.assertFalse(r["enabled"])
        self.assertEqual(r["assignees"], [])
        r = team_config.resolve_team(None)
        self.assertFalse(r["enabled"])
        self.assertEqual(r["assignees"], [])

    def test_existing_team_not_disabled_by_new_default(self):
        # Instalación existente: compañeros listados, sin clave `enabled`.
        # El default nuevo (individual) NO la desactiva.
        r = team_config.resolve_team({"gods": ["BSK", "JEV"]})
        self.assertTrue(r["enabled"])
        self.assertEqual(r["assignees"], ["BSK", "JEV"])

    def test_explicit_enabled_false_wins(self):
        r = team_config.resolve_team({"enabled": False, "gods": ["BSK"]})
        self.assertFalse(r["enabled"])
        self.assertEqual(r["assignees"], [])

    def test_explicit_enabled_true_with_empty_list(self):
        r = team_config.resolve_team({"enabled": True, "assignees": []})
        self.assertTrue(r["enabled"])
        self.assertEqual(r["assignees"], [])

    def test_string_form_accepted(self):
        r = team_config.resolve_team({"assignees": "BSK, JEV"})
        self.assertEqual(r["assignees"], ["BSK", "JEV"])

    def test_unicode_name_preserved(self):
        r = team_config.resolve_team({"assignees": ["Panteón"]})
        self.assertEqual(r["assignees"], ["Panteón"])
        self.assertIn("panteón", tf.god_keys(r["assignees"]))

    def test_duplicates_deduped_case_insensitively(self):
        r = team_config.resolve_team({"assignees": ["BSK", "bsk", " JEV ", ""]})
        self.assertEqual(r["assignees"], ["BSK", "JEV"])

    def test_non_dict_team_is_individual(self):
        r = team_config.resolve_team("BSK")
        self.assertFalse(r["enabled"])
        self.assertEqual(r["assignees"], [])


class TestTeamConfigIntegration(TeamBase):
    """La resolución vive en el plugin: _team() y los hooks la usan."""

    def test_team_via_assignees_reaches_hooks(self):
        self.ctx.cfg["team"] = {"assignees": ["BSK"]}
        self.assertEqual(bca._team()["assignees"], ["BSK"])
        self.assertTrue(bca._team()["enabled"])

    def test_fresh_install_hooks_stand_down(self):
        # Sin clave `team`: individual. Un kanban_completed no registra
        # nada (no hay equipo que entregar) y no se inventan compañeros:
        # crear una tarjeta para un perfil no configurado se bloquea.
        self._create_and_complete("t-fresh", "BSK", summary="fresh result")
        self.assertEqual(self._store().stats()["total"], 0)
        res = self._tool("kanban_create",
                         {"title": "x", "assignee": "BSK"})
        self.assertIsNotNone(res)
        self.assertEqual(res["action"], "block")
        self.assertIn("team.assignees", res["message"])
        # Una tarjeta sin asignar sigue permitida (trabajo propio en
        # el tablero, no delegación a un compañero inventado).
        self.assertIsNone(self._tool("kanban_create", {"title": "x"}))

    def test_empty_assignee_list_blocks_any_named_assignee(self):
        # Regla directa: con la lista vacía, cualquier `kanban_create`
        # con asignatario es una tarea sin permiso.
        decision = tf.evaluate(
            "kanban_create", {"title": "x", "assignee": "BSK"},
            kanban_lock=True, wait_for_god=True, gods=[],
            db_path=self.tmp / "missing-kanban.db", session_id="bot",
            notify_path=self.notify)
        self.assertEqual(decision["rule"], "team_assignee")
        self.assertIn("BSK", decision["detail"])

    def test_existing_team_survives_new_default(self):
        # Solo `gods`, sin `enabled`: el equipo sigue activo.
        self.ctx.cfg["team"] = {"gods": ["BSK"]}
        self.assertTrue(bca._team()["enabled"])
        self.assertEqual(bca._team()["assignees"], ["BSK"])
        self.assertTrue(self._allowed(self._tool(
            "kanban_create", {"title": "x", "assignee": "BSK"})))


class TestFourProfilesCoexist(TeamBase):
    """Convivencia con Panteón, JEV, Tormen y BSK (configuración real)."""

    def setUp(self):
        super().setUp()
        self.ctx.cfg["team"] = {"assignees": list(FOUR_PROFILES)}

    def test_create_allowed_for_each_of_the_four(self):
        for name in FOUR_PROFILES:
            with self.subTest(profile=name):
                self.assertTrue(
                    self._allowed(self._tool(
                        "kanban_create",
                        {"title": f"work for {name}", "assignee": name})),
                    f"kanban_create a {name} debe permitirse")

    def test_create_blocked_for_unlisted_profile(self):
        res = self._tool("kanban_create",
                         {"title": "x", "assignee": "Alguien"})
        self.assertIsNotNone(res)
        self.assertEqual(res["action"], "block")

    def test_wait_for_each_profile_until_done(self):
        board = make_kanban([
            {"id": "t-pan", "title": "a", "assignee": "Panteón",
             "status": "open", "result": "", "started_at": 0,
             "session_id": "", "idempotency_key": ""},
            {"id": "t-jev", "title": "b", "assignee": "JEV",
             "status": "working", "result": "", "started_at": 0,
             "session_id": "", "idempotency_key": ""},
            {"id": "t-bsk", "title": "c", "assignee": "BSK",
             "status": "done", "result": "", "started_at": 0,
             "session_id": "", "idempotency_key": ""},
        ])
        self.ctx.cfg["kanban_db"] = str(board)
        for task_id, blocked in (("t-pan", True), ("t-jev", True),
                                 ("t-bsk", False)):
            with self.subTest(task=task_id):
                res = self._tool("kanban_complete", {"id": task_id})
                if blocked:
                    self.assertIsNotNone(res, f"{task_id} debe bloquearse")
                    self.assertEqual(res["action"], "block")
                else:
                    self.assertIsNone(res, f"{task_id} done debe permitirse")

    def test_each_result_reaches_only_its_own_session(self):
        self._create_and_complete("t-pan-1", "Panteón", session="bot",
                                  summary="pan-result")
        self._create_and_complete("t-bsk-1", "BSK", session="bot2",
                                  summary="bsk-result")
        ctx_bot = self._pre(session="bot", turn="turn-a")
        ctx_bot2 = self._pre(session="bot2", turn="turn-b")
        self.assertIn("pan-result", ctx_bot)
        self.assertNotIn("bsk-result", ctx_bot)
        self.assertIn("bsk-result", ctx_bot2)
        self.assertNotIn("pan-result", ctx_bot2)

    def test_unicode_assignee_matches_case_insensitively(self):
        # El nombre con acento se guarda tal cual y casa sin importar
        # mayúsculas/minúsculas ni el acento exacto del lookup.
        self._create_and_complete("t-pan-2", "Panteón", session="bot",
                                  summary="pan-result-2")
        self.assertIn("pan-result-2", self._pre(session="bot", turn="t"))
        self.assertTrue(self._allowed(self._tool(
            "kanban_create", {"title": "x", "assignee": "panteón"})))


class TestAuthorizationsIntact(TeamBase):
    """Un conflicto de configuración no abre la guarda ni apaga el equipo."""

    def setUp(self):
        super().setUp()
        self.ctx.cfg["team"] = {"assignees": ["JEV"], "gods": ["BSK"]}

    def test_guard_still_blocks_secrets_with_broken_team_config(self):
        res = self._tool("terminal", {"command": "cat .env"})
        self.assertIsNotNone(res)
        self.assertEqual(res["action"], "block")

    def test_broken_team_config_fails_closed_with_explanation(self):
        res = self._tool("kanban_create",
                         {"title": "x", "assignee": "JEV"})
        self.assertIsNotNone(res)
        self.assertEqual(res["action"], "block")
        self.assertIn("team.assignees", res["message"])
        self.assertIn("team.gods", res["message"])

    def test_broken_team_config_records_no_delivery(self):
        # Sin equipo resoluble no se registra nada: ni aviso fantasma
        # ni entrega en otra sesión. La correlación perdida va a la
        # bandeja sin asignar, nunca a un chat equivocado.
        self.ctx.hooks["kanban_task_completed"](
            task_id="t-broken", assignee="JEV", summary="broken result")
        self.assertEqual(self._store().stats()["total"], 0)
        # Pero el turno sigue rindiendo: el error se VE en la nota
        # (delivery_warning), no se traga en silencio.
        ctx_bot = self._pre(session="bot", turn="turn-x")
        self.assertNotIn("broken result", ctx_bot)
        self.assertIn("team.assignees", ctx_bot)
        self.assertIn("team.gods", ctx_bot)
        self.assertNotIn("broken result",
                         self._pre(session="bot2", turn="turn-y"))

    def test_valid_config_keeps_previous_behaviour(self):
        self.ctx.cfg["team"] = {"assignees": ["BSK"]}
        self.assertTrue(self._allowed(self._tool(
            "kanban_create", {"title": "x", "assignee": "BSK"})))
        res = self._tool("terminal", {"command": "cat .env"})
        self.assertEqual(res["action"], "block")


class TestCoreDegradation(TeamBase):
    """Degradación declarada si faltan hooks del core (sin inventar nada)."""

    def setUp(self):
        super().setUp()
        self.ctx.cfg["team"] = {"assignees": ["BSK"]}

    def test_without_post_llm_call_nothing_is_confirmed(self):
        self._create_and_complete("t-deg", "BSK", summary="deg result")
        ctx = self._pre(turn="turn-1")
        self.assertIn("deg result", ctx)
        # El turno NO genera: post_llm_call nunca se dispara.
        row = self._store().get("t-deg")[0]
        self.assertEqual(row["status"], "offered")
        # ...y el aviso se vuelve a ofrecer tras vencer el lease
        # (at-least-once; la deduplicación por ID está en el texto).
        released = self._store().release_expired(now=time.time() + 601)
        self.assertEqual(released, 1)
        self.assertEqual(self._store().get("t-deg")[0]["status"], "pending")
        ctx2 = self._pre(turn="turn-2")
        self.assertIn("deg result", ctx2)

    def test_without_agent_loop_stopped_lease_is_the_mechanism(self):
        self._create_and_complete("t-deg2", "BSK", summary="deg result 2")
        self._pre(turn="turn-1")
        # Sin el hook, la oferta sigue ahí: solo el lease la libera.
        self.assertEqual(self._store().get("t-deg2")[0]["status"], "offered")
        released = self._store().release_expired(now=time.time() + 601)
        self.assertEqual(released, 1)

    def test_without_kanban_task_blocked_no_blocked_notice_is_invented(self):
        board = make_kanban([
            {"id": "t-blocked", "title": "stuck", "assignee": "BSK",
             "status": "blocked", "result": "needs input",
             "started_at": 0, "session_id": "", "idempotency_key": ""},
        ])
        self.ctx.cfg["kanban_db"] = str(board)
        # El hook nunca se dispara: el plugin no registra ningún aviso.
        self.assertEqual(self._store().stats()["total"], 0)
        self.assertNotIn("needs input", self._pre(session="bot", turn="t"))
        # La tarjeta sigue visible en el tablero (el usuario la ve ahí).
        with sqlite3.connect(board) as c:
            status = c.execute(
                "SELECT status FROM tasks WHERE id='t-blocked'").fetchone()[0]
        self.assertEqual(status, "blocked")


def _core_plugins_text(rev):
    """``hermes_cli/plugins.py`` at *rev* via git; when the checkout has no history
    (release tarball, e.g. v0.23.1) fall back to the working-tree file."""
    try:
        out = subprocess.run(
            ["git", "show", f"{rev}:hermes_cli/plugins.py"],
            cwd=str(CORE_CHECKOUT), capture_output=True, text=True, timeout=60)
        if out.returncode == 0 and out.stdout:
            return out.stdout
    except Exception:  # pragma: no cover - entorno sin git
        pass
    path = CORE_CHECKOUT / "hermes_cli" / "plugins.py"
    return path.read_text(encoding="utf-8") if path.exists() else ""


class TestLiveCoreHooks(unittest.TestCase):
    """Los hooks que se usan existen en el vivo 2bd378ed (v0.23.0)."""

    def test_hooks_exist_in_live_core(self):
        if not (CORE_CHECKOUT / "hermes_cli" / "plugins.py").exists():
            self.skipTest("sin checkout del core en este entorno")
        text = _core_plugins_text(CORE_REV)
        # VALID_HOOKS se cierra con `}` a principio de línea; el cuerpo
        # contiene comentarios con llaves, así que no vale `[^}]*`.
        valid = re.search(r"VALID_HOOKS.*?=\s*\{(.*?)\n\}", text, re.S)
        self.assertIsNotNone(valid, "VALID_HOOKS no localizado en el vivo")
        body = valid.group(1)
        for hook in REQUIRED_HOOKS:
            self.assertIn(f'"{hook}"', body,
                          f"{hook} debe existir en VALID_HOOKS del vivo")

    def test_declared_floor_has_the_hooks(self):
        """El piso de plugin.yaml tiene los tres hooks en su tag."""
        text = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
        m = re.search(r'requires_hermes:\s*">=?\s*([0-9]+\.[0-9]+\.[0-9]+)"', text)
        self.assertIsNotNone(m, "requires_hermes no declarado en plugin.yaml")
        floor = m.group(1)
        if not (CORE_CHECKOUT / "hermes_cli" / "plugins.py").exists():
            self.skipTest("sin checkout del core en este entorno")
        text = _core_plugins_text(f"v{floor}")
        self.assertTrue(text, f"no hay tag v{floor} ni archivo en el core")
        valid = re.search(r"VALID_HOOKS.*?=\s*\{(.*?)\n\}", text, re.S)
        self.assertIsNotNone(valid, "VALID_HOOKS no localizado en el tag")
        body = valid.group(1)
        for hook in REQUIRED_HOOKS:
            self.assertIn(f'"{hook}"', body,
                          f"{hook} debe existir en VALID_HOOKS de v{floor}")


if __name__ == "__main__":
    unittest.main()
