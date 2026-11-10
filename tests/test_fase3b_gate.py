"""Fase 3b — puerta de avance (v1.2 §3b, rúbrica docs/fase3b-rubric.md v1.0).

El plugin no programa nada por su cuenta: toda la programación la hace el
cron del core. La puerta corre contra el core REAL (checkout vivo
``2bd378ed``): creación, vencimiento, disparo (``run_one_job``, trabajos
``no_agent`` con script) y libro de ejecuciones son llamadas reales al core,
en un subproceso con el Python del runtime PM (``tests/_fase3b_core_driver.py``);
el proceso de la puerta (Python del sistema, que no puede importar el core)
observa y entrega sobre los mismos ficheros en disco con el lector por
disco del plugin. El disparo con agente (modelo) no se ejecuta offline: el
plugin solo lee los artefactos durables del disparo, idénticos para
trabajos con agente y ``no_agent``.

Criterios de la puerta (rúbrica §4):

- 20 tareas dentro de ±60 s; 0 perdidas tras reinicio; sin dobles disparos;
  sin ejecución sin permiso.
- Stop-all pausa las programadas de la sesión; reanudación explícita.
- ``pre_tool_call`` fuerza ``deliver="local"`` en ``cronjob create``.
- Estados visibles en la nota del turno; el tiempo por sí solo no entrega.

Solo Linux/fork y Python 3.12.3 en este entorno (el vivo usa 3.14.9);
no se usa ningún rasgo posterior a 3.12.
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
from datetime import datetime, timedelta, timezone
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

import delivery_store  # noqa: E402
import team_flow as tf  # noqa: E402

CORE_CHECKOUT = Path(os.environ.get(
    "HERMES_AGENT_CHECKOUT",
    Path.home() / "workspace/hermes-work/hermes-agent"))
CORE_REV = "2bd378ed"

SESSION_A = "s1"
SESSION_B = "s2"


class FakeCtx:
    def __init__(self, cfg=None):
        self.cfg = cfg or {}
        self.hooks = {}

    def get_config(self, key, default=None):
        return self.cfg.get(key, default)

    def register_hook(self, name, fn):
        self.hooks[name] = fn


def find_core_python() -> Path:
    """Python que puede importar el core: variable de entorno, o el Python
    del runtime PM instalado (``~/.hermes/installs/*/pm-runtime/...``).

    Falla alto y claro cuando no hay ninguno: la puerta no tiene sentido
    sin el core real.
    """
    env = os.environ.get("HERMES_FASE3B_PYTHON", "").strip()
    if env:
        return Path(env).expanduser()
    installs = Path.home() / ".hermes" / "installs"
    candidates = sorted(
        installs.glob("*/pm-runtime/generations/*/bin/python"),
        key=lambda p: p.stat().st_mtime, reverse=True)
    if candidates:
        return candidates[0]
    raise AssertionError(
        "Fase 3b: no hay un Python que pueda importar el core "
        "(ni HERMES_FASE3B_PYTHON ni ~/.hermes/installs/*/pm-runtime). "
        "Sin el core real la puerta no puede correr.")


