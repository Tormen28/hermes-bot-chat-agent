"""Fase 5 measurement harness (rubric docs/fase5-rubric.md v1.0, level A, offline).

Shared by the gate (tests/test_fase5_gate.py) and the full bench run
(``python3 tests/_fase5_bench.py``), which publishes docs/fase5-medicion.md
+ docs/fase5-medicion.json. No network, no model, no cost. Runs on
Python 3.12.3 and 3.14.9; no feature newer than 3.12 is used.

What each route measures is defined in the rubric §1. Every sample is a
wall-clock time in milliseconds (``time.perf_counter_ns``). Failures are
counted separately from latency: a rep that raises never enters the
percentiles, it is a failure.
"""
import importlib.util
import json
import math
import os
import shutil
import signal
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

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
import context_budget  # noqa: E402
import delivery_store  # noqa: E402
import team_flow as tf  # noqa: E402

GODS = ["researcher"]
PY314 = Path.home() / ".hermes" / "installs" / "fa100bfb2a12cc42" / "pm-runtime" / \
    "generations" / "e8260b4d60db4f20b0d0749cfa53817c" / "bin" / "python"


# ------------------------------------------------------------------ stats
def pct(samples, p):
    """Percentile with linear interpolation (``samples`` non-empty)."""
    ordered = sorted(samples)
    if len(ordered) == 1:
        return float(ordered[0])
    rank = (p / 100.0) * (len(ordered) - 1)
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return float(ordered[int(rank)])
    return float(ordered[lo] + (ordered[hi] - ordered[lo]) * (rank - lo))


def summarize(route, samples, *, failures=0, extra=None):
    """One published row: p50, p95, n, dispersion, failures."""
    samples = [float(s) for s in samples]
    out = {
        "route": route,
        "n": len(samples),
        "failures": int(failures),
        "p50_ms": round(pct(samples, 50), 3) if samples else None,
        "p95_ms": round(pct(samples, 95), 3) if samples else None,
        "min_ms": round(min(samples), 3) if samples else None,
        "max_ms": round(max(samples), 3) if samples else None,
        "mean_ms": round(statistics.fmean(samples), 3) if samples else None,
        "stdev_ms": round(statistics.pstdev(samples), 3) if len(samples) > 1 else 0.0,
    }
    if extra:
        out.update(extra)
    return out


def _ms_since(t0_ns):
    return (time.perf_counter_ns() - t0_ns) / 1e6


# ------------------------------------------------------------------ env
class FakeCtx:
    def __init__(self, cfg=None):
        self.cfg = cfg or {}
        self.hooks = {}

    def get_config(self, key, default=None):
        return self.cfg.get(key, default)

    def register_hook(self, name, fn):
        self.hooks[name] = fn


def build_home(sessions=("s1", "s2")):
    """Temp Hermes home: state.db (Bot Chat sessions) + kanban.db + notify."""
    home = Path(tempfile.mkdtemp(prefix="fase5-"))
    state = home / "state.db"
    with sqlite3.connect(state) as c:
        c.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, "
                  "parent_session_id TEXT, source TEXT)")
        c.executemany(
            "INSERT INTO sessions VALUES (?,?,?,?)",
            [(sid, "Bot Chat", None, "chat") for sid in sessions],
        )
    kb = home / "kanban.db"
    with sqlite3.connect(kb) as c:
        c.execute("CREATE TABLE tasks (id TEXT, title TEXT, assignee TEXT, "
                  "status TEXT, result TEXT, started_at TEXT, session_id TEXT, "
                  "idempotency_key TEXT)")
        c.execute("CREATE TABLE task_links (parent_id TEXT, child_id TEXT)")
    notify = home / "bot-chat-agent" / "team-notify.json"
    notify.parent.mkdir(parents=True, exist_ok=True)
    return {"home": home, "state": state, "kb": kb, "notify": notify}


class Env:
    """One isolated plugin instance (its own home, store, ctx).

    Saves and restores the plugin module's globals (``_ctx``,
    ``_state_db``, ``HERMES_HOME``) so a short-lived Env (cold reps)
    never leaves them pointing at a deleted home: the shared env of
    a longer run keeps working after 200 cold envs come and go.
    """

    def __init__(self, sessions=("s1", "s2")):
        self.parts = build_home(sessions)
        self.home = self.parts["home"]
        self.notify = self.parts["notify"]
        self._old_home = os.environ.get("HERMES_HOME")
        self._old_ctx = bca._ctx
        self._old_state_db = bca._state_db
        os.environ["HERMES_HOME"] = str(self.home)
        bca._cache.clear()
        bca._questions.clear()
        state = self.parts["state"]
        bca._state_db = lambda s=state: s
        self.ctx = FakeCtx(cfg={
            "language": "en",
            "team": {"enabled": True, "assignees": list(GODS)},
            "kanban_db": str(self.parts["kb"]),
            "notify_state_path": str(self.notify),
        })
        bca.register(self.ctx)

    def store(self):
        return delivery_store.DeliveryStore(
            delivery_store.store_path_for(self.notify))

    def cleanup(self):
        if self._old_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = self._old_home
        bca._ctx = self._old_ctx
        bca._state_db = self._old_state_db
        shutil.rmtree(self.home, ignore_errors=True)


