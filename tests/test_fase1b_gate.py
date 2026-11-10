"""Fase 1b — puerta de avance (v1.2 §5): outbox completo y destino durable.

Criterios de la puerta:
- 10.000 eventos con VARIOS procesos productores; 0 faltantes inexplicados;
  0 duplicados visibles; contabilidad por ID completa; 0 PermissionError
  sin manejar.
- Pruebas de caída: kill -9 antes y después de cada escritura; reconexión;
  reintento tras enviar pero antes de confirmar (re-ofrece exactamente una
  vez, nunca se pierde ni se duplica visiblemente).
- Destino conmutable: (a) oferta sin B1, (b) B1 con un ctx falso que imita
  los métodos B1, (c) comprobación real contra el checkout del core — esa
  última NO se hace aquí (el core vive en otro repo y otro intérprete);
  se verifica aparte corriendo el test del core
  ``tests/hermes_cli/test_plugin_state_ctx.py`` con ``scripts/run_tests.sh``.
- La batería completa sigue en verde y los 9 tests de puerta de la 1a
  siguen pasando (se comprueba corriendo toda la suite, no aquí).

Solo Linux/fork y Python 3.12.3 en este entorno (el vivo usa 3.14.9);
no se usa ningún rasgo posterior a 3.12.
"""
import importlib.util
import json
import multiprocessing as mp
import os
import shutil
import signal
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
import sys as _sys, pathlib as _pl
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))
from _bca_testenv import requires_fork, requires_sigkill  # noqa: E402

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
import durable_destination as dd  # noqa: E402
import team_flow as tf  # noqa: E402

GODS = ["researcher"]


class SessionTurnLeaseLostError(Exception):
    """Fake del error del core: la sesión destino tiene un turno vivo."""


class FakeCtx:
    def __init__(self, cfg=None):
        self.cfg = cfg or {}
        self.hooks = {}

    def get_config(self, key, default=None):
        return self.cfg.get(key, default)

    def register_hook(self, name, fn):
        self.hooks[name] = fn


class FakeB1Ctx(FakeCtx):
    """Ctx falso que imita los métodos B1 del core (firma exacta).

    Escribe en un dict claveado por delegation_id/admission_id: un
    reintento con el mismo id devuelve el MISMO message id (idempotencia
    B1 real). ``mode``: "ok" | "denied" (devuelve None, sin capability) |
    "lease_lost" (levanta SessionTurnLeaseLostError).
    """

    def __init__(self, cfg=None, mode="ok"):
        super().__init__(cfg)
        self.mode = mode
        self.written: dict[tuple[str, str], int] = {}
        self.rows: list[dict] = []
        self._next_id = 0

    def _write(self, kind, stable, session_id, content):
        if self.mode == "denied":
            return None
        if self.mode == "lease_lost":
            raise SessionTurnLeaseLostError("destination session has a live turn")
        key = (kind, stable)
        if key in self.written:
            return self.written[key]
        self._next_id += 1
        self.written[key] = self._next_id
        self.rows.append({"kind": kind, "id": stable, "session_id": session_id,
                          "content": content, "message_id": self._next_id})
        return self._next_id

    def record_delegation_delivery(self, session_id, content, *, delegation_id,
                                   delivery_notice="", metadata=None):
        return self._write("delegation", delegation_id, session_id, content)

    def record_completion_note(self, session_id, content, *, admission_id,
                               display_kind="internal_notification", metadata=None):
        return self._write("note", admission_id, session_id, content)


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
            [
                ("bot", "Bot Chat", None, "desktop"),
                ("bot2", "Bot Chat", None, "desktop"),
            ],
        )
    return p


def _producer(notify_path, task_ids):
    for tid in task_ids:
        tf.record_delivery(
            Path(notify_path),
            task_id=tid,
            assignee="researcher",
            summary=f"result {tid}",
            gods=list(GODS),
        )