class CoreDriver:
    """El core real en un subproceso (ver tests/_fase3b_core_driver.py)."""

    def __init__(self, home: Path):
        self.home = home
        self.proc = subprocess.Popen(
            [str(find_core_python()), str(ROOT / "tests" / "_fase3b_core_driver.py"),
             "--core", str(CORE_CHECKOUT), "--home", str(home),
             "--rev", CORE_REV, "--plugin", str(ROOT)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1,
            env={**os.environ, "HERMES_HOME": str(home)})
        self._closed = False

    def call(self, msg: dict) -> dict:
        if self.proc.stdin is None or self.proc.stdout is None:
            raise AssertionError("driver sin canales")
        self.proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            err = (self.proc.stderr.read() if self.proc.stderr else "")
            raise AssertionError(
                f"el driver del core murió sin responder a {msg.get('cmd')}: "
                f"{err[-2000:]}")
        out = json.loads(line)
        if not out.get("ok"):
            raise AssertionError(
                f"driver {msg.get('cmd')} falló: {out.get('error')}")
        return out

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.call({"cmd": "quit"})
        except Exception:
            pass
        try:
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass


class DriverCronAdapter:
    """Adaptador del protocolo ``cron_bridge`` sobre el driver (escrituras
    reales al core desde el proceso de la puerta). Las lecturas van por el
    lector por disco del plugin, no por aquí."""

    def __init__(self, driver: CoreDriver):
        self.driver = driver
        self.mode = "driver"

    def get_job(self, job_id):
        return self.driver.call({"cmd": "job", "job_id": job_id}).get("job")

    def pause_job(self, job_id, reason=None):
        return self.driver.call(
            {"cmd": "pause", "job_id": job_id, "reason": reason}).get("job")

    def resume_job(self, job_id):
        return self.driver.call({"cmd": "resume", "job_id": job_id}).get("job")

    def remove_job(self, job_id):
        return self.driver.call(
            {"cmd": "remove", "job_id": job_id}).get("removed")

    def terminal_executions(self, job_id, limit=50):
        raise AssertionError("las lecturas van por el lector por disco")

    def latest_output(self, job_id):
        raise AssertionError("las lecturas van por el lector por disco")


def build_env(script_body: str = "print('INFORME: todo en orden')\n"):
    """Home temporal del core + home del plugin + driver. Devuelve
    ``(tmp, home, notify, driver)``."""
    tmp = Path(tempfile.mkdtemp(prefix="fase3b-"))
    home = tmp / "hermes-home"
    (home / "scripts").mkdir(parents=True)
    (home / "scripts" / "probe.py").write_text(script_body, encoding="utf-8")
    (home / "scripts" / "failing.py").write_text(
        "import sys\nprint('algo salió mal')\nsys.exit(1)\n", encoding="utf-8")
    (home / "scripts" / "silent.py").write_text(
        "print('{\"wakeAgent\": false}')\n", encoding="utf-8")
    state_db = tmp / "state.db"
    with sqlite3.connect(state_db) as c:
        c.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, "
                  "parent_session_id TEXT, source TEXT)")
        c.executemany(
            "INSERT INTO sessions VALUES (?,?,?,?)",
            [(SESSION_A, "Bot Chat", None, "chat"),
             (SESSION_B, "Bot Chat", None, "chat")])
    notify = tmp / "plugin" / "bot-chat-agent" / "team-notify.json"
    notify.parent.mkdir(parents=True, exist_ok=True)
    driver = CoreDriver(home)
    return tmp, home, notify, driver


def store_for(notify):
    return bca._context_store()


def one_shot_at(when: datetime) -> str:
    return when.astimezone(timezone.utc).isoformat()


