"""Fase 3b — driver del core real (NO es un test; lo ejecuta la puerta).

Corre en un subproceso con el Python del runtime PM (el Python del sistema
no puede importar el core: faltan ``ruamel``, ``dotenv``, ...). Habla JSON
por líneas por stdin/stdout: la puerta (proceso padre) le pide crear,
disparar, pausar, reanudar y leer trabajos del core, y él ejecuta las
llamadas REALES contra el checkout vivo (``cron.jobs`` / ``cron.executions``
/ ``cron.scheduler.run_one_job``). No tiene lógica propia más allá del
sondeo de vencidos: cada respuesta es el resultado directo de la API del
core.

Dos adaptaciones del entorno, documentadas (rúbrica §5):

- ``dotenv`` se sustituye en ``sys.modules`` antes de importar el core, el
  mismo patrón que usan los tests de gateway del propio core
  (``tests/hermes_cli/test_fireworks_provider.py``): el home temporal no
  tiene fichero ``.env``, así que ``load_dotenv`` es un no-op allí.
- ``tui_gateway/`` no está materializado en el clon parcial (sparse
  checkout); el scheduler importa ``tui_gateway.launch_profile_policy`` en
  cada disparo. El driver extrae esos dos ficheros REALES (byte a byte,
  vivo ``2bd378ed``, vía ``git show``) a un directorio temporal y lo pone
  primero en ``sys.path``. El checkout no se toca.

Protocolo (una línea JSON por mensaje):

- ``{"cmd": "ping"}`` → ``{"ok": true, "python": ..., "core": ...}``
- ``{"cmd": "create", "jobs": [{...kwargs de create_job...}]}`` →
  ``{"ok": true, "jobs": [{id, name, next_run_at, state, ...}]}``
- ``{"cmd": "fire", "job_ids": [...], "timeout_s": 120}`` → sondea
  ``get_due_jobs()`` y ejecuta ``run_one_job`` sobre cada trabajo debido
  esperado hasta que todos tengan una ejecución terminal, o agota el
  timeout: ``{"ok": true, "fires": [{job_id, execution_id, status,
  finished_at, error}]}``
- ``{"cmd": "job", "job_id": ...}`` → ``{"ok": true, "job": {...}|null}``
- ``{"cmd": "due"}`` → ``{"ok": true, "job_ids": [...]}``
- ``{"cmd": "pause"|"resume", "job_id": ..., "reason": ...}`` →
  ``{"ok": true, "job": {...}|null}``
- ``{"cmd": "run", "job_id": ...}`` → ejecuta ``run_one_job`` sobre el
  trabajo aunque no esté vencido (para probar la deduplicación por
  ejecución con un trabajo recurrente): ``{"ok": true, "ran": bool,
  "execution_id": ..., "status": ...}``
- ``{"cmd": "remove", "job_id": ...}`` → ``{"ok": true, "removed": bool}``
- ``{"cmd": "bridge", "job_id": ...}`` → importa el ``cron_bridge`` del
  plugin en ESTE proceso (donde el core sí es importable) y devuelve lo que
  ve su modo por importación: ``{"ok": true, "executions": [...],
  "output": "..."}``. La puerta lo compara con su propia lectura por disco.
- ``{"cmd": "quit"}`` → sale con código 0.

Cualquier error devuelve ``{"ok": false, "error": ...}`` sin tumbar el
proceso (salvo ``quit``).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import types
from pathlib import Path


def _stub_dotenv() -> None:
    """El core importa ``dotenv`` a nivel de módulo (``hermes_cli.env_loader``).

    Sus propios tests de gateway hacen stub de ``sys.modules["dotenv"]``;
    aquí igual: el home temporal no tiene ``.env``.
    """
    if "dotenv" in sys.modules:
        return
    fake = types.ModuleType("dotenv")
    fake.load_dotenv = lambda *args, **kwargs: False
    fake.dotenv_values = lambda *args, **kwargs: {}
    sys.modules["dotenv"] = fake


def _extract_tui_shim(core: Path, rev: str) -> Path:
    """Extrae los ficheros reales de ``tui_gateway`` que el clon parcial no
    materializó. Solo lectura del checkout (``git show``); el checkout no
    se toca."""
    out = Path(tempfile.mkdtemp(prefix="fase3b-tui-shim-")) / "tui_gateway"
    out.mkdir(parents=True)
    if (core / "tui_gateway" / "__init__.py").is_file():
        # Full checkout / release tarball (no partial clone): the real files are
        # already importable from ``core``; the shim stays empty and unused.
        return out.parent
    for name in ("__init__.py", "launch_profile_policy.py"):
        proc = subprocess.run(
            ["git", "-C", str(core), "-c", "gc.auto=0", "show",
             f"{rev}:tui_gateway/{name}"],
            capture_output=True, timeout=60)
        if proc.returncode != 0:
            raise RuntimeError(
                f"no se pudo extraer tui_gateway/{name} de {rev}: "
                f"{proc.stderr.decode('utf-8', 'replace')[:300]}")
        (out / name).write_bytes(proc.stdout)
    return out.parent


def _send(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--core", required=True)
    ap.add_argument("--home", required=True)
    ap.add_argument("--rev", default="2bd378ed")
    ap.add_argument("--plugin", default="")
    args = ap.parse_args()

    core = Path(args.core).expanduser().resolve()
    home = Path(args.home).expanduser().resolve()
    os.environ["HERMES_HOME"] = str(home)

    shim = _extract_tui_shim(core, args.rev)
    sys.path.insert(0, str(shim))
    _stub_dotenv()
    sys.path.insert(0, str(core))

    from cron import executions as cex  # noqa: E402
    from cron import jobs as cj  # noqa: E402
    from cron.scheduler import run_one_job  # noqa: E402

    terminal = {"completed", "failed", "cancelled"}

    def do_create(msg):
        made = []
        for spec in msg.get("jobs") or []:
            job = cj.create_job(**spec)
            made.append({k: job.get(k) for k in (
                "id", "name", "state", "enabled", "next_run_at",
                "schedule_display", "deliver", "no_agent", "script")})
        return {"ok": True, "jobs": made}

    def do_fire(msg):
        wanted = [str(j) for j in (msg.get("job_ids") or [])]
        pending = set(wanted)
        deadline = time.time() + float(msg.get("timeout_s") or 120)
        fires: dict[str, dict] = {}
        while pending and time.time() < deadline:
            for job in cj.get_due_jobs():
                jid = str(job.get("id") or "")
                if jid in pending:
                    run_one_job(dict(job))
                    pending.discard(jid)
            if pending:
                time.sleep(0.5)
        for jid in wanted:
            latest = cex.latest_execution(jid)
            if latest is not None:
                fires[jid] = {
                    "job_id": jid, "execution_id": str(latest.get("id") or ""),
                    "status": str(latest.get("status") or ""),
                    "finished_at": str(latest.get("finished_at") or ""),
                    "error": str(latest.get("error") or ""),
                    "scheduled_instant": str(latest.get("scheduled_instant") or ""),
                }
        missing = [jid for jid in wanted
                   if fires.get(jid, {}).get("status") not in terminal]
        if missing:
            return {"ok": False, "error": f"sin ejecución terminal: {missing}",
                    "fires": [fires[j] for j in wanted if j in fires]}
        return {"ok": True, "fires": [fires[j] for j in wanted]}

    def do_bridge(msg):
        jid = str(msg.get("job_id") or "")
        plugin = Path(args.plugin).expanduser().resolve() if args.plugin else None
        if plugin is None or not (plugin / "cron_bridge.py").is_file():
            return {"ok": False, "error": "plugin root sin cron_bridge.py"}
        if str(plugin) not in sys.path:
            sys.path.insert(0, str(plugin))
        import cron_bridge as cb
        adapter = cb.default_adapter(home)
        return {"ok": True, "mode": adapter.mode,
                "executions": adapter.terminal_executions(jid, limit=50),
                "output": adapter.latest_output(jid)}

    def do_run(msg):
        jid = str(msg.get("job_id") or "")
        job = cj.get_job(jid)
        if job is None:
            return {"ok": False, "error": f"trabajo desconocido: {jid}"}
        ran = run_one_job(dict(job))
        latest = cex.latest_execution(jid)
        return {"ok": True, "ran": bool(ran),
                "execution_id": str(latest.get("id") or "") if latest else "",
                "status": str(latest.get("status") or "") if latest else ""}

    handlers = {
        "ping": lambda m: {"ok": True, "python": sys.executable,
                           "core": str(core), "home": str(home)},
        "create": do_create,
        "fire": do_fire,
        "job": lambda m: {"ok": True, "job": cj.get_job(str(m.get("job_id") or ""))},
        "due": lambda m: {"ok": True,
                          "job_ids": [str(j.get("id") or "") for j in cj.get_due_jobs()]},
        "pause": lambda m: {"ok": True, "job": cj.pause_job(
            str(m.get("job_id") or ""), reason=m.get("reason") or None)},
        "run": do_run,
        "resume": lambda m: {"ok": True, "job": cj.resume_job(str(m.get("job_id") or ""))},
        "remove": lambda m: {"ok": True,
                             "removed": bool(cj.remove_job(str(m.get("job_id") or "")))},
        "bridge": do_bridge,
    }

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError as exc:
            _send({"ok": False, "error": f"JSON inválido: {exc}"})
            continue
        cmd = str(msg.get("cmd") or "")
        if cmd == "quit":
            _send({"ok": True})
            return 0
        fn = handlers.get(cmd)
        if fn is None:
            _send({"ok": False, "error": f"comando desconocido: {cmd!r}"})
            continue
        try:
            _send(fn(msg))
        except Exception as exc:  # noqa: BLE001 - el driver nunca debe caerse
            _send({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
