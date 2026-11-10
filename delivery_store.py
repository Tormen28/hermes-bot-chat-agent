"""Durable delivery store for Bot Chat (Fase 1a, v1.2).

Replaces ``team-notify.json`` as the live store. SQLite, safe between
processes: the plugin runs at the same time in the chat, the kanban worker,
the gateway/dispatcher and the cron worker, and ``threading.Lock`` is
useless across processes (E1).

Protocol: ``pending -> offered -> delivered_in_turn`` (+ ``dead_letter``).
Reading a turn or generating never consumes an event: only ``confirm``
(after ``post_llm_call``) moves a row to ``delivered_in_turn``.

Design decision (Gustavo, checkpoint Q1): delivery confirmation depends on
the OFFER LEASE, not on ``agent_loop_stopped`` firing. The lease is the
mechanism; ``agent_loop_stopped`` is only a best-effort optimization (a
faster release when it fires and correlates). If it never fires, offers are
released (a) by lease expiry, swept on every offer, and (b) by the next
turn of the same session: a session runs one turn at a time, so when a new
turn starts, the previous turn's offers belong to a turn that ended without
confirmation and are re-offered. Neither path depends on the core.

Safety rules (the opposite of E3/E4):
- A failed read never wipes state: operations abort on error, they never
  save an empty state over the real one.
- Every failure is raised to the caller (the hooks log it at warning);
  nothing is swallowed in ``debug``.

Storage rules:
- One writer at a time (SQLite), short ``BEGIN IMMEDIATE`` transactions,
  ``busy_timeout`` 10.000 ms, bounded retry with jitter on ``SQLITE_BUSY``.
- WAL only on a local disk. The database must never live in a synced
  folder (OneDrive, Dropbox, Google Drive) or on a network path:
  :func:`check_local_path` refuses those at construction with an
  explanatory error.
- Retention is explicit: :meth:`DeliveryStore.prune` deletes only old
  ``delivered_in_turn``/``seen`` rows; ``pending``/``offered``/
  ``dead_letter`` are never pruned, and there is no trimmed list that can
  resurrect old ids (the opposite of E6).

Python: 3.12 here (the live runtime is 3.14.9); no feature past 3.12 is used.

Fase 1b (v1.2 §5) adds, on top of the 1a protocol:

- :meth:`DeliveryStore.claim_for_relay` — a relay claims pending rows under
  a ``relay:<token>`` lease to write them durably through the core (B1).
- ``durable_marks`` — one row per delivery already written durably (B1
  message id). Written *before* the confirm, so a crash between the B1
  write and the confirm cannot re-offer the result: the next
  :meth:`DeliveryStore.offer` confirms marked rows instead of offering them.
- :meth:`DeliveryStore.maintenance` — one sweep (expired leases + prune);
  :meth:`DeliveryStore.oldest_pending_age_seconds` and
  :meth:`DeliveryStore.dead_letter_rows` for accounting.
"""
from __future__ import annotations

import random
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any, Optional

SCHEMA_VERSION = 4
DEFAULT_LEASE_SECONDS = 600
DEFAULT_RETENTION_DAYS = 30
# Fase 3: background-task states (rubric docs/fase3-rubric.md §1).
BG_STATUSES = ("received", "queued", "working", "blocked", "ready", "error", "cancelled")
BG_TERMINAL_STATUSES = ("ready", "error", "cancelled")
BG_OPEN_STATUSES = ("received", "queued", "working", "blocked")
# Allowed transitions. ready/error/cancelled are terminal; blocked is
# NOT terminal (an unblock can re-queue the card and it can still finish).
_BG_TRANSITIONS = {
    "received": {"queued", "working", "blocked", "ready", "error", "cancelled"},
    "queued": {"working", "blocked", "ready", "error", "cancelled"},
    "working": {"blocked", "ready", "error", "cancelled"},
    "blocked": {"queued", "working", "ready", "error", "cancelled"},
    "ready": set(),
    "error": set(),
    "cancelled": set(),
}
# A cancel marker older than this is stale (the block it belonged to
# never fired) and must not relabel a later worker-initiated block.
CANCEL_REQUEST_TTL_SECONDS = 3600
RESUME_OFFER_TTL_SECONDS = 86400
BUSY_TIMEOUT_MS = 10_000
MAX_RETRIES = 5
MAX_ATTEMPTS = 5
_KINDS = ("completed", "blocked")
_STATUSES = ("pending", "offered", "delivered_in_turn", "seen", "dead_letter")
# Best-effort guard: folder names of common sync services. A database under
# one of these is refused (E2: mandatory locking on Windows + sync churn).
_SYNC_DIR_NAMES = {
    "onedrive", "dropbox", "google drive", "googledrive",
    "iclouddrive", "box sync", "mega", "nextcloud", "pcloud",
}

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS deliveries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL CHECK (kind IN ('completed', 'blocked')),
  task_id TEXT NOT NULL,
  session_id TEXT NOT NULL DEFAULT '',
  project_id TEXT NOT NULL DEFAULT '',
  assignee TEXT NOT NULL DEFAULT '',
  summary TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'offered', 'delivered_in_turn', 'seen', 'dead_letter')),
  offered_turn TEXT,
  offered_at REAL,
  locked_until REAL,
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_deliveries_task_kind
  ON deliveries(task_id, kind);
CREATE INDEX IF NOT EXISTS idx_deliveries_session_status
  ON deliveries(session_id, status);
