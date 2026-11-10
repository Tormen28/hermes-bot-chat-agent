"""Fase 5 gate (rubric docs/fase5-rubric.md v1.0, level A, offline).

Evals propios como puerta: ACK, TTFT (lado plugin), cola, herramientas,
handoff, entrega, interrupción, estrés de hooks, diez secuencias de
100 turnos, pocos recursos, errores, recuperación tras kill -9 y la
parte medible offline de la barra (JS puro). La batería completa y las
puertas 1a/1b/2/3/3b/4 se re-ejecutan aparte y el informe de medición
(docs/fase5-medicion.md) registra su resultado.

Solo Linux/fork y Python 3.12.3 en este entorno (el vivo usa 3.14.9);
no se usa ningún rasgo posterior a 3.12.
"""
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

import _fase5_bench as bench  # noqa: E402
import delivery_store  # noqa: E402

ACK_P95_MS = 100.0
TTFT_HOT_P95_MS = 100.0
TTFT_COLD_P95_MS = 500.0
HOOK_P95_MS = 100.0
GUARD_P95_MS = 100.0
HANDOFF_P95_MS = 100.0
DELIVERY_P95_MS = 100.0
STOPPED_P95_MS = 500.0
RECOVERY_REOPEN_MS = 1000.0
BAR_JS_P95_MS = 50.0