def remember(env, task_id, session_id="s1", title="Work", assignee="researcher"):
    tf.remember_create(
        env.notify, session_id=session_id, title=title, assignee=assignee,
        idempotency=tf.idempotency_key(session_id, assignee, title),
        result={"id": task_id}, gods=list(GODS), resource="")


# ------------------------------------------------------------------ runner
def run_reps(route, fn, reps):
    """Time ``fn(i)`` (returns ms) ``reps`` times; exceptions are failures."""
    samples = []
    failures = 0
    for i in range(reps):
        try:
            samples.append(float(fn(i)))
        except Exception:
            failures += 1
    return summarize(route, samples, failures=failures)


# ------------------------------------------------------------------ routes
def ack_hot(env):
    """can_launch + launch, steady state (Fase 3 ACK).

    One session per rep: the visible queue caps at 10 open tasks per
    session, so reusing one session would trip the limit after 10 reps.
    """
    def fn(i):
        sid = f"ack-s{i}"
        t0 = time.perf_counter_ns()
        ok, _why = bgt.can_launch(env.store(), sid)
        assert ok, "can_launch refused with an empty queue"
        bgt.launch(env.store(), task_id=f"ack-{i}", session_id=sid,
                   title="Work", assignee="researcher")
        return _ms_since(t0)
    return fn


def ack_cold(_i):
    """First launch with a brand-new home/store (cold path)."""
    env = Env()
    try:
        t0 = time.perf_counter_ns()
        ok, _why = bgt.can_launch(env.store(), "s1")
        assert ok
        bgt.launch(env.store(), task_id="ack-cold", session_id="s1",
                   title="Work", assignee="researcher")
        return _ms_since(t0)
    finally:
        env.cleanup()


def pre_hot(env, pending=False):
    """pre_llm_call: time until the plugin returns its first visible content.

    With ``pending=True`` one delivery is recorded before each rep (outside
    the timed section) so the turn has something to offer.
    """
    def fn(i):
        if pending:
            remember(env, f"pre-{i}")
            env.store().record(kind="completed", task_id=f"pre-{i}",
                               session_id="s1", assignee="researcher",
                               summary=f"result {i}")
        t0 = time.perf_counter_ns()
        out = bca._on_pre_llm_call(session_id="s1", turn_id=f"pre-{i}")
        dt = _ms_since(t0)
        assert out is not None and out.get("context"), "pre_llm_call returned nothing"
        if pending:
            assert f"pre-{i}" in out["context"] or "result" in out["context"], \
                "offered delivery missing from the turn context"
            bca._on_post_llm_call(session_id="s1", turn_id=f"pre-{i}")
        return dt
    return fn


def pre_cold(_i):
    """First pre_llm_call with a brand-new home/store (cold path)."""
    env = Env()
    try:
        t0 = time.perf_counter_ns()
        out = bca._on_pre_llm_call(session_id="s1", turn_id="cold-1")
        dt = _ms_since(t0)
        assert out is not None and out.get("context"), "cold pre_llm_call returned nothing"
        return dt
    finally:
        env.cleanup()


def post_hot(env):
    """post_llm_call: confirm this turn's offered delivery."""
    def fn(i):
        remember(env, f"post-{i}")
        env.store().record(kind="completed", task_id=f"post-{i}",
                           session_id="s1", assignee="researcher",
                           summary=f"result {i}")
        offered = env.store().offer("s1", turn_id=f"post-{i}")
        assert offered, "nothing offered before post_llm_call"
        t0 = time.perf_counter_ns()
        bca._on_post_llm_call(session_id="s1", turn_id=f"post-{i}")
        dt = _ms_since(t0)
        rows = env.store().get(f"post-{i}")
        assert rows and rows[0]["status"] == "delivered_in_turn", \
            "post_llm_call did not confirm the delivery"
        return dt
    return fn


