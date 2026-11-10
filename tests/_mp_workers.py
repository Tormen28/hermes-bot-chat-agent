"""Top-level worker functions for ``spawn`` multiprocessing (importable on Windows)."""
import sys


def _store(root, db):
    if root not in sys.path:
        sys.path.insert(0, root)
    import delivery_store
    return delivery_store.DeliveryStore(db)


def produce(root, db, barrier, worker, own, shared, session_id):
    """Insert ``own`` rows unique to this worker and ``shared`` rows every worker inserts."""
    store = _store(root, db)
    barrier.wait(timeout=60)
    for i in range(own):
        store.record(kind="completed", task_id=f"w{worker}-{i}", session_id=session_id, summary="x")
    for i in range(shared):
        store.record(kind="completed", task_id=f"shared-{i}", session_id=session_id, summary="x")


def consume(root, db, barrier, session_id, limit, queue):
    """Claim in a loop until nothing is left; report the ids this worker claimed."""
    store = _store(root, db)
    barrier.wait(timeout=60)
    got = []
    while True:
        _token, rows = store.claim_for_relay(session_id=session_id, limit=limit)
        if not rows:
            break
        got.extend(r["id"] for r in rows)
    queue.put(got)