class TestFase5Gate(unittest.TestCase):
    def setUp(self):
        self.env = bench.Env()
        self.addCleanup(self.env.cleanup)

    def test_ack_p95(self):
        hot = bench.run_reps("ack/hot", bench.ack_hot(self.env), 200)
        self.assertEqual(hot["failures"], 0, f"ACK caliente con fallos: {hot}")
        self.assertLessEqual(hot["p95_ms"], ACK_P95_MS, f"ACK caliente p95: {hot}")
        cold = bench.run_reps("ack/cold", bench.ack_cold, 200)
        self.assertEqual(cold["failures"], 0, f"ACK frío con fallos: {cold}")
        self.assertLessEqual(cold["p95_ms"], 500.0, f"ACK frío p95: {cold}")

    def test_ttft_plugin_side(self):
        hot = bench.run_reps("ttft/pre_llm_hot", bench.pre_hot(self.env), 200)
        self.assertEqual(hot["failures"], 0, f"pre_llm_call caliente con fallos: {hot}")
        self.assertLessEqual(hot["p95_ms"], TTFT_HOT_P95_MS,
                             f"TTFT lado plugin caliente p95: {hot}")
        pending = bench.run_reps("ttft/pre_llm_hot_pending",
                                 bench.pre_hot(self.env, pending=True), 200)
        self.assertEqual(pending["failures"], 0, f"pre con oferta con fallos: {pending}")
        self.assertLessEqual(pending["p95_ms"], TTFT_HOT_P95_MS,
                             f"TTFT con oferta p95: {pending}")
        cold = bench.run_reps("ttft/pre_llm_cold", bench.pre_cold, 200)
        self.assertEqual(cold["failures"], 0, f"pre_llm_call frío con fallos: {cold}")
        self.assertLessEqual(cold["p95_ms"], TTFT_COLD_P95_MS,
                             f"TTFT lado plugin frío p95: {cold}")

    def test_queue_latency_and_limit(self):
        rows = bench.queue_checks(self.env)
        for row in rows.values():
            self.assertEqual(row["failures"], 0, f"cola con fallos: {row}")
            self.assertLessEqual(row["p95_ms"], 100.0, f"cola p95: {row}")

    def test_tool_guard_latency(self):
        allow = bench.run_reps("tools/guard_allow", bench.guard_allow(self.env), 200)
        self.assertEqual(allow["failures"], 0, f"guardia (permitida) con fallos: {allow}")
        self.assertLessEqual(allow["p95_ms"], GUARD_P95_MS, f"guardia p95: {allow}")
        block = bench.run_reps("tools/guard_block", bench.guard_block(self.env), 200)
        self.assertEqual(block["failures"], 0, f"guardia (bloqueada) con fallos: {block}")
        self.assertLessEqual(block["p95_ms"], GUARD_P95_MS, f"guardia p95: {block}")
        post = bench.run_reps("tools/post_tool", bench.post_tool(self.env), 200)
        self.assertEqual(post["failures"], 0, f"post_tool_call con fallos: {post}")
        self.assertLessEqual(post["p95_ms"], GUARD_P95_MS, f"post_tool p95: {post}")

    def test_handoff_latency(self):
        row = bench.run_reps("handoff/record_to_offer", bench.handoff(self.env), 200)
        self.assertEqual(row["failures"], 0, f"handoff con fallos: {row}")
        self.assertLessEqual(row["p95_ms"], HANDOFF_P95_MS, f"handoff p95: {row}")

    def test_delivery_cycle_latency(self):
        row = bench.run_reps("delivery/offer_to_confirm",
                             bench.delivery_cycle(self.env), 200)
        self.assertEqual(row["failures"], 0, f"ciclo de entrega con fallos: {row}")
        self.assertLessEqual(row["p95_ms"], DELIVERY_P95_MS, f"entrega p95: {row}")
        relay = bench.run_reps("delivery/relay_claim_confirm",
                               bench.relay_cycle(self.env), 200)
        self.assertEqual(relay["failures"], 0, f"relay con fallos: {relay}")
        self.assertLessEqual(relay["p95_ms"], DELIVERY_P95_MS, f"relay p95: {relay}")

    def test_interruption_latency(self):
        row = bench.run_reps("interrupt/agent_loop_stopped",
                             bench.stopped_hot(self.env), 200)
        self.assertEqual(row["failures"], 0, f"interrupción con fallos: {row}")
        self.assertLessEqual(row["p95_ms"], STOPPED_P95_MS,
                             f"interrupción p95: {row}")

    def test_hook_stress_latency(self):
        for kind in ("claimed", "spawned", "completed", "blocked"):
            row = bench.run_reps(f"hook/{kind}", bench.hook_fn(self.env, kind), 100)
            self.assertEqual(row["failures"], 0, f"hook {kind} con fallos: {row}")
            self.assertLessEqual(row["p95_ms"], HOOK_P95_MS,
                                 f"hook {kind} p95: {row}")

    def test_long_sessions_10x100(self):
        res = bench.run_long_sessions(self.env, sequences=10, turns=100)
        self.assertEqual(res["pre"]["failures"], 0,
                         f"sesiones largas con fallos: {res['pre']}")
        self.assertLessEqual(res["pre"]["p95_ms"], TTFT_HOT_P95_MS,
                             f"pre en sesiones largas p95: {res['pre']}")
        self.assertEqual(res["budget_violations"], 0,
                         f"violaciones de presupuesto: {res}")
        for sid, acc in res["accounting"].items():
            self.assertEqual(acc["lost"], 0, f"pérdidas en {sid}: {acc}")
            self.assertEqual(acc["duplicates"], 0, f"duplicados en {sid}: {acc}")
            self.assertEqual(acc["pending"], 0, f"pendientes en {sid}: {acc}")
            self.assertEqual(acc["offered_left"], 0, f"ofrecidas en {sid}: {acc}")
            self.assertEqual(acc["delivered"], acc["recorded"],
                             f"no todo llegó en {sid}: {acc}")
        self.assertLessEqual(res["slope_last_vs_first_decile_p50"], 2.0,
                             f"crecimiento sin cota entre deciles: {res}")

    def test_low_resources_loaded_store(self):
        base = bench.run_reps("ttft/pre_llm_hot", bench.pre_hot(self.env), 200)
        bench.preload_store(
            delivery_store.store_path_for(self.env.notify), 10000)
        loaded = bench.run_reps("ttft/pre_llm_hot", bench.pre_hot(self.env), 200)
        self.assertEqual(loaded["failures"], 0, f"cargado con fallos: {loaded}")
        self.assertLessEqual(
            loaded["p95_ms"], 2.0 * max(base["p95_ms"], 1e-9),
            f"p95 con 10.000 filas {loaded['p95_ms']} > 2x base {base['p95_ms']}")
        offer = bench.run_reps("handoff/offer_1000_pending",
                               bench.offer_many(self.env, 1000), 10)
        self.assertEqual(offer["failures"], 0, f"offer de 1.000 con fallos: {offer}")

    def test_low_resources_cpu_contention(self):
        with bench.cpu_hog(2):
            row = bench.run_reps("ttft/pre_llm_hot", bench.pre_hot(self.env), 200)
        self.assertEqual(row["failures"], 0, f"contención con fallos: {row}")
        # Sin meta absoluta bajo contención (el hardware manda): solo que
        # nada se pierda; la degradación se publica en el informe.

    def test_error_paths_fail_closed(self):
        out = bench.error_paths(self.env)
        self.assertNotIn("corrupt_pre_raised", out, f"el turno lanzó: {out}")
        self.assertTrue(out.get("corrupt_pre_returned"),
                        f"el turno no renderizó con la base corrupta: {out}")
        self.assertTrue(out.get("row_survived_corrupt"),
                        f"la fila no sobrevivió a la base corrupta: {out}")
        lock = bench.error_paths_with_lock_release(self.env)
        self.assertTrue(lock.get("present"),
                        f"la escritura no sobrevivió a la espera del lock: {lock}")

    def test_recovery_after_kill9(self):
        res = bench.recovery_after_kill9()
        self.assertEqual(res["integrity"], "ok", f"integridad: {res}")
        self.assertEqual(res["lost"], 0, f"pérdidas tras kill -9: {res}")
        self.assertGreater(res["journaled"], 0, f"el escritor no escribió: {res}")
        self.assertLessEqual(res["reopen_ms"], RECOVERY_REOPEN_MS,
                             f"reapertura tras kill -9: {res}")

    def test_bar_js_latency(self):
        res = bench.bar_js_bench(200)
        self.assertNotIn("skipped", res, "node no disponible para la barra")
        self.assertEqual(res.get("failures"), 0, f"barra JS con fallos: {res}")
        self.assertLessEqual(res["p95_ms"], BAR_JS_P95_MS,
                             f"barra JS p95: {res}")


