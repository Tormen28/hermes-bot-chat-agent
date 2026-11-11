"""Per-turn SQLite connection scope (latency).

A turn used to open and close a connection for every single query: about
18 connections and 90 statements per ``pre_llm_call``. On Windows each
connect + schema check + close (WAL checkpoint on the last close) costs
1-3 ms, so that alone was ~100 ms per turn on a quiet machine (measured:
p50 108 ms vs 11 ms on Linux for the same code).

Inside ``with turn_scope():`` every ``open_conn()`` for the same database
file returns the one connection opened for it, whose ``close()`` is a
no-op (it only rolls back an unfinished transaction). The real close
happens once, when the scope ends. Outside a scope nothing changes:
``open_conn()`` is a plain ``sqlite3.connect`` and ``close()`` really closes.

The scope is thread-local, so a hook running on another thread never
shares a connection (sqlite3 connections are not thread-safe). Scopes
nest: only the outermost one owns the connections.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Optional

_local = threading.local()


class _PinnedConnection(sqlite3.Connection):
    """A connection that outlives the ``close()`` calls of single operations."""

    _pinned = False

    def close(self) -> None:  # type: ignore[override]
        if self._pinned:
            try:
                if self.in_transaction:
                    self.rollback()  # an operation that failed half-way must not leak its transaction
            except sqlite3.Error:
                pass
            return
        super().close()

    def _really_close(self) -> None:
        self._pinned = False
        try:
            if self.in_transaction:
                self.rollback()
        except sqlite3.Error:
            pass
        super().close()


def _key(path: Any) -> str:
    try:
        return os.path.normcase(os.path.abspath(os.fspath(path)))
    except Exception:  # noqa: BLE001
        return str(path)


def active() -> bool:
    return getattr(_local, "conns", None) is not None


def open_conn(path: Any, connect: Callable[[Any, Any], sqlite3.Connection],
              setup: Optional[Callable[[sqlite3.Connection], None]] = None) -> sqlite3.Connection:
    """Return a connection for ``path``.

    ``connect(path, factory)`` must call ``sqlite3.connect`` with its own options and the
    given ``factory`` (``None`` outside a scope). ``setup`` runs once per fresh connection
    (pragmas/schema) and is skipped when the scope reuses a connection.
    """
    conns = getattr(_local, "conns", None)
    if conns is None:
        conn = connect(path, None)
        if setup is not None:
            try:
                setup(conn)
            except BaseException:
                conn.close()
                raise
        return conn
    key = _key(path)
    conn = conns.get(key)
    if conn is not None:
        return conn
    conn = connect(path, _PinnedConnection)
    try:
        if setup is not None:
            setup(conn)
    except BaseException:
        sqlite3.Connection.close(conn)
        raise
    conn._pinned = True
    conns[key] = conn
    return conn


def has_conn(path: Any) -> bool:
    """True when the current scope already holds a connection for ``path``."""
    conns = getattr(_local, "conns", None)
    return bool(conns) and _key(path) in conns


def forget(path: Any) -> None:
    """Really close the scoped connection of ``path`` (e.g. before the file is moved aside)."""
    conns = getattr(_local, "conns", None)
    if not conns:
        return
    conn = conns.pop(_key(path), None)
    if conn is not None:
        conn._really_close()


@contextmanager
def turn_scope() -> Iterator[None]:
    """Share one connection per database file for the duration of the block."""
    if getattr(_local, "conns", None) is not None:
        yield  # nested: the outermost scope owns the connections
        return
    _local.conns = {}
    try:
        yield
    finally:
        conns, _local.conns = _local.conns, None
        for conn in conns.values():
            try:
                conn._really_close()
            except Exception:  # noqa: BLE001 - closing must never break the turn
                pass
