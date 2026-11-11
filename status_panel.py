"""Compact live status line for Bot Chat (header + optional desktop bar).

Idle turns with nothing to report render an empty string so the style
budget (A0.8) stays intact. Fail-open: any error returns "".
"""
from __future__ import annotations

from typing import Any, Iterable

import background_tasks as _bgt

_HEAD = {"en": "[Status]", "es": "[Estado]"}


def _lang(lang: str) -> str:
    """Model-facing text exists in es / en only."""
    return "es" if str(lang or "").lower().startswith("es") else "en"


_USER_LANGS = ("en", "es", "fr", "pt", "de")


def _ulang(lang: str) -> str:
    """Card text (what the user reads) exists in en / es / fr / pt / de."""
    code = str(lang or "").lower()[:2]
    return code if code in _USER_LANGS else "en"


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


import json
import os
import uuid
import time
from pathlib import Path

STATUS_FILENAME = "status.json"
DEFAULT_STALLED_AFTER = 120
_GENERIC_TITLES = {"bot chat", "bot-chat", ""}

_DELEGATION_STATE = {
    "en": {
        "stopped": "stopped",
        "received": "waiting for reply",
        "queued": "waiting for reply",
        "waiting": "waiting for reply",
        "working": "working",
        "blocked": "blocked",
        "ready": "done",
        "error": "error",
        "cancelled": "cancelled",
    },
    "es": {
        "stopped": "detenida",
        "received": "esperando respuesta",
        "queued": "esperando respuesta",
        "waiting": "esperando respuesta",
        "blocked": "bloqueado",
        "working": "trabajando",
        "ready": "listo",
        "error": "error",
        "cancelled": "cancelado",
    },
    "fr": {
        "stopped": "arrêtée",
        "received": "en attente de réponse", "queued": "en attente de réponse",
        "waiting": "en attente de réponse", "blocked": "bloqué", "working": "en cours",
        "ready": "terminé", "error": "erreur", "cancelled": "annulé",
    },
    "pt": {
        "stopped": "interrompida",
        "received": "aguardando resposta", "queued": "aguardando resposta",
        "waiting": "aguardando resposta", "blocked": "bloqueado", "working": "trabalhando",
        "ready": "pronto", "error": "erro", "cancelled": "cancelado",
    },
    "de": {
        "stopped": "gestoppt",
        "received": "wartet auf Antwort", "queued": "wartet auf Antwort",
        "waiting": "wartet auf Antwort", "blocked": "blockiert", "working": "arbeitet",
        "ready": "fertig", "error": "Fehler", "cancelled": "abgebrochen",
    },
}
_STOPPED = {"en": "stopped", "es": "detenida", "fr": "arrêtée", "pt": "interrompida", "de": "gestoppt"}

# Plain-language reason + what to do, shown under a delegation the core's loop guard refused.
_REASON_NOTE = {
    "hop_limit": {
        "en": "this chat reached the limit of {n} bot-to-bot messages, so nothing more was sent. Ask me to answer directly, or send a new message to continue.",
        "es": "este chat llegó al límite de {n} mensajes entre bots, así que no se envió nada más. Pídeme que responda directamente o envía un mensaje nuevo para continuar.",
        "fr": "ce chat a atteint la limite de {n} messages entre bots, donc rien d'autre n'a été envoyé. Demande-moi de répondre directement, ou envoie un nouveau message pour continuer.",
        "pt": "este chat atingiu o limite de {n} mensagens entre bots, então nada mais foi enviado. Peça para eu responder diretamente ou envie uma nova mensagem para continuar.",
        "de": "dieser Chat hat das Limit von {n} Bot-zu-Bot-Nachrichten erreicht, daher wurde nichts mehr gesendet. Bitte mich, direkt zu antworten, oder schick eine neue Nachricht, um weiterzumachen.",
    },
    "no_reply_needed": {
        "en": "that bot's reply needed no answer, so the exchange is over. Nothing to do; ask me for the result.",
        "es": "la respuesta de ese bot no necesitaba contestación, así que el intercambio terminó. No hay nada que hacer; pídeme el resultado.",
        "fr": "la réponse de ce bot n'appelait pas de réponse, donc l'échange est terminé. Rien à faire ; demande-moi le résultat.",
        "pt": "a resposta desse bot não precisava de resposta, então a conversa terminou. Nada a fazer; peça o resultado para mim.",
        "de": "die Antwort dieses Bots brauchte keine Antwort, der Austausch ist damit beendet. Nichts zu tun; frag mich nach dem Ergebnis.",
    },
    "duplicate": {
        "en": "the same request was already sent to that bot. Wait for its answer, or send something new.",
        "es": "esa misma petición ya se le envió a ese bot. Espera su respuesta o envía algo nuevo.",
        "fr": "la même demande a déjà été envoyée à ce bot. Attends sa réponse ou envoie autre chose.",
        "pt": "o mesmo pedido já foi enviado a esse bot. Aguarde a resposta dele ou envie algo novo.",
        "de": "dieselbe Anfrage wurde diesem Bot schon geschickt. Warte auf seine Antwort oder schick etwas Neues.",
    },
}


