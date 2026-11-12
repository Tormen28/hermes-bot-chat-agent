"""Fase 3 gate (rubric docs/fase3-rubric.md v1.0, level A, offline).

Ruta A, sin cambios en el core: el trabajo de fondo se lanza como
tarjeta kanban, el dispatcher del core lo ejecuta en otro proceso, el
resultado vuelve por el camino de la Fase 1 y el resultado nunca se
inyecta a mitad de un turno. Estados conectados a eventos reales;
cola visible de 10 por sesión; serialización por recurso con
``parents``; cancelación en tres niveles; stop-all reanudable.
"""
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

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

import background_tasks as bgt  # noqa: E402
import team_flow as tf  # noqa: E402

GODS = ["BSK", "JEV"]


class FakeCtx:
    def __init__(self, cfg=None):
        self.cfg = cfg or {}
        self.hooks = {}

    def get_config(self, key, default=None):
        return self.cfg.get(key, default)

    def register_hook(self, name, fn):
        self.hooks[name] = fn


def build_home():
    home = Path(tempfile.mkdtemp())
    with sqlite3.connect(home / "state.db") as c:
        c.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, "
                  "parent_session_id TEXT, source TEXT)")
        c.executemany(
            "INSERT INTO sessions VALUES (?,?,?,?)",
            [("s1", "Bot Chat", None, "chat"), ("s2", "Bot Chat", None, "chat")])
    kb = home / "kanban.db"
    with sqlite3.connect(kb) as c:
        c.execute("CREATE TABLE tasks (id TEXT, title TEXT, assignee TEXT, "
                  "status TEXT, result TEXT, started_at TEXT, session_id TEXT, "
                  "idempotency_key TEXT)")
        c.execute("CREATE TABLE task_links (parent_id TEXT, child_id TEXT)")
    notify = home / "bot-chat-agent" / "team-notify.json"
    notify.parent.mkdir(parents=True, exist_ok=True)
    return home, kb, notify


def launch(notify, task_id, session_id="s1", assignee="BSK", title="Work",
           resource=""):
    """Simulate a successful kanban_create in the chat (post_tool_call)."""
    tf.remember_create(
        notify, session_id=session_id, title=title, assignee=assignee,
        idempotency=tf.idempotency_key(session_id, assignee, title),
        result={"id": task_id}, gods=GODS, resource=resource)
    return task_id


def store_for(notify):
    return bca._context_store()


