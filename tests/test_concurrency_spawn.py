"""Concurrency with REAL contention, portable (spawn): runs on Linux, macOS and Windows.

* producers: N processes write at the same time (own + shared task ids) - no row lost,
  shared ids deduplicated, SQLite integrity ok;
* consumers: N processes claim from the same backlog at the same time with a small
  per-claim limit - every row claimed exactly once, and the work is really shared
  (the old R25 probe let one consumer take all 50 rows, so it proved nothing about contention).
"""
import multiprocessing as mp
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _mp_workers  # noqa: E402
import delivery_store  # noqa: E402

PROCS = 4


class TestSpawnContention(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bca-spawn-"))
        self.db = self.tmp / "deliveries.db"
        self.ctx = mp.get_context("spawn")

    def test_producers_no_loss_shared_ids_deduplicated(self):
        own, shared = 100, 20
        barrier = self.ctx.Barrier(PROCS)
        procs = [self.ctx.Process(target=_mp_workers.produce,
                                  args=(str(ROOT), str(self.db), barrier, w, own, shared, "s-1"))
                 for w in range(PROCS)]
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=180)
            self.assertEqual(p.exitcode, 0, "a producer crashed or hung")
        store = delivery_store.DeliveryStore(self.db)
        self.assertEqual(store.integrity_check(), "ok")
        self.assertEqual(store.stats()["total"], PROCS * own + shared)

    def test_consumers_claim_each_row_exactly_once_under_contention(self):
        total = 600
        store = delivery_store.DeliveryStore(self.db)
        for i in range(total):
            store.record(kind="completed", task_id=f"t-{i}", session_id="s-1", summary="x")
        barrier = self.ctx.Barrier(PROCS)
        queue = self.ctx.Queue()
        procs = [self.ctx.Process(target=_mp_workers.consume,
                                  args=(str(ROOT), str(self.db), barrier, "s-1", 10, queue))
                 for _ in range(PROCS)]
        for p in procs:
            p.start()
        batches = [queue.get(timeout=180) for _ in procs]
        for p in procs:
            p.join(timeout=60)
            self.assertEqual(p.exitcode, 0)
        ids = [i for b in batches for i in b]
        self.assertEqual(len(ids), total, "every row claimed")
        self.assertEqual(len(set(ids)), total, "no row claimed twice")
        busy = sum(1 for b in batches if b)
        self.assertGreaterEqual(busy, 2, "the work must be shared between consumers (real contention)")
        self.assertEqual(delivery_store.DeliveryStore(self.db).integrity_check(), "ok")


if __name__ == "__main__":
    mp.freeze_support()
    unittest.main()
