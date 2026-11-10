"""Compact live status line for Bot Chat (header + optional desktop bar).

Idle turns with nothing to report render an empty string so the style
budget (A0.8) stays intact. Fail-open: any error returns "".
"""
from __future__ import annotations

from typing import Any, Iterable

import background_tasks as _bgt

_HEAD = {"en": "[Status]", "es": "[Estado]"}


def _lang(lang: str) -> str:
    return "es" if str(lang or "").lower().startswith("es") else "en"


def _task_bit(task: dict[str, Any], lang: str) -> str:
    title = " ".join(str(task.get("title") or task.get("task_id") or "task").split())[:32]
    status = _bgt.label_for(str(task.get("status") or ""), lang)
    return f"{status}: {title}" if title else status


def _recalled_bit(items: Iterable[dict[str, Any]], lang: str) -> str:
    rows = [i for i in (items or []) if isinstance(i, dict)]
    if not rows:
        return ""
    titles = []
    for item in rows[:3]:
        title = " ".join(str(item.get("title") or item.get("text") or "").split())
        if title:
            titles.append(title[:32] + ("…" if len(title) > 32 else ""))
    n = len(rows)
    if lang == "es":
        head = "1 recordada" if n == 1 else f"{n} recordadas"
    else:
        head = "1 recalled" if n == 1 else f"{n} recalled"
    if titles:
        return f"{head} ({', '.join(titles)})"
    return head


def render(*, lang: str = "en", enabled: bool = True,
           tasks: Iterable[dict[str, Any]] | None = None,
           pending: Iterable[str] | None = None,
           recalled: Iterable[dict[str, Any]] | None = None,
           context_tokens: int = 0) -> str:
    """One compact header (at most a couple of lines). Empty if there is nothing to show."""
    try:
        if not enabled:
            return ""
        lang = _lang(lang)
        bits: list[str] = []
        task_list = [t for t in (tasks or []) if isinstance(t, dict)]
        if task_list:
            bits.extend(_task_bit(t, lang) for t in task_list[:3])
        pending_list = [p for p in (pending or []) if str(p).strip()]
        if pending_list:
            label = "Pendiente" if lang == "es" else "pending"
            if len(pending_list) == 1:
                bits.append(f"1 {label}")
            else:
                bits.append(f"{len(pending_list)} {label}s" if lang != "es"
                            else f"{len(pending_list)} pendientes")
        rec = _recalled_bit(recalled or [], lang)
        if rec:
            bits.append(rec)
        if int(context_tokens or 0) > 0 and bits:
            bits.append(f"{int(context_tokens)} tok")
        if not bits:
            return ""
        head = _HEAD[lang]
        # Prefer a single line; wrap only if it would be very long.
        body = " · ".join(bits)
        if len(body) <= 160:
            return f"{head} {body}"
        return f"{head}\n{body}"
    except Exception:
        return ""