class TestFase3Gate(unittest.TestCase):
    def setUp(self):
        self.home, self.kb, self.notify = build_home()
        os.environ["HERMES_HOME"] = str(self.home)
        bca._state_db = lambda: self.home / "state.db"
        bca.register(FakeCtx(cfg={
            "language": "en",
            "team": {"enabled": True, "gods": list(GODS)},
            "kanban_db": str(self.kb),
            "notify_state_path": str(self.notify),
        }))

    # ------------------------------------------------------- turn vs worker
    def test_turn_does_not_wait_for_worker(self):
        launch(self.notify, "t-w1", title="Research")
        bca._on_kanban_claimed(task_id="t-w1", assignee="BSK", run_id=7)
        bca._on_kanban_worker_spawned(task_id="t-w1", run_id=7)
        store = store_for(self.notify)
        self.assertEqual(store.background_task("t-w1")["status"], "working")
        times = []
        first = None
        for i in range(200):
            start = time.perf_counter()
            out = bca._on_pre_llm_call(session_id="s1", turn_id=f"turn-{i}")
            times.append(time.perf_counter() - start)
            if first is None:
                first = out
            self.assertIsNotNone(out, "el turno debe arrancar aunque el worker siga")
        times.sort()
        p95 = times[int(0.95 * len(times)) - 1]
        self.assertLessEqual(p95, 0.1, f"ACK local p95 {p95:.3f}s > 100 ms")
        # The first turn's context shows the background task state; the
        # turn never waits for the worker. (Later turns render the delta,
        # so they do not repeat it.)
        self.assertIn("Working", first["context"])
        self.assertIn("t-w1", first["context"])

    # ------------------------------------------------------------- states
    def test_received_queued_working_ready_by_real_events(self):
        launch(self.notify, "t-1", title="Research")
        store = store_for(self.notify)
        self.assertEqual(store.background_task("t-1")["status"], "received")
        bca._on_kanban_claimed(task_id="t-1", assignee="BSK", run_id=1)
        self.assertEqual(store.background_task("t-1")["status"], "queued")
        bca._on_kanban_worker_spawned(task_id="t-1", run_id=1)
        self.assertEqual(store.background_task("t-1")["status"], "working")
        bca._on_kanban_completed(task_id="t-1", assignee="BSK", summary="done it")
        row = store.background_task("t-1")
        self.assertEqual(row["status"], "ready")
        self.assertIsNotNone(row["delivery_id"], "listo sin entrega persistida")
        self.assertEqual(len(store.get("t-1", kind="completed")), 1)

    def test_claimed_without_known_session_is_ignored(self):
        # A card created outside any Bot Chat session cannot be attributed
        # (wow 8): the claimed hook must not invent a session for it.
        store = store_for(self.notify)
        bca._on_kanban_claimed(task_id="t-ghost", assignee="BSK", run_id=1)
        self.assertIsNone(store.background_task("t-ghost"))

    def test_blocked_state(self):
        launch(self.notify, "t-2", title="Blocked work")
        bca._on_kanban_claimed(task_id="t-2", assignee="BSK", run_id=2)
        bca._on_kanban_blocked(task_id="t-2", assignee="BSK",
                               reason="waiting on API key")
        store = store_for(self.notify)
        self.assertEqual(store.background_task("t-2")["status"], "blocked")
        rows = store.get("t-2", kind="blocked")
        self.assertEqual(len(rows), 1, "el bloqueo debe registrar entrega blocked")

    def test_blocked_can_be_unblocked_and_finish(self):
        launch(self.notify, "t-3", title="Flaky")
        bca._on_kanban_blocked(task_id="t-3", assignee="BSK", reason="flaky")
        store = store_for(self.notify)
        self.assertEqual(store.background_task("t-3")["status"], "blocked")
        bca._on_kanban_claimed(task_id="t-3", assignee="BSK", run_id=3)
        self.assertEqual(store.background_task("t-3")["status"], "queued")
        bca._on_kanban_completed(task_id="t-3", assignee="BSK", summary="ok now")
        self.assertEqual(store.background_task("t-3")["status"], "ready")

    def test_waiting_derived_from_open_parent(self):
        launch(self.notify, "t-parent", title="Parent")
        launch(self.notify, "t-child", title="Child")
        with sqlite3.connect(self.kb) as c:
            c.executemany(
                "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?)",
                [("t-parent", "Parent", "BSK", "todo", "", "", "s1", ""),
                 ("t-child", "Child", "BSK", "todo", "", "", "s1", "")])
            c.execute("INSERT INTO task_links VALUES ('t-parent', 't-child')")
        lines = tf.background_status_lines("s1", self.kb, store_for(self.notify), "en")
        by_id = {item["id"]: item for item in lines}
        self.assertEqual(by_id["t-child"]["status"], "waiting")
        self.assertEqual(by_id["t-parent"]["status"], "received")
        self.assertIn("Waiting", by_id["t-child"]["line"])
        # Parent done: the child stops waiting.
        with sqlite3.connect(self.kb) as c:
            c.execute("UPDATE tasks SET status='done' WHERE id='t-parent'")
        lines = tf.background_status_lines("s1", self.kb, store_for(self.notify), "en")
        by_id = {item["id"]: item for item in lines}
        self.assertEqual(by_id["t-child"]["status"], "received")

    def test_no_false_ready_when_store_fails(self):
        launch(self.notify, "t-4", title="Doomed")
        bca._on_kanban_claimed(task_id="t-4", assignee="BSK", run_id=4)
        bca._on_kanban_worker_spawned(task_id="t-4", run_id=4)
        store = store_for(self.notify)
        # The unit guarantee: no delivery id, no ready.
        self.assertIsNone(bgt.on_completed(store, "t-4", delivery_id=None))
        self.assertEqual(store.background_task("t-4")["status"], "working")
        # The hook guarantee: a failing record never reports ready.
        with mock.patch.object(tf, "record_delivery",
                               side_effect=RuntimeError("disk gone")):
            bca._on_kanban_completed(task_id="t-4", assignee="BSK", summary="x")
        self.assertEqual(store.background_task("t-4")["status"], "working")
        self.assertEqual(store.get("t-4", kind="completed"), [])

    def test_terminal_states_never_regress(self):
        launch(self.notify, "t-5", title="Once")
        store = store_for(self.notify)
        bca._on_kanban_completed(task_id="t-5", assignee="BSK", summary="done")
        self.assertEqual(store.background_task("t-5")["status"], "ready")
        # A late claimed/completed event cannot resurrect it.
        bca._on_kanban_claimed(task_id="t-5", assignee="BSK", run_id=9)
        self.assertEqual(store.background_task("t-5")["status"], "ready")
        self.assertIsNone(bgt.on_completed(store, "t-5", delivery_id=999))

    # ------------------------------------------------- dispatch & permission
    def test_no_double_dispatch_same_key(self):
        key = tf.idempotency_key("s1", "BSK", "Research")
        d1 = tf.evaluate("kanban_create",
                         {"title": "Research", "assignee": "BSK"},
                         kanban_lock=True, wait_for_god=True, gods=list(GODS),
                         db_path=self.kb, session_id="s1",
                         notify_path=self.notify)
        d2 = tf.evaluate("kanban_create",
                         {"title": "Research", "assignee": "BSK"},
                         kanban_lock=True, wait_for_god=True, gods=list(GODS),
                         db_path=self.kb, session_id="s1",
                         notify_path=self.notify)
        self.assertEqual(d1["modify"]["idempotency_key"], key)
        self.assertEqual(d2["modify"]["idempotency_key"], key)
        # The core returns the existing card for the same key; the plugin
        # upserts the same task id: one row, not two.
        launch(self.notify, "t-dup", title="Research")
        launch(self.notify, "t-dup", title="Research")
        store = store_for(self.notify)
        self.assertEqual(len(store.background_tasks("s1")), 1)
        self.assertEqual(store.open_background_count("s1"), 1)

    def test_create_to_unlisted_profile_blocked(self):
        decision = tf.evaluate(
            "kanban_create", {"title": "Sneaky", "assignee": "stranger"},
            kanban_lock=True, wait_for_god=True, gods=list(GODS),
            db_path=self.kb, session_id="s1", notify_path=self.notify)
        self.assertEqual(decision["rule"], "team_assignee")
        self.assertIn("BSK", decision["detail"])
        self.assertIn("stranger", decision["detail"])

    def test_queue_limit_10(self):
        for i in range(10):
            launch(self.notify, f"t-q{i}", title=f"Job {i}")
        store = store_for(self.notify)
        self.assertEqual(store.open_background_count("s1"), 10)
        decision = tf.evaluate(
            "kanban_create", {"title": "Job 10", "assignee": "BSK"},
            kanban_lock=True, wait_for_god=True, gods=list(GODS),
            db_path=self.kb, session_id="s1", notify_path=self.notify)
        self.assertEqual(decision["rule"], "queue_full")
        self.assertIn("10", decision["detail"])
        # One finishes: the queue accepts again.
        bca._on_kanban_completed(task_id="t-q0", assignee="BSK", summary="done")
        decision = tf.evaluate(
            "kanban_create", {"title": "Job 10", "assignee": "BSK"},
            kanban_lock=True, wait_for_god=True, gods=list(GODS),
            db_path=self.kb, session_id="s1", notify_path=self.notify)
        self.assertIsNone(decision["rule"])
        self.assertIn("idempotency_key", decision["modify"])

    def test_queue_limit_is_per_session(self):
        for i in range(10):
            launch(self.notify, f"t-s1-{i}", session_id="s1", title=f"Job {i}")
        decision = tf.evaluate(
            "kanban_create", {"title": "Other session", "assignee": "BSK"},
            kanban_lock=True, wait_for_god=True, gods=list(GODS),
            db_path=self.kb, session_id="s2", notify_path=self.notify)
        self.assertIsNone(decision["rule"], "la cola llena de s1 no bloquea a s2")

    def test_serialization_same_resource_via_parents(self):
        # resource is the normalized key resource_key() builds from the
        # kanban_create args (path:<abspath> / project:<slug>).
        launch(self.notify, "t-r1", title="Edit A", resource="path:/tmp/proj-x")
        decision = tf.evaluate(
            "kanban_create",
            {"title": "Edit B", "assignee": "BSK", "workspace_path": "/tmp/proj-x"},
            kanban_lock=True, wait_for_god=True, gods=list(GODS),
            db_path=self.kb, session_id="s1", notify_path=self.notify)
        self.assertIsNone(decision["rule"])
        self.assertEqual(decision["modify"]["parents"], ["t-r1"])
        # The idempotency patch still applies alongside.
        self.assertIn("idempotency_key", decision["modify"])
        # Same resource by project slug.
        launch(self.notify, "t-r2", title="Proj A", resource="project:acme")
        decision = tf.evaluate(
            "kanban_create",
            {"title": "Proj B", "assignee": "JEV", "project": "acme"},
            kanban_lock=True, wait_for_god=True, gods=list(GODS),
            db_path=self.kb, session_id="s1", notify_path=self.notify)
        self.assertEqual(decision["modify"]["parents"], ["t-r2"])

    def test_serialization_keeps_model_parents_and_ignores_other_resource(self):
        launch(self.notify, "t-r3", title="Edit C", resource="path:/tmp/proj-y")
        decision = tf.evaluate(
            "kanban_create",
            {"title": "Edit D", "assignee": "BSK", "workspace_path": "/tmp/proj-y",
             "parents": ["t-other"]},
            kanban_lock=True, wait_for_god=True, gods=list(GODS),
            db_path=self.kb, session_id="s1", notify_path=self.notify)
        self.assertEqual(decision["modify"]["parents"], ["t-other", "t-r3"])
        decision = tf.evaluate(
            "kanban_create",
            {"title": "Edit E", "assignee": "BSK", "workspace_path": "/tmp/other"},
            kanban_lock=True, wait_for_god=True, gods=list(GODS),
            db_path=self.kb, session_id="s1", notify_path=self.notify)
        self.assertNotIn("parents", decision["modify"])

    # ------------------------------------------------------- cancellation
    def test_cancel_three_levels(self):
        store = store_for(self.notify)
        # Level 1 — response: /stop releases the offers, the task goes on.
        launch(self.notify, "t-l1", title="Long job")
        bca._on_kanban_claimed(task_id="t-l1", assignee="BSK", run_id=1)
        bca._on_kanban_worker_spawned(task_id="t-l1", run_id=1)
        tf.record_delivery(self.notify, task_id="t-l1x", assignee="BSK",
                           summary="partial", gods=list(GODS), session_id="s1")
        bca._on_pre_llm_call(session_id="s1", turn_id="turn-1")
        bca._on_agent_loop_stopped(session_id="s1")
        self.assertEqual(store.background_task("t-l1")["status"], "working")
        self.assertEqual(store.get("t-l1x", kind="completed")[0]["status"], "pending")
        # Level 2 — task: the chat blocks the card -> cancelled, and a
        # later completion is NOT announced.
        launch(self.notify, "t-l2", title="Cancel me")
        bca._on_kanban_claimed(task_id="t-l2", assignee="BSK", run_id=2)
        decision = tf.evaluate(
            "kanban_block", {"task_id": "t-l2", "kind": "needs_input",
                             "reason": "user asked to cancel"},
            kanban_lock=True, wait_for_god=True, gods=list(GODS),
            db_path=self.kb, session_id="s1", notify_path=self.notify)
        self.assertIsNone(decision["rule"], "bloquear es la acción, no se bloquea")
        bca._on_kanban_blocked(task_id="t-l2", assignee="BSK",
                               reason="user asked to cancel")
        self.assertEqual(store.background_task("t-l2")["status"], "cancelled")
        bca._on_kanban_completed(task_id="t-l2", assignee="BSK", summary="too late")
        self.assertEqual(store.get("t-l2", kind="completed"), [],
                         "una tarea cancelada no se anuncia")
        self.assertEqual(store.background_task("t-l2")["status"], "cancelled")

    def test_worker_block_without_chat_marker_is_blocked_not_cancelled(self):
        launch(self.notify, "t-l3", title="Stuck")
        bca._on_kanban_claimed(task_id="t-l3", assignee="BSK", run_id=3)
        # No kanban_block from the chat: the worker blocked on its own.
        bca._on_kanban_blocked(task_id="t-l3", assignee="BSK",
                               reason="needs an API key")
        store = store_for(self.notify)
        self.assertEqual(store.background_task("t-l3")["status"], "blocked")

    def test_stop_all_keeps_persisted(self):
        store = store_for(self.notify)
        launch(self.notify, "t-s1", title="Job A")
        launch(self.notify, "t-s2", title="Job B")
        bca._on_kanban_claimed(task_id="t-s1", assignee="BSK", run_id=1)
        # One delivery already confirmed (persisted): it must survive.
        tf.record_delivery(self.notify, task_id="t-done", assignee="BSK",
                           summary="already done", gods=list(GODS), session_id="s1")
        bca._on_pre_llm_call(session_id="s1", turn_id="turn-1")
        bca._on_post_llm_call(session_id="s1", turn_id="turn-1")
        self.assertEqual(store.delivered_task_ids("s1"), ["t-done"])
        result = tf.stop_all_session(self.notify, "s1", reason="user: stop all")
        self.assertEqual(sorted(result["cancelled"]), ["t-s1", "t-s2"])
        self.assertTrue(store.stop_all_active("s1"))
        self.assertEqual(store.open_background_count("s1"), 0)
        self.assertEqual(store.delivered_task_ids("s1"), ["t-done"])
        # No new background work while stopped.
        decision = tf.evaluate(
            "kanban_create", {"title": "New", "assignee": "BSK"},
            kanban_lock=True, wait_for_god=True, gods=list(GODS),
            db_path=self.kb, session_id="s1", notify_path=self.notify)
        self.assertEqual(decision["rule"], "stop_all")
        # Another session is unaffected.
        self.assertFalse(store.stop_all_active("s2"))

    def test_stop_all_resume_on_retry(self):
        tf.stop_all_session(self.notify, "s1", reason="user: stop all")
        args = {"title": "Resume me", "assignee": "BSK"}
        kw = dict(kanban_lock=True, wait_for_god=True, gods=list(GODS),
                  db_path=self.kb, session_id="s1", notify_path=self.notify)
        first = tf.evaluate("kanban_create", args, turn_id="turn-1", **kw)
        self.assertEqual(first["rule"], "stop_all")
        # Same turn retry: still blocked.
        retry_same = tf.evaluate("kanban_create", args, turn_id="turn-1", **kw)
        self.assertEqual(retry_same["rule"], "stop_all")
        # Later turn retry of the same request: the user confirmed, the
        # flag clears and the request goes through.
        retry_later = tf.evaluate("kanban_create", args, turn_id="turn-2", **kw)
        self.assertIsNone(retry_later["rule"])
        self.assertIn("idempotency_key", retry_later["modify"])
        self.assertFalse(store_for(self.notify).stop_all_active("s1"))

    def test_cancel_only_own_session_task(self):
        launch(self.notify, "t-own", session_id="s1", title="Mine")
        store = store_for(self.notify)
        self.assertFalse(store.cancel_background_task("t-own", session_id="s2"))
        self.assertEqual(store.background_task("t-own")["status"], "received")
        self.assertTrue(store.cancel_background_task("t-own", session_id="s1"))

    # ------------------------------------------------------- note & config
    def test_states_in_turn_note(self):
        launch(self.notify, "t-n1", title="Research")
        launch(self.notify, "t-n2", title="Writeup")
        bca._on_kanban_claimed(task_id="t-n1", assignee="BSK", run_id=1)
        bca._on_kanban_worker_spawned(task_id="t-n1", run_id=1)
        bca._on_kanban_blocked(task_id="t-n2", assignee="JEV", reason="stuck")
        built = bca._build_turn_context("s1", turn_id="turn-1")
        self.assertIn("Working", built["dynamic"])
        self.assertIn("t-n1", built["dynamic"])
        self.assertIn("Blocked", built["dynamic"])
        self.assertIn("t-n2", built["dynamic"])
        # Second turn without changes: the delta does not repeat them.
        bca._on_post_llm_call(session_id="s1", turn_id="turn-1")
        second = bca._build_turn_context("s1", turn_id="turn-2")
        self.assertNotIn("t-n1", second["dynamic"])

    def test_individual_mode_worker_profile(self):
        # Individual mode: the user's own worker profile is listed in
        # team.gods; the same path tracks it.
        bca.register(FakeCtx(cfg={
            "language": "en",
            "team": {"enabled": True, "gods": ["worker-1"]},
            "kanban_db": str(self.kb),
            "notify_state_path": str(self.notify),
        }))
        tf.remember_create(
            self.notify, session_id="s1", title="Solo job", assignee="worker-1",
            idempotency=tf.idempotency_key("s1", "worker-1", "Solo job"),
            result={"id": "t-solo"}, gods=["worker-1"])
        store = store_for(self.notify)
        self.assertEqual(store.background_task("t-solo")["status"], "received")
        bca._on_kanban_claimed(task_id="t-solo", assignee="worker-1", run_id=1)
        bca._on_kanban_completed(task_id="t-solo", assignee="worker-1",
                                 summary="solo done")
        self.assertEqual(store.background_task("t-solo")["status"], "ready")

    def test_plugin_yaml_declares_fase3_hooks(self):
        text = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
        for hook in ("kanban_task_claimed", "on_kanban_worker_spawned",
                     "kanban_task_blocked", "post_llm_call", "agent_loop_stopped"):
            self.assertIn(hook, text, f"plugin.yaml no declara {hook}")

    def test_desktop_js_suite(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("node no disponible; la suite JS se corre aparte")
        proc = subprocess.run(
            [node, "--test", "tests/test_fase3_desktop.mjs"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0,
                         f"suite JS falló:\n{proc.stdout}\n{proc.stderr}")


if __name__ == "__main__":
    unittest.main()