def _crash_producer(notify_path, task_ids, journal_path):
    """Registra y apunta cada id en el journal DESPUÉS de que record vuelve.

    El padre lo mata con SIGKILL a mitad: el store debe contener
    exactamente los ids del journal (cada record es una transacción).
    """
    notify = Path(notify_path)
    journal = Path(journal_path)
    for tid in task_ids:
        tf.record_delivery(
            notify, task_id=tid, assignee="researcher",
            summary=f"result {tid}", gods=list(GODS),
        )
        with open(journal, "a", encoding="utf-8") as fh:
            fh.write(tid + "\n")


def _consumer(notify_path, taken_path, stop_event):
    notify = Path(notify_path)
    with open(taken_path, "a", encoding="utf-8") as fh:
        while not stop_event.is_set():
            fresh = tf.take_announcements(notify)
            if fresh:
                fh.write("\n".join(item["id"] for item in fresh) + "\n")
                fh.flush()
                tf.confirm_turn(notify, "", turn_id=None)
            time.sleep(0.0005)
        fresh = tf.take_announcements(notify)
        if fresh:
            fh.write("\n".join(item["id"] for item in fresh) + "\n")
            fh.flush()
            tf.confirm_turn(notify, "", turn_id=None)


def _drain_consumer(notify_path, taken_path):
    """Vacía lo que quede (tras una caída): ofrece y confirma hasta agotar."""
    notify = Path(notify_path)
    with open(taken_path, "a", encoding="utf-8") as fh:
        while True:
            fresh = tf.take_announcements(notify)
            if not fresh:
                return
            fh.write("\n".join(item["id"] for item in fresh) + "\n")
            fh.flush()
            tf.confirm_turn(notify, "", turn_id=None)