class TestFase3bGate(unittest.TestCase):
    def setUp(self):
        self.tmp, self.home, self.notify, self.driver = build_env()
        os.environ["HERMES_HOME"] = str(self.home)
        bca._state_db = lambda: self.tmp / "state.db"
        bca.register(FakeCtx(cfg={
            "language": "es",
            "team": {"enabled": True, "gods": []},
            "notify_state_path": str(self.notify),
        }))
        import cron_bridge as cb
        import scheduled_tasks as st
        self.cb = cb
        self.st = st
        self.disk = cb.default_adapter(self.home)
        self.assertEqual(self.disk.mode, "disk",
                         "en el proceso de la puerta el core no es importable")
        self.drv = DriverCronAdapter(self.driver)

    def tearDown(self):
        try:
            self.driver.close()
        finally:
            shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------- helpers
    def create_jobs(self, n, *, session=SESSION_A, start_delay=2.0,
                    step=0.5, script="probe.py", schedule_kind="once"):
        """Crea ``n`` trabajos reales en el core y los registra en el
        plugin con consentimiento. Devuelve la lista de registros del core.
        """
        now = datetime.now(timezone.utc)
        specs = []
        for i in range(n):
            when = now + timedelta(seconds=start_delay + i * step)
            specs.append({
                "prompt": f"tarea programada {i}",
                "schedule": one_shot_at(when) if schedule_kind == "once" else "1m",
                "name": f"tarea-{i}",
                "no_agent": True, "script": script, "deliver": "local",
            })
        made = self.driver.call({"cmd": "create", "jobs": specs})["jobs"]
        store = store_for(self.notify)
        for job, spec in zip(made, specs):
            self.st.register(
                store, job_id=job["id"], session_id=session,
                name=job["name"], prompt=spec["prompt"],
                schedule_display=job["schedule_display"],
                schedule_raw=spec["schedule"])
        return made

    def fire_and_observe(self, jobs, session=SESSION_A):
        """Dispara los trabajos en el core y observa+entrega en el plugin.
        Devuelve (fires, observed)."""
        fires = self.driver.call(
            {"cmd": "fire", "job_ids": [j["id"] for j in jobs],
             "timeout_s": 120})["fires"]
        store = store_for(self.notify)
        observed = self.st.observe_fires(store, session, cron=self.disk)
        return fires, observed

    # ------------------------------------------------------- puerta: 20 tareas
    def test_20_tasks_fire_within_60s(self):
        jobs = self.create_jobs(20, start_delay=2.0, step=0.5)
        scheduled = {}
        for job in jobs:
            rec = self.driver.call({"cmd": "job", "job_id": job["id"]})["job"]
            scheduled[job["id"]] = rec["next_run_at"]
        fires, observed = self.fire_and_observe(jobs)
        self.assertEqual(observed["delivered"], 20,
                         f"observadas: {observed}")
        self.assertEqual(observed["failed"], 0)
        store = store_for(self.notify)
        for fire in fires:
            self.assertEqual(fire["status"], "completed", fire)
            row = store.scheduled_fire(fire["execution_id"])
            self.assertIsNotNone(row, f"sin fila de disparo: {fire}")
            delivery = store.get(
                self.st.delivery_task_id(fire["job_id"], fire["execution_id"]))
            self.assertEqual(len(delivery), 1, "una entrega por disparo")
            self.assertEqual(delivery[0]["session_id"], SESSION_A)
            self.assertEqual(delivery[0]["kind"], "completed")
            self.assertIn("INFORME: todo en orden", delivery[0]["summary"])
            # Dentro de ±60 s del instante programado.
            due = datetime.fromisoformat(scheduled[fire["job_id"]])
            delivered_at = datetime.fromtimestamp(
                delivery[0]["created_at"], tz=timezone.utc)
            lag = (delivered_at - due).total_seconds()
            self.assertLessEqual(lag, 60, f"disparo tarde: {lag:.1f}s")
            self.assertGreaterEqual(lag, -60, f"disparo antes de hora: {lag:.1f}s")

    # ------------------------------------------------------- reinicio
    def test_no_loss_after_restart(self):
        jobs = self.create_jobs(3, start_delay=2.0, step=0.5)
        fires, observed = self.fire_and_observe(jobs)
        self.assertEqual(observed["delivered"], 3)
        # "Reinicio": objetos nuevos, mismos ficheros en disco.
        store2 = delivery_store.DeliveryStore(
            delivery_store.store_path_for(self.notify))
        disk2 = self.cb.default_adapter(self.home)
        again = self.st.observe_fires(store2, SESSION_A, cron=disk2)
        self.assertEqual(again["delivered"], 0, "nada que re-entregar")
        self.assertEqual(again["observed"], 3, "los 3 ya estaban contados")
        # Disparos nuevos mientras estaba "caído": se entregan al volver.
        more = self.create_jobs(3, start_delay=1.0, step=0.5)
        fires2 = self.driver.call(
            {"cmd": "fire", "job_ids": [j["id"] for j in more],
             "timeout_s": 120})["fires"]
        self.assertEqual(len(fires2), 3)
        store3 = delivery_store.DeliveryStore(
            delivery_store.store_path_for(self.notify))
        after = self.st.observe_fires(
            store3, SESSION_A, cron=self.cb.default_adapter(self.home))
        self.assertEqual(after["delivered"], 3, f"tras reinicio: {after}")
        total = store3.rows_for_session(SESSION_A, ("pending", "offered"))
        self.assertEqual(len(total), 6, "6 entregas, 0 perdidas, 0 duplicadas")

    # ------------------------------------------------------- sin dobles
    def test_no_double_fire(self):
        jobs = self.create_jobs(1, start_delay=1.0)
        fires, observed = self.fire_and_observe(jobs)
        self.assertEqual(observed["delivered"], 1)
        store = store_for(self.notify)
        second = self.st.observe_fires(store, SESSION_A, cron=self.disk)
        self.assertEqual(second["delivered"], 0, "observar dos veces no duplica")
        self.assertEqual(second["observed"], 1)
        delivery = store.get(self.st.delivery_task_id(
            jobs[0]["id"], fires[0]["execution_id"]))
        self.assertEqual(len(delivery), 1)
        # Ofrecer dos turnos seguidos tampoco duplica la oferta.
        offered1 = store.offer(SESSION_A, turn_id="turn-1")
        offered2 = store.offer(SESSION_A, turn_id="turn-2")
        self.assertEqual(len(offered1), 1)
        self.assertEqual(offered2, offered1, "el mismo turno re-ofrece, no duplica")

    def test_recurring_job_delivers_each_fire_once(self):
        jobs = self.create_jobs(1, schedule_kind="interval")
        jid = jobs[0]["id"]
        for _ in range(2):
            self.driver.call({"cmd": "run", "job_id": jid})
        store = store_for(self.notify)
        observed = self.st.observe_fires(store, SESSION_A, cron=self.disk)
        self.assertEqual(observed["delivered"], 2, f"{observed}")
        rows = store.rows_for_session(SESSION_A, ("pending",))
        self.assertEqual(len(rows), 2)
        self.assertEqual(len({r["task_id"] for r in rows}), 2,
                         "cada disparo tiene su propia fila")

    # ------------------------------------------------------- sin permiso
    def test_no_execution_without_permission(self):
        # Trabajo del core SIN registro en el plugin: dispara, pero el
        # observador no lo entrega (sin consentimiento no hay origen).
        now = datetime.now(timezone.utc)
        made = self.driver.call({"cmd": "create", "jobs": [{
            "prompt": "sin permiso", "schedule": one_shot_at(now + timedelta(seconds=1)),
            "name": "huerfano", "no_agent": True, "script": "probe.py",
            "deliver": "local"}]})["jobs"]
        fires = self.driver.call(
            {"cmd": "fire", "job_ids": [made[0]["id"]], "timeout_s": 60})["fires"]
        self.assertEqual(fires[0]["status"], "completed")
        store = store_for(self.notify)
        observed = self.st.observe_fires(store, SESSION_A, cron=self.disk)
        self.assertEqual(observed["delivered"], 0)
        self.assertEqual(observed["observed"], 0)
        self.assertEqual(store.rows_for_session(SESSION_A, ("pending",)), [])
        # Trabajo pausado: el escaneo de vencidos del core no lo ve.
        jobs = self.create_jobs(1, start_delay=300)
        self.driver.call({"cmd": "pause", "job_id": jobs[0]["id"],
                          "reason": "pausa manual"})
        due = self.driver.call({"cmd": "due"})["job_ids"]
        self.assertNotIn(jobs[0]["id"], due)

    def test_event_trigger_rejected(self):
        store = store_for(self.notify)
        with self.assertRaises(ValueError):
            self.st.register(
                store, job_id="cualquiera", session_id=SESSION_A,
                name="evento", prompt="cuando llegue un mensaje",
                schedule_display="al recibir un mensaje", schedule_raw="",
                trigger="event")

    def test_time_alone_delivers_nothing(self):
        # Sin turno no hay observación: el tiempo por sí solo nunca entrega.
        jobs = self.create_jobs(1, start_delay=1.0)
        self.driver.call({"cmd": "fire", "job_ids": [jobs[0]["id"]],
                          "timeout_s": 60})
        time.sleep(2)
        store = store_for(self.notify)
        self.assertEqual(store.rows_for_session(SESSION_A, ("pending",)), [],
                         "sin observar no hay entrega: el plugin no programa")
        observed = self.st.observe_fires(store, SESSION_A, cron=self.disk)
        self.assertEqual(observed["delivered"], 1, "al observar, se entrega")

    # ------------------------------------------------------- disparo fallido
    def test_failed_fire_is_honest(self):
        jobs = self.create_jobs(1, start_delay=1.0, script="failing.py")
        fires, observed = self.fire_and_observe(jobs)
        self.assertEqual(fires[0]["status"], "failed")
        self.assertEqual(observed["failed"], 1)
        self.assertEqual(observed["delivered"], 0)
        store = store_for(self.notify)
        delivery = store.get(self.st.delivery_task_id(
            jobs[0]["id"], fires[0]["execution_id"]))
        self.assertEqual(len(delivery), 1)
        self.assertEqual(delivery[0]["kind"], "blocked")
        self.assertIn("fall", delivery[0]["summary"].lower())

    def test_silent_fire_delivers_nothing(self):
        jobs = self.create_jobs(1, start_delay=1.0, script="silent.py")
        fires, observed = self.fire_and_observe(jobs)
        self.assertEqual(fires[0]["status"], "completed")
        self.assertEqual(observed["silent"], 1)
        self.assertEqual(observed["delivered"], 0)
        store = store_for(self.notify)
        self.assertEqual(store.rows_for_session(SESSION_A, ("pending",)), [])

    # ------------------------------------------------------- stop-all
    def test_stop_all_pauses_scheduled(self):
        jobs_a = self.create_jobs(2, session=SESSION_A, start_delay=300)
        jobs_b = self.create_jobs(1, session=SESSION_B, start_delay=300)
        result = tf.stop_all_session(self.notify, SESSION_A, cron=self.drv)
        self.assertEqual(sorted(result["cancelled"]), [])
        self.assertEqual(
            sorted(result["paused_scheduled"]),
            sorted(j["id"] for j in jobs_a))
        store = store_for(self.notify)
        for job in jobs_a:
            row = store.scheduled_task(job["id"])
            self.assertEqual(row["status"], "paused_by_stop_all")
            live = self.driver.call({"cmd": "job", "job_id": job["id"]})["job"]
            self.assertEqual(live["state"], "paused")
            self.assertFalse(live["enabled"])
            # El core conserva next_run_at al pausar (resume_job lo
            # mantiene como instante debido si ya pasó); lo que importa
            # es que el trabajo pausado no está vencido.
            self.assertNotIn(job["id"],
                             self.driver.call({"cmd": "due"})["job_ids"])
        for job in jobs_b:
            row = store.scheduled_task(job["id"])
            self.assertEqual(row["status"], "active")
            live = self.driver.call({"cmd": "job", "job_id": job["id"]})["job"]
            self.assertEqual(live["state"], "scheduled")
        self.assertTrue(store.stop_all_active(SESSION_A))
        # Reanudación explícita de UNA: el agente reanuda en el core y el
        # plugin lo observa (limpia el flag y reanuda solo esa).
        target = jobs_a[0]["id"]
        self.driver.call({"cmd": "resume", "job_id": target})
        out = bca._on_post_tool_call(
            tool_name="cronjob",
            args={"action": "resume", "job_id": target},
            result=json.dumps({"success": True, "job": {"id": target}}),
            session_id=SESSION_A, status="ok")
        self.assertIsNone(out)
        row = store.scheduled_task(target)
        self.assertEqual(row["status"], "active")
        live = self.driver.call({"cmd": "job", "job_id": target})["job"]
        self.assertEqual(live["state"], "scheduled")
        self.assertIsNotNone(live["next_run_at"])
        self.assertFalse(store.stop_all_active(SESSION_A),
                         "la reanudación explícita es la confirmación")
        other = store.scheduled_task(jobs_a[1]["id"])
        self.assertEqual(other["status"], "paused_by_stop_all",
                         "la otra sigue pausada")

    # ------------------------------------------------------- guardia
    def test_create_forces_local_delivery(self):
        out = bca._on_pre_tool_call(
            tool_name="cronjob",
            args={"action": "create", "prompt": "x", "schedule": "30m",
                  "deliver": "telegram", "failure_deliver": "bot-chat"},
            session_id=SESSION_A, turn_id="t1")
        self.assertIsNotNone(out)
        self.assertEqual(out.get("action"), "modify")
        self.assertEqual(out["args"].get("deliver"), "local")
        self.assertEqual(out["args"].get("failure_deliver"), "local")
        # Con deliver ya local no hay parche ni bloqueo.
        out2 = bca._on_pre_tool_call(
            tool_name="cronjob",
            args={"action": "create", "prompt": "x", "schedule": "30m",
                  "deliver": "local"},
            session_id=SESSION_A, turn_id="t1")
        self.assertIsNone(out2)
        # Con deliver omitido también se fuerza a local (el defecto del
        # core sería el origen capturado y puentearía el outbox).
        out2b = bca._on_pre_tool_call(
            tool_name="cronjob",
            args={"action": "create", "prompt": "x", "schedule": "30m"},
            session_id=SESSION_A, turn_id="t1")
        self.assertIsNotNone(out2b)
        self.assertEqual(out2b.get("action"), "modify")
        self.assertEqual(out2b["args"].get("deliver"), "local")
        # Con stop-all activo la creación se bloquea.
        store = store_for(self.notify)
        store.set_stop_all(SESSION_A, reason="parada")
        out3 = bca._on_pre_tool_call(
            tool_name="cronjob",
            args={"action": "create", "prompt": "x", "schedule": "30m"},
            session_id=SESSION_A, turn_id="t1")
        self.assertIsNotNone(out3)
        self.assertEqual(out3.get("action"), "block")
        self.assertIn("stop", str(out3.get("message", "")).lower())

    def test_post_tool_call_registers_create_and_pause(self):
        out = bca._on_post_tool_call(
            tool_name="cronjob",
            args={"action": "create", "prompt": "recuérdame", "schedule": "30m"},
            result=json.dumps({"success": True, "job_id": "abc123def456",
                               "name": "recuerdo", "schedule": "cada 30 minutos",
                               "next_run_at": "2026-11-08T10:00:00Z"}),
            session_id=SESSION_A, status="ok")
        self.assertIsNone(out)
        store = store_for(self.notify)
        row = store.scheduled_task("abc123def456")
        self.assertIsNotNone(row)
        self.assertEqual(row["session_id"], SESSION_A)
        self.assertEqual(row["status"], "active")
        self.assertEqual(row["consent_source"], "user_request")
        self.assertTrue(float(row["consent_at"] or 0) > 0)
        # Una creación fallida no registra nada.
        bca._on_post_tool_call(
            tool_name="cronjob",
            args={"action": "create", "prompt": "x", "schedule": "mala"},
            result=json.dumps({"success": False, "error": "Invalid schedule"}),
            session_id=SESSION_A, status="ok")
        self.assertEqual(store.scheduled_tasks(SESSION_A), [row])
        # Pausa observada.
        bca._on_post_tool_call(
            tool_name="cronjob",
            args={"action": "pause", "job_id": "abc123def456"},
            result=json.dumps({"success": True}),
            session_id=SESSION_A, status="ok")
        self.assertEqual(
            store.scheduled_task("abc123def456")["status"], "paused")

    # ------------------------------------------------------- estados visibles
    def test_states_visible_in_turn_note(self):
        jobs = self.create_jobs(2, start_delay=300)
        self.driver.call({"cmd": "pause", "job_id": jobs[1]["id"],
                          "reason": "pausa manual"})
        store = store_for(self.notify)
        self.st.note_core_pause(store, jobs[1]["id"], reason="pausa manual")
        built = bca._build_turn_context(SESSION_A, turn_id="turn-1")
        self.assertIn("tarea-0", built["dynamic"])
        self.assertIn("tarea-1", built["dynamic"])
        self.assertIn("Pausada", built["dynamic"])
        # Segundo turno sin cambios: el delta no repite las tareas.
        bca._on_post_llm_call(session_id=SESSION_A, turn_id="turn-1")
        second = bca._build_turn_context(SESSION_A, turn_id="turn-2")
        self.assertNotIn("tarea-0", second["dynamic"])
        self.assertNotIn("tarea-1", second["dynamic"])

    def test_fire_offered_in_same_turn_note(self):
        jobs = self.create_jobs(1, start_delay=1.0)
        self.driver.call({"cmd": "fire", "job_ids": [jobs[0]["id"]],
                          "timeout_s": 60})
        built = bca._build_turn_context(SESSION_A, turn_id="turn-1")
        self.assertIn("INFORME: todo en orden", built["dynamic"],
                      "el disparo se ofrece en la nota del turno")

    # ------------------------------------------------------- lector por disco
    def test_bridge_modes_agree(self):
        jobs = self.create_jobs(1, start_delay=1.0)
        fires = self.driver.call(
            {"cmd": "fire", "job_ids": [jobs[0]["id"]], "timeout_s": 60})["fires"]
        disk_execs = self.disk.terminal_executions(jobs[0]["id"])
        disk_out = self.disk.latest_output(jobs[0]["id"])
        bridged = self.driver.call({"cmd": "bridge", "job_id": jobs[0]["id"]})
        self.assertIn(bridged["mode"], ("registry", "disk"))
        self.assertEqual(
            [e["id"] for e in bridged["executions"]],
            [e["id"] for e in disk_execs],
            "el lector por disco ve las mismas ejecuciones que el core")
        self.assertEqual(bridged["output"], disk_out)
        self.assertIn("INFORME: todo en orden", disk_out)
        self.assertEqual(fires[0]["execution_id"], disk_execs[0]["id"])

    def test_extract_response_shapes(self):
        agent_doc = (
            "# Cron Job: t\n\n**Job ID:** x\n**Run Time:** 2026-11-08 10:00:00\n"
            "**Schedule:** cada 30 minutos\n\n**Prompt Characters:** 5\n"
            "## Prompt\n\ntexto\n\n**Response Characters:** 11\n"
            "## Response\n\nhecho: 42\n")
        self.assertEqual(self.cb.extract_response(agent_doc), "hecho: 42")
        no_agent_doc = ("# Cron Job: t\n\n**Job ID:** x\n**Run Time:** 2026-11-08 10:00:00\n"
                        "**Mode:** no_agent (script)\n\n---\n\nINFORME: todo en orden\n")
        self.assertEqual(self.cb.extract_response(no_agent_doc),
                         "INFORME: todo en orden")
        self.assertEqual(self.cb.extract_response(""), "")
        self.assertTrue(self.cb.is_silent_response("[SILENT]"))
        self.assertTrue(self.cb.is_silent_response("SILENT"))
        self.assertFalse(self.cb.is_silent_response("hecho: [SILENT] citado"))

    # ------------------------------------------------------- presupuesto
    def test_style_budget_still_300(self):
        import context_budget  # noqa: E402
        for lang in ("en", "es"):
            tokens = context_budget.estimate_tokens(bca.STYLE[lang].rstrip())
            self.assertLessEqual(tokens, 300,
                                 f"STYLE[{lang}] supera el presupuesto estable")


if __name__ == "__main__":
    unittest.main()
