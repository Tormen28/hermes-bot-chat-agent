"""Background-task policy for Bot Chat (Fase 3, ruta A, v1.2).

Route A: the plugin coordinates and delivers; it never executes
background work and never launches workers or schedulers of its own.
A background job is a kanban card (``kanban_create`` to a teammate, or
to the user's own worker profile in individual mode — that profile
must be listed in ``team.assignees``). The core dispatcher runs it in
another process; the result returns through the Fase 1 delivery path.

This module is the policy over :mod:`delivery_store`'s
``background_tasks`` table: the state machine, the visible-queue
limit, the per-resource serialization, and the three cancellation
levels. It never touches the kanban board (read-only there) and never
writes ``state.db``/``kanban.db``.

State machine (rubric docs/fase3-rubric.md §1)::

    received -> queued -> working -> ready
       |           |           |-> blocked -> queued/working/ready
       |           |           |-> error / cancelled (terminal)
       +-----------------------> cancelled (terminal)

``ready`` is only reachable with a persisted delivery id (wow 4: no
``listo`` without a durable result). ``cancelled`` is plugin-side: the
core has no ``kanban_cancel`` and no ``cancelled`` status, so a
cancelled card's worker keeps running in the core until it finishes or
blocks; the plugin stops tracking it and never announces its result.
Its result stays in ``kanban.db`` (the core), it is not lost — Bot
Chat just does not announce it. Changing that is the B3 proposal to
the core (design only, not implemented).

Timeouts and rate limits are the core's (claim TTL, failure breaker,
respawn guard in the dispatcher). They surface here as
``kanban_task_blocked`` (the dispatcher auto-blocks a crashed,
timed-out or rate-limited card) → ``blocked`` with a delivery row.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

import delivery_store

logger = logging.getLogger(__name__)

MAX_PER_SESSION = 10
"""Visible-queue limit per session (plan §6): open background tasks."""

STATUSES = delivery_store.BG_STATUSES
TERMINAL_STATUSES = delivery_store.BG_TERMINAL_STATUSES
OPEN_STATUSES = delivery_store.BG_OPEN_STATUSES

STATUS_LABELS = {
    "en": {
        "received": "Received",
        "queued": "Queued",
        "working": "Working",
        "waiting": "Waiting",
        "blocked": "Blocked",
        "ready": "Ready",
        "error": "Error",
        "cancelled": "Cancelled",
    },
    "es": {
        "received": "Recibido",
        "queued": "En cola",
        "working": "Trabajando",
        "waiting": "Esperando",
        "blocked": "Bloqueado",
        "ready": "Listo",
        "error": "Error",
        "cancelled": "Cancelado",
    },
}


def label_for(status: str, lang: str = "en") -> str:
    """Display label for a task state (``waiting`` is derived, §1)."""
    table = STATUS_LABELS.get("es" if str(lang or "").lower().startswith("es") else "en",
                              STATUS_LABELS["en"])
    return table.get(str(status or ""), str(status or ""))


def resource_key(args: Any) -> str:
    """Serialization key for a ``kanban_create`` call.

    Same absolute ``workspace_path`` or same explicit ``project`` /
    ``project_id`` means the same resource: cards on it are serialized
    through ``parents`` instead of racing. Anything else (no explicit
    resource) means a fresh scratch workspace per card, which is the
    core default and needs no serialization.
    """
    data = args if isinstance(args, dict) else {}
    path = str(data.get("workspace_path") or "").strip()
    if path:
        # Same directory on disk, whatever the spelling.
        return "path:" + os.path.normpath(path)
    project = str(data.get("project") or data.get("project_id") or "").strip()
    if project:
        return "project:" + project
    return ""


def display_status(task: dict[str, Any], open_parent_ids: Any = ()) -> str:
    """Display state of a task row.

    ``waiting`` is derived, not stored: an open task (received/queued/
    working) whose parent is still open is waiting on its teammate, not
    running. Terminal and blocked states are never re-derived.
    """
    status = str(task.get("status") or "")
    if status in ("received", "queued", "working"):
        parents = {str(p) for p in (open_parent_ids or ())}
        if str(task.get("task_id") or "") in parents:
            return "waiting"
    return status


def launch(store: Any, *, task_id: str, session_id: str = "", project_id: str = "",
           title: str = "", assignee: str = "", resource: str = "") -> Optional[dict[str, Any]]:
    """Record a launched card (``received``). First write wins."""
    if store is None:
        return None
    return store.record_background_task(
        task_id=task_id, session_id=session_id, project_id=project_id,
        title=title, assignee=assignee, resource=resource)


def on_claimed(store: Any, task_id: str, run_id: Optional[int] = None) -> Optional[dict[str, Any]]:
    """``kanban_task_claimed`` → ``queued`` (dispatcher took the card)."""
    if store is None:
        return None
    return store.update_background_task(task_id, "queued", run_id=run_id)


def on_spawned(store: Any, task_id: str, run_id: Optional[int] = None) -> Optional[dict[str, Any]]:
    """``on_kanban_worker_spawned`` → ``working`` (worker process up)."""
    if store is None:
        return None
    return store.update_background_task(task_id, "working", run_id=run_id)


def on_completed(store: Any, task_id: str, delivery_id: Optional[int]) -> Optional[dict[str, Any]]:
    """``kanban_task_completed`` → ``ready``, but ONLY with a persisted
    delivery id. ``delivery_id=None`` (the record failed or was skipped)
    never reports ready: no false ``listo`` (wow 4)."""
    if store is None or delivery_id is None:
        return None
    return store.update_background_task(task_id, "ready", delivery_id=int(delivery_id))


def on_blocked(store: Any, task_id: str, *, cancelled: bool = False) -> Optional[dict[str, Any]]:
    """``kanban_task_blocked`` → ``blocked`` — or ``cancelled`` when the
    block came from the chat (a cancel marker was consumed)."""
    if store is None:
        return None
    return store.update_background_task(
        task_id, "cancelled" if cancelled else "blocked")


def on_dead_letter(store: Any, task_id: str) -> Optional[dict[str, Any]]:
    """A delivery that reached ``dead_letter`` → ``error`` (accounted)."""
    if store is None:
        return None
    return store.update_background_task(task_id, "error")


def cancel(store: Any, task_id: str, session_id: str = "") -> bool:
    """Cancel one task (plugin-side; the core worker keeps running)."""
    if store is None:
        return False
    return store.cancel_background_task(task_id, session_id=session_id)


def can_launch(store: Any, session_id: str, *, max_per_session: int = MAX_PER_SESSION) -> tuple[bool, str]:
    """Whether a new background task may be launched in this session.

    Returns ``(True, "")`` or ``(False, reason)`` where reason is
    ``"stop_all"`` or ``"queue_full"``. The store is the source of
    truth, so the check holds across processes.
    """
    if store is None:
        return True, ""
    if store.stop_all_active(session_id):
        return False, "stop_all"
    if store.open_background_count(session_id) >= max(0, int(max_per_session)):
        return False, "queue_full"
    return True, ""


def serialize_parents(store: Any, session_id: str, resource: str,
                      exclude_task_id: str = "") -> list[str]:
    """Open tasks of this session on the same resource, oldest first.

    The new card is created with these as ``parents`` so the core
    serializes it behind them instead of racing the same workspace.
    """
    if store is None:
        return []
    rows = store.open_background_with_resource(
        session_id, resource, exclude_task_id=exclude_task_id)
    return [str(r["task_id"]) for r in rows if str(r.get("task_id") or "")]


def stop_all(store: Any, session_id: str, reason: str = "",
             cron: Any = None) -> dict[str, Any]:
    """Stop everything for a session (level 3).

    Cancels every open task, pauses the session's scheduled tasks in the
    core (Fase 3b: the core has no pause-by-session, so each job is
    paused on its own), and sets the stop-all flag. Delivered rows are
    untouched, unconfirmed offers stay ``pending`` (they are re-offered
    next turn). Returns ``{"cancelled": [task ids], "paused_scheduled":
    [job ids]}``. Scheduled tasks stay paused until an explicit resume
    (clearing the flag alone never resumes them).
    """
    if store is None:
        return {"cancelled": [], "paused_scheduled": []}
    cancelled = store.cancel_session_tasks(session_id)
    paused: list[str] = []
    try:
        import scheduled_tasks as _sched
        paused = _sched.pause_for_stop_all(store, session_id, cron=cron)
    except Exception as exc:  # noqa: BLE001 - stop-all never fails on this
        logger.warning("stop-all scheduled pause skipped: %s", exc)
    store.set_stop_all(session_id, reason=reason)
    return {"cancelled": cancelled, "paused_scheduled": paused}


def resume(store: Any, session_id: str) -> None:
    """Clear a session's stop-all flag (the user confirmed a resume)."""
    if store is not None:
        store.clear_stop_all(session_id)
