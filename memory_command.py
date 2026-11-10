"""In-session slash command ``/memoria`` (alias ``/memory``).

Handler shape for Hermes v0.23: ``fn(raw_args: str) -> str | None``, plus
any session-context keywords the core actually passes (``session_id``).
Every path returns a readable chat string and never raises.
"""
from __future__ import annotations

from typing import Any, Optional

import bot_chat_memory as mem

_ALIASES = {
    "list": "list", "listar": "list", "ls": "list", "show": "list",
    "forget": "forget", "olvidar": "forget", "delete": "forget",
    "edit": "edit", "editar": "edit", "correct": "edit", "corregir": "edit",
    "clear": "clear", "borrar": "clear", "reset": "clear",
    "help": "help", "ayuda": "help", "?": "help",
}

_KIND = {
    "en": {"decision": "decision", "project_state": "project state", "lesson": "lesson"},
    "es": {"decision": "Decisión", "project_state": "Estado del proyecto", "lesson": "Lección"},
}


def _lang_from(raw_args: str, lang: str = "", command: str = "") -> str:
    blob = f"{command} {raw_args}".lower()
    if str(lang).lower().startswith("es"):
        return "es"
    spanish = ("listar", "olvidar", "editar", "borrar", "ayuda", "confirmar",
               "corregir", "memoria")
    if any(w in blob for w in spanish):
        return "es"
    return "en" if not str(lang).lower().startswith("es") else "es"


def parse(raw_args: str) -> tuple[str, list[str]]:
    parts = (raw_args or "").strip().split()
    if not parts:
        return "list", []
    action = _ALIASES.get(parts[0].lower(), "")
    if not action:
        return "help", parts
    return action, parts[1:]


def handle(raw_args: str = "", *, store: Optional[mem.MemoryStore] = None,
           session_id: str = "", lang: str = "en", command: str = "") -> str:
    try:
        lang = _lang_from(raw_args, lang, command)
        action, rest = parse(raw_args)
        if store is None:
            return _msg(lang, "no_store")
        if action == "help":
            return _help(lang)
        if action == "list":
            return _list(store, session_id, lang)
        if action == "forget":
            return _forget(store, rest, lang)
        if action == "edit":
            return _edit(store, rest, raw_args, lang)
        if action == "clear":
            return _clear(store, rest, lang)
        return _help(lang)
    except Exception as exc:
        return f"Memory command failed ({exc}). Nothing was changed."


def _msg(lang: str, key: str, **kw: Any) -> str:
    es = lang == "es"
    table = {
        "no_store": "No se pudo abrir el almacén de memoria." if es else "Memory store is not available.",
        "not_found": "No encontrada: {token}. Nada se cambió." if es else "not found: {token}. Nothing was changed.",
        "ambiguous": "Id ambiguo: {token}. Usa más caracteres." if es else "Ambiguous id: {token}. Use more characters.",
        "need_id": "Indica el id (los 8 caracteres que muestra /memoria)." if es
                   else "Pass the id (the 8 characters shown by /memory).",
        "need_text": "Indica el id y el texto nuevo: /memoria editar <id> <texto>." if es
                     else "Pass the id and the new text: /memory edit <id> <text>.",
        "forgot": "Olvidada {kind} {sid}." if es else "Forgot {kind} {sid}.",
        "updated": "Actualizada {kind} {sid}." if es else "Updated {kind} {sid}.",
        "confirm": ("Escribe /memoria borrar confirmar para borrar todas las "
                    "memorias largas. No se puede deshacer.") if es
                   else ("Type /memory clear confirm to erase all long-term "
                         "memories. This cannot be undone."),
        "cleared": "Borradas {n} memorias." if es else "Cleared {n} memories.",
        "empty": "(vacío)" if es else "(empty)",
    }
    text = table.get(key, key)
    try:
        return text.format(**kw)
    except Exception:
        return text