def stopped_hot(env):
    """agent_loop_stopped: release this turn's offers (interruption)."""
    def fn(i):
        remember(env, f"stop-{i}")
        env.store().record(kind="completed", task_id=f"stop-{i}",
                           session_id="s1", assignee="researcher",
                           summary=f"result {i}")
        offered = env.store().offer("s1", turn_id=f"stop-{i}")
        assert offered, "nothing offered before agent_loop_stopped"
        t0 = time.perf_counter_ns()
        bca._on_agent_loop_stopped(session_id="s1")
        dt = _ms_since(t0)
        rows = env.store().get(f"stop-{i}")
        assert rows and rows[0]["status"] == "pending", \
            "agent_loop_stopped did not release the offer"
        return dt
    return fn


def hook_fn(env, kind):
    """One kanban hook handler (100 reps each, rubric §2)."""
    def claimed(i):
        tid = f"hk-claim-{i}"
        remember(env, tid)
        t0 = time.perf_counter_ns()
        bca._on_kanban_claimed(task_id=tid, assignee="researcher", run_id=7)
        return _ms_since(t0)

    def spawned(i):
        tid = f"hk-spawn-{i}"
        remember(env, tid)
        bca._on_kanban_claimed(task_id=tid, assignee="researcher", run_id=7)
        t0 = time.perf_counter_ns()
        bca._on_kanban_worker_spawned(task_id=tid, run_id=7)
        return _ms_since(t0)

    def completed(i):
        tid = f"hk-done-{i}"
        remember(env, tid)
        t0 = time.perf_counter_ns()
        bca._on_kanban_completed(task_id=tid, assignee="researcher",
                                 summary=f"result {i}")
        dt = _ms_since(t0)
        rows = env.store().get(tid)
        assert rows and rows[0]["status"] == "pending", \
            "kanban_task_completed did not persist the delivery"
        return dt

    def blocked(i):
        tid = f"hk-block-{i}"
        remember(env, tid)
        t0 = time.perf_counter_ns()
        bca._on_kanban_blocked(task_id=tid, assignee="researcher",
                               reason="stuck")
        dt = _ms_since(t0)
        rows = env.store().get(tid)
        assert rows and rows[0]["status"] == "pending", \
            "kanban_task_blocked did not persist the delivery"
        return dt

    return {"claimed": claimed, "spawned": spawned,
            "completed": completed, "blocked": blocked}[kind]


def guard_allow(env):
    def fn(i):
        t0 = time.perf_counter_ns()
        out = bca._on_pre_tool_call("read_file", {"path": "x.py"},
                                    session_id="s1", turn_id=f"g{i}")
        dt = _ms_since(t0)
        assert out is None, f"guard blocked an allowed call: {out}"
        return dt
    return fn


def guard_block(env):
    def fn(i):
        t0 = time.perf_counter_ns()
        out = bca._on_pre_tool_call("rm", {"path": "x"},
                                    session_id="s1", turn_id=f"g{i}")
        dt = _ms_since(t0)
        assert out is not None and out.get("action") == "block", \
            "guard did not block the destructive call"
        return dt
    return fn


def post_tool(env):
    def fn(i):
        tid = f"pt-{i}"
        t0 = time.perf_counter_ns()
        bca._on_post_tool_call(
            "kanban_create", {"title": "Work", "assignee": "researcher"},
            result={"id": tid}, session_id="s1", status="ok")
        return _ms_since(t0)
    return fn


def handoff(env):
    """record (writer conn) -> offer (reader conn): the cross-process path.

    Two DeliveryStore instances on the same file are two connections —
    the same boundary SQLite enforces between real processes.
    """
    writer = env.store()
    reader = env.store()

    def fn(i):
        tid = f"ho-{i}"
        remember(env, tid)
        t0 = time.perf_counter_ns()
        writer.record(kind="completed", task_id=tid, session_id="s1",
                      assignee="researcher", summary=f"result {i}")
        offered = reader.offer("s1", turn_id=f"ho-{i}")
        dt = _ms_since(t0)
        assert any(r["task_id"] == tid for r in offered), \
            "reader did not see the writer's delivery"
        reader.confirm("s1", turn_id=f"ho-{i}")
        return dt
    return fn


def delivery_cycle(env):
    """offer -> confirm inside one turn."""
    def fn(i):
        tid = f"dc-{i}"
        remember(env, tid)
        env.store().record(kind="completed", task_id=tid, session_id="s1",
                           assignee="researcher", summary=f"result {i}")
        t0 = time.perf_counter_ns()
        offered = env.store().offer("s1", turn_id=f"dc-{i}")
        assert any(r["task_id"] == tid for r in offered), "offer missed the row"
        env.store().confirm("s1", turn_id=f"dc-{i}")
        dt = _ms_since(t0)
        rows = env.store().get(tid)
        assert rows and rows[0]["status"] == "delivered_in_turn"
        return dt
    return fn