def reason_note(reason: str, lang: str = "en", limit: int = 0) -> str:
    row = _REASON_NOTE.get(str(reason or ""))
    if not row:
        return ""
    return row.get(_ulang(lang), row["en"]).format(n=limit or 4)


_STALLED = {"en": "stalled", "es": "sin avance", "fr": "sans progrès", "pt": "sem progresso", "de": "keine Fortschritte"}

MAX_CARD_DELEGATIONS = 4
_OPEN = {"received", "queued", "working", "waiting", "blocked"}


def _home(home: Path | None = None) -> Path:
    if home is not None:
        return Path(home)
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except Exception:
        return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))


def status_path(home: Path | None = None) -> Path:
    return _home(home) / "bot-chat-agent" / STATUS_FILENAME


def elapsed_seconds(started_at: Any, now: float | None = None) -> int:
    try:
        start = float(started_at or 0)
        if start <= 0:
            return 0
        return max(0, int((now if now is not None else time.time()) - start))
    except Exception:
        return 0


def format_elapsed(seconds: Any, lang: str = "en") -> str:
    try:
        total = max(0, int(seconds or 0))
    except Exception:
        return ""
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {secs}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def is_stalled(updated_at: Any, *, now: float | None = None,
               threshold: int = DEFAULT_STALLED_AFTER, status: str = "working") -> bool:
    try:
        if str(status or "") not in _OPEN:
            return False
        if float(updated_at or 0) <= 0:
            return False
        age = (now if now is not None else time.time()) - float(updated_at)
        return age >= max(15, int(threshold or DEFAULT_STALLED_AFTER))
    except Exception:
        return False


def delegation_state_label(state: str, lang: str = "en") -> str:
    lang = _ulang(lang)
    key = str(state or "waiting")
    table = _DELEGATION_STATE[lang]
    return table.get(key, table["waiting"])


def delegation_line(item: dict[str, Any] | None, lang: str = "en") -> str:
    try:
        row = item if isinstance(item, dict) else {}
        src = str(row.get("from") or row.get("origin") or "Bot Chat").strip() or "Bot Chat"
        dst = str(row.get("to") or row.get("assignee") or "").strip()
        if not dst:
            return ""
        lang = _ulang(lang)
        raw_state = str(row.get("state") or row.get("status") or "waiting")
        if raw_state == "error" and row.get("reason"):
            raw_state = "stopped"  # the core refused it on purpose: not a failure of the teammate
        state = delegation_state_label(raw_state, lang)
        line = f"{src} → {dst}: {state}"
        if row.get("stalled"):
            line += " · " + _STALLED[lang]
        return line
    except Exception:
        return ""


def _task_title(task: dict[str, Any]) -> str:
    return " ".join(str(task.get("title") or task.get("task_id") or "task").split())[:80]


def _normalize_task(task: dict[str, Any], *, origin: str, now: float,
                    threshold: int) -> dict[str, Any]:
    created = float(task.get("created_at") or 0)
    updated = float(task.get("updated_at") or created or 0)
    status = str(task.get("status") or "received")
    elapsed = elapsed_seconds(created, now=now)
    if status not in _OPEN and updated >= created > 0:
        # finished work stops the clock: "done 30s" must not turn into "done 20m 1s" as time passes
        elapsed = elapsed_seconds(created, now=updated)
    stalled = is_stalled(updated, now=now, threshold=threshold, status=status)
    assignee = str(task.get("assignee") or "").strip()
    return {
        "task_id": str(task.get("task_id") or ""),
        "title": _task_title(task),
        "status": status,
        "assignee": assignee,
        "session_id": str(task.get("session_id") or ""),
        "created_at": created,
        "updated_at": updated,
        "elapsed_seconds": elapsed,
        "stalled": stalled,
        "from": origin or "Bot Chat",
        "to": assignee,
        "reason": str(task.get("reason") or ""),
        "limit": int(task.get("limit") or 0),
    }


