"""Scheduled-task policy for Bot Chat (Fase 3b, v1.2 §3b, wow 6).

The core's cron schedules and fires; this module coordinates and delivers.
It never executes work, never launches workers or schedulers of its own,
and never writes the core's store — every core write goes through the
:mod:`cron_bridge` adapter (which calls the core's own functions).

Consent: a task only exists here because the user explicitly asked for it
in the chat (the agent created it with the core's ``cronjob`` tool, observed
in ``post_tool_call``). The registry row is the evidence; without it the
observer never delivers a fire, even when the core job exists.

Delivery: the observer (called from ``pre_llm_call``, before the offer)
reads terminal executions from the core's durable ledger plus the newest
saved output file, and records one outbox delivery per execution, to the
origin session, through the Fase 1 protocol. A fire is announced in the
next turn's note — never mid-turn, never by a timer.

Stop-all: ``pause_for_stop_all`` pauses each active job of the session in
the core (there is no pause-by-session in the core) and marks the rows
``paused_by_stop_all``. Resuming is explicit per task; clearing the
stop-all flag alone never resumes a scheduled task.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import cron_bridge

logger = logging.getLogger(__name__)

STATUSES = ("active", "paused", "paused_by_stop_all", "removed")
OPEN_STATUSES = ("active", "paused", "paused_by_stop_all")

STATUS_LABELS = {
    "en": {
        "active": "Scheduled",
        "paused": "Paused",
        "paused_by_stop_all": "Paused (stop-all)",
        "removed": "Removed",
    },
    "es": {
        "active": "Programada",
        "paused": "Pausada",
        "paused_by_stop_all": "Pausada (stop-all)",
        "removed": "Eliminada",
    },
}

# Event triggers are backlog in v1.2 (§3b): only schedule triggers exist.
TRIGGERS = ("schedule",)


def label_for(status: str, lang: str = "en") -> str:
    """Display label for a scheduled-task state."""
    table = STATUS_LABELS.get("es" if str(lang or "").lower().startswith("es") else "en",
                              STATUS_LABELS["en"])
    return table.get(str(status or ""), str(status or ""))


def delivery_task_id(job_id: str, execution_id: str) -> str:
    """Outbox task id for one fire: unique per execution, so a recurring
    job delivers each fire in its own row (and the outbox dedups by it)."""
    return f"cron:{job_id}:{execution_id}"


def _truncate(text: str, limit: int = cron_bridge.SUMMARY_MAX_CHARS) -> str:
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def register(store: Any, *, job_id: str, session_id: str = "",
             name: str = "", prompt: str = "",
             schedule_display: str = "", schedule_raw: str = "",
             consent_source: str = "user_request",
             trigger: str = "schedule",
             now: Optional[float] = None) -> Optional[dict[str, Any]]:
    """Register a job created from a Bot Chat session, with consent.

    ``trigger`` must be ``"schedule"``: event triggers (new message, board
    change) are backlog in v1.2 and are refused, not silently stored.
    Returns the row, or ``None`` when the job id is empty.
    """
    if str(trigger or "") != "schedule":
        raise ValueError(
            f"event triggers are backlog in v1.2, only 'schedule' is supported: "
            f"trigger={trigger!r}")
    if store is None:
        return None
    return store.register_scheduled_task(
        job_id=job_id, session_id=session_id, name=name, prompt=prompt,
        schedule_display=schedule_display, schedule_raw=schedule_raw,
        consent_source=consent_source, now=now)


def scheduled_task(store: Any, job_id: str) -> Optional[dict[str, Any]]:
    """One registered task, or ``None`` when unknown."""
    if store is None:
        return None
    return store.scheduled_task(job_id)


def scheduled_tasks(store: Any, session_id: str,
                    statuses: Any = ()) -> list[dict[str, Any]]:
    """Registered tasks of a session (non-``removed`` by default)."""
    if store is None:
        return []
    return store.scheduled_tasks(session_id, statuses)


def note_core_pause(store: Any, job_id: str, reason: str = "") -> Optional[dict[str, Any]]:
    """Mirror a ``cronjob pause`` observed in the chat: the row becomes
    ``paused`` (manual). A ``paused_by_stop_all`` row is NOT overwritten:
    the stop-all pause keeps its reason until an explicit resume."""
    if store is None:
        return None
    row = store.scheduled_task(job_id)
    if row is None:
        return None
    if str(row.get("status") or "") == "paused_by_stop_all":
        return row
    return store.set_scheduled_task_status(job_id, "paused",
                                           paused_reason=reason)


def note_core_resume(store: Any, job_id: str) -> Optional[dict[str, Any]]:
    """Mirror a ``cronjob resume`` observed in the chat.

    Returns ``(row, was_stop_all)``: ``was_stop_all`` is True when the row
    was ``paused_by_stop_all`` — the caller clears the stop-all flag then,
    because the explicit resume is the user's confirmation.
    """
    if store is None:
        return None
    row = store.scheduled_task(job_id)
    if row is None:
        return None
    was_stop_all = str(row.get("status") or "") == "paused_by_stop_all"
    updated = store.set_scheduled_task_status(job_id, "active")
    if updated is None:
        return None
    return updated, was_stop_all


def note_core_remove(store: Any, job_id: str) -> Optional[dict[str, Any]]:
    """Mirror a ``cronjob remove`` observed in the chat (terminal)."""
    if store is None:
        return None
    return store.set_scheduled_task_status(job_id, "removed")


def _deliver_fire(store: Any, *, row: dict[str, Any],
                  execution: dict[str, Any], adapter: Any,
                  now: Optional[float] = None) -> str:
    """Deliver one terminal execution to the origin session.

    Returns ``"delivered"`` / ``"failed"`` / ``"silent"``. The fire row is
    recorded FIRST (by execution id) so a crash after the outbox write
    can never deliver the same execution twice: the outbox ``record`` is
    idempotent on the same ``task_id`` anyway.
    """
    job_id = str(row.get("job_id") or "")
    session_id = str(row.get("session_id") or "")
    execution_id = str(execution.get("id") or "")
    status = str(execution.get("status") or "")
    name = str(row.get("name") or job_id)
    finished_at = str(execution.get("finished_at") or "")

    if status == "missed":
        # A skipped slot is not a run: no output, nothing to announce.
        # Recorded as silent so it is never re-examined.
        store.record_scheduled_fire(
            execution_id=execution_id, job_id=job_id, session_id=session_id,
            status="silent", delivery_id=None, finished_at=finished_at, now=now)
        return "silent"

    if status in ("failed", "cancelled"):
        error = str(execution.get("error") or "").strip()
        output = ""
        try:
            output = adapter.latest_output(job_id)
        except Exception as exc:  # noqa: BLE001 - the error text is enough
            logger.debug("scheduled: output read failed for %s: %s", job_id, exc)
        detail = _truncate(error or cron_bridge.extract_response(output)
                           or "sin detalle")
        summary = (f"La tarea programada «{name}» falló: {detail}"
                   if status == "failed" else
                   f"La tarea programada «{name}» se canceló: {detail}")
        delivery_id = store.record(
            kind="blocked", task_id=delivery_task_id(job_id, execution_id),
            session_id=session_id, summary=summary, now=now)
        store.record_scheduled_fire(
            execution_id=execution_id, job_id=job_id, session_id=session_id,
            status="failed", delivery_id=delivery_id,
            finished_at=finished_at, now=now)
        return "failed"

    # Completed: the result is the saved output (or the ledger error when
    # there is no output file at all — a run that died before saving).
    try:
        output = adapter.latest_output(job_id)
    except Exception as exc:  # noqa: BLE001 - fall back to the ledger
        logger.debug("scheduled: output read failed for %s: %s", job_id, exc)
        output = ""
    response = cron_bridge.extract_response(output)
    if cron_bridge.is_silent_response(response):
        store.record_scheduled_fire(
            execution_id=execution_id, job_id=job_id, session_id=session_id,
            status="silent", delivery_id=None, finished_at=finished_at, now=now)
        return "silent"
    summary = _truncate(response) or (
        f"La tarea programada «{name}» terminó sin salida guardada.")
    delivery_id = store.record(
        kind="completed", task_id=delivery_task_id(job_id, execution_id),
        session_id=session_id, summary=summary, now=now)
    store.record_scheduled_fire(
        execution_id=execution_id, job_id=job_id, session_id=session_id,
        status="delivered", delivery_id=delivery_id,
        finished_at=finished_at, now=now)
    return "delivered"


def observe_fires(store: Any, session_id: str, *, cron: Any = None,
                  now: Optional[float] = None) -> dict[str, Any]:
    """Observe the session's registered jobs for new terminal executions
    and deliver each unseen fire to the origin session via the outbox.

    Only durable core state is read (execution ledger + output files);
    nothing here schedules, waits or runs anything. Jobs without a
    registry row (no consent) are never looked at. Returns
    ``{"observed", "delivered", "failed", "silent", "errors"}``.
    """
    counts = {"observed": 0, "delivered": 0, "failed": 0, "silent": 0,
              "errors": []}
    if store is None or not str(session_id or "").strip():
        return counts
    adapter = cron if cron is not None else cron_bridge.default_adapter()
    try:
        rows = store.scheduled_tasks(session_id)
    except Exception as exc:  # noqa: BLE001 - observation never breaks a turn
        logger.warning("scheduled: registry read failed: %s", exc)
        counts["errors"].append(str(exc))
        return counts
    for row in rows:
        job_id = str(row.get("job_id") or "")
        if not job_id:
            continue
        try:
            executions = adapter.terminal_executions(job_id, limit=50)
        except Exception as exc:  # noqa: BLE001 - one job never blocks the rest
            logger.warning("scheduled: executions read failed for %s: %s",
                           job_id, exc)
            counts["errors"].append(f"{job_id}: {exc}")
            continue
        for execution in reversed(executions):  # oldest first
            execution_id = str(execution.get("id") or "")
            if not execution_id:
                continue
            try:
                if store.scheduled_fire(execution_id) is not None:
                    counts["observed"] += 1
                    continue
            except Exception as exc:  # noqa: BLE001 - fail-closed: skip it
                logger.warning("scheduled: fire check failed for %s: %s",
                               execution_id, exc)
                counts["errors"].append(f"{execution_id}: {exc}")
                continue
            try:
                outcome = _deliver_fire(store, row=row, execution=execution,
                                        adapter=adapter, now=now)
            except Exception as exc:  # noqa: BLE001 - one fire never blocks the rest
                logger.warning("scheduled: delivery failed for %s: %s",
                               execution_id, exc)
                counts["errors"].append(f"{execution_id}: {exc}")
                continue
            counts["observed"] += 1
            counts[outcome] += 1
    return counts


def pause_for_stop_all(store: Any, session_id: str, *,
                       cron: Any = None) -> list[str]:
    """Pause every active scheduled task of a session (stop-all).

    The core has no pause-by-session, so each job is paused on its own
    (``pause_job`` with reason ``"stop-all"``). A job whose core pause
    fails stays ``active`` in the registry and is reported in the log —
    never silently marked paused. Returns the paused job ids.
    """
    if store is None or not str(session_id or "").strip():
        return []
    adapter = cron if cron is not None else cron_bridge.default_adapter()
    paused: list[str] = []
    for row in store.scheduled_tasks(session_id, ("active",)):
        job_id = str(row.get("job_id") or "")
        if not job_id:
            continue
        try:
            adapter.pause_job(job_id, reason="stop-all")
        except Exception as exc:  # noqa: BLE001 - the core pause is the truth
            logger.warning("scheduled: stop-all pause failed for %s: %s",
                           job_id, exc)
            continue
        updated = store.set_scheduled_task_status(
            job_id, "paused_by_stop_all", paused_reason="stop-all")
        if updated is not None:
            paused.append(job_id)
    return paused


def resume_stopped(store: Any, session_id: str, *,
                   cron: Any = None) -> list[str]:
    """Resume the session's ``paused_by_stop_all`` tasks in the core.

    Explicit only: nothing calls this on its own (clearing the stop-all
    flag does not resume scheduled tasks). Returns the resumed job ids.
    """
    if store is None or not str(session_id or "").strip():
        return []
    adapter = cron if cron is not None else cron_bridge.default_adapter()
    resumed: list[str] = []
    for row in store.scheduled_tasks(session_id, ("paused_by_stop_all",)):
        job_id = str(row.get("job_id") or "")
        if not job_id:
            continue
        try:
            adapter.resume_job(job_id)
        except Exception as exc:  # noqa: BLE001 - the core resume is the truth
            logger.warning("scheduled: resume failed for %s: %s", job_id, exc)
            continue
        updated = store.set_scheduled_task_status(job_id, "active")
        if updated is not None:
            resumed.append(job_id)
    return resumed


def status_lines(store: Any, session_id: str, *, cron: Any = None,
                 lang: str = "en") -> list[dict[str, Any]]:
    """Visible state of a session's scheduled tasks for the turn note.

    Each item is ``{"id", "status", "title", "line"}``. The next run comes
    from the core job record when readable; when the core is unavailable
    the line shows the stored schedule text instead of failing. Terminal
    (``removed``) tasks are not listed: a removed task is announced
    through the delta ("Terminada").
    """
    if store is None or not str(session_id or "").strip():
        return []
    adapter = cron if cron is not None else cron_bridge.default_adapter()
    try:
        rows = store.scheduled_tasks(session_id)
    except Exception:
        return []
    es = str(lang or "").lower().startswith("es")
    out: list[dict[str, Any]] = []
    for row in rows:
        job_id = str(row.get("job_id") or "")
        if not job_id:
            continue
        status = str(row.get("status") or "")
        title = str(row.get("name") or job_id)[:80]
        schedule = str(row.get("schedule_display") or "")
        next_run = ""
        try:
            live = adapter.get_job(job_id)
            if live is not None:
                next_run = str(live.get("next_run_at") or "")
        except Exception:  # noqa: BLE001 - the stored text is enough
            next_run = ""
        if status == "active" and next_run:
            detail = f"próxima {next_run}" if es else f"next {next_run}"
        elif schedule:
            detail = schedule
        else:
            detail = ""
        label = label_for(status, lang)
        line = f"{label}: {job_id} {title}"
        if detail:
            line += f" — {detail}"
        out.append({"id": job_id, "status": status, "title": title,
                    "line": line})
    return out