def relay_cycle(env):
    """claim_for_relay -> confirm_ids (outbox relay accounting)."""
    def fn(i):
        tid = f"rl-{i}"
        env.store().record(kind="completed", task_id=tid, session_id="s1",
                           assignee="researcher", summary=f"result {i}")
        t0 = time.perf_counter_ns()
        token, claimed = env.store().claim_for_relay(session_id="s1", limit=50)
        assert token.startswith("relay:"), f"token inesperado: {token}"
        assert any(r["task_id"] == tid for r in claimed), "relay claim missed"
        ids = [r["id"] for r in claimed if r["task_id"] == tid]
        env.store().confirm_ids(ids)
        return _ms_since(t0)
    return fn


def queue_checks(env):
    """can_launch at 0 / 9 / 10 open tasks + the rejection itself."""
    store = env.store()
    samples = {"can_launch_empty": [], "can_launch_9": [],
               "can_launch_full": [], "launch_rejected": []}
    for _ in range(10):
        t0 = time.perf_counter_ns()
        ok, _ = bgt.can_launch(store, "s1")
        samples["can_launch_empty"].append(_ms_since(t0))
        assert ok
    for i in range(9):
        bgt.launch(store, task_id=f"q-{i}", session_id="s1",
                   title="Work", assignee="researcher")
    for _ in range(10):
        t0 = time.perf_counter_ns()
        ok, _ = bgt.can_launch(store, "s1")
        samples["can_launch_9"].append(_ms_since(t0))
        assert ok
    bgt.launch(store, task_id="q-9", session_id="s1",
               title="Work", assignee="researcher")
    for _ in range(10):
        t0 = time.perf_counter_ns()
        ok, why = bgt.can_launch(store, "s1")
        samples["can_launch_full"].append(_ms_since(t0))
        assert not ok and why == "queue_full", f"full queue accepted: {ok} {why}"
        t1 = time.perf_counter_ns()
        ok2, why2 = bgt.can_launch(store, "s1")
        samples["launch_rejected"].append(_ms_since(t1))
        assert not ok2 and why2 == "queue_full"
    return {k: summarize(f"queue/{k}", v) for k, v in samples.items()}