def _delegation_from_task(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "from": task.get("from") or "Bot Chat",
        "to": task.get("to") or task.get("assignee") or "",
        "title": task.get("title") or "",
        "state": task.get("status") or "waiting",
        "elapsed_seconds": int(task.get("elapsed_seconds") or 0),
        "stalled": bool(task.get("stalled")),
        "task_id": task.get("task_id") or "",
        "reason": task.get("reason") or "",
        "limit": int(task.get("limit") or 0),
    }


def _bots(origin: str, session_id: str, tasks: list[dict[str, Any]],
          pending: list[str], lang: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    origin = (origin or "Bot Chat").strip() or "Bot Chat"
    open_here = [t for t in tasks if str(t.get("status") or "") in _OPEN]
    if open_here:
        state = "working" if any(t.get("status") == "working" for t in open_here) else "waiting"
        last = " · ".join(filter(None, [
            delegation_line(_delegation_from_task(open_here[0]), lang),
        ])) or "delegating"
    elif pending:
        state = "waiting"
        last = pending[0][:80]
    else:
        state = "idle"
        last = ""
    active = {"name": origin, "role": "active", "state": state,
              "session_id": session_id, "last_activity": last}
    others: list[dict[str, Any]] = []
    seen = {origin.lower()}
    for task in tasks:
        name = str(task.get("assignee") or "").strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        others.append({
            "name": name,
            "role": "other",
            "state": str(task.get("status") or "idle"),
            "last_activity": _task_title(task),
            "elapsed_seconds": int(task.get("elapsed_seconds") or 0),
            "stalled": bool(task.get("stalled")),
        })
    return active, others


def snapshot(*, lang: str = "en", enabled: bool = True, origin: str = "",
             session_id: str = "", tasks: Iterable[dict[str, Any]] | None = None,
             pending: Iterable[str] | None = None,
             recalled: Iterable[dict[str, Any]] | None = None,
             context_tokens: int = 0, now: float | None = None,
             stalled_after_seconds: int = DEFAULT_STALLED_AFTER,
             hint: str | None = None) -> dict[str, Any]:
    """Structured status card payload. Fail-open: never raises."""
    try:
        lang = _ulang(lang)
        stamp = now if now is not None else time.time()
        if not enabled:
            return {"enabled": False, "lang": lang, "updated_at": stamp,
                    "bots": [], "delegations": [], "tasks": [],
                    "pending": [], "recalled": [], "context_tokens": 0}
        origin = str(origin or "").strip() or "Bot Chat"
        threshold = max(15, int(stalled_after_seconds or DEFAULT_STALLED_AFTER))
        raw_tasks = [t for t in (tasks or []) if isinstance(t, dict)]
        norm = [_normalize_task(t, origin=origin, now=stamp, threshold=threshold)
                for t in raw_tasks]
        pending_list = [str(p).strip() for p in (pending or []) if str(p).strip()][:5]
        rec_rows = []
        for item in (recalled or []):
            if not isinstance(item, dict):
                continue
            title = " ".join(str(item.get("title") or item.get("text") or "").split())
            if title:
                rec_rows.append({"title": title[:80], "type": str(item.get("type") or item.get("kind") or "")})
        # A live card, not a log: open work first, then the most recent, at most MAX_CARD_DELEGATIONS.
        ordered = sorted((t for t in norm if t.get("to")),
                         key=lambda t: (0 if str(t.get("status") or "") in _OPEN else 1,
                                        -float(t.get("updated_at") or 0)))
        delegations = [_delegation_from_task(t) for t in ordered[:MAX_CARD_DELEGATIONS]]
        for d in delegations:
            if d.get("reason"):
                d["note"] = reason_note(d["reason"], lang, int(d.get("limit") or 0))
        active, others = _bots(origin, session_id, norm, pending_list, lang)
        return {
            "enabled": True,
            "lang": lang,
            "updated_at": stamp,
            "session_id": session_id or "",
            "origin": origin,
            "active_bot": active,
            "bots": [active] + others,
            "tasks": norm,
            "delegations": delegations,
            "pending": pending_list,
            "recalled": rec_rows,
            "context_tokens": int(context_tokens or 0),
            "stalled_after_seconds": threshold,
            "hint": hint or ("/memoria" if lang == "es" else "/botmemory"),
        }
    except Exception:
        return {"enabled": False, "bots": [], "delegations": [], "tasks": [],
                "pending": [], "recalled": [], "context_tokens": 0}


def write_snapshot(path: Path | str, payload: dict[str, Any] | None) -> bool:
    """Atomic JSON write. Returns False on error, never raises."""
    try:
        dest = Path(path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(payload or {}, ensure_ascii=False, indent=2)
        # Explicit temp path (not mkstemp+fdopen) so the R26 disclosure audit sees a real
        # path under HERMES_HOME instead of a bare file descriptor.
        tmp = str(dest.parent / f".status-{os.getpid()}-{uuid.uuid4().hex[:8]}.json")
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(data)
            # No fsync: status.json is a regenerable UI snapshot rewritten every turn;
            # the temp-file + os.replace keeps readers from ever seeing a partial file
            # (fsync cost ~1-3 ms per turn on Windows for no benefit).
            os.replace(tmp, dest)
        except Exception:
            try:
                os.unlink(tmp)
            except Exception:
                pass
            raise
        return True
    except Exception:
        return False


def read_snapshot(path: Path | str | None = None) -> dict[str, Any]:
    """Read the published status file. Missing or corrupt → {}."""
    try:
        dest = Path(path) if path is not None else status_path()
        raw = dest.read_text(encoding="utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def refresh_live(snap: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    """Re-derive elapsed time and the stalled flag at READ time.

    ``status.json`` is only rewritten on hooks, so between turns (or after the app restarted and
    the teammate's reply was lost) a delegation would read "waiting for reply 11s" forever and the
    stalled warning could never appear. Pure; returns a new dict; never raises.
    """
    try:
        if not isinstance(snap, dict) or not snap:
            return snap
        t = float(now if now is not None else time.time())
        threshold = int(snap.get("stalled_after_seconds") or DEFAULT_STALLED_AFTER)
        out = dict(snap)
        by_id: dict[str, dict[str, Any]] = {}
        by_name: dict[str, dict[str, Any]] = {}
        tasks = []
        for task in snap.get("tasks") or []:
            if not isinstance(task, dict):
                continue
            row = dict(task)
            created = float(row.get("created_at") or 0)
            updated = float(row.get("updated_at") or created or 0)
            status = str(row.get("status") or "received")
            if status in _OPEN or not (updated >= created > 0):
                row["elapsed_seconds"] = elapsed_seconds(created, now=t)
            else:
                row["elapsed_seconds"] = elapsed_seconds(created, now=updated)
            row["stalled"] = is_stalled(updated, now=t, threshold=threshold, status=status)
            tasks.append(row)
            if row.get("task_id"):
                by_id[str(row["task_id"])] = row
            name = str(row.get("assignee") or "").strip().lower()
            if name:
                by_name.setdefault(name, row)
        out["tasks"] = tasks
        deleg = []
        for d in snap.get("delegations") or []:
            if not isinstance(d, dict):
                continue
            row = dict(d)
            src = by_id.get(str(row.get("task_id") or ""))
            if src:
                row["elapsed_seconds"], row["stalled"] = src["elapsed_seconds"], src["stalled"]
            deleg.append(row)
        out["delegations"] = deleg
        bots = []
        for b in snap.get("bots") or []:
            if not isinstance(b, dict):
                continue
            row = dict(b)
            src = by_name.get(str(row.get("name") or "").strip().lower()) if row.get("role") == "other" else None
            if src:
                row["elapsed_seconds"], row["stalled"] = src["elapsed_seconds"], src["stalled"]
            bots.append(row)
        out["bots"] = bots
        return out
    except Exception:
        return snap


def publish(*, home: Path | None = None, **kwargs: Any) -> dict[str, Any]:
    """Build a snapshot and write it to the default status.json path."""
    try:
        payload = snapshot(**kwargs)
        write_snapshot(status_path(home), payload)
        return payload
    except Exception:
        return {"enabled": False}
