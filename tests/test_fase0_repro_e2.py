"""E2 (Fase 0) — reproducción del PermissionError de ``os.replace`` en Windows.

``team_flow._save_state`` escribe un temporal y hace ``os.replace(tmp, path)``
sobre ``team-notify.json``. En Windows, mientras **otro proceso** mantiene ese
archivo abierto, ``MoveFileEx(MOVEFILE_REPLACE_EXISTING)`` falla con
``[WinError 5] Acceso denegado``; ``record_delivery`` no lo maneja, la excepción
sube sin capturar y la entrega se pierde.

Estado esperado:

- **Hoy (0.3.0-dev, 89f8b25): el test FALLA.** Es su propósito: fija el defecto.
- **Tras la Fase 1a** (almacén en SQLite, sin ``os.replace`` sobre el JSON de
  notificaciones) **debe PASAR** sin tocar el test.

Medido en este host (Windows 11, Python 3.14.9): basta ``open(path, "rb")`` en el
otro proceso; **no** hace falta bloqueo exclusivo. ``E2_HOLDER_MODE=lock`` activa
el modo con ``msvcrt.locking`` por si otro Windows lo exige.

El test no modifica el plugin: los dos procesos hijos cargan ``team_flow.py``
tal cual desde la copia del repo.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]
TASK_ID = "t_e2fase0"
RECORDER_TIMEOUT = 60

#: Proceso que mantiene ``team-notify.json`` abierto hasta que el test lo cierra.
HOLDER_CODE = r'''
import sys

path, mode = sys.argv[1], sys.argv[2]
handle = open(path, "rb")
handle.read()
if mode == "lock":
    import msvcrt
    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
print("READY", flush=True)
sys.stdin.readline()
handle.close()
'''

#: Proceso que registra la entrega usando el ``team_flow`` real del repo.
RECORDER_CODE = r'''
import importlib.util
import sys
from pathlib import Path

root, path, task_id = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
spec = importlib.util.spec_from_file_location("team_flow_e2", root / "team_flow.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.record_delivery(
    path,
    task_id=task_id,
    assignee="dike",
    summary="E2: entrega con team-notify.json abierto por otro proceso",
    gods=["dike"],
)
print("RECORDED", flush=True)
'''


@unittest.skipUnless(os.name == "nt", "E2 reproduce un bloqueo de ficheros de Windows")
class ReplaceWhileAnotherProcessHoldsTheFileTests(unittest.TestCase):
    """Fija E2: la entrega debe quedar registrada aunque el JSON esté abierto."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="e2-fase0-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = self.tmp / "team-notify.json"
        self.state.write_text(
            json.dumps(
                {
                    "pending": {
                        "t_prev01": {"assignee": "apollo", "summary": "Pendiente previo 1"},
                        "t_prev02": {"assignee": "harmonia", "summary": "Pendiente previo 2"},
                        "t_prev03": {"assignee": "rafael", "summary": "Pendiente previo 3"},
                    },
                    "announced": [],
                    "creates": [],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        self.holder: subprocess.Popen | None = None
        self.mode = os.environ.get("E2_HOLDER_MODE", "plain")

    # -- procesos ------------------------------------------------------- #

    def _start_holder(self) -> None:
        self.holder = subprocess.Popen(
            [sys.executable, "-u", "-B", "-c", HOLDER_CODE, str(self.state), self.mode],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(self._stop_holder)
        ready = (self.holder.stdout.readline() or "").strip()
        if ready != "READY":
            error = (self.holder.stderr.read() or "").strip()
            self.fail(f"el proceso que retiene el archivo no arrancó (modo={self.mode}): {error}")

    def _stop_holder(self) -> None:
        holder = self.holder
        if holder is None:
            return
        try:
            if holder.poll() is None:
                try:
                    holder.stdin.write("\n")
                    holder.stdin.flush()
                except (OSError, ValueError):
                    pass
                try:
                    holder.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    holder.kill()
                    holder.wait(timeout=10)
        finally:
            for stream in (holder.stdin, holder.stdout, holder.stderr):
                try:
                    if stream is not None:
                        stream.close()
                except OSError:
                    pass
            self.holder = None

    def _record_in_another_process(self) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-B", "-c", RECORDER_CODE, str(ROOT), str(self.state), TASK_ID],
            capture_output=True, text=True, timeout=RECORDER_TIMEOUT,
        )

    # -- pruebas -------------------------------------------------------- #

    def test_delivery_is_recorded_while_the_file_is_open(self) -> None:
        """E2: con el JSON abierto en otro proceso, la entrega debe registrarse."""
        self._start_holder()
        result = self._record_in_another_process()
        self._stop_holder()

        detail = (
            f"modo={self.mode} rc={result.returncode}\n"
            f"stdout={(result.stdout or '').strip()[:400]}\n"
            f"stderr={(result.stderr or '').strip()[-1500:]}"
        )
        self.assertNotIn(
            "PermissionError", result.stderr or "",
            "record_delivery propagó un PermissionError sin manejar con el archivo "
            "abierto por otro proceso (E2 reproducido):\n" + detail,
        )
        self.assertEqual(
            result.returncode, 0,
            "record_delivery falló con el archivo abierto por otro proceso:\n" + detail,
        )
        self.assertIn("RECORDED", result.stdout or "", "el proceso no llegó al final:\n" + detail)

        data = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertIn(TASK_ID, data.get("pending", {}), "la entrega no quedó registrada en team-notify.json")
        self.assertEqual(data["pending"][TASK_ID].get("assignee"), "dike")
        for previous in ("t_prev01", "t_prev02", "t_prev03"):
            self.assertIn(previous, data["pending"], f"se perdió el pendiente previo {previous}")
        self.assertFalse(
            self.state.with_suffix(self.state.suffix + ".tmp").exists(),
            "quedó un temporal huérfano tras el registro",
        )

    def test_delivery_is_recorded_when_nobody_holds_the_file(self) -> None:
        """Control: sin nadie reteniendo el archivo, el registro ya funciona hoy."""
        result = self._record_in_another_process()
        self.assertEqual(result.returncode, 0, f"(control) record_delivery falló:\n{result.stderr[-1500:]}")
        data = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertIn(TASK_ID, data.get("pending", {}), "(control) la entrega no quedó registrada")


if __name__ == "__main__":
    unittest.main()
