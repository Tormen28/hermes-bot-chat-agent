"""Fase 1a — puerta de avance (v1.2): entrega segura entre procesos.

Cada test corresponde a un criterio de la puerta:
- 50 cancelaciones seguidas de reapertura con 0 avisos perdidos.
- 3 procesos productores x 1.000 eventos con 0 faltantes y 0 duplicados
  visibles (el consumidor confirma cada turno, como post_llm_call).
- 0 PermissionError sin manejar (almacén no escribible -> error visible).
- 0 avisos anunciados en una sesión distinta de la de origen.
- La liberación por lease funciona aunque agent_loop_stopped no se dispare.
- Importación del JSON legacy (sin modificarlo) y retención/poda explícitas.
- Interruptor de configuración para volver al camino JSON.

Solo Linux/fork y Python 3.12.3 en este entorno (el vivo usa 3.14.9);
no se usa ningún rasgo posterior a 3.12.
"""
import importlib.util
import json
import multiprocessing as mp
import shutil
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

import delivery_store  # noqa: E402  (el __init__ del plugin ya lo puso en sys.path)
import team_flow as tf  # noqa: E402

GODS = ["researcher"]


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
            [
                ("bot", "Bot Chat", None, "desktop"),
                ("bot2", "Bot Chat", None, "desktop"),
            ],
        )
    return p


def _producer(notify_path, task_ids):
    """Proceso worker: registra entregas como el hook kanban_task_completed."""
    for tid in task_ids:
        tf.record_delivery(
            Path(notify_path),
            task_id=tid,
            assignee="researcher",
            summary=f"result {tid}",
            gods=list(GODS),
        )


def _consumer(notify_path, taken_path, stop_event):
    """Proceso del chat: toma y confirma cada turno (como pre/post_llm_call)."""
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