# ---------------------------------------------------------------- long sessions
def run_long_sessions(env, sequences=10, turns=100):
    """Ten sequences of 100 turns (rubric §2).

    Each turn: pre_llm_call + post_llm_call. Completions are recorded
    before turns 10, 20, 25, 30, ..., 90 (10 per sequence); turns 25, 50
    and 75 are interruptions (agent_loop_stopped instead of post), so
    the offers of turns 25 and 50 are re-offered and confirmed on the
    next turn. Every 10th turn the budgets are checked. Sessions
    alternate s1/s2 across sequences; the accounting must show each
    session only ever received its own deliveries.
    """
    pre_samples, post_samples = [], []
    budget_violations = 0
    budget_checks = 0
    failures = 0
    for seq in range(sequences):
        sid = "s1" if seq % 2 == 0 else "s2"
        for turn in range(1, turns + 1):
            tid = f"ls-s{seq}-t{turn}"
            if turn in (10, 20, 25, 30, 40, 50, 60, 70, 80, 90):
                remember(env, tid, session_id=sid)
                env.store().record(kind="completed", task_id=tid,
                                   session_id=sid, assignee="researcher",
                                   summary=f"result {tid}")
            try:
                t0 = time.perf_counter_ns()
                out = bca._on_pre_llm_call(session_id=sid, turn_id=f"ls-{seq}-{turn}")
                pre_samples.append(_ms_since(t0))
                if out is None:
                    failures += 1
            except Exception:
                failures += 1
            if turn % 10 == 0:
                budget_checks += 1
                try:
                    built = bca._build_turn_context(sid, f"ls-{seq}-{turn}")
                    chk = context_budget.check_budgets(
                        built["stable"], built["dynamic"])
                    if not chk["ok"] or built["over_budget"]:
                        budget_violations += 1
                except Exception:
                    budget_violations += 1
            try:
                t0 = time.perf_counter_ns()
                if turn in (25, 50, 75):
                    bca._on_agent_loop_stopped(session_id=sid)
                else:
                    bca._on_post_llm_call(session_id=sid, turn_id=f"ls-{seq}-{turn}")
                post_samples.append(_ms_since(t0))
            except Exception:
                failures += 1
    store = env.store()
    accounting = {}
    for sid in ("s1", "s2"):
        rows = store.rows_for_session(sid)
        by_status = {}
        for r in rows:
            by_status[r["status"]] = by_status.get(r["status"], 0) + 1
        delivered = {r["task_id"] for r in rows
                     if r["status"] == "delivered_in_turn"}
        expected = {f"ls-s{s}-t{t}" for s in range(sequences)
                    if ("s1" if s % 2 == 0 else "s2") == sid
                    for t in (10, 20, 25, 30, 40, 50, 60, 70, 80, 90)}
        accounting[sid] = {
            "recorded": len(expected),
            "delivered": len(delivered),
            "lost": len(expected - delivered),
            "duplicates": len(rows) - len({r["task_id"] for r in rows}),
            "pending": by_status.get("pending", 0),
            "offered_left": by_status.get("offered", 0),
            "by_status": by_status,
        }
    first_decile = pre_samples[: 10 * turns // 10] if pre_samples else []
    last_decile = pre_samples[-10 * turns // 10:] if pre_samples else []
    slope = None
    if first_decile and last_decile:
        slope = round(pct(last_decile, 50) / max(pct(first_decile, 50), 1e-9), 2)
    return {
        "route": "long_sessions/10x100_turns",
        "sequences": sequences,
        "turns_per_sequence": turns,
        "pre": summarize("long_sessions/pre_llm_call", pre_samples,
                         failures=failures),
        "post": summarize("long_sessions/post_llm_call_or_stopped", post_samples),
        "budget_checks": budget_checks,
        "budget_violations": budget_violations,
        "slope_last_vs_first_decile_p50": slope,
        "accounting": accounting,
    }


# ---------------------------------------------------------------- low resources
def preload_store(path, n=10000, session="sX"):
    """10.000 rows in the deliveries table (mixed statuses), one txn.

    They belong to another session (``sX``) so the loaded-store routes
    do the same work as the baseline — the comparison measures the
    table size, not a different offer. Offering many pending rows of
    the measured session is a separate route (``offer_many``).
    """
    now = time.time()
    rows = []
    statuses = ["delivered_in_turn", "seen", "delivered_in_turn", "pending"]
    for i in range(n):
        rows.append(("completed", f"preload-{i}", session, "", "researcher",
                     f"old result {i}", statuses[i % len(statuses)],
                     now - i, now - i))
    with sqlite3.connect(path) as c:
        c.executemany(
            "INSERT INTO deliveries (kind, task_id, session_id, project_id, "
            "assignee, summary, status, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)", rows)
    return delivery_store.DeliveryStore(path)


def offer_many(env, n=1000):
    """offer() with ``n`` pending rows of the measured session.

    Correctness + latency are published; the rubric sets no threshold
    for this route (it measures offer-at-scale, not the common path).
    """
    store = env.store()
    for i in range(n):
        tid = f"om-{i}"
        remember(env, tid)
        store.record(kind="completed", task_id=tid, session_id="s1",
                     assignee="researcher", summary=f"result {i}")

    def fn(i):
        t0 = time.perf_counter_ns()
        offered = store.offer("s1", turn_id=f"om-turn-{i}")
        dt = _ms_since(t0)
        assert len(offered) == n, f"offer returned {len(offered)} of {n}"
        return dt
    return fn


class cpu_hog:
    """Context manager: saturate the CPUs with busy loops (contención)."""

    def __init__(self, n=2):
        self.n = n
        self.procs = []

    def __enter__(self):
        for _ in range(self.n):
            self.procs.append(subprocess.Popen(
                [sys.executable, "-c", "while True: pass"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        time.sleep(0.2)
        return self

    def __exit__(self, *exc):
        for p in self.procs:
            p.kill()
        for p in self.procs:
            p.wait()


# ---------------------------------------------------------------- recovery
def _crash_writer(store_path, journal_path, count):
    """Record rows in a loop; journal each committed row (for the parent)."""
    store = delivery_store.DeliveryStore(store_path)
    with open(journal_path, "w", encoding="utf-8") as fh:
        for i in range(count):
            tid = f"crash-{i}"
            store.record(kind="completed", task_id=tid, session_id="s1",
                         assignee="researcher", summary=f"crash {i}")
            fh.write(tid + "\n")
            fh.flush()
            time.sleep(0.002)


def recovery_after_kill9():
    """kill -9 mid-write: reopen + integrity_check timed, 0 losses."""
    env = Env()
    try:
        path = delivery_store.store_path_for(env.notify)
        journal = env.home / "journal.txt"
        proc = subprocess.Popen(
            [sys.executable, "-c",
             # tests/ is not a package: import by file location so a core checkout on
             # PYTHONPATH (which ships its own ``tests`` package) cannot shadow it.
             "import sys; sys.path.insert(0, %r); sys.path.insert(0, %r); "
             "from _fase5_bench import _crash_writer; "
             "_crash_writer(%r, %r, 500)" % (str(ROOT), str(ROOT / "tests"), str(path), str(journal))],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.4)
        proc.send_signal(signal.SIGKILL)
        proc.wait()
        assert proc.returncode == -signal.SIGKILL
        journaled = journal.read_text(encoding="utf-8").split() if journal.exists() else []
        t0 = time.perf_counter_ns()
        store = delivery_store.DeliveryStore(path)
        integrity = store.integrity_check()
        dt = _ms_since(t0)
        present = {r["task_id"] for r in store.rows_for_session("s1")}
        lost = [t for t in journaled if t not in present]
        return {"route": "recovery/kill9_reopen_integrity",
                "reopen_ms": round(dt, 3), "integrity": integrity,
                "journaled": len(journaled), "lost": len(lost),
                "present": len(present)}
    finally:
        env.cleanup()


# ---------------------------------------------------------------- error paths
def error_paths(env):
    """Corrupt store: the hooks must fail closed, 0 losses.

    The turn still renders (the offer failure is logged, the note is
    built without announcements) and the row survives once the good
    file is restored. The locked-store wait is measured separately in
    :func:`error_paths_with_lock_release` (the holder lets go after
    300 ms; a lock held forever would just run into ``busy_timeout``).
    """
    store = env.store()
    remember(env, "err-1")
    store.record(kind="completed", task_id="err-1", session_id="s1",
                 assignee="researcher", summary="must survive")
    path = delivery_store.store_path_for(env.notify)
    good = path.read_bytes()
    out = {}
    # Corrupt file: the turn still renders, the row is not deleted.
    path.write_bytes(b"\x00not a database at all\x00" * 64)
    t0 = time.perf_counter_ns()
    try:
        res = bca._on_pre_llm_call(session_id="s1", turn_id="err-corrupt")
        out["corrupt_pre_ms"] = round(_ms_since(t0), 3)
        out["corrupt_pre_returned"] = res is not None
    except Exception as exc:  # noqa: BLE001 - the point is it must not raise
        out["corrupt_pre_ms"] = round(_ms_since(t0), 3)
        out["corrupt_pre_raised"] = type(exc).__name__
    path.write_bytes(good)
    rows = store.get("err-1")
    out["row_survived_corrupt"] = bool(rows) and rows[0]["status"] in (
        "pending", "offered", "delivered_in_turn")
    return out


def error_paths_with_lock_release(env):
    """Same as error_paths but the lock is released after 300 ms.

    (error_paths holds the lock while recording; this variant measures
    the wait when the holder lets go mid-wait.)
    """
    import threading

    store = env.store()
    path = delivery_store.store_path_for(env.notify)
    # check_same_thread=False: the release thread must be able to roll
    # back the holder connection created here (default sqlite3 objects
    # are bound to their creating thread and raise ProgrammingError).
    holder = sqlite3.connect(path, timeout=1.0, check_same_thread=False)
    holder.execute("BEGIN EXCLUSIVE")

    def _release():
        time.sleep(0.3)
        try:
            holder.rollback()
            holder.close()
        except Exception:
            pass

    threading.Thread(target=_release, daemon=True).start()
    t0 = time.perf_counter_ns()
    store.record(kind="completed", task_id="err-3", session_id="s1",
                 assignee="researcher", summary="waited for lock")
    dt = _ms_since(t0)
    return {"locked_wait_ms": round(dt, 3),
            "present": bool(store.get("err-3"))}


# ---------------------------------------------------------------- bar (node)
def bar_js_bench(reps=200):
    """Time applyTeamEvent + classifyPhase in node (pure JS, rubric §1)."""
    node = shutil.which("node")
    if node is None:
        return {"route": "bar/js_pure", "skipped": "node no disponible"}
    proc = subprocess.run(
        [node, "tests/_fase5_bar_bench.mjs", str(reps)],
        cwd=str(ROOT), capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        return {"route": "bar/js_pure", "failures": 1,
                "error": (proc.stdout + proc.stderr)[-500:]}
    return json.loads(proc.stdout.strip().splitlines()[-1])


# ---------------------------------------------------------------- report
def write_report(results, md_path, json_path):
    lines = [
        "# Fase 5 — Medición (publicado)",
        "",
        f"Generado por `tests/_fase5_bench.py` (rúbrica `docs/fase5-rubric.md` v1.0).",
        f"Runtime: {results['runtime']} · Python {results['python']} · "
        f"sin red, sin modelo, sin coste.",
        "",
        "## Cifras por ruta",
        "",
        "| Ruta | n | p50 (ms) | p95 (ms) | min | max | media | desv. | fallos |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in results["routes"]:
        lines.append(
            f"| {row['route']} | {row['n']} | {row['p50_ms']} | {row['p95_ms']} | "
            f"{row['min_ms']} | {row['max_ms']} | {row['mean_ms']} | "
            f"{row['stdev_ms']} | {row['failures']} |")
    lines += [
        "",
        "## Sesiones largas (10 × 100 turnos)",
        "",
        f"- pre_llm_call p50 {results['long_sessions']['pre']['p50_ms']} ms, "
        f"p95 {results['long_sessions']['pre']['p95_ms']} ms, "
        f"fallos {results['long_sessions']['pre']['failures']}",
        f"- post_llm_call / stopped p50 "
        f"{results['long_sessions']['post']['p50_ms']} ms, "
        f"p95 {results['long_sessions']['post']['p95_ms']} ms",
        f"- Controles de presupuesto: {results['long_sessions']['budget_checks']}, "
        f"violaciones: {results['long_sessions']['budget_violations']}",
        f"- Pendiente último vs primer decil (p50): "
        f"{results['long_sessions']['slope_last_vs_first_decile_p50']}×",
        "",
        "| Sesión | registradas | entregadas | perdidas | duplicadas | pendientes | ofrecidas |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for sid, acc in results["long_sessions"]["accounting"].items():
        lines.append(
            f"| {sid} | {acc['recorded']} | {acc['delivered']} | {acc['lost']} | "
            f"{acc['duplicates']} | {acc['pending']} | {acc['offered_left']} |")
    lines += [
        "",
        "## Barra ≤ 1 s (descomposición medible)",
        "",
        f"- Persistencia Python (`record`, p95): "
        f"{results['bar']['persist_p95_ms']} ms",
        f"- JS puro (`applyTeamEvent` + `classifyPhase`, p95): "
        f"{results['bar']['js'].get('p95_ms')} ms "
        f"(n={results['bar']['js'].get('n')})",
        "- Despacho de eventos del gateway: **no medible offline**; queda para "
        "la máquina viva. El ≤ 1 s de extremo a extremo sigue sin probarse.",
        "",
        "## Pocos recursos y errores",
        "",
        f"- Almacén con 10.000 filas: pre_llm_call p95 "
        f"{results['low_resources']['loaded']['pre_p95_ms']} ms "
        f"(base {results['low_resources']['loaded']['base_pre_p95_ms']} ms, "
        f"ratio {results['low_resources']['loaded']['ratio']}×), "
        f"pérdidas {results['low_resources']['loaded']['lost']}",
        f"- Contención de CPU (2 busy-loops): pre_llm_call p95 "
        f"{results['low_resources']['cpu_contention']['pre_p95_ms']} ms, "
        f"pérdidas {results['low_resources']['cpu_contention']['lost']}",
        f"- Base corrupta: turno renderizado sin excepción: "
        f"{results['errors'].get('corrupt_pre_returned')}, fila intacta: "
        f"{results['errors'].get('row_survived_corrupt')}",
        f"- Base bloqueada (espera {results['errors'].get('locked_wait_ms')} ms "
        f"y éxito): {results['errors'].get('present')}",
        f"- Recuperación tras kill -9: reapertura + integridad "
        f"{results['recovery']['reopen_ms']} ms, integridad "
        f"{results['recovery']['integrity']}, registradas "
        f"{results['recovery']['journaled']}, perdidas "
        f"{results['recovery']['lost']}",
        "",
        "## Batería y puertas (re-ejecutadas en esta corrida)",
        "",
        f"- Batería: {results['suites']['battery']}",
        f"- Puertas: {results['suites']['gates']}",
        f"- Suite JS (node): {results['suites']['node']}",
        "",
        "## Pendiente (máquina viva / decisión de Gustavo)",
        "",
        "- TTFT de extremo a extremo y barra ≤ 1 s de extremo a extremo: "
        "necesitan modelo + gateway + app de escritorio (Fase 6, canario).",
        "- Piloto pagado (nivel B de coherencia, comparación de modelos): "
        "NO ejecutado; ver `docs/fase5-piloto-pendiente.md`.",
        "- Confirmación en Windows: esta medición es Linux; la batería "
        "completa en Windows queda para la Fase 6.",
        "",
    ]
    md_path.write_text("\n".join(lines), encoding="utf-8")
    json_path.write_text(json.dumps(results, indent=2, ensure_ascii=False),
                         encoding="utf-8")


# ---------------------------------------------------------------- full run
def full_bench():
    env = Env()
    results = {"runtime": "offline, esta VM (Linux)",
               "python": sys.version.split()[0],
               "routes": []}
    try:
        results["routes"].append(run_reps("ack/hot", ack_hot(env), 200))
        results["routes"].append(run_reps("ack/cold", ack_cold, 200))
        results["routes"].append(run_reps("ttft/pre_llm_hot", pre_hot(env), 200))
        results["routes"].append(
            run_reps("ttft/pre_llm_hot_pending", pre_hot(env, pending=True), 200))
        results["routes"].append(run_reps("ttft/pre_llm_cold", pre_cold, 200))
        results["routes"].append(run_reps("delivery/post_llm", post_hot(env), 200))
        results["routes"].append(run_reps("interrupt/agent_loop_stopped",
                                          stopped_hot(env), 200))
        for kind in ("claimed", "spawned", "completed", "blocked"):
            results["routes"].append(
                run_reps(f"hook/{kind}", hook_fn(env, kind), 100))
        results["routes"].append(run_reps("tools/guard_allow", guard_allow(env), 200))
        results["routes"].append(run_reps("tools/guard_block", guard_block(env), 200))
        results["routes"].append(run_reps("tools/post_tool", post_tool(env), 200))
        results["routes"].append(run_reps("handoff/record_to_offer", handoff(env), 200))
        results["routes"].append(run_reps("delivery/offer_to_confirm",
                                          delivery_cycle(env), 200))
        results["routes"].append(run_reps("delivery/relay_claim_confirm",
                                          relay_cycle(env), 200))
        env4 = Env()
        try:
            results["routes"].append(run_reps("handoff/offer_1000_pending",
                                              offer_many(env4, 1000), 200))
        finally:
            env4.cleanup()
        # Fresh env: the shared one already holds hundreds of open
        # background tasks in s1 from the hook/guard routes, which
        # would trip the 10-task queue limit before measuring it.
        env_q = Env()
        try:
            for row in queue_checks(env_q).values():
                results["routes"].append(row)
        finally:
            env_q.cleanup()
        # Fresh env for the same reason: the turn note renders open
        # background tasks, and the shared env's stale rows would
        # inflate every turn of the long sequences.
        env_ls = Env()
        try:
            results["long_sessions"] = run_long_sessions(env_ls)
        finally:
            env_ls.cleanup()
        # Low resources: loaded store.
        env2 = Env()
        try:
            base_pre = run_reps("ttft/pre_llm_hot", pre_hot(env2), 200)
            preload_store(delivery_store.store_path_for(env2.notify), 10000)
            loaded_pre = run_reps("ttft/pre_llm_hot", pre_hot(env2), 200)
            results["low_resources"] = {
                "loaded": {
                    "pre_p95_ms": loaded_pre["p95_ms"],
                    "base_pre_p95_ms": base_pre["p95_ms"],
                    "ratio": round(loaded_pre["p95_ms"] / max(base_pre["p95_ms"], 1e-9), 2),
                    "lost": loaded_pre["failures"],
                },
            }
            with cpu_hog(2):
                cont = run_reps("ttft/pre_llm_hot", pre_hot(env2), 200)
            results["low_resources"]["cpu_contention"] = {
                "pre_p95_ms": cont["p95_ms"], "lost": cont["failures"]}
        finally:
            env2.cleanup()
        env3 = Env()
        try:
            results["errors"] = error_paths(env3)
            results["errors"].update(error_paths_with_lock_release(env3))
        finally:
            env3.cleanup()
        results["recovery"] = recovery_after_kill9()
        results["bar"] = {
            "js": bar_js_bench(200),
            "persist_p95_ms": next(
                r["p95_ms"] for r in results["routes"]
                if r["route"] == "handoff/record_to_offer"),
        }
        results["suites"] = run_suites()
        return results
    finally:
        env.cleanup()


def run_suites():
    """Re-run battery + gate files + node suite under this interpreter."""
    out = {}
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=900)
    tail = (proc.stderr or proc.stdout).strip().splitlines()[-3:]
    out["battery"] = " ".join(tail) + f" (exit {proc.returncode})"
    node = shutil.which("node")
    if node:
        proc = subprocess.run(
            [node, "--test", "tests/test_fase3_desktop.mjs"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=120)
        out["node"] = f"exit {proc.returncode}"
    else:
        out["node"] = "node no disponible"
    out["gates"] = ("1a/1b/2/3/3b/4 incluidas en la batería discover; "
                    "ver salida completa")
    return out


if __name__ == "__main__":
    results = full_bench()
    write_report(results,
                 ROOT / "docs" / "fase5-medicion.md",
                 ROOT / "docs" / "fase5-medicion.json")
    print(json.dumps(results, indent=2, ensure_ascii=False))
