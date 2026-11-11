"""Per-turn SQLite connection scope (latency fix): behaviour and the regression guard.

The latency regression is guarded by COUNTING connections per ``pre_llm_call`` instead of
timing it: wall-clock budgets depend on the machine (the old code took ~11 ms on Linux and
~108 ms on a quiet Windows PC for the same 18 connect/close cycles), the connection count
does not.
"""
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

import dbscope  # noqa: E402
import delivery_store  # noqa: E402
import bot_chat_memory  # noqa: E402


def _connect(path, factory):
    kw = {"factory": factory} if factory is not None else {}
    return sqlite3.connect(path, isolation_level=None, **kw)


class TestScope(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dbscope-")
        self.db = Path(self.tmp) / "a.db"

    def test_outside_a_scope_close_really_closes(self):
        conn = dbscope.open_conn(self.db, _connect)
        conn.close()
        with self.assertRaises(sqlite3.ProgrammingError):
            conn.execute("select 1")

    def test_inside_a_scope_one_connection_per_file_and_close_is_a_noop(self):
        with dbscope.turn_scope():
            a = dbscope.open_conn(self.db, _connect)
            a.close()
            b = dbscope.open_conn(self.db, _connect)
            self.assertIs(a, b)
            self.assertEqual(b.execute("select 1").fetchone()[0], 1)
            other = dbscope.open_conn(Path(self.tmp) / "b.db", _connect)
            self.assertIsNot(a, other)
        with self.assertRaises(sqlite3.ProgrammingError):
            a.execute("select 1")  # really closed when the scope ended
        self.assertFalse(dbscope.active())

    def test_setup_runs_once_per_scope_connection(self):
        calls = []
        with dbscope.turn_scope():
            for _ in range(3):
                dbscope.open_conn(self.db, _connect, lambda c: calls.append(1))
        self.assertEqual(len(calls), 1)

    def test_failed_operation_does_not_leak_its_transaction(self):
        with dbscope.turn_scope():
            c = dbscope.open_conn(self.db, _connect)
            c.execute("create table t (x)")
            c.execute("BEGIN IMMEDIATE")
            c.execute("insert into t values (1)")
            c.close()  # what an operation's ``finally`` does after an error
            self.assertFalse(c.in_transaction)
            self.assertEqual(c.execute("select count(*) from t").fetchone()[0], 0)

    def test_scope_ends_even_when_the_block_raises(self):
        with self.assertRaises(RuntimeError):
            with dbscope.turn_scope():
                c = dbscope.open_conn(self.db, _connect)
                raise RuntimeError("boom")
        self.assertFalse(dbscope.active())
        with self.assertRaises(sqlite3.ProgrammingError):
            c.execute("select 1")

    def test_nested_scope_is_owned_by_the_outermost(self):
        with dbscope.turn_scope():
            a = dbscope.open_conn(self.db, _connect)
            with dbscope.turn_scope():
                self.assertIs(dbscope.open_conn(self.db, _connect), a)
            self.assertEqual(a.execute("select 1").fetchone()[0], 1)  # inner exit did not close it

    def test_scope_is_thread_local(self):
        seen = {}
        with dbscope.turn_scope():
            main_conn = dbscope.open_conn(self.db, _connect)

            def other():
                seen["active"] = dbscope.active()
                c = dbscope.open_conn(self.db, _connect)
                seen["same"] = c is main_conn
                c.close()

            t = threading.Thread(target=other)
            t.start()
            t.join()
        self.assertEqual(seen, {"active": False, "same": False})

    def test_failed_setup_leaves_nothing_cached(self):
        def bad(conn):
            raise sqlite3.OperationalError("database is locked")
        with dbscope.turn_scope():
            with self.assertRaises(sqlite3.OperationalError):
                dbscope.open_conn(self.db, _connect, bad)
            self.assertFalse(dbscope.has_conn(self.db))
            self.assertIsNotNone(dbscope.open_conn(self.db, _connect))


class TestStoresUnderScope(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dbscope-store-")
        self.notify = Path(self.tmp) / "team-notify.json"

    def test_delivery_store_roundtrip_inside_and_outside(self):
        store = delivery_store.DeliveryStore(delivery_store.store_path_for(self.notify))
        with dbscope.turn_scope():
            store.record(kind="completed", task_id="t1", session_id="s1",
                         assignee="x", summary="in scope")
            rows = store.offer("s1", turn_id="turn-1")
            self.assertEqual([r["task_id"] for r in rows], ["t1"])
        # Everything committed: a fresh, unscoped store sees it.
        again = delivery_store.DeliveryStore(delivery_store.store_path_for(self.notify))
        self.assertEqual(again.get("t1")[0]["status"], "offered")
        again.confirm("s1", turn_id="turn-1")
        self.assertEqual(again.get("t1")[0]["status"], "delivered_in_turn")

    def test_delivery_schema_step_is_skipped_when_the_version_is_current(self):
        path = delivery_store.store_path_for(self.notify)
        delivery_store.DeliveryStore(path).record(
            kind="completed", task_id="t1", session_id="s1", assignee="x", summary="s")
        store = delivery_store.DeliveryStore(path)
        conn = store._conn()
        try:
            self.assertEqual(
                conn.execute("select value from meta where key='schema_version'").fetchone()[0],
                str(delivery_store.SCHEMA_VERSION))
        finally:
            conn.close()
        statements = []
        real_connect = sqlite3.connect

        def spy(*a, **k):
            c = real_connect(*a, **k)
            c.set_trace_callback(lambda sql: statements.append(sql))
            return c

        with mock.patch.object(sqlite3, "connect", spy):
            conn = store._conn()
            conn.close()
        joined = "\n".join(statements).upper()
        self.assertNotIn("CREATE TABLE", joined)
        self.assertNotIn("UPDATE META", joined)

    def test_old_delivery_db_without_version_row_is_migrated(self):
        path = delivery_store.store_path_for(self.notify)
        conn = sqlite3.connect(path)
        conn.execute("create table meta (key text primary key, value text)")
        conn.execute("insert into meta values ('schema_version', '1')")
        conn.commit()
        conn.close()
        store = delivery_store.DeliveryStore(path)
        store.record(kind="completed", task_id="t1", session_id="s1", assignee="x", summary="s")
        self.assertEqual(store.get("t1")[0]["task_id"], "t1")

    def test_memory_store_roundtrip_and_user_version(self):
        mem = bot_chat_memory.MemoryStore(Path(self.tmp) / "memory.db")
        with dbscope.turn_scope():
            mem.save_pending("s1", ["a pending ask"])
            self.assertEqual(mem.load_pending("s1"), ["a pending ask"])
        again = bot_chat_memory.MemoryStore(Path(self.tmp) / "memory.db")
        self.assertEqual(again.load_pending("s1"), ["a pending ask"])
        c = sqlite3.connect(Path(self.tmp) / "memory.db")
        try:
            self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], 1)
        finally:
            c.close()

    def test_corrupt_memory_store_is_quarantined_inside_a_scope(self):
        p = Path(self.tmp) / "memory.db"
        p.write_bytes(b"this is not a sqlite database" * 50)
        mem = bot_chat_memory.MemoryStore(p)
        with dbscope.turn_scope():
            mem.save_pending("s1", ["fresh start"])
            self.assertEqual(mem.load_pending("s1"), ["fresh start"])
        self.assertTrue(list(Path(self.tmp).glob("memory.db.corrupt-*")))