class TestFase1aGate(unittest.TestCase):
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

    def _store(self):
        return delivery_store.DeliveryStore(
            delivery_store.store_path_for(self.notify))

    def _create_and_complete(self, task_id, session="bot", summary=None):
        self.ctx.hooks["post_tool_call"](
            tool_name="kanban_create",
            args={"title": f"Work {task_id}", "assignee": "researcher"},
            result={"id": task_id}, session_id=session, status="ok",
        )
        self.ctx.hooks["kanban_task_completed"](
            task_id=task_id, assignee="researcher",
            summary=summary or f"result for {task_id}",
        )

    def _pre(self, session="bot", turn=""):
        return self.ctx.hooks["pre_llm_call"](
            session_id=session, turn_id=turn)["context"]

    def _post(self, session="bot", turn=""):
        return self.ctx.hooks["post_llm_call"](session_id=session, turn_id=turn)

    def test_50_interruptions_no_loss(self):
        """50 turnos interrumpidos: al reabrir, los 50 avisos siguen ahí."""
        for i in range(50):
            tid = f"t-{i:02d}"
            self._create_and_complete(tid, summary=f"result-{i:02d}")
            ctx = self._pre(turn=f"turn-{i}")
            self.assertIn(f"result-{i:02d}", ctx,
                          f"el aviso {i} debe entrar en su turno")
            # --- el turno se interrumpe aquí: sin post_llm_call ---
        final = self._pre(turn="turn-final")
        for i in range(50):
            self.assertEqual(
                final.count(f"result-{i:02d}"), 1,
                f"el aviso result-{i:02d} debe aparecer exactamente una vez "
                "al reabrir tras 50 interrupciones",
            )
        self.assertEqual(self._store().stats()["total"], 50,
                         "los 50 avisos deben seguir en el store")

    def test_lease_expiry_releases_offer(self):
        """Sin agent_loop_stopped: el lease vencido libera la oferta."""
        self._create_and_complete("t-lease", summary="lease result")
        ctx = self._pre(turn="turn-1")
        self.assertIn("lease result", ctx)
        store = self._store()
        rows = store.get("t-lease")
        self.assertEqual(rows[0]["status"], "offered")
        self.assertIsNotNone(rows[0]["locked_until"])
        # Pasa el tiempo: el lease (600 s) venció y nadie llamó al hook.
        released = store.release_expired(now=time.time() + 601)
        self.assertEqual(released, 1, "el lease vencido debe liberar 1 oferta")
        self.assertEqual(store.get("t-lease")[0]["status"], "pending")
        # El siguiente turno vuelve a ofrecer el aviso.
        ctx2 = self._pre(turn="turn-2")
        self.assertIn("lease result", ctx2)
        self._post(turn="turn-2")
        self.assertEqual(store.get("t-lease")[0]["status"], "delivered_in_turn")

    @requires_fork

    def test_three_producers_1000_events(self):
        """3 productores x 1.000 eventos: 0 faltantes y 0 duplicados visibles."""
        ctx = mp.get_context("fork")
        tmp = Path(tempfile.mkdtemp(prefix="fase1a-gate-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        notify = tmp / "team-notify.json"
        taken_path = tmp / "taken.txt"
        n_producers, per = 3, 1000
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

    def test_unwritable_store_raises_and_hook_warns(self):
        """Almacén no escribible: la operación falla con error, nunca en silencio."""
        blocker = self.tmp / "blocker-file"
        blocker.write_text("soy un archivo, no un directorio", encoding="utf-8")
        notify = blocker / "notify.json"
        store = delivery_store.DeliveryStore(delivery_store.store_path_for(notify))
        with self.assertRaises(
            OSError,
            msg="escribir en un almacén no escribible debe fallar con error",
        ):
            store.record(kind="completed", task_id="t-x", session_id="bot",
                         assignee="researcher", summary="x")
        # Por el hook: no se propaga (no rompe el turno), pero queda warning.
        self.ctx.cfg["notify_state_path"] = str(notify)
        with self.assertLogs("bot_chat_agent", level="WARNING") as cm:
            self.assertIsNone(
                self.ctx.hooks["kanban_task_completed"](
                    task_id="t-x", assignee="researcher", summary="x"))
        self.assertTrue(
            any("delivery note skipped" in line for line in cm.output),
            f"la entrega fallida debe quedar en warning, no en debug: {cm.output}",
        )

    def test_agent_loop_stopped_is_safe_noop(self):
        """agent_loop_stopped: optimización, nunca libera otra sesión."""
        self._create_and_complete("t-stop", summary="stop result")
        self._create_and_complete("t-stop2", session="bot2", summary="stop result 2")
        self.assertIn("stop result", self._pre(turn="t1"))
        self.assertIn("stop result 2", self._pre(session="bot2", turn="t1"))
        store = self._store()
        # Sin correlación: no libera nada, no rompe nada.
        self.assertIsNone(
            self.ctx.hooks["agent_loop_stopped"](session_key="unknown-key-xyz"))
        self.assertEqual(store.get("t-stop")[0]["status"], "offered")
        self.assertEqual(store.get("t-stop2")[0]["status"], "offered")
        # Con session_id: libera solo esa sesión.
        self.assertIsNone(
            self.ctx.hooks["agent_loop_stopped"](session_id="bot"))
        self.assertEqual(store.get("t-stop")[0]["status"], "pending")
        self.assertEqual(
            store.get("t-stop2")[0]["status"], "offered",
            "agent_loop_stopped de bot no debe liberar los avisos de bot2",
        )

    def test_unassigned_tray_never_announced(self):
        """Sin correlación: el aviso va a la bandeja, nunca a otra sesión."""
        self.ctx.hooks["kanban_task_completed"](
            task_id="t-u", assignee="researcher", summary="unassigned result")
        ctx = self._pre(turn="t1")
        self.assertNotIn("unassigned result", ctx,
                         "un aviso sin sesión no debe anunciarse en bot")
        self.assertIn("unassigned tray", ctx,
                      "la bandeja sin asignar debe ser visible en la nota")
        self.assertEqual(self._store().unassigned_count(), 1)

    def test_import_json_counts(self):
        """Importar team-notify.json: conteos e IDs, sin modificar el archivo."""
        legacy = {
            "pending": {
                "t-a": {"assignee": "researcher", "summary": "A"},
                "t-b": {"assignee": "researcher", "summary": "B"},
                "t-c": {"assignee": "researcher", "summary": "C"},
            },
            "announced": ["t-c"],
            "creates": [
                {"task_id": "t-a", "session_id": "bot-a"},
                {"task_id": "t-b", "session_id": "bot-b"},
            ],
        }
        self.notify.write_text(json.dumps(legacy), encoding="utf-8")
        before = self.notify.read_bytes()
        counts = tf.import_notify_json(self.notify)
        self.assertEqual(
            counts,
            {"imported": 2, "skipped_announced": 1, "skipped_existing": 0,
             "total_pending": 3, "error": ""},
            f"conteos de importación incorrectos: {counts}",
        )
        store = self._store()
        self.assertEqual(store.get("t-a")[0]["session_id"], "bot-a")
        self.assertEqual(store.get("t-b")[0]["session_id"], "bot-b")
        self.assertEqual(store.get("t-c"), [], "t-c ya estaba anunciado: no se importa")
        self.assertEqual(self.notify.read_bytes(), before,
                         "la importación no debe modificar team-notify.json")

    def test_prune_and_dead_letter(self):
        """Retención explícita: prune solo toca entregadas; dead_letter cuenta."""
        store = self._store()
        store.record(kind="completed", task_id="t-a", session_id="bot",
                     assignee="researcher", summary="A")
        store.record(kind="completed", task_id="t-b", session_id="bot",
                     assignee="researcher", summary="B")
        store.record(kind="completed", task_id="t-c", session_id="bot",
                     assignee="researcher", summary="C")
        store.offer("bot", turn_id="t1")
        # t-c a dead_letter mientras está offered (dead_letter no mueve
        # filas ya delivered_in_turn).
        dead_id = store.get("t-c")[0]["id"]
        self.assertEqual(store.dead_letter(dead_id, "no route"), 1)
        store.confirm("bot", turn_id="t1")  # t-a y t-b quedan delivered_in_turn
        # t-a2 queda pending y luego a dead_letter por reintentos.
        store.record(kind="completed", task_id="t-a2", session_id="bot",
                     assignee="researcher", summary="A2")
        fail_id = store.get("t-a2")[0]["id"]
        for _ in range(5):
            status, attempts = store.mark_failed(fail_id, "boom")
        self.assertEqual((status, attempts), ("dead_letter", 5))
        pruned = store.prune(older_than_days=0)
        self.assertEqual(pruned, 2, "prune solo borra las delivered_in_turn (t-b y t-a)")
        stats = store.stats()
        self.assertEqual(stats["by_status"].get("pending", 0), 0)
        self.assertEqual(stats["by_status"].get("dead_letter", 0), 2)
        self.assertEqual(stats["total"], 2)

    def test_json_backend_revert_switch(self):
        """delivery.backend=json: vuelve al camino anterior sin desinstalar."""
        self.ctx.cfg["delivery"] = {"backend": "json"}
        self.ctx.hooks["kanban_task_completed"](
            task_id="t-json", assignee="researcher", summary="legacy result")
        data = json.loads(self.notify.read_text(encoding="utf-8"))
        self.assertIn("t-json", data.get("pending", {}),
                      "con backend=json la entrega debe escribir team-notify.json")
        ctx = self._pre(turn="t1")
        self.assertIn("legacy result", ctx)


if __name__ == "__main__":
    unittest.main()
