"""Durable destination for Bot Chat deliveries (Fase 1b, v1.2 §5).

The plugin can deliver a result two ways:

- ``offer`` (default, and the only path on the live core): the result is
  offered in the destination session's next turn (``pre_llm_call``) and
  confirmed after the turn generates. Nothing here is needed for it.
- ``b1`` / ``auto``: when the core exposes the B1 surface
  (``ctx.record_delegation_delivery`` /
  ``ctx.record_completion_note``, capability ``state.detached_result``,
  core branch ``core-repairs/plugin-surface``, commit ``2ebbb3d5d``), the
  relay writes the result as a durable row in the destination session's
  transcript, so it survives even if no turn ever runs again.

Detection is by runtime capability (``hasattr`` on the plugin ``ctx``),
never by version string: the plugin works against the live core (no B1,
offer path) and against a core with B1, with no code change. The default
mode is ``offer`` — the live core has no B1 — and ``auto``/``b1`` only
take effect when B1 is actually present. A B1 write that cannot happen
(capability denied, live turn holding the lease, closed session) never
loses the delivery: the row stays in the outbox and the offer path still
delivers it in-turn. B1 is a durable extra, never a replacement for the
offer guarantee.

Idempotency: both B1 methods take a stable id. The relay uses
``bot-chat:<task_id>:<kind>`` as ``delegation_id`` / ``admission_id``, so
a retry after a crash (write done, confirm lost) returns the SAME message
id instead of a second transcript row (at-least-once write, exactly-once
visible effect).

Kind mapping: ``completed`` -> ``record_delegation_delivery`` (a delegation
result); ``blocked`` -> ``record_completion_note`` (a completion note about
a blocked card). The summary is worker content: it goes in as data, never
as an instruction (same A0.7 framing as the offer path).
"""
from __future__ import annotations

from typing import Any, Optional

try:  # A0.7: worker text is data; neutralise instruction-like spans before it reaches the model
    from untrusted import neutralize as _neutralize
except Exception:  # pragma: no cover - sibling module missing: keep a conservative fallback
    def _neutralize(value, max_chars=600):
        text = " ".join(str(value or "").split())
        return text[:max_chars]

STATE_DETACHED_RESULT_CAPABILITY = "state.detached_result"
_B1_METHODS = ("record_delegation_delivery", "record_completion_note")
_MODES = ("offer", "auto", "b1")


def has_b1(ctx: Any) -> bool:
    """True when ``ctx`` exposes the B1 detached-result surface."""
    if ctx is None:
        return False
    try:
        return all(callable(getattr(ctx, name, None)) for name in _B1_METHODS)
    except Exception:  # a ctx whose attribute access raises is "no B1", never a crash
        return False


def normalize_mode(raw: Any) -> str:
    """Config value -> one of ``offer`` / ``auto`` / ``b1`` (default ``offer``)."""
    mode = str(raw or "offer").strip().lower()
    return mode if mode in _MODES else "offer"


def stable_id_for(task_id: str, kind: str) -> str:
    """Stable idempotency id for one delivery (survives relay retries)."""
    return f"bot-chat:{str(task_id or '').strip()}:{str(kind or 'completed').strip()}"


def content_for(row: dict[str, Any]) -> str:
    """Transcript text for a delivery row (data, never an instruction)."""
    kind = str(row.get("kind") or "completed")
    verb = "Blocked" if kind == "blocked" else "Delivered"
    summary = _neutralize(row.get("summary"))
    line = f"[Bot Chat] {verb}: {row.get('task_id')} by {row.get('assignee') or 'teammate'}"
    if summary:
        line += f" — «{summary}»"
    return line


def _classify_error(exc: BaseException) -> str:
    """B1 failure -> a short reason the relay can act on."""
    name = type(exc).__name__
    text = f"{name}: {exc}".lower()
    if "sessionturnleaselost" in name.lower() or "live turn" in text or "turn lease" in text:
        return "lease_lost"
    if "compressionsessionclosed" in name.lower() or "closed parent" in text:
        return "session_closed"
    if isinstance(exc, ValueError):
        return "bad_request"
    return "error"


class DurableDestination:
    """One destination behind a switchable interface (Fase 1b).

    ``mode`` is ``offer`` (never call B1), ``auto`` (B1 when detected,
    else offer) or ``b1`` (B1 when detected; offer fallback when it is
    not — a missing B1 never loses a delivery, it only skips the durable
    write). :meth:`deliver` returns a dict; it never raises for a B1
    outcome (capability denial, lease lost, core guard): those are
    reported so the relay can keep the row pending.
    """

    def __init__(self, ctx: Any = None, mode: str = "offer"):
        self.ctx = ctx
        self.mode = normalize_mode(mode)

    @property
    def available(self) -> bool:
        """B1 is usable right now (mode wants it AND ctx exposes it)."""
        return self.mode in ("auto", "b1") and has_b1(self.ctx)

    @property
    def active_path(self) -> str:
        """Which path is actually delivering: ``"b1"`` or ``"offer"``."""
        return "b1" if self.available else "offer"

    def deliver(self, row: dict[str, Any]) -> dict[str, Any]:
        """Write one delivery row durably through B1.

        Returns ``{"ok": True, "message_id": int, "method": ...}`` on
        success; ``{"ok": False, "reason": ...}`` otherwise. ``reason``
        is ``"b1_unavailable"`` (mode is offer, or ctx has no B1),
        ``"capability_not_granted"`` (B1 returned ``None``),
        ``"lease_lost"`` (destination turn live — retry later),
        ``"session_closed"`` (destination gone — do not retry the same
        id) or ``"bad_request"`` / ``"error"`` (see :func:`_classify_error`).
        """
        if not self.available:
            return {"ok": False, "reason": "b1_unavailable",
                    "message_id": None, "method": None}
        task_id = str(row.get("task_id") or "").strip()
        kind = str(row.get("kind") or "completed")
        session_id = str(row.get("session_id") or "").strip()
        if not task_id or not session_id:
            return {"ok": False, "reason": "bad_request",
                    "message_id": None, "method": None}
        content = content_for(row)
        stable = stable_id_for(task_id, kind)
        try:
            if kind == "blocked":
                method = "record_completion_note"
                message_id = self.ctx.record_completion_note(
                    session_id, content, admission_id=stable,
                    metadata={"bot_chat_task_id": task_id, "bot_chat_kind": kind},
                )
            else:
                method = "record_delegation_delivery"
                message_id = self.ctx.record_delegation_delivery(
                    session_id, content, delegation_id=stable,
                    delivery_notice=f"Bot Chat delivery for {task_id}",
                    metadata={"bot_chat_task_id": task_id, "bot_chat_kind": kind},
                )
        except Exception as exc:  # B1 guards are documented exceptions
            return {"ok": False, "reason": _classify_error(exc),
                    "message_id": None, "method": None, "error": str(exc)}
        if message_id is None:
            return {"ok": False, "reason": "capability_not_granted",
                    "message_id": None, "method": method}
        return {"ok": True, "reason": "", "message_id": int(message_id),
                "method": method}