CREATE TABLE IF NOT EXISTS creates (
  task_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL DEFAULT '',
  project_id TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '',
  assignee TEXT NOT NULL DEFAULT '',
  idempotency_key TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS durable_marks (
  delivery_id INTEGER PRIMARY KEY,
  message_id INTEGER NOT NULL,
  destination TEXT NOT NULL DEFAULT 'b1',
  recorded_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
-- Fase 2: incremental team note. One row per session: the fingerprint
-- of the last rendered dynamic section (cards {id: status}, confirmed
-- delivery ids, tray count, card version, warning flag). Written only
-- by the chat process (single writer). Losing a row is safe: the next
-- turn rebuilds the full note from durable state.
CREATE TABLE IF NOT EXISTS context_state (
  session_id TEXT PRIMARY KEY,
  fingerprint TEXT NOT NULL DEFAULT '',
  updated_at REAL NOT NULL
);
-- Fase 2: project-card version history (wow 7). The Markdown file is
-- the source of truth; every content change records a version row here.
-- Derived content never writes the file, so every row here is an
-- explicit (manual) edit.
CREATE TABLE IF NOT EXISTS project_card_versions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id TEXT NOT NULL DEFAULT '',
  version INTEGER NOT NULL,
  content_hash TEXT NOT NULL,
  content TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'manual',
  author TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_card_versions_project_version
  ON project_card_versions(project_id, version);
-- Fase 2: minimal log of tools with side effects (deletes, destructive
-- git, protected pushes, external sends, kanban mutations). It survives
-- compaction as part of the protected block (plan §6).
CREATE TABLE IF NOT EXISTS tool_effects (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL DEFAULT '',
  tool_name TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tool_effects_session
  ON tool_effects(session_id, created_at);
-- Fase 3: background-task registry (route A). One row per kanban card
-- the plugin launched or observed for a teammate / worker profile.
-- ``status`` is the UI state driven by real core events (claimed,
-- spawned, completed, blocked) plus the plugin-side ``cancelled``.
-- ``resource`` is the serialization key (workspace_path or project);
-- ``delivery_id`` is set only when the completed delivery was
-- persisted (no persisted delivery, no ``ready`` — wow 4).
CREATE TABLE IF NOT EXISTS background_tasks (
  task_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL DEFAULT '',
  project_id TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '',
  assignee TEXT NOT NULL DEFAULT '',
  resource TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'received'
    CHECK (status IN ('received', 'queued', 'working', 'blocked', 'ready', 'error', 'cancelled')),
  wait_reason TEXT NOT NULL DEFAULT '',
  delivery_id INTEGER,
  run_id INTEGER,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bg_tasks_session_status
  ON background_tasks(session_id, status);
CREATE INDEX IF NOT EXISTS idx_bg_tasks_resource
  ON background_tasks(session_id, resource, status);
-- Fase 3: stop-all flag per session. While set, no new background work
-- is accepted for the session (the guard blocks kanban_create).
CREATE TABLE IF NOT EXISTS session_flags (
  session_id TEXT PRIMARY KEY,
  stop_all INTEGER NOT NULL DEFAULT 0,
  stopped_at REAL,
  reason TEXT NOT NULL DEFAULT ''
);
-- Fase 3: cancel markers. The guard writes one when the chat blocks a
-- card (kanban_block in a Bot Chat session); the kanban_task_blocked
-- hook consumes it and marks the task ``cancelled`` instead of
-- ``blocked``. A block with no marker is a worker-initiated block.
CREATE TABLE IF NOT EXISTS cancel_requests (
  task_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL DEFAULT '',
  requested_at REAL NOT NULL
);
-- Fase 3: resume offers. When the guard blocks a kanban_create because
-- stop-all is active, it records the offer (per idempotency key). A
-- retry of the SAME request in a LATER turn clears the flag: the user
-- confirmed the resume. Same-turn retries stay blocked.
CREATE TABLE IF NOT EXISTS resume_offers (
  session_id TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  turn_id TEXT NOT NULL DEFAULT '',
  offered_at REAL NOT NULL,
  PRIMARY KEY (session_id, idempotency_key)
);
-- Fase 3b: tareas programadas (wow 6). Un registro por trabajo del core
-- creado desde una sesión Bot Chat. ``status`` es el estado del plugin
-- (el core guarda el suyo: enabled/state/next_run_at). ``consent_at`` /
-- ``consent_source`` son la evidencia del permiso explícito del usuario:
-- sin fila aquí no hay entrega, aunque el trabajo exista en el core.
-- ``paused`` (manual) y ``paused_by_stop_all`` son distintos: la
-- reanudación explícita solo toca el segundo. ``removed`` es terminal.
CREATE TABLE IF NOT EXISTS scheduled_tasks (
  job_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL DEFAULT '',
  name TEXT NOT NULL DEFAULT '',
  prompt TEXT NOT NULL DEFAULT '',
  schedule_display TEXT NOT NULL DEFAULT '',
  schedule_raw TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'active'
    CHECK (status IN ('active', 'paused', 'paused_by_stop_all', 'removed')),
  paused_reason TEXT NOT NULL DEFAULT '',
  consent_at REAL,
  consent_source TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sched_tasks_session_status
  ON scheduled_tasks(session_id, status);
-- Fase 3b: disparos ya entregados (o silenciados). Clave por
-- ``execution_id`` del libro del core: observar dos veces el mismo
-- disparo nunca entrega dos veces. ``delivery_id`` es la fila del outbox
-- (NULL en ``silent``: el disparo no produjo nada que anunciar).
CREATE TABLE IF NOT EXISTS scheduled_fires (
  execution_id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL,
  session_id TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'delivered'
    CHECK (status IN ('delivered', 'failed', 'silent')),
  delivery_id INTEGER,
  finished_at TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sched_fires_job
  ON scheduled_fires(job_id, created_at);
"""


def check_local_path(path: Any) -> Path:
    """Validate a database path: local disk, never a synced folder or network.

    Raises ``ValueError`` with an explanatory message when the path is a
    UNC/network path or sits under a known sync-service folder. Best
    effort: an exotic mount can still slip through, in which case SQLite
    errors surface as warnings at the call site instead of silent loss.
    """
    raw = str(path or "").strip()
    if not raw:
        raise ValueError("delivery database path is empty")
    if raw.startswith("\\\\") or raw.startswith("//"):
        raise ValueError(
            f"delivery database must be on a local disk, not a network path: {raw}"
        )
    p = Path(raw).expanduser()
    hit = {part.lower() for part in p.parts} & _SYNC_DIR_NAMES
    if hit:
        raise ValueError(
            "delivery database must not live in a synced folder "
            f"({sorted(hit)[0]}): {p}. Move it to a local folder that no "
            "sync service watches."
        )
    return p


def store_path_for(notify_path: Any) -> Path:
    """Database path for a ``team-notify.json`` path: same dir, ``deliveries.db``."""
    if notify_path is None:
        raise ValueError("notify path is required to locate the delivery store")
    return Path(notify_path).expanduser().parent / "deliveries.db"


def _is_busy(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return isinstance(exc, sqlite3.OperationalError) and ("locked" in msg or "busy" in msg)


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


class DeliveryStore:
    """SQLite-backed delivery store. One short-lived connection per operation."""

    def __init__(self, path: Any, lease_seconds: Optional[int] = None):
        self.path = check_local_path(path)
        self.lease_seconds = (
            DEFAULT_LEASE_SECONDS if lease_seconds is None
            else max(0, int(lease_seconds))
        )

    # ------------------------------------------------------------------ internals
    def _conn(self) -> sqlite3.Connection:
        """Open a connection (creating parent dir and schema on first use).

        Retries setup on ``SQLITE_BUSY``/``SQLITE_LOCKED``: ``PRAGMA
        journal_mode = WAL`` needs a lock that ``busy_timeout`` does not
        cover, so under multi-process contention a fresh connection can
        fail setup with ``OperationalError: database is locked``. Without
        this retry that error escaped ``_write`` (which only retries after
        the connection is open) and killed the consumer mid-gate.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        last: Optional[BaseException] = None
        for attempt in range(MAX_RETRIES + 1):
            conn = sqlite3.connect(
                self.path, timeout=BUSY_TIMEOUT_MS / 1000.0, isolation_level=None
            )
            conn.row_factory = sqlite3.Row
            try:
                conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
                conn.execute("PRAGMA journal_mode = WAL")
                conn.execute("PRAGMA synchronous = FULL")
                conn.executescript(_SCHEMA_SQL)
                conn.execute(
                    "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
                # Migrated databases keep their old version row without
                # this: the version must say what schema is actually on
                # disk after the CREATE TABLE IF NOT EXISTS above ran.
                conn.execute(
                    "UPDATE meta SET value = ? WHERE key = 'schema_version'",
                    (str(SCHEMA_VERSION),),
                )
            except Exception as exc:  # noqa: BLE001 - retried selectively below
                conn.close()
                last = exc
                if _is_busy(exc) and attempt < MAX_RETRIES:
                    delay = min(0.05 * (2 ** attempt), 0.5) + random.uniform(0, 0.05)
                    time.sleep(delay)
                    continue
                raise
            return conn
        assert last is not None  # pragma: no cover - loop always raises or returns
        raise last

    def _write(self, fn: Any) -> Any:
        """Run ``fn(conn)`` in one short ``BEGIN IMMEDIATE`` transaction.

        Retries with jitter only on ``SQLITE_BUSY``/``SQLITE_LOCKED``.
        Any other error aborts the transaction and propagates: a failed
        operation never writes an empty or partial state (E4).
        """
        last: Optional[BaseException] = None
        for attempt in range(MAX_RETRIES + 1):
            conn = self._conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    result = fn(conn)
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
                conn.execute("COMMIT")
                return result
            except Exception as exc:  # noqa: BLE001 - retried selectively below
                last = exc
                if _is_busy(exc) and attempt < MAX_RETRIES:
                    delay = min(0.05 * (2 ** attempt), 0.5) + random.uniform(0, 0.05)
                    time.sleep(delay)
                    continue
                raise
            finally:
                conn.close()
        assert last is not None  # pragma: no cover - loop always raises or returns
        raise last

    def _read(self, fn: Any) -> Any:
        if not self.path.is_file():
            return fn(None)
        with closing(self._conn()) as conn:
            return fn(conn)

    # ------------------------------------------------------------------ creates
    def remember_create(self, *, task_id: str, session_id: str = "", project_id: str = "",
                        title: str = "", assignee: str = "", idempotency_key: str = "",
                        now: Optional[float] = None) -> None:
        """Remember that ``task_id`` was created in ``session_id`` (wow 8).

        Upsert: a re-created card replaces the previous mapping.
        """
        tid = str(task_id or "").strip()
        ts = time.time() if now is None else float(now)

        def _do(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO creates (task_id, session_id, project_id, title, "
                "assignee, idempotency_key, created_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(task_id) DO UPDATE SET session_id = excluded.session_id, "
                "project_id = excluded.project_id, title = excluded.title, "
                "assignee = excluded.assignee, idempotency_key = excluded.idempotency_key, "
                "created_at = excluded.created_at",
                (tid, str(session_id or ""), str(project_id or ""), str(title or ""),
                 str(assignee or ""), str(idempotency_key or ""), ts),
            )

        self._write(_do)

    def lookup_session(self, task_id: str) -> str:
        """Session that created ``task_id``, or ``""`` when unknown."""
        tid = str(task_id or "").strip()
        if not tid:
            return ""

        def _do(conn: Optional[sqlite3.Connection]) -> str:
            if conn is None:
                return ""
            row = conn.execute(
                "SELECT session_id FROM creates WHERE task_id = ? LIMIT 1", (tid,)
            ).fetchone()
            return str(row["session_id"] or "") if row else ""

        return self._read(_do)

    # ------------------------------------------------------------------ deliveries
    def record(self, *, kind: str, task_id: str, session_id: Optional[str] = None,
               project_id: str = "", assignee: str = "", summary: str = "",
               now: Optional[float] = None) -> int:
        """Record a delivery once (idempotent on ``(task_id, kind)``).

        ``session_id=None`` resolves the destination through the ``creates``
        mapping (``remember_create``); ``""`` records it as unassigned (it
        goes to the unassigned tray, never to a session). Returns the row id,
        whether it was just inserted or already existed.
        """
        if kind not in _KINDS:
            raise ValueError(f"unknown delivery kind: {kind!r}")
        tid = str(task_id or "").strip()
        if not tid:
            raise ValueError("task_id is required")
        ts = time.time() if now is None else float(now)
        sid = self.lookup_session(tid) if session_id is None else str(session_id)

        def _do(conn: sqlite3.Connection) -> int:
            conn.execute(
                "INSERT INTO deliveries (kind, task_id, session_id, project_id, "
                "assignee, summary, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?) "
                "ON CONFLICT(task_id, kind) DO NOTHING",
                (kind, tid, sid, str(project_id or ""), str(assignee or ""),
                 str(summary or ""), ts, ts),
            )
            row = conn.execute(
                "SELECT id FROM deliveries WHERE task_id = ? AND kind = ? LIMIT 1",
                (tid, kind),
            ).fetchone()
            assert row is not None  # just inserted or already there
            return int(row["id"])

        return self._write(_do)

    def offer(self, session_id: str, turn_id: str = "",
              lease_seconds: Optional[int] = None,
              now: Optional[float] = None) -> list[dict[str, Any]]:
        """Offer this session's pending deliveries for one turn.

        Atomically (one transaction): (1) release offers whose lease
        expired, (2) release this session's offers from earlier turns (a
        session runs one turn at a time, so those belong to a turn that
        ended without confirmation), (3) claim this session's pending rows
        as ``offered`` under this turn's token. Unassigned rows
        (``session_id=''``) are never offered to a session; use
        :meth:`unassigned` for the tray. Returns the offered rows.
        """
        sid = str(session_id or "")
        ts = time.time() if now is None else float(now)
        lease = self.lease_seconds if lease_seconds is None else max(0, int(lease_seconds))
        token = str(turn_id or "").strip() or uuid.uuid4().hex[:12]

        def _do(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            # (1b) Rows already written durably through B1 (they carry a
            # durable mark) are confirmed, never re-offered: this closes the
            # crash window between the B1 write and the relay's confirm, so
            # a result is never announced twice (once in the transcript, once
            # in the turn note).
            conn.execute(
                "UPDATE deliveries SET status = 'delivered_in_turn', updated_at = ? "
                "WHERE status IN ('pending', 'offered') AND id IN "
                "(SELECT delivery_id FROM durable_marks)",
                (ts,),
            )
            conn.execute(
                "UPDATE deliveries SET status = 'pending', offered_turn = NULL, "
                "offered_at = NULL, locked_until = NULL "
                "WHERE status = 'offered' AND locked_until IS NOT NULL "
                "AND locked_until <= ?",
                (ts,),
            )
            conn.execute(
                "UPDATE deliveries SET status = 'pending', offered_turn = NULL, "
                "offered_at = NULL, locked_until = NULL "
                "WHERE status = 'offered' AND session_id = ? "
                "AND offered_turn IS NOT NULL AND offered_turn != ? "
                "AND offered_turn NOT LIKE 'relay:%'",
                (sid, token),
            )
            conn.execute(
                "UPDATE deliveries SET status = 'offered', offered_turn = ?, "
                "offered_at = ?, locked_until = ?, updated_at = ? "
                "WHERE status = 'pending' AND session_id = ?",
                (token, ts, ts + lease, ts, sid),
            )
            rows = conn.execute(
                "SELECT id, kind, task_id, session_id, project_id, assignee, summary "
                "FROM deliveries WHERE status = 'offered' AND session_id = ? "
                "AND offered_turn = ? ORDER BY created_at, id",
                (sid, token),
            ).fetchall()
            return [_row_to_dict(r) for r in rows]

        return self._write(_do)

    def confirm(self, session_id: str, turn_id: Optional[str] = None,
                now: Optional[float] = None) -> int:
        """Mark this turn's offered rows as delivered.

        ``turn_id=None`` (or empty) confirms every offered row of the
        session. Returns how many rows moved to ``delivered_in_turn``.
        """
        sid = str(session_id or "")
        ts = time.time() if now is None else float(now)
        token = str(turn_id or "").strip() or None

        def _do(conn: sqlite3.Connection) -> int:
            if token:
                cur = conn.execute(
                    "UPDATE deliveries SET status = 'delivered_in_turn', updated_at = ? "
                    "WHERE status = 'offered' AND session_id = ? AND offered_turn = ?",
                    (ts, sid, token),
                )
            else:
                cur = conn.execute(
                    "UPDATE deliveries SET status = 'delivered_in_turn', updated_at = ? "
                    "WHERE status = 'offered' AND session_id = ?",
                    (ts, sid),
                )
            return cur.rowcount

        return self._write(_do)

    def release_turn(self, session_id: str, turn_id: Optional[str] = None,
                     now: Optional[float] = None) -> int:
        """Return this session's offered rows to ``pending`` (best effort).

        Used by the ``agent_loop_stopped`` handler as an optimization only:
        the lease (swept on every offer) is the mechanism, this is just
        faster. Returns how many rows were released.
        """
        sid = str(session_id or "")
        ts = time.time() if now is None else float(now)
        token = str(turn_id or "").strip() or None

        def _do(conn: sqlite3.Connection) -> int:
            if token:
                cur = conn.execute(
                    "UPDATE deliveries SET status = 'pending', offered_turn = NULL, "
                    "offered_at = NULL, locked_until = NULL, updated_at = ? "
                    "WHERE status = 'offered' AND session_id = ? AND offered_turn = ?",
                    (ts, sid, token),
                )
            else:
                cur = conn.execute(
                    "UPDATE deliveries SET status = 'pending', offered_turn = NULL, "
                    "offered_at = NULL, locked_until = NULL, updated_at = ? "
                    "WHERE status = 'offered' AND session_id = ?",
                    (ts, sid),
                )
            return cur.rowcount

        return self._write(_do)

    def release_expired(self, now: Optional[float] = None) -> int:
        """Sweep offers whose lease expired back to ``pending``. Returns count."""
        ts = time.time() if now is None else float(now)

        def _do(conn: sqlite3.Connection) -> int:
            cur = conn.execute(
                "UPDATE deliveries SET status = 'pending', offered_turn = NULL, "
                "offered_at = NULL, locked_until = NULL, updated_at = ? "
                "WHERE status = 'offered' AND locked_until IS NOT NULL "
                "AND locked_until <= ?",
                (ts, ts),
            )
            return cur.rowcount

        return self._write(_do)

    # ------------------------------------------------------------------ 1b relay
    def claim_for_relay(self, session_id: str = "", limit: int = 50,
                        lease_seconds: Optional[int] = None,
                        now: Optional[float] = None) -> tuple[str, list[dict[str, Any]]]:
        """Claim up to ``limit`` pending rows for a durable (B1) write.

        Atomically moves them to ``offered`` under a ``relay:<token>`` turn
        token with a lease. Only rows with a known session (``session_id !=
        ''``) are claimed — B1 requires a destination session, and
        unassigned rows stay in the tray. Rows that already carry a durable
        mark are never claimed (their write already succeeded; the next
        :meth:`offer` confirms them instead of re-offering). Returns
        ``(token, rows)``. A crashed relay leaves the claim behind; the
        lease (swept by :meth:`offer` / :meth:`release_expired`) returns the
        rows to ``pending``, and the B1 write itself is idempotent by stable
        id, so a retry never writes a second transcript row.
        """
        sid = str(session_id or "")
        ts = time.time() if now is None else float(now)
        lease = self.lease_seconds if lease_seconds is None else max(0, int(lease_seconds))
        token = "relay:" + uuid.uuid4().hex[:12]
        n = max(0, int(limit))

        def _do(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            params: list[Any] = [token, ts, ts + lease, ts]
            where = "status = 'pending' AND session_id != '' AND id NOT IN " \
                    "(SELECT delivery_id FROM durable_marks)"
            if sid:
                where += " AND session_id = ?"
                params.append(sid)
            conn.execute(
                "UPDATE deliveries SET status = 'offered', offered_turn = ?, "
                "offered_at = ?, locked_until = ?, updated_at = ? "
                f"WHERE id IN (SELECT id FROM deliveries WHERE {where} "
                "ORDER BY created_at, id LIMIT ?)",
                (*params, n),
            )
            rows = conn.execute(
                "SELECT id, kind, task_id, session_id, project_id, assignee, summary "
                "FROM deliveries WHERE offered_turn = ? ORDER BY created_at, id",
                (token,),
            ).fetchall()
            return [_row_to_dict(r) for r in rows]

        return token, self._write(_do)

    def confirm_ids(self, ids: Any, now: Optional[float] = None) -> int:
        """Mark the given row ids as delivered. Returns how many moved."""
        ts = time.time() if now is None else float(now)
        wanted = [int(i) for i in (ids or [])]
        if not wanted:
            return 0

        def _do(conn: sqlite3.Connection) -> int:
            cur = conn.execute(
                "UPDATE deliveries SET status = 'delivered_in_turn', updated_at = ? "
                "WHERE status IN ('pending', 'offered') AND id IN "
                f"({','.join('?' * len(wanted))})",
                (ts, *wanted),
            )
            return cur.rowcount

        return self._write(_do)

    def release_claim(self, token: str, now: Optional[float] = None) -> int:
        """Return a relay claim's rows to ``pending``. Returns count."""
        ts = time.time() if now is None else float(now)
        tok = str(token or "")

        def _do(conn: sqlite3.Connection) -> int:
            cur = conn.execute(
                "UPDATE deliveries SET status = 'pending', offered_turn = NULL, "
                "offered_at = NULL, locked_until = NULL, updated_at = ? "
                "WHERE status = 'offered' AND offered_turn = ?",
                (ts, tok),
            )
            return cur.rowcount

        return self._write(_do)

    def durable_mark(self, delivery_id: int, message_id: int,
                     destination: str = "b1",
                     now: Optional[float] = None) -> None:
        """Record that a delivery was written durably (B1 message id).

        Written BEFORE the confirm, in its own transaction: if the relay
        crashes between the B1 write and the confirm, the mark survives and
        the next :meth:`offer` confirms the row instead of re-offering it.
        """
        ts = time.time() if now is None else float(now)

        def _do(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT OR REPLACE INTO durable_marks "
                "(delivery_id, message_id, destination, recorded_at) "
                "VALUES (?, ?, ?, ?)",
                (int(delivery_id), int(message_id), str(destination or "b1"), ts),
            )

        self._write(_do)

    def durable_mark_for(self, delivery_id: int) -> Optional[dict[str, Any]]:
        """The durable mark for a row, or ``None`` when it has none."""

        def _do(conn: Optional[sqlite3.Connection]) -> Optional[dict[str, Any]]:
            if conn is None:
                return None
            row = conn.execute(
                "SELECT * FROM durable_marks WHERE delivery_id = ? LIMIT 1",
                (int(delivery_id),),
            ).fetchone()
            return _row_to_dict(row) if row else None

        return self._read(_do)

    def dead_letter_rows(self) -> list[dict[str, Any]]:
        """Dead-letter rows, for accounting (each one explained by id)."""

        def _do(conn: Optional[sqlite3.Connection]) -> list[dict[str, Any]]:
            if conn is None:
                return []
            rows = conn.execute(
                "SELECT * FROM deliveries WHERE status = 'dead_letter' "
                "ORDER BY id"
            ).fetchall()
            return [_row_to_dict(r) for r in rows]

        return self._read(_do)

    def oldest_pending_age_seconds(self, now: Optional[float] = None) -> Optional[float]:
        """Age of the oldest pending delivery, or ``None`` when none pending."""
        return self.stats(now=now)["oldest_pending_age_seconds"]

    def maintenance(self, older_than_days: int = DEFAULT_RETENTION_DAYS,
                    now: Optional[float] = None) -> dict[str, Any]:
        """One sweep: release expired leases, prune old delivered rows.

        Returns ``{"released_expired": n, "pruned": n, "stats": {...}}``.
        Never touches ``pending``/``offered``/``dead_letter`` (see
        :meth:`prune`). Safe to run at loop end / startup.
        """
        released = self.release_expired(now=now)
        pruned = self.prune(older_than_days=older_than_days, now=now)
        # Fase 3: stale cancel/resume markers never outlive their TTL.
        ts = time.time() if now is None else float(now)
        self._write(lambda conn: conn.execute(
            "DELETE FROM cancel_requests WHERE requested_at < ?",
            (ts - CANCEL_REQUEST_TTL_SECONDS,)))
        self._write(lambda conn: conn.execute(
            "DELETE FROM resume_offers WHERE offered_at < ?",
            (ts - RESUME_OFFER_TTL_SECONDS,)))
        return {"released_expired": released, "pruned": pruned,
                "stats": self.stats(now=now)}

    def integrity_check(self) -> str:
        """SQLite integrity check on the store (``'ok'`` when healthy)."""

        def _do(conn: Optional[sqlite3.Connection]) -> str:
            if conn is None:
                return "ok"
            row = conn.execute("PRAGMA integrity_check").fetchone()
            return str(row[0]) if row else "ok"

        return self._read(_do)

    def dead_letter(self, delivery_id: int, error: str = "",
                    now: Optional[float] = None) -> int:
        """Move a delivery to ``dead_letter`` (explicit, accounted). Returns count."""
        ts = time.time() if now is None else float(now)

        def _do(conn: sqlite3.Connection) -> int:
            cur = conn.execute(
                "UPDATE deliveries SET status = 'dead_letter', last_error = ?, "
                "updated_at = ? WHERE id = ? AND status != 'delivered_in_turn'",
                (str(error or ""), ts, int(delivery_id)),
            )
            return cur.rowcount

        return self._write(_do)

    def mark_failed(self, delivery_id: int, error: str = "",
                    max_attempts: int = MAX_ATTEMPTS,
                    now: Optional[float] = None) -> tuple[str, int]:
        """Count a failed attempt; to ``dead_letter`` after ``max_attempts``.

        Returns ``(status, attempts)``. The 1b relay uses this; 1a only
        exposes it (a failed store operation raises instead, it never
        silently drops the delivery).
        """
        ts = time.time() if now is None else float(now)

        def _do(conn: sqlite3.Connection) -> tuple[str, int]:
            conn.execute(
                "UPDATE deliveries SET attempts = attempts + 1, last_error = ?, "
                "updated_at = ? WHERE id = ?",
                (str(error or ""), ts, int(delivery_id)),
            )
            row = conn.execute(
                "SELECT status, attempts FROM deliveries WHERE id = ? LIMIT 1",
                (int(delivery_id),),
            ).fetchone()
            if row is None:
                return ("missing", 0)
            status, attempts = str(row["status"]), int(row["attempts"])
            if status != "delivered_in_turn" and attempts >= max_attempts:
                conn.execute(
                    "UPDATE deliveries SET status = 'dead_letter', updated_at = ? "
                    "WHERE id = ?",
                    (ts, int(delivery_id)),
                )
                status = "dead_letter"
            return (status, attempts)

        return self._write(_do)

    def prune(self, older_than_days: int = DEFAULT_RETENTION_DAYS,
              now: Optional[float] = None) -> int:
        """Delete old ``delivered_in_turn``/``seen`` rows only. Returns count.

        ``pending``/``offered``/``dead_letter`` are never pruned: pruning
        must never resurrect an old id (the opposite of E6).
        """
        ts = time.time() if now is None else float(now)
        cutoff = ts - max(0, int(older_than_days)) * 86400

        def _do(conn: sqlite3.Connection) -> int:
            cur = conn.execute(
                "DELETE FROM deliveries WHERE status IN ('delivered_in_turn', 'seen') "
                "AND updated_at < ?",
                (cutoff,),
            )
            removed = cur.rowcount
            # Fase 2 caches: a stale fingerprint only forces a full rebuild
            # next turn; old tool-effect lines are a rolling log. Both are
            # safe to drop (unlike pending/offered/dead_letter, untouched).
            conn.execute(
                "DELETE FROM context_state WHERE updated_at < ?", (cutoff,))
            conn.execute(
                "DELETE FROM tool_effects WHERE created_at < ?", (cutoff,))
            return removed

        return self._write(_do)

    # ------------------------------------------------------------------ queries
    def get(self, task_id: str, kind: Optional[str] = None) -> list[dict[str, Any]]:
        """All rows for a task (optionally one kind), as dicts."""
        tid = str(task_id or "").strip()

        def _do(conn: Optional[sqlite3.Connection]) -> list[dict[str, Any]]:
            if conn is None or not tid:
                return []
            if kind:
                rows = conn.execute(
                    "SELECT * FROM deliveries WHERE task_id = ? AND kind = ? "
                    "ORDER BY id",
                    (tid, kind),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM deliveries WHERE task_id = ? ORDER BY id",
                    (tid,),
                ).fetchall()
            return [_row_to_dict(r) for r in rows]

        return self._read(_do)

    def rows_for_session(self, session_id: str, statuses: Any = (),
                         limit: int = 200) -> list[dict[str, Any]]:
        """Delivery rows for a session, optionally filtered by status.

        Newest first. Used by the Fase 2 reconstruction (pending/offered
        rows are the facts to re-render; delivered rows are the result
        IDs that must survive compaction).
        """
        sid = str(session_id or "").strip()
        wanted = tuple(str(s) for s in (statuses or ()))

        def _do(conn: Optional[sqlite3.Connection]) -> list[dict[str, Any]]:
            if conn is None or not sid:
                return []
            if wanted:
                marks = ",".join("?" for _ in wanted)
                rows = conn.execute(
                    f"SELECT * FROM deliveries WHERE session_id = ? AND status IN ({marks}) "
                    "ORDER BY created_at DESC, id DESC LIMIT ?",
                    (sid, *wanted, max(1, int(limit))),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM deliveries WHERE session_id = ? "
                    "ORDER BY created_at DESC, id DESC LIMIT ?",
                    (sid, max(1, int(limit))),
                ).fetchall()
            return [_row_to_dict(r) for r in rows]

        return self._read(_do)

    def delivered_task_ids(self, session_id: str, limit: int = 500) -> list[str]:
        """Task ids already delivered in this session (confirmed turns)."""
        rows = self.rows_for_session(session_id, ("delivered_in_turn",), limit=limit)
        seen: list[str] = []
        for row in rows:
            tid = str(row.get("task_id") or "")
            if tid and tid not in seen:
                seen.append(tid)
        return seen

    def unassigned(self) -> list[dict[str, Any]]:
        """Pending deliveries without a known session (the unassigned tray)."""

        def _do(conn: Optional[sqlite3.Connection]) -> list[dict[str, Any]]:
            if conn is None:
                return []
            rows = conn.execute(
                "SELECT id, kind, task_id, session_id, project_id, assignee, summary "
                "FROM deliveries WHERE status = 'pending' AND session_id = '' "
                "ORDER BY created_at, id"
            ).fetchall()
            return [_row_to_dict(r) for r in rows]

        return self._read(_do)

    def unassigned_count(self) -> int:
        return len(self.unassigned())

    def stats(self, now: Optional[float] = None) -> dict[str, Any]:
        """Accounting by id: counts per status + oldest pending age (1b)."""
        ts = time.time() if now is None else float(now)

        def _do(conn: Optional[sqlite3.Connection]) -> dict[str, Any]:
            if conn is None:
                return {"by_status": {}, "oldest_pending_age_seconds": None,
                        "total": 0, "durable_marked": 0}
            by_status = {
                str(r["status"]): int(r["n"])
                for r in conn.execute(
                    "SELECT status, COUNT(*) AS n FROM deliveries GROUP BY status"
                ).fetchall()
            }
            row = conn.execute(
                "SELECT MIN(created_at) AS oldest FROM deliveries "
                "WHERE status = 'pending'"
            ).fetchone()
            oldest = row["oldest"] if row else None
            total = sum(by_status.values())
            durable = conn.execute(
                "SELECT COUNT(*) AS n FROM durable_marks"
            ).fetchone()["n"]
            return {
                "by_status": by_status,
                "oldest_pending_age_seconds": (ts - float(oldest)) if oldest else None,
                "total": total,
                "durable_marked": int(durable),
            }

        return self._read(_do)

    # ------------------------------------------------------- Fase 2: context
    def save_context_state(self, session_id: str, fingerprint: str,
                           now: Optional[float] = None) -> None:
        """Store the incremental-note fingerprint for a session."""
        sid = str(session_id or "").strip()
        ts = time.time() if now is None else float(now)
        if not sid:
            return

        def _do(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO context_state (session_id, fingerprint, updated_at) "
                "VALUES (?, ?, ?) ON CONFLICT(session_id) DO UPDATE SET "
                "fingerprint = excluded.fingerprint, updated_at = excluded.updated_at",
                (sid, str(fingerprint or ""), ts),
            )

        self._write(_do)

    def load_context_state(self, session_id: str) -> str:
        """Fingerprint of the last rendered dynamic section ('' when none)."""
        sid = str(session_id or "").strip()

        def _do(conn: Optional[sqlite3.Connection]) -> str:
            if conn is None or not sid:
                return ""
            row = conn.execute(
                "SELECT fingerprint FROM context_state WHERE session_id = ? LIMIT 1",
                (sid,),
            ).fetchone()
            return str(row["fingerprint"] or "") if row else ""

        return self._read(_do)

    def clear_context_state(self, session_id: str) -> int:
        """Drop a session fingerprint (cancel / compaction / reset).

        The next turn rebuilds the full note from durable state.
        Returns the deleted row count.
        """
        sid = str(session_id or "").strip()

        def _do(conn: sqlite3.Connection) -> int:
            if not sid:
                return 0
            return conn.execute(
                "DELETE FROM context_state WHERE session_id = ?", (sid,)
            ).rowcount

        return self._write(_do)

    def record_card_version(self, *, project_id: str = "", version: int = 0,
                            content_hash: str = "", content: str = "",
                            source: str = "manual", author: str = "",
                            now: Optional[float] = None) -> int:
        """Record one project-card version. Returns the row id."""
        ts = time.time() if now is None else float(now)

        def _do(conn: sqlite3.Connection) -> int:
            cur = conn.execute(
                "INSERT INTO project_card_versions "
                "(project_id, version, content_hash, content, source, author, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(project_id, version) DO UPDATE SET "
                "content_hash = excluded.content_hash, content = excluded.content, "
                "source = excluded.source, author = excluded.author, "
                "created_at = excluded.created_at",
                (str(project_id or ""), int(version), str(content_hash or ""),
                 str(content or ""), str(source or "manual"),
                 str(author or ""), ts),
            )
            return int(cur.lastrowid or 0)

        return self._write(_do)

    def last_card_version(self, project_id: str = "") -> Optional[dict[str, Any]]:
        """Newest recorded card version for a project (``None`` when none)."""

        def _do(conn: Optional[sqlite3.Connection]) -> Optional[dict[str, Any]]:
            if conn is None:
                return None
            row = conn.execute(
                "SELECT * FROM project_card_versions WHERE project_id = ? "
                "ORDER BY version DESC, id DESC LIMIT 1",
                (str(project_id or ""),),
            ).fetchone()
            return _row_to_dict(row) if row else None

        return self._read(_do)

    def card_versions(self, project_id: str = "", limit: int = 20) -> list[dict[str, Any]]:
        """Version history for a project, newest first."""

        def _do(conn: Optional[sqlite3.Connection]) -> list[dict[str, Any]]:
            if conn is None:
                return []
            rows = conn.execute(
                "SELECT * FROM project_card_versions WHERE project_id = ? "
                "ORDER BY version DESC, id DESC LIMIT ?",
                (str(project_id or ""), max(1, int(limit))),
            ).fetchall()
            return [_row_to_dict(r) for r in rows]

        return self._read(_do)

    def record_tool_effect(self, session_id: str, tool_name: str, detail: str = "",
                           now: Optional[float] = None) -> int:
        """Append one entry to the minimal tool-effects log. Returns row id."""
        ts = time.time() if now is None else float(now)

        def _do(conn: sqlite3.Connection) -> int:
            cur = conn.execute(
                "INSERT INTO tool_effects (session_id, tool_name, detail, created_at) "
                "VALUES (?, ?, ?, ?)",
                (str(session_id or ""), str(tool_name or ""),
                 str(detail or "")[:240], ts),
            )
            return int(cur.lastrowid or 0)

        return self._write(_do)

    def tool_effects(self, session_id: str, limit: int = 10) -> list[dict[str, Any]]:
        """Newest tool-effect entries for a session (default last 10)."""

        def _do(conn: Optional[sqlite3.Connection]) -> list[dict[str, Any]]:
            if conn is None:
                return []
            rows = conn.execute(
                "SELECT * FROM tool_effects WHERE session_id = ? "
                "ORDER BY created_at DESC, id DESC LIMIT ?",
                (str(session_id or ""), max(1, int(limit))),
            ).fetchall()
            return [_row_to_dict(r) for r in rows]

        return self._read(_do)

    # ------------------------------------------------- Fase 3: background tasks
    def record_background_task(self, *, task_id: str, session_id: str = "",
                               project_id: str = "", title: str = "",
                               assignee: str = "", resource: str = "",
                               now: Optional[float] = None) -> Optional[dict[str, Any]]:
        """Record a launched background task (status ``received``).

        First write wins: a re-record of the same ``task_id`` (double
        dispatch of the same idempotency key) returns the existing row
        instead of a second one. Returns the row, or ``None`` when the
        task id is empty.
        """
        tid = str(task_id or "").strip()
        if not tid:
            return None
        ts = time.time() if now is None else float(now)

        def _do(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO background_tasks (task_id, session_id, project_id, "
                "title, assignee, resource, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'received', ?, ?) "
                "ON CONFLICT(task_id) DO NOTHING",
                (tid, str(session_id or ""), str(project_id or ""),
                 str(title or "")[:240], str(assignee or ""),
                 str(resource or ""), ts, ts),
            )

        self._write(_do)
        return self.background_task(tid)

    def background_task(self, task_id: str) -> Optional[dict[str, Any]]:
        """One background-task row, or ``None`` when unknown."""
        tid = str(task_id or "").strip()

        def _do(conn: Optional[sqlite3.Connection]) -> Optional[dict[str, Any]]:
            if conn is None or not tid:
                return None
            row = conn.execute(
                "SELECT * FROM background_tasks WHERE task_id = ? LIMIT 1",
                (tid,),
            ).fetchone()
            return _row_to_dict(row) if row else None

        return self._read(_do)

    def background_tasks(self, session_id: str, statuses: Any = (),
                         limit: int = 200) -> list[dict[str, Any]]:
        """Background tasks of a session, optionally filtered by status.

        Oldest first (the visible queue order).
        """
        sid = str(session_id or "").strip()
        wanted = tuple(str(s) for s in (statuses or ()))

        def _do(conn: Optional[sqlite3.Connection]) -> list[dict[str, Any]]:
            if conn is None or not sid:
                return []
            if wanted:
                marks = ",".join("?" for _ in wanted)
                rows = conn.execute(
                    f"SELECT * FROM background_tasks WHERE session_id = ? "
                    f"AND status IN ({marks}) ORDER BY created_at, task_id LIMIT ?",
                    (sid, *wanted, max(1, int(limit))),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM background_tasks WHERE session_id = ? "
                    "ORDER BY created_at, task_id LIMIT ?",
                    (sid, max(1, int(limit))),
                ).fetchall()
            return [_row_to_dict(r) for r in rows]

        return self._read(_do)

    def open_background_count(self, session_id: str) -> int:
        """Open background tasks of a session (received/queued/working/blocked)."""
        return len(self.background_tasks(session_id, BG_OPEN_STATUSES, limit=10000))

    def open_background_with_resource(self, session_id: str, resource: str,
                                      exclude_task_id: str = "") -> list[dict[str, Any]]:
        """Open tasks of a session on the same resource (serialization).

        Used to link a new card as a child (``parents``) of the open one
        instead of racing it on the same workspace/project.
        """
        res = str(resource or "").strip()
        if not res:
            return []
        rows = self.background_tasks(session_id, BG_OPEN_STATUSES, limit=10000)
        return [r for r in rows
                if str(r.get("resource") or "") == res
                and str(r.get("task_id") or "") != str(exclude_task_id or "")]

    def update_background_task(self, task_id: str, status: str, *,
                               run_id: Optional[int] = None,
                               delivery_id: Optional[int] = None,
                               wait_reason: Optional[str] = None,
                               now: Optional[float] = None) -> Optional[dict[str, Any]]:
        """Move a task to ``status`` under the guarded transition table.

        Returns the updated row, or ``None`` when the task is unknown or
        the transition is illegal (a terminal state never regresses; a
        same-status update is a no-op success). ``delivery_id`` is only
        stored when given: ``ready`` without a persisted delivery is
        refused by the caller, never by silently writing ``ready``.
        """
        tid = str(task_id or "").strip()
        target = str(status or "").strip()
        if not tid or target not in BG_STATUSES:
            return None
        ts = time.time() if now is None else float(now)

        def _do(conn: sqlite3.Connection) -> Optional[dict[str, Any]]:
            row = conn.execute(
                "SELECT * FROM background_tasks WHERE task_id = ? LIMIT 1",
                (tid,),
            ).fetchone()
            if row is None:
                return None
            current = str(row["status"])
            if current == target:
                # Same-status is an idempotent no-op — unless the caller
                # tries to attach a DIFFERENT delivery/run id (a late
                # duplicate event): the first delivery wins, the late
                # one is refused instead of overwriting it.
                if delivery_id is not None and row["delivery_id"] is not None \
                        and int(delivery_id) != int(row["delivery_id"]):
                    return None
                if run_id is not None and row["run_id"] is not None \
                        and int(run_id) != int(row["run_id"]):
                    return None
                return _row_to_dict(row)
            if target not in _BG_TRANSITIONS.get(current, set()):
                return None
            sets = ["status = ?", "updated_at = ?"]
            params: list[Any] = [target, ts]
            if run_id is not None:
                sets.append("run_id = ?")
                params.append(int(run_id))
            if delivery_id is not None:
                sets.append("delivery_id = ?")
                params.append(int(delivery_id))
            if wait_reason is not None:
                sets.append("wait_reason = ?")
                params.append(str(wait_reason or ""))
            params.append(tid)
            conn.execute(
                f"UPDATE background_tasks SET {', '.join(sets)} WHERE task_id = ?",
                params,
            )
            updated = conn.execute(
                "SELECT * FROM background_tasks WHERE task_id = ? LIMIT 1",
                (tid,),
            ).fetchone()
            return _row_to_dict(updated) if updated else None

        return self._write(_do)

    def cancel_background_task(self, task_id: str, session_id: str = "") -> bool:
        """Cancel one task (plugin-side). Only a non-terminal task of the
        given session (``""`` matches any session) moves to ``cancelled``.
        Returns True when it changed."""
        tid = str(task_id or "").strip()
        sid = str(session_id or "")
        ts = time.time()

        def _do(conn: sqlite3.Connection) -> bool:
            row = conn.execute(
                "SELECT session_id, status FROM background_tasks "
                "WHERE task_id = ? LIMIT 1",
                (tid,),
            ).fetchone()
            if row is None:
                return False
            if sid and str(row["session_id"] or "") != sid:
                return False
            if str(row["status"]) in BG_TERMINAL_STATUSES:
                return False
            cur = conn.execute(
                "UPDATE background_tasks SET status = 'cancelled', updated_at = ? "
                "WHERE task_id = ?",
                (ts, tid),
            )
            return cur.rowcount == 1

        return self._write(_do)

    def cancel_session_tasks(self, session_id: str) -> list[str]:
        """Cancel every open background task of a session. Returns the ids."""
        sid = str(session_id or "")
        ts = time.time()

        def _do(conn: sqlite3.Connection) -> list[str]:
            rows = conn.execute(
                "SELECT task_id FROM background_tasks WHERE session_id = ? "
                "AND status IN ('received', 'queued', 'working', 'blocked')",
                (sid,),
            ).fetchall()
            ids = [str(r["task_id"]) for r in rows]
            if ids:
                conn.execute(
                    "UPDATE background_tasks SET status = 'cancelled', "
                    "updated_at = ? WHERE session_id = ? "
                    "AND status IN ('received', 'queued', 'working', 'blocked')",
                    (ts, sid),
                )
            return ids

        return self._write(_do)

    # ------------------------------------------------------- Fase 3: stop-all
    def set_stop_all(self, session_id: str, reason: str = "",
                     now: Optional[float] = None) -> None:
        """Flag a session as stopped: no new background work is accepted."""
        sid = str(session_id or "").strip()
        if not sid:
            return
        ts = time.time() if now is None else float(now)

        def _do(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO session_flags (session_id, stop_all, stopped_at, reason) "
                "VALUES (?, 1, ?, ?) ON CONFLICT(session_id) DO UPDATE SET "
                "stop_all = 1, stopped_at = excluded.stopped_at, "
                "reason = excluded.reason",
                (sid, ts, str(reason or "")),
            )

        self._write(_do)

    def clear_stop_all(self, session_id: str) -> None:
        """Clear a session's stop-all flag (the user confirmed a resume)."""
        sid = str(session_id or "").strip()
        if not sid:
            return

        def _do(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE session_flags SET stop_all = 0 WHERE session_id = ?",
                (sid,),
            )

        self._write(_do)

    def stop_all_active(self, session_id: str) -> bool:
        """True when the session's stop-all flag is set."""
        sid = str(session_id or "").strip()

        def _do(conn: Optional[sqlite3.Connection]) -> bool:
            if conn is None or not sid:
                return False
            row = conn.execute(
                "SELECT stop_all FROM session_flags WHERE session_id = ? LIMIT 1",
                (sid,),
            ).fetchone()
            return bool(row and row["stop_all"])

        return self._read(_do)

    # ------------------------------------------- Fase 3: cancel/resume markers
    def record_cancel_request(self, task_id: str, session_id: str = "",
                              now: Optional[float] = None) -> None:
        """Mark that the chat blocked this card (a user cancel request)."""
        tid = str(task_id or "").strip()
        if not tid:
            return
        ts = time.time() if now is None else float(now)

        def _do(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO cancel_requests (task_id, session_id, requested_at) "
                "VALUES (?, ?, ?) ON CONFLICT(task_id) DO UPDATE SET "
                "session_id = excluded.session_id, requested_at = excluded.requested_at",
                (tid, str(session_id or ""), ts),
            )

        self._write(_do)

    def consume_cancel_request(self, task_id: str,
                               now: Optional[float] = None) -> Optional[dict[str, Any]]:
        """Take (and delete) a cancel marker, or ``None`` when there is
        none — or when the only one is stale (older than
        ``CANCEL_REQUEST_TTL_SECONDS``). A stale marker never relabels a
        later worker-initiated block as a cancel."""
        tid = str(task_id or "").strip()
        ts = time.time() if now is None else float(now)
        if not tid:
            return None

        def _do(conn: sqlite3.Connection) -> Optional[dict[str, Any]]:
            row = conn.execute(
                "SELECT * FROM cancel_requests WHERE task_id = ? LIMIT 1",
                (tid,),
            ).fetchone()
            if row is None:
                return None
            data = _row_to_dict(row)
            conn.execute("DELETE FROM cancel_requests WHERE task_id = ?", (tid,))
            if ts - float(data.get("requested_at") or 0) > CANCEL_REQUEST_TTL_SECONDS:
                return None
            return data

        return self._write(_do)

    def offer_resume(self, session_id: str, idempotency_key: str, turn_id: str = "",
                     now: Optional[float] = None) -> None:
        """Record that a stop-all block offered a resume for this request."""
        sid = str(session_id or "").strip()
        key = str(idempotency_key or "").strip()
        if not sid or not key:
            return
        ts = time.time() if now is None else float(now)

        def _do(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO resume_offers (session_id, idempotency_key, turn_id, offered_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(session_id, idempotency_key) "
                "DO UPDATE SET turn_id = excluded.turn_id, offered_at = excluded.offered_at",
                (sid, key, str(turn_id or ""), ts),
            )

        self._write(_do)

    def consume_resume_offer(self, session_id: str, idempotency_key: str,
                             turn_id: str = "", now: Optional[float] = None) -> bool:
        """True when a resume was offered for this request in an EARLIER
        turn: the user confirmed by retrying. Consumes the offer either
        way when it exists (a same-turn retry does NOT consume it — the
        user has not answered yet — it only returns False)."""
        sid = str(session_id or "").strip()
        key = str(idempotency_key or "").strip()
        turn = str(turn_id or "")
        ts = time.time() if now is None else float(now)
        if not sid or not key:
            return False

        def _do(conn: sqlite3.Connection) -> bool:
            row = conn.execute(
                "SELECT * FROM resume_offers WHERE session_id = ? "
                "AND idempotency_key = ? LIMIT 1",
                (sid, key),
            ).fetchone()
            if row is None:
                return False
            data = _row_to_dict(row)
            if ts - float(data.get("offered_at") or 0) > RESUME_OFFER_TTL_SECONDS:
                conn.execute(
                    "DELETE FROM resume_offers WHERE session_id = ? "
                    "AND idempotency_key = ?",
                    (sid, key),
                )
                return False
            offered_turn = str(data.get("turn_id") or "")
            if not turn or turn == offered_turn:
                return False
            conn.execute(
                "DELETE FROM resume_offers WHERE session_id = ? "
                "AND idempotency_key = ?",
                (sid, key),
            )
            return True

        return self._write(_do)

    def lookup_create(self, task_id: str) -> Optional[dict[str, Any]]:
        """The ``creates`` mapping row for a task (session/title/assignee)."""

        def _do(conn: Optional[sqlite3.Connection]) -> Optional[dict[str, Any]]:
            if conn is None:
                return None
            row = conn.execute(
                "SELECT * FROM creates WHERE task_id = ? LIMIT 1",
                (str(task_id or "").strip(),),
            ).fetchone()
            return _row_to_dict(row) if row else None

        return self._read(_do)

    # ------------------------------------------- Fase 3b: tareas programadas
    def register_scheduled_task(self, *, job_id: str, session_id: str = "",
                                name: str = "", prompt: str = "",
                                schedule_display: str = "",
                                schedule_raw: str = "",
                                consent_source: str = "user_request",
                                now: Optional[float] = None) -> Optional[dict[str, Any]]:
        """Register a core cron job created from a Bot Chat session.

        First write wins on ``job_id`` (a re-observed ``cronjob create``
        for the same job returns the existing row instead of a second
        one). ``consent_source`` is the evidence of the user's explicit
        request; without this row the observer never delivers a fire.
        Returns the row, or ``None`` when the job id is empty.
        """
        jid = str(job_id or "").strip()
        if not jid:
            return None
        ts = time.time() if now is None else float(now)

        def _do(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO scheduled_tasks (job_id, session_id, name, "
                "prompt, schedule_display, schedule_raw, status, "
                "consent_at, consent_source, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?) "
                "ON CONFLICT(job_id) DO NOTHING",
                (jid, str(session_id or ""), str(name or "")[:240],
                 str(prompt or "")[:1000], str(schedule_display or "")[:240],
                 str(schedule_raw or "")[:240], ts,
                 str(consent_source or ""), ts, ts),
            )

        self._write(_do)
        return self.scheduled_task(jid)

    def scheduled_task(self, job_id: str) -> Optional[dict[str, Any]]:
        """One scheduled-task row, or ``None`` when unknown."""
        jid = str(job_id or "").strip()

        def _do(conn: Optional[sqlite3.Connection]) -> Optional[dict[str, Any]]:
            if conn is None or not jid:
                return None
            row = conn.execute(
                "SELECT * FROM scheduled_tasks WHERE job_id = ? LIMIT 1",
                (jid,),
            ).fetchone()
            return _row_to_dict(row) if row else None

        return self._read(_do)

    def scheduled_tasks(self, session_id: str, statuses: Any = (),
                        limit: int = 200) -> list[dict[str, Any]]:
        """Scheduled tasks of a session, optionally filtered by status.

        Oldest first (creation order). ``statuses=()`` returns every
        non-``removed`` row.
        """
        sid = str(session_id or "").strip()
        wanted = tuple(str(s) for s in (statuses or ()))

        def _do(conn: Optional[sqlite3.Connection]) -> list[dict[str, Any]]:
            if conn is None or not sid:
                return []
            if wanted:
                marks = ",".join("?" for _ in wanted)
                rows = conn.execute(
                    f"SELECT * FROM scheduled_tasks WHERE session_id = ? "
                    f"AND status IN ({marks}) ORDER BY created_at, job_id LIMIT ?",
                    (sid, *wanted, max(1, int(limit))),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM scheduled_tasks WHERE session_id = ? "
                    "AND status != 'removed' ORDER BY created_at, job_id LIMIT ?",
                    (sid, max(1, int(limit))),
                ).fetchall()
            return [_row_to_dict(r) for r in rows]

        return self._read(_do)

    def set_scheduled_task_status(self, job_id: str, status: str,
                                  paused_reason: str = "",
                                  now: Optional[float] = None) -> Optional[dict[str, Any]]:
        """Move a scheduled task to ``status``.

        ``removed`` is terminal: a removed row never leaves that state
        (the core job is gone; a re-created job has a new id). Returns
        the updated row, or ``None`` when the job is unknown or the move
        is illegal.
        """
        jid = str(job_id or "").strip()
        target = str(status or "").strip()
        if not jid or target not in ("active", "paused", "paused_by_stop_all",
                                     "removed"):
            return None
        ts = time.time() if now is None else float(now)

        def _do(conn: sqlite3.Connection) -> Optional[dict[str, Any]]:
            row = conn.execute(
                "SELECT * FROM scheduled_tasks WHERE job_id = ? LIMIT 1",
                (jid,),
            ).fetchone()
            if row is None:
                return None
            if str(row["status"]) == "removed":
                return None
            conn.execute(
                "UPDATE scheduled_tasks SET status = ?, paused_reason = ?, "
                "updated_at = ? WHERE job_id = ?",
                (target, str(paused_reason or ""), ts, jid),
            )
            updated = conn.execute(
                "SELECT * FROM scheduled_tasks WHERE job_id = ? LIMIT 1",
                (jid,),
            ).fetchone()
            return _row_to_dict(updated) if updated else None

        return self._write(_do)

    def record_scheduled_fire(self, *, execution_id: str, job_id: str,
                              session_id: str = "", status: str = "delivered",
                              delivery_id: Optional[int] = None,
                              finished_at: str = "",
                              now: Optional[float] = None) -> Optional[dict[str, Any]]:
        """Record that an execution was accounted for (delivered/failed/
        silent). First write wins on ``execution_id``: a second record of
        the same execution returns the existing row, so observing twice
        can never deliver twice. Returns the row, or ``None`` when the
        execution id is empty.
        """
        eid = str(execution_id or "").strip()
        if not eid or str(status or "") not in ("delivered", "failed", "silent"):
            return None
        ts = time.time() if now is None else float(now)

        def _do(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO scheduled_fires (execution_id, job_id, "
                "session_id, status, delivery_id, finished_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(execution_id) DO NOTHING",
                (eid, str(job_id or ""), str(session_id or ""),
                 str(status), delivery_id,
                 str(finished_at or ""), ts),
            )

        self._write(_do)
        return self.scheduled_fire(eid)

    def scheduled_fire(self, execution_id: str) -> Optional[dict[str, Any]]:
        """One recorded fire, or ``None`` when the execution is unseen."""
        eid = str(execution_id or "").strip()

        def _do(conn: Optional[sqlite3.Connection]) -> Optional[dict[str, Any]]:
            if conn is None or not eid:
                return None
            row = conn.execute(
                "SELECT * FROM scheduled_fires WHERE execution_id = ? LIMIT 1",
                (eid,),
            ).fetchone()
            return _row_to_dict(row) if row else None

        return self._read(_do)
