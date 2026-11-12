"""Pending-asks tracking: user requests that never got a final answer.

Why: in a live Bot Chat test the user asked for a status check, then sent a
second message while the first one was still being worked on (the core
delivers it as an ``[OUT-OF-BAND USER MESSAGE ...]`` steer). The model said
"change of topic", answered only the second ask, and the first one was lost.

``pre_llm_call`` receives the turn's messages. This module finds earlier real
user requests that were followed by another user message (a steer or a new
message) with no final assistant reply in between, so the plugin can remind the
model once or twice, in the next turn, to close them. It is pure: no I/O, no
state beyond the small reminder counter below, and every failure returns [].
"""
from __future__ import annotations

import hashlib
import re
import threading
from typing import Any, Iterable

OOB_MARKER = "OUT-OF-BAND USER MESSAGE"
_OOB_RE = re.compile(
    r"\[OUT-OF-BAND USER MESSAGE[^\]]*\]\s*(.*?)\s*\[/OUT-OF-BAND USER MESSAGE\]",
    re.S)
SCAN_WINDOW = 80          # only the recent tail is scanned (cost bound)
MAX_ITEMS = 3
MAX_CHARS = 160
MAX_REMINDERS = 2         # one ask is surfaced at most this many times
MIN_ASK_CHARS = 8

_lock = threading.Lock()
_reminded: dict[str, int] = {}
_REMINDED_CAP = 2048


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                parts.append(str(part.get("text") or ""))
            elif isinstance(part, str):
                parts.append(part)
        return "".join(parts)
    return ""


def _is_oob(text: str) -> bool:
    return OOB_MARKER in text


def _is_final_reply(msg: dict) -> bool:
    return (str(msg.get("role") or "") == "assistant"
            and not msg.get("tool_calls")
            and bool(_text(msg.get("content")).strip()))


def find_dangling_asks(messages: Iterable[Any], *, max_items: int = MAX_ITEMS) -> list[str]:
    """Earlier real user asks with no final assistant reply before the next
    user message. The current turn's ask (last real user message) is never
    reported. Returns the ask texts, oldest first."""
    try:
        msgs = [m for m in list(messages or [])[-SCAN_WINDOW:] if isinstance(m, dict)]
    except Exception:
        return []
    # user messages with their positions: (index, is_oob, text)
    users: list[tuple[int, bool, str]] = []
    for i, m in enumerate(msgs):
        if str(m.get("role") or "") != "user":
            continue
        t = _text(m.get("content"))
        users.append((i, _is_oob(t), t))
    real = [u for u in users if not u[1]]
    if len(real) < 1 or len(users) < 2:
        return []
    last_real_idx = real[-1][0]
    found: list[str] = []
    for pos, (i, oob, t) in enumerate(users[:-1]):
        if oob or i == last_real_idx:
            continue
        ask = " ".join(t.split())
        if len(ask) < MIN_ASK_CHARS or ask.startswith("/"):
            continue
        nxt = users[pos + 1][0]
        segment = msgs[i + 1:nxt]
        if any(_is_final_reply(s) for s in segment):
            continue
        found.append(ask if len(ask) <= MAX_CHARS else ask[:MAX_CHARS - 1] + "…")
    return found[-max_items:]


def _key(session_id: str, ask: str) -> str:
    return hashlib.sha1(f"{session_id}\0{ask}".encode("utf-8", "replace")).hexdigest()


def pending_for_turn(session_id: str, messages: Iterable[Any]) -> list[str]:
    """Dangling asks that still have reminders left; counts this reminder."""
    asks = find_dangling_asks(messages)
    out: list[str] = []
    with _lock:
        for ask in asks:
            k = _key(session_id or "", ask)
            n = _reminded.get(k, 0)
            if n >= MAX_REMINDERS:
                continue
            _reminded[k] = n + 1
            out.append(ask)
        if len(_reminded) > _REMINDED_CAP:
            for k in list(_reminded)[: _REMINDED_CAP // 2]:
                _reminded.pop(k, None)
    return out


def reset() -> None:
    with _lock:
        _reminded.clear()


HEADER = {
    "en": ("[Pending asks] These earlier requests from the user never got a final "
           "answer (a newer message interrupted them). After the current ask, "
           "close each with one line of result, or ask if they still want it:"),
    "es": ("[Pendientes] Estos pedidos anteriores del usuario quedaron sin respuesta "
           "final (un mensaje nuevo los interrumpió). Tras lo actual, cierra cada uno "
           "con una línea de resultado, o pregunta si aún lo quiere:"),
}


def render(asks: list[str], lang: str = "en") -> str:
    if not asks:
        return ""
    head = HEADER.get(lang, HEADER["en"])
    return head + "\n" + "\n".join(f"- «{a}»" for a in asks)