class TestFase1bDestination(unittest.TestCase):
    def setUp(self):
        bca._cache.clear()
        bca._questions.clear()
        self.tmp = Path(tempfile.mkdtemp())
        self.ctx = FakeB1Ctx()
        bca.register(self.ctx)
        self.state = make_state()
        bca._state_db = lambda: self.state
        self.ctx.cfg["team"] = {"enabled": True, "gods": ["researcher"]}
        self.ctx.cfg["kanban_db"] = str(self.tmp / "missing-kanban.db")
        self.ctx.cfg["notify_state_path"] = str(self.tmp / "notify.json")
        self.notify = Path(self.ctx.cfg["notify_state_path"])

    def _store(self):
        return delivery_store.DeliveryStore(
            delivery_store.store_path_for(self.notify))

    def _create_and_complete(self, task_id, session="bot", summary=None,
                             kind="completed"):
        self.ctx.hooks["post_tool_call"](
            tool_name="kanban_create",
            args={"title": f"Work {task_id}", "assignee": "researcher"},
            result={"id": task_id}, session_id=session, status="ok",
        )
        hook = ("kanban_task_completed" if kind == "completed"
                else "kanban_task_blocked")
        kwargs = {"task_id": task_id, "assignee": "researcher"}
        if kind == "completed":
            kwargs["summary"] = summary or f"result for {task_id}"
        else:
            kwargs["reason"] = summary or f"blocked {task_id}"
        self.ctx.hooks[hook](**kwargs)

    def _pre(self, session="bot", turn=""):
        return self.ctx.hooks["pre_llm_call"](
            session_id=session, turn_id=turn)["context"]

    def _post(self, session="bot", turn=""):
        return self.ctx.hooks["post_llm_call"](session_id=session, turn_id=turn)

    # ---------------------------------------------------------- (a) oferta
    def test_default_destination_is_offer_even_with_b1_ctx(self):
        """Por defecto el camino activo es la oferta, aunque ctx tenga B1."""
        self.assertEqual(bca._delivery_destination(), "offer")
        dest = dd.DurableDestination(self.ctx, mode=bca._delivery_destination())
        self.assertEqual(dest.active_path, "offer")
        self.assertTrue(dd.has_b1(self.ctx), "el ctx falso SÍ expone B1")
        self._create_and_complete("t-offer", summary="offer result")
        counts = tf.relay_pending(self.notify, ctx=self.ctx,
                                  destination=bca._delivery_destination())
        self.assertEqual(counts["active_path"], "offer")
        self.assertEqual(counts["delivered"], 0)
        self.assertEqual(len(self.ctx.rows), 0, "sin B1 activo no se escribe nada")
        ctx = self._pre(turn="t1")
        self.assertIn("offer result", ctx, "la oferta sigue entregando el aviso")
        self._post(turn="t1")
        self.assertEqual(self._store().get("t-offer")[0]["status"],
                         "delivered_in_turn")

    def test_offer_path_without_b1_ctx(self):
        """(a) Sin B1 en ctx: relay es no-op y la oferta entrega."""
        plain = FakeCtx(cfg=dict(self.ctx.cfg))
        self.assertFalse(dd.has_b1(plain))
        dest = dd.DurableDestination(plain, mode="auto")
        self.assertEqual(dest.active_path, "offer")
        self._create_and_complete("t-plain", summary="plain result")
        counts = tf.relay_pending(self.notify, ctx=plain, destination="auto")
        self.assertEqual(counts, {"attempted": 0, "delivered": 0, "lease_lost": 0,
                                  "capability_denied": 0, "failed": 0,
                                  "skipped": 0, "active_path": "offer"})
        self.assertIn("plain result", self._pre(turn="t1"))

    # ---------------------------------------------------------- (b) B1 falso
    def test_b1_completed_delivery_and_no_reoffer(self):
        """(b) B1 escribe el resultado y la oferta ya no lo re-anuncia."""
        self.ctx.cfg["delivery"] = {"destination": "auto"}
        self._create_and_complete("t-b1", summary="b1 result")
        store = self._store()
        self.assertEqual(store.get("t-b1")[0]["status"], "delivered_in_turn",
                         "el hook kanban ya debió relayar a B1 y confirmar")
        self.assertEqual(len(self.ctx.rows), 1)
        row = self.ctx.rows[0]
        self.assertEqual(row["kind"], "delegation")
        self.assertEqual(row["id"], "bot-chat:t-b1:completed")
        self.assertEqual(row["session_id"], "bot")
        self.assertIn("b1 result", row["content"])
        mark = store.durable_mark_for(store.get("t-b1")[0]["id"])
        self.assertIsNotNone(mark)
        self.assertEqual(mark["message_id"], row["message_id"])
        ctx = self._pre(turn="t1")
        self.assertNotIn("b1 result", ctx,
                         "lo ya escrito en B1 no debe re-ofrecerse en el turno")

    def test_b1_blocked_uses_completion_note(self):
        """(b) Una tarjeta bloqueada va por record_completion_note."""
        self.ctx.cfg["delivery"] = {"destination": "auto"}
        self._create_and_complete("t-blk", summary="blocked by X", kind="blocked")
        self.assertEqual(len(self.ctx.rows), 1)
        self.assertEqual(self.ctx.rows[0]["kind"], "note")
        self.assertEqual(self.ctx.rows[0]["id"], "bot-chat:t-blk:blocked")
        self.assertEqual(self._store().get("t-blk", kind="blocked")[0]["status"],
                         "delivered_in_turn")

    def test_b1_idempotent_retry_same_message_id(self):
        """(b) Reintentar la misma entrega devuelve el mismo message id."""
        store = self._store()
        store.record(kind="completed", task_id="t-idem", session_id="bot",
                     assignee="researcher", summary="idem result")
        dest = dd.DurableDestination(self.ctx, mode="b1")
        row = store.get("t-idem")[0]
        first = dest.deliver(row)
        second = dest.deliver(row)
        self.assertTrue(first["ok"] and second["ok"])
        self.assertEqual(first["message_id"], second["message_id"])
        self.assertEqual(len(self.ctx.rows), 1,
                         "un reintento no escribe una segunda fila")

    def test_b1_capability_denied_falls_back_to_offer(self):
        """(b) B1 devuelve None (sin capability): el aviso sigue por oferta."""
        self.ctx.mode = "denied"
        self.ctx.cfg["delivery"] = {"destination": "auto"}
        self._create_and_complete("t-den", summary="denied result")
        # El hook kanban intentó el relay y fue denegado: sigue pending.
        self.assertEqual(self._store().get("t-den")[0]["status"], "pending")
        self.assertEqual(len(self.ctx.rows), 0, "denegado no escribe nada")
        counts = tf.relay_pending(self.notify, ctx=self.ctx, destination="auto")
        self.assertEqual(counts["capability_denied"], 1)
        self.assertIn("denied result", self._pre(turn="t1"),
                      "sin capability, la oferta entrega el aviso")
        self._post(turn="t1")
        self.assertEqual(self._store().get("t-den")[0]["status"],
                         "delivered_in_turn")

    def test_b1_lease_lost_keeps_pending_for_offer(self):
        """(b) Turno vivo en destino (lease): el aviso queda pending."""
        self.ctx.mode = "lease_lost"
        self.ctx.cfg["delivery"] = {"destination": "auto"}
        self._create_and_complete("t-lease", summary="lease result")
        self.assertEqual(self._store().get("t-lease")[0]["status"], "pending")
        counts = tf.relay_pending(self.notify, ctx=self.ctx, destination="auto")
        self.assertEqual(counts["lease_lost"], 1)
        self.assertIn("lease result", self._pre(turn="t1"))
        self._post(turn="t1")
        self.assertEqual(self._store().get("t-lease")[0]["status"],
                         "delivered_in_turn")

    def test_crash_between_b1_write_and_confirm(self):
        """Caída tras escribir en B1 y ANTES de confirmar: no se re-ofrece.

        Simula el kill -9 entre durable_mark y confirm_ids: la siguiente
        oferta confirma la fila marcada en vez de ofrecerla.
        """
        store = self._store()
        store.record(kind="completed", task_id="t-crash1", session_id="bot",
                     assignee="researcher", summary="crash result")
        token, rows = store.claim_for_relay("bot")
        self.assertEqual(len(rows), 1)
        dest = dd.DurableDestination(self.ctx, mode="b1")
        result = dest.deliver(rows[0])
        self.assertTrue(result["ok"])
        store.durable_mark(rows[0]["id"], result["message_id"])
        # --- el proceso muere aquí (kill -9): sin confirm_ids ---
        offered = store.offer("bot", turn_id="turn-after-crash")
        self.assertEqual(offered, [],
                         "una fila con marca durable no debe re-ofrecerse")
        self.assertEqual(store.get("t-crash1")[0]["status"], "delivered_in_turn",
                         "la siguiente oferta la confirma por la marca")
        self.assertEqual(len(self.ctx.rows), 1)

    def test_crash_between_b1_write_and_mark(self):
        """Caída tras escribir en B1 y ANTES de la marca: reintento idempotente.

        La escritura B1 ya ocurrió pero ni la marca ni la confirmación.
        El relay reintenta: B1 devuelve el MISMO message id (sin duplicar
        la fila en el transcript) y la entrega se confirma una sola vez.
        """
        store = self._store()
        store.record(kind="completed", task_id="t-crash2", session_id="bot",
                     assignee="researcher", summary="crash2 result")
        token, rows = store.claim_for_relay("bot")
        dest = dd.DurableDestination(self.ctx, mode="b1")
        first = dest.deliver(rows[0])
        self.assertTrue(first["ok"])
        # --- el proceso muere aquí: sin durable_mark ni confirm ---
        store.release_claim(token)  # el lease vence / lo libera el sweep
        counts = tf.relay_pending(self.notify, ctx=self.ctx, destination="b1")
        self.assertEqual(counts["delivered"], 1)
        self.assertEqual(len(self.ctx.rows), 1,
                         "el reintento no duplica la fila en el transcript")
        self.assertEqual(self.ctx.rows[0]["message_id"], first["message_id"])
        self.assertEqual(store.get("t-crash2")[0]["status"], "delivered_in_turn")
        self.assertEqual(store.offer("bot", turn_id="t-next"), [])

    def test_relay_500_events_through_fake_b1(self):
        """500 entregas por el relay B1: 1 fila por id, 0 pendientes."""
        self.ctx.cfg["delivery"] = {"destination": "auto", "relay_limit": 50}
        for i in range(500):
            session = "bot" if i % 2 == 0 else "bot2"
            self._create_and_complete(f"t-r{i:03d}", session=session,
                                      summary=f"relay result {i:03d}")
        stats = self._store().stats()
        self.assertEqual(stats["by_status"].get("delivered_in_turn", 0), 500,
                         f"las 500 deben quedar confirmadas; stats={stats}")
        self.assertEqual(stats.get("durable_marked"), 500)
        self.assertEqual(len(self.ctx.rows), 500)
        self.assertEqual(len({r["id"] for r in self.ctx.rows}), 500)
        sessions = {r["session_id"] for r in self.ctx.rows}
        self.assertEqual(sessions, {"bot", "bot2"})

    # ---------------------------------------------------------- outbox 1b
    def test_startup_check_refuses_synced_and_network_paths(self):
        """Arranque: ruta sincronizada o de red se rechaza con explicación."""
        for bad in ("OneDrive", "Dropbox", "Google Drive"):
            notify = self.tmp / bad / "team-notify.json"
            problem = tf.verify_store_startup(notify)
            self.assertIn(bad.split()[0].lower(), problem.lower(),
                          f"la ruta {bad} debe rechazarse: {problem!r}")
        self.assertIn("network", tf.verify_store_startup(
            Path("//server/share/team-notify.json")).lower())
        self.assertEqual(tf.verify_store_startup(self.notify), "",
                         "una ruta local sana no debe dar problema")
        with self.assertRaises(ValueError):
            delivery_store.DeliveryStore(
                delivery_store.store_path_for(self.tmp / "OneDrive" / "n.json"))

    def test_maintenance_and_oldest_pending_age(self):
        """Mantenimiento: libera leases, poda entregadas, mide el pendiente."""
        store = self._store()
        old = time.time() - 3600
        store.record(kind="completed", task_id="t-old", session_id="bot",
                     assignee="researcher", summary="old", now=old)
        store.record(kind="completed", task_id="t-new", session_id="bot",
                     assignee="researcher", summary="new")
        age = store.oldest_pending_age_seconds()
        self.assertIsNotNone(age)
        self.assertGreaterEqual(age, 3590)
        self.assertLess(age, 3700)
        store.offer("bot", turn_id="t1")
        store.confirm("bot", turn_id="t1")
        # t-old y t-new entregadas hace un instante: prune con 0 días las borra.
        result = store.maintenance(older_than_days=0)
        self.assertEqual(result["pruned"], 2)
        self.assertEqual(result["released_expired"], 0)
        self.assertEqual(result["stats"]["total"], 0)
        # Dead-letter nunca se poda: queda contabilizada por id.
        store.record(kind="completed", task_id="t-dl", session_id="bot",
                     assignee="researcher", summary="dl")
        row_id = store.get("t-dl")[0]["id"]
        store.dead_letter(row_id, "no route")
        self.assertEqual([r["task_id"] for r in store.dead_letter_rows()], ["t-dl"])
        self.assertEqual(store.maintenance(older_than_days=0)["pruned"], 0)

    def test_integrity_check_ok_after_writes(self):
        store = self._store()
        for i in range(20):
            store.record(kind="completed", task_id=f"t-i{i}", session_id="bot",
                         assignee="researcher", summary="x")
        self.assertEqual(store.integrity_check(), "ok")


