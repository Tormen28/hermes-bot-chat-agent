"""Fase 0 — reproducción de E1: pérdida de avisos entre procesos.

Falla (v1.2 §1.2 E1): ``record_delivery`` corre en el proceso worker y
``take_announcements`` en el proceso del chat; ambos hacen leer-modificar-escribir
sobre ``team-notify.json`` protegidos solo por ``threading.Lock``, que es válido
dentro de un proceso e inútil entre procesos. El último escritor gana y las
entregas de los demás se pierden en silencio.

Este fixture USA PROCESOS REALES (``multiprocessing`` con ``fork``). Con hilos
la falla NO se reproduce: los hilos comparten el lock del módulo y se
serializan. 2 productores x 100 entregas + 1 consumidor concurrente.

Estado esperado: este test FALLA con el código actual (89f8b25) y debe PASAR
cuando la Fase 1a sustituya el JSON por un almacén seguro entre procesos.

Notas:
- Solo Linux/fork (el entorno cerrado de Fase 0 es Python 3.12.3; el vivo usa
  3.14.9 — no se usa ningún rasgo posterior a 3.12).
- E2 (``os.replace`` -> ``PermissionError``) NO se reproduce aquí: en Linux el
  reemplazo funciona aunque otro proceso tenga el archivo abierto; es solo
  Windows y queda anotado como tal.
"""
import json
import multiprocessing as mp
import shutil
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

if "team_flow" in sys.modules:
    tf = sys.modules["team_flow"]
else:
    import importlib.util

    spec = importlib.util.spec_from_file_location("team_flow", ROOT / "team_flow.py")
    tf = importlib.util.module_from_spec(spec)
    sys.modules["team_flow"] = tf
    spec.loader.exec_module(tf)

GODS = ["researcher"]
N_PRODUCERS = 2
PER_PRODUCER = 100
N_TOTAL = N_PRODUCERS * PER_PRODUCER  # 200 entregas


def _producer(notify_path, task_ids):
    """Proceso worker: registra entregas como lo hace el hook kanban_task_completed."""
    for tid in task_ids:
        tf.record_delivery(
            Path(notify_path),
            task_id=tid,
            assignee="researcher",
            summary=f"result {tid}",
            gods=list(GODS),
        )


def _consumer(notify_path, taken_path, stop_event):
    """Proceso del chat: toma avisos como lo hace pre_llm_call/team_note."""
    with open(taken_path, "a", encoding="utf-8") as fh:
        while not stop_event.is_set():
            fresh = tf.take_announcements(Path(notify_path))
            if fresh:
                fh.write("\n".join(item["id"] for item in fresh) + "\n")
                fh.flush()
            time.sleep(0.0005)
        # Drenaje final: lo que llegó después del último ciclo.
        fresh = tf.take_announcements(Path(notify_path))
        if fresh:
            fh.write("\n".join(item["id"] for item in fresh) + "\n")
            fh.flush()


class TestReproE1(unittest.TestCase):
    @requires_fork
    def test_cross_process_delivery_loss(self):
        """E1: 200 entregas desde 2 procesos + 1 consumidor; 0 avisos perdidos."""
        ctx = mp.get_context("fork")
        tmp = Path(tempfile.mkdtemp(prefix="fase0-e1-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        notify = tmp / "team-notify.json"
        taken_path = tmp / "taken.txt"

        expected = [f"t-{i:04d}" for i in range(N_TOTAL)]
        chunks = [
            expected[i * PER_PRODUCER:(i + 1) * PER_PRODUCER]
            for i in range(N_PRODUCERS)
        ]

        stop = ctx.Event()
        consumer = ctx.Process(
            target=_consumer, args=(str(notify), str(taken_path), stop)
        )
        producers = [
            ctx.Process(target=_producer, args=(str(notify), chunk))
            for chunk in chunks
        ]

        start = time.monotonic()
        consumer.start()
        for p in producers:
            p.start()
        for p in producers:
            p.join(timeout=120)
        stop.set()
        consumer.join(timeout=60)
        elapsed = time.monotonic() - start

        alive = [p for p in producers + [consumer] if p.is_alive()]
        for p in alive:
            p.terminate()
        self.assertEqual(alive, [], "procesos del fixture no terminaron a tiempo")

        final = (
            json.loads(notify.read_text(encoding="utf-8"))
            if notify.is_file()
            else {"pending": {}, "announced": []}
        )
        taken = (
            {line.strip() for line in taken_path.read_text(encoding="utf-8").splitlines()
             if line.strip()}
            if taken_path.is_file()
            else set()
        )
        observed = (
            set(final.get("pending", {}))
            | {str(x) for x in final.get("announced", [])}
            | taken
        )
        lost = sorted(set(expected) - observed)
        msg = (
            f"E1 REPRODUCIDA: se perdieron {len(lost)}/{N_TOTAL} avisos "
            f"({len(lost) / N_TOTAL:.1%}) en {elapsed:.2f}s con {N_PRODUCERS} "
            f"productores y 1 consumidor en procesos reales; primeros perdidos: "
            f"{lost[:15]}; observados={len(observed)} "
            f"(pending={len(final.get('pending', {}))}, "
            f"announced={len(final.get('announced', []))}, "
            f"tomados por el consumidor={len(taken)})"
        )
        self.assertEqual(len(lost), 0, msg)


if __name__ == "__main__":
    unittest.main()
