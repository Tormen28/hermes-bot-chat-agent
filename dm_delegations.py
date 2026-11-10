"""Delegations made with the core ``message_agent`` tool, for the status card.

``message_agent`` is fire-and-forget: it returns a queued ack at once and the teammate's reply
comes back later as a background-process completion notice. Both are real signals the plugin can
see on v0.23.0 / v0.23.1 without any core change:

* ``post_tool_call(message_agent)``: the ack. ``status: queued`` + ``delivery_id`` → the card says
  "<me> → <teammate>: waiting for reply". An error result → "error". A call that returned nothing
  usable records nothing.
* the completion notice, seen as the next ``user_message`` of a turn
  (``[IMPORTANT: Background process ... completed ...]`` / the concise "finished" line). It marks
  a delegation done ONLY when the text names that teammate and carries success evidence
  (``exit_code=0``, or a "finished/completed" line with no failure/exit-code marker). A failure
  marker → "error". Anything ambiguous leaves the delegation waiting: never "done" by timer or guess.

State is a small JSON file next to ``status.json``; pure functions here, hooks in ``__init__``.
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Optional

FILENAME = "dm-delegations.json"
MAX_ITEMS = 40
KEEP_SECONDS = 24 * 3600

_NOTICE_HEAD = re.compile(r"\[IMPORTANT:\s*(?:\d+\s+)?background process", re.I)
_FINISHED = re.compile(r"(task finished|finished|completed|exited with code 0|tarea (?:terminada|finalizada)|termin[oó])", re.I)
_FAILED = re.compile(r"(task failed|failed|fall[oó]|exit[_ ]code\s*[=:]?\s*[1-9]|exited with code [1-9]|timed out|error)", re.I)
_EXIT0 = re.compile(r"exit[_ ]code\s*[=:]?\s*0\b", re.I)


def path_for(home: Path | None = None) -> Path:
    import status_panel
    return status_panel._home(home) / "bot-chat-agent" / FILENAME


def _load(path: Path | None = None) -> list[dict[str, Any]]:
    try:
        data = json.loads((path or path_for()).read_text(encoding="utf-8"))
        return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []
    except Exception:
        return []


def _save(items: list[dict[str, Any]], path: Path | None = None) -> bool:
    try:
        dest = path or path_for()
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(dest.parent / f".dm-{os.getpid()}-{uuid.uuid4().hex[:8]}.json")
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(items[-MAX_ITEMS:], ensure_ascii=False, indent=2))
        os.replace(tmp, dest)
        return True
    except Exception:
        return False


def _parse(result: Any) -> Optional[dict[str, Any]]:
    if isinstance(result, dict):
        return result
    if isinstance(result, str):
        try:
            data = json.loads(result)
            return data if isinstance(data, dict) else None
        except ValueError:
            return None
    return None


def _target_name(args: Any, data: dict[str, Any]) -> str:
    a = args if isinstance(args, dict) else {}
    raw = a.get("target") or a.get("to") or data.get("to") or ""
    if isinstance(raw, (list, tuple)):
        raw = raw[0] if raw else ""
    return str(raw or "").strip().lstrip("@").split(" on ")[0].strip()


def record_call(args: Any, result: Any, *, session_id: str = "", origin: str = "",
                now: float | None = None, path: Path | None = None) -> Optional[dict[str, Any]]:
    """Register the ack (or the refusal) of one ``message_agent`` call. None when unusable."""
    data = _parse(result)
    if data is None:
        return None
    t = float(now if now is not None else time.time())
    a = args if isinstance(args, dict) else {}
    to = _target_name(a, data)
    if not to:
        return None
    title = " ".join(str(a.get("message") or a.get("content") or "").split())[:80]
    if data.get("error"):
        state, did = "error", f"err-{uuid.uuid4().hex[:8]}"
    elif str(data.get("status") or "").lower() in ("queued", "delivered", "accepted") and data.get("delivery_id"):
        state, did = "queued", str(data["delivery_id"])
    else:
        return None
    items = _load(path)
    if any(i.get("id") == did for i in items):
        return None
    item = {"id": did, "session_id": session_id or "", "from": origin or "", "to": to, "title": title,
            "state": state, "created_at": t, "updated_at": t, "evidence": "ack" if state == "queued" else str(data.get("error"))[:120]}
    items.append(item)
    _save(items, path)
    return item


def is_completion_notice(text: Any) -> bool:
    return isinstance(text, str) and bool(_NOTICE_HEAD.search(text[:400]) or
                                           (_FINISHED.search(text[:300]) and "`" in text[:300]))


def apply_completion(text: Any, *, now: float | None = None, path: Path | None = None) -> list[dict[str, Any]]:
    """Mark open delegations done/failed when ``text`` is a completion notice naming them."""
    if not isinstance(text, str) or not text.strip():
        return []
    if not is_completion_notice(text):
        return []
    t = float(now if now is not None else time.time())
    items = _load(path)
    low = text.lower()
    changed: list[dict[str, Any]] = []
    for it in items:
        if it.get("state") not in ("queued", "waiting", "working"):
            continue
        name = str(it.get("to") or "").lower()
        if not name or not re.search(r"(?<![a-z0-9_-])@?" + re.escape(name) + r"(?![a-z0-9_-])", low):
            continue
        failed = bool(_FAILED.search(text)) and not _EXIT0.search(text)
        ok = bool(_EXIT0.search(text)) or (bool(_FINISHED.search(text)) and not _FAILED.search(text))
        if failed:
            it["state"], it["evidence"] = "error", "completion notice reported a failure"
        elif ok:
            it["state"], it["evidence"] = "ready", "completion notice: finished"
        else:
            continue
        it["updated_at"] = t
        changed.append(it)
    if changed:
        _save(items, path)
    return changed


def has_call_to(name: str, since: float = 0.0, *, path: Path | None = None) -> bool:
    """True when a ``message_agent`` call to ``name`` was recorded at or after ``since``."""
    want = str(name or "").lower()
    return any(str(i.get("to") or "").lower() == want and float(i.get("created_at") or 0) >= float(since or 0)
               for i in _load(path))


def as_status_tasks(session_id: str = "", *, now: float | None = None, path: Path | None = None) -> list[dict[str, Any]]:
    """Rows shaped like the plugin's background tasks, for ``status_panel.snapshot``."""
    t = float(now if now is not None else time.time())
    out = []
    for it in _load(path):
        if t - float(it.get("updated_at") or 0) > KEEP_SECONDS:
            continue
        if session_id and it.get("session_id") and it["session_id"] != session_id:
            continue
        out.append({"task_id": f"dm:{it['id']}", "title": it.get("title") or f"→ {it.get('to')}",
                    "status": {"queued": "queued", "ready": "ready", "error": "error"}.get(it["state"], "queued"),
                    "assignee": it.get("to"), "session_id": it.get("session_id") or "",
                    "created_at": it.get("created_at"), "updated_at": it.get("updated_at"),
                    "source": "message_agent", "evidence": it.get("evidence")})
    return out