class TestFase5MultiprocessHandoff(unittest.TestCase):
    """Validación real con 2 procesos: productor + consumidor (Fase 1b)."""

    def test_two_processes_zero_loss(self):
        import multiprocessing as mp

        tmp = Path(tempfile.mkdtemp(prefix="fase5-mp-"))
        notify = tmp / "team-notify.json"
        store_path = delivery_store.store_path_for(notify)

        def _producer(path, task_ids):
            store = delivery_store.DeliveryStore(path)
            for tid in task_ids:
                store.record(kind="completed", task_id=tid, session_id="s1",
                             assignee="researcher", summary=f"result {tid}")

        ctx = mp.get_context("fork")
        task_ids = [f"mp-{i}" for i in range(200)]
        proc = ctx.Process(target=_producer, args=(store_path, task_ids))
        proc.start()
        proc.join(timeout=60)
        self.assertEqual(proc.exitcode, 0, "el productor falló")
        reader = delivery_store.DeliveryStore(store_path)
        offered = reader.offer("s1", turn_id="mp-turn")
        seen = {r["task_id"] for r in offered}
        self.assertEqual(seen, set(task_ids),
                         f"faltantes: {set(task_ids) - seen}")
        self.assertEqual(len(offered), len(task_ids), "duplicados en la oferta")
        reader.confirm("s1", turn_id="mp-turn")
        rows = reader.rows_for_session("s1")
        self.assertTrue(all(r["status"] == "delivered_in_turn" for r in rows))
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