def _help(lang: str) -> str:
    if lang == "es":
        return (
            "/memoria (alias /memory) — lo que el plugin recuerda.\n"
            "  /memoria                listar decisiones, estado, lecciones, resumen y pendientes\n"
            "  /memoria olvidar <id>   olvidar una entrada\n"
            "  /memoria editar <id> <texto>   corregir una entrada\n"
            "  /memoria borrar         pide confirmación\n"
            "  /memoria borrar confirmar   borra toda la memoria larga"
        )
    return (
        "/memory (alias /memoria) — what the plugin remembers.\n"
        "  /memory                     list decisions, project state, lessons, summary and pending asks\n"
        "  /memory forget <id>         forget one entry\n"
        "  /memory edit <id> <text>    correct one entry\n"
        "  /memory clear               ask for confirmation\n"
        "  /memory clear confirm       erase all long-term memories"
    )


def _list(store: mem.MemoryStore, session_id: str, lang: str) -> str:
    es = lang == "es"
    kinds = _KIND["es" if es else "en"]
    lines: list[str] = ["[Memoria]" if es else "[Memory]"]
    rows = store.list_all()
    if not rows:
        lines.append(f"  {_msg(lang, 'empty')}")
    else:
        lines.append("Memoria larga:" if es else "Long-term:")
        for row in rows:
            kind = kinds.get(str(row.get("kind") or ""), str(row.get("kind") or ""))
            sid = mem.short_id(str(row.get("id") or ""))
            text = " ".join(str(row.get("text") or "").split())
            lines.append(f"  {kind:<22} {sid}  {text}")
    summary = store.load_summary(session_id).get("text") or ""
    lines.append("")
    lines.append("[Resumen de sesión]" if es else "[Session summary]")
    if summary.strip():
        lines.extend(summary.strip().splitlines())
    else:
        lines.append(f"  {_msg(lang, 'empty')}")
    pending = store.load_pending(session_id)
    lines.append("")
    lines.append("[Pendientes]" if es else "[Pending asks]")
    if pending:
        for ask in pending:
            lines.append(f"  - {ask}")
    else:
        lines.append(f"  {_msg(lang, 'empty')}")
    return "\n".join(lines)


def _forget(store: mem.MemoryStore, rest: list[str], lang: str) -> str:
    if not rest:
        return _msg(lang, "need_id")
    token = rest[0]
    row, err = store.resolve(token)
    if err == "ambiguous":
        return _msg(lang, "ambiguous", token=token)
    deleted = store.delete(token)
    if not deleted:
        return _msg(lang, "not_found", token=token)
    kind = str(deleted.get("kind") or "entry")
    return _msg(lang, "forgot", kind=kind, sid=mem.short_id(deleted["id"]))


def _edit(store: mem.MemoryStore, rest: list[str], raw_args: str, lang: str) -> str:
    if len(rest) < 2:
        return _msg(lang, "need_text")
    token = rest[0]
    # Everything after the action and the id is the new text.
    parts = (raw_args or "").strip().split(None, 2)
    text = parts[2] if len(parts) >= 3 else ""
    if not text.strip():
        return _msg(lang, "need_text")
    row, err = store.resolve(token)
    if err == "ambiguous":
        return _msg(lang, "ambiguous", token=token)
    if err:
        return _msg(lang, "not_found", token=token)
    updated = store.update(token, text)
    if not updated:
        return _msg(lang, "not_found", token=token)
    return _msg(lang, "updated", kind=str(updated.get("kind") or "entry"),
                sid=mem.short_id(updated["id"]))


def _clear(store: mem.MemoryStore, rest: list[str], lang: str) -> str:
    token = (rest[0].lower() if rest else "")
    if token not in {"confirm", "confirmar", "yes", "sí", "si"}:
        return _msg(lang, "confirm")
    n = store.clear_all()
    return _msg(lang, "cleared", n=n)