class TestConnectionBudgetPerTurn(unittest.TestCase):
    """Platform-independent latency guard: a hot turn opens few connections."""

    MAX_CONNECTS_PER_PRE_LLM_CALL = 6   # was ~18 before dbscope (2 own stores + 2 read-only core DBs)
    MAX_CONNECTS_PER_POST_LLM_CALL = 6

    def test_pre_and_post_llm_call_connection_counts(self):
        import _fase5_bench as bench
        env = bench.Env()
        try:
            fn = bench.pre_hot(env)
            for i in range(3):
                fn(i)  # warm: schema, caches
            real_connect = sqlite3.connect
            count = {"n": 0}

            def spy(*a, **k):
                count["n"] += 1
                return real_connect(*a, **k)

            with mock.patch.object(sqlite3, "connect", spy):
                bench.bca._on_pre_llm_call(session_id="s1", turn_id="count-1")
                pre = count["n"]
                count["n"] = 0
                bench.bca._on_post_llm_call(session_id="s1", turn_id="count-1")
                post = count["n"]
            self.assertLessEqual(pre, self.MAX_CONNECTS_PER_PRE_LLM_CALL,
                                 f"pre_llm_call opened {pre} connections")
            self.assertLessEqual(post, self.MAX_CONNECTS_PER_POST_LLM_CALL,
                                 f"post_llm_call opened {post} connections")
        finally:
            env.cleanup()


if __name__ == "__main__":
    unittest.main()