class TestFase1bCrashAndGate(unittest.TestCase):
    def setUp(self):
        bca._cache.clear()
        bca._questions.clear()

    @requires_sigkill

    def test_kill9_producer_mid_record(self):
        """kill -9 en mitad de las escrituras: el store queda íntegro y
        contiene exactamente lo confirmado (journal == store)."""
        ctx = mp.get_context("fork")
        tmp = Path(tempfile.mkdtemp(prefix="fase1b-crash-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        notify = tmp / "team-notify.json"
        journal = tmp / "journal.txt"
        expected = [f"t-k{i:04d}" for i in range(2000)]
        proc = ctx.Process(target=_crash_producer,
                           args=(str(notify), expected, str(journal)))
        proc.start()
        time.sleep(0.8)
        os.kill(proc.pid, signal.SIGKILL)
        proc.join(timeout=30)
        self.assertEqual(proc.exitcode, -signal.SIGKILL,
                         "el productor debe haber muerto por SIGKILL")
        journaled = ([line.strip() for line in journal.read_text(
            encoding="utf-8").splitlines() if line.strip()]
            if journal.is_file() else [])
        self.assertGreater(len(journaled), 0, "el productor debió registrar algo")
        store = delivery_store.DeliveryStore(delivery_store.store_path_for(notify))
        self.assertEqual(store.integrity_check(), "ok")
        stored = sorted(r["task_id"] for r in
                        [store.get(tid)[0] for tid in journaled])
        self.assertEqual(stored, sorted(journaled),
                         "el store debe contener exactamente lo confirmado")
        # El store puede llevar UNA fila más que el journal: el record que
        # confirmó justo cuando llegó el SIGKILL, antes de apuntarse en el
        # journal. Es la única discrepancia admisible (una transacción en
        # vuelo, nunca una fila parcial ni un id fuera de la secuencia).
        db_path = delivery_store.store_path_for(notify)
        with sqlite3.connect(db_path) as conn:
            stored_set = {r[0] for r in conn.execute(
                "SELECT task_id FROM deliveries")}
        extra = sorted(stored_set - set(journaled))
        self.assertLessEqual(len(extra), 1,
                             f"store={len(stored_set)} journal={len(journaled)}: "
                             "más de una fila sin confirmar en el journal")
        self.assertTrue(set(journaled) <= stored_set,
                        "todo lo confirmado en el journal debe estar en el store")
        self.assertTrue(stored_set <= set(expected),
                        "el store no debe contener ids fuera de la secuencia")
        if extra:
            self.assertEqual(extra[0], expected[len(journaled)],
                             "la fila extra debe ser la siguiente en secuencia")

    @requires_sigkill

    def test_kill9_consumer_mid_loop_then_recover(self):
        """kill -9 al consumidor en mitad del bucle: al re-conectar, los
        1.000 eventos se entregan exactamente una vez (0 perdidos, 0 dups)."""
        ctx = mp.get_context("fork")
        tmp = Path(tempfile.mkdtemp(prefix="fase1b-crash-cons-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        notify = tmp / "team-notify.json"
        taken_path = tmp / "taken.txt"
        expected = [f"t-c{i:04d}" for i in range(1000)]
        for tid in expected:  # productores ya terminaron antes de la caída
            tf.record_delivery(notify, task_id=tid, assignee="researcher",
                               summary=f"result {tid}", gods=list(GODS))
        stop = ctx.Event()
        consumer = ctx.Process(target=_consumer,
                               args=(str(notify), str(taken_path), stop))
        consumer.start()
        time.sleep(0.5)
        os.kill(consumer.pid, signal.SIGKILL)
        consumer.join(timeout=30)
        self.assertEqual(consumer.exitcode, -signal.SIGKILL)
        # Reconexión: un consumidor nuevo vacía lo que quede.
        drainer = ctx.Process(target=_drain_consumer,
                              args=(str(notify), str(taken_path)))
        drainer.start()
        drainer.join(timeout=120)
        self.assertEqual(drainer.exitcode, 0)
        taken = ([line.strip() for line in taken_path.read_text(
            encoding="utf-8").splitlines() if line.strip()]
            if taken_path.is_file() else [])
        self.assertEqual(sorted(set(taken)), sorted(expected),
                         f"faltan {len(set(expected) - set(taken))} avisos")
        self.assertEqual(len(taken), len(expected),
                         "cada aviso debe entregarse exactamente una vez")
        store = delivery_store.DeliveryStore(delivery_store.store_path_for(notify))
        self.assertEqual(
            store.stats()["by_status"].get("delivered_in_turn", 0), len(expected))

    def test_conn_setup_retries_when_locked(self):
        """Regresión: sin reintento en _conn, un PRAGMA journal_mode = WAL
        que fallaba con OperationalError: database is locked bajo
        contención escapaba de _write (que solo reintenta tras abrir la
        conexión) y mataba al consumidor en mitad de la puerta. Se inyecta
        el fallo en el primer intento de setup: _conn debe reintentarlo y
        la escritura completarse."""
        tmp = Path(tempfile.mkdtemp(prefix="fase1b-conn-retry-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        store = delivery_store.DeliveryStore(
            delivery_store.store_path_for(tmp / "team-notify.json"))
        real_connect = sqlite3.connect
        state = {"n": 0}

        class _FlakyConn:
            """Proxy que falla una sola vez en PRAGMA journal_mode = WAL."""
            def __init__(self, real):
                object.__setattr__(self, "_real", real)
                object.__setattr__(self, "_failed", False)

            def execute(self, sql, *args, **kwargs):
                if "journal_mode" in str(sql) and not self._failed:
                    object.__setattr__(self, "_failed", True)
                    raise sqlite3.OperationalError("database is locked")
                return self._real.execute(sql, *args, **kwargs)

            def __getattr__(self, name):
                return getattr(self._real, name)

            def __setattr__(self, name, value):
                setattr(self._real, name, value)

        def _flaky_connect(*args, **kwargs):
            conn = real_connect(*args, **kwargs)
            if state["n"] == 0:
                state["n"] += 1
                return _FlakyConn(conn)
            return conn

        sqlite3.connect = _flaky_connect
        try:
            store.record(kind="completed", task_id="t-lock", session_id="bot")
        finally:
            sqlite3.connect = real_connect
        self.assertEqual(state["n"], 1, "el primer intento debe haber fallado")
        self.assertIsNotNone(store.get("t-lock"),
                             "la entrega debe quedar registrada tras el reintento")

    @requires_fork

    def test_10000_events_five_producers(self):
        """PUERTA 1b: 5 productores x 2.000 eventos; 0 faltantes, 0 dups."""
        ctx = mp.get_context("fork")
        tmp = Path(tempfile.mkdtemp(prefix="fase1b-gate-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        notify = tmp / "team-notify.json"
        taken_path = tmp / "taken.txt"
        n_producers, per = 5, 2000
        expected = [f"t-p{p}-{i:04d}" for p in range(n_producers) for i in range(per)]
        chunks = [expected[p * per:(p + 1) * per] for p in range(n_producers)]

        stop = ctx.Event()
        consumer = ctx.Process(
            target=_consumer, args=(str(notify), str(taken_path), stop))
        producers = [
            ctx.Process(target=_producer, args=(str(notify), chunk))
            for chunk in chunks
        ]
        start = time.monotonic()
        consumer.start()
        for p in producers:
            p.start()
        for p in producers:
            p.join(timeout=300)
        stop.set()
        consumer.join(timeout=120)
        elapsed = time.monotonic() - start
        print(f"\n[1b-gate] 10000 events, 5 producers, {elapsed:.1f}s")

        for p in producers + [consumer]:
            self.assertEqual(p.exitcode, 0,
                             f"proceso con exitcode {p.exitcode}; el traceback "
                             "del hijo está en stderr (no es un conteo de "
                             "PermissionError: ese literal era engañoso)")
        alive = [p for p in producers + [consumer] if p.is_alive()]
        for p in alive:
            p.terminate()
        self.assertEqual(alive, [], "procesos del fixture no terminaron a tiempo")

        taken_list = (
            [line.strip() for line in taken_path.read_text(encoding="utf-8").splitlines()
             if line.strip()]
            if taken_path.is_file() else []
        )
        store = delivery_store.DeliveryStore(delivery_store.store_path_for(notify))
        stats = store.stats()
        lost = sorted(set(expected) - set(taken_list))
        self.assertEqual(
            len(lost), 0,
            f"se perdieron {len(lost)}/{len(expected)} avisos en {elapsed:.1f}s; "
            f"primeros: {lost[:10]}",
        )
        self.assertEqual(
            len(taken_list), len(expected),
            f"el consumidor tomó {len(taken_list)} avisos de {len(expected)}",
        )
        self.assertEqual(
            len(set(taken_list)), len(taken_list),
            "hay avisos duplicados visibles: un mismo id se anunció dos veces",
        )
        self.assertEqual(
            stats["total"], len(expected),
            f"el store debe guardar los {len(expected)} avisos; stats={stats}",
        )
        self.assertEqual(
            stats["by_status"].get("delivered_in_turn", 0), len(expected),
            f"todos los avisos deben quedar confirmados; stats={stats}",
        )
        self.assertEqual(stats.get("durable_marked", 0), 0,
                         "en modo oferta no hay marcas durables")


if __name__ == "__main__":
    unittest.main()
