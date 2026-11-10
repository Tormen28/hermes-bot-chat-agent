"""In-session slash command ``/memoria`` (alias ``/botmemory``).

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
    "help": "help", "ayuda": "help", "?": "help", "aide": "help", "ajuda": "help", "hilfe": "help",
    "lister": "list", "liste": "list", "listar": "list", "auflisten": "list",
    "oublier": "forget", "esquecer": "forget", "vergessen": "forget",
    "éditer": "edit", "editer": "edit", "corriger": "edit", "bearbeiten": "edit", "korrigieren": "edit",
    "effacer": "clear", "apagar": "clear", "löschen": "clear", "loeschen": "clear",
}

_KIND = {
    "en": {"decision": "decision", "project_state": "project state", "lesson": "lesson"},
    "es": {"decision": "Decisión", "project_state": "Estado del proyecto", "lesson": "Lección"},
    "fr": {"decision": "Décision", "project_state": "État du projet", "lesson": "Leçon"},
    "pt": {"decision": "Decisão", "project_state": "Estado do projeto", "lesson": "Lição"},
    "de": {"decision": "Entscheidung", "project_state": "Projektstand", "lesson": "Lektion"},
}

_LANGS = ("en", "es", "fr", "pt", "de")


def _lang_from(raw_args: str, lang: str = "", command: str = "") -> str:
    """The language to answer in: the one passed (the user's), else a hint from the words used."""
    code = str(lang or "").lower()[:2]
    if code in _LANGS:
        return code
    blob = f"{command} {raw_args}".lower()
    hints = {
        "es": ("listar", "olvidar", "editar", "borrar", "ayuda", "confirmar", "corregir", "memoria"),
        "fr": ("oublier", "éditer", "corriger", "effacer", "aide", "confirmer"),
        "pt": ("esquecer", "apagar", "ajuda"),
        "de": ("vergessen", "bearbeiten", "löschen", "hilfe", "bestätigen"),
    }
    words = blob.split()
    for code, ws in hints.items():
        if any(w in words or (code == "es" and w in blob) for w in ws):
            return code
    return "en"


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
        return _msg(str(lang or "en")[:2], "failed", err=exc)


_MSG = {
    "no_store": {
        "en": "Memory store is not available.", "es": "No se pudo abrir el almacén de memoria.",
        "fr": "Le stockage de la mémoire n'est pas disponible.", "pt": "O armazenamento da memória não está disponível.",
        "de": "Der Gedächtnisspeicher ist nicht verfügbar."},
    "not_found": {
        "en": "not found: {token}. Nothing was changed.", "es": "No encontrada: {token}. Nada se cambió.",
        "fr": "Introuvable : {token}. Rien n'a été modifié.", "pt": "Não encontrada: {token}. Nada foi alterado.",
        "de": "Nicht gefunden: {token}. Nichts wurde geändert."},
    "ambiguous": {
        "en": "Ambiguous id: {token}. Use more characters.", "es": "Id ambiguo: {token}. Usa más caracteres.",
        "fr": "Identifiant ambigu : {token}. Utilise plus de caractères.",
        "pt": "ID ambíguo: {token}. Use mais caracteres.", "de": "Mehrdeutige ID: {token}. Nimm mehr Zeichen."},
    "need_id": {
        "en": "Pass the id (the 8 characters shown by /botmemory).",
        "es": "Indica el id (los 8 caracteres que muestra /memoria).",
        "fr": "Indique l'identifiant (les 8 caractères affichés par /botmemory).",
        "pt": "Informe o ID (os 8 caracteres mostrados por /botmemory).",
        "de": "Gib die ID an (die 8 Zeichen, die /botmemory anzeigt)."},
    "need_text": {
        "en": "Pass the id and the new text: /botmemory edit <id> <text>.",
        "es": "Indica el id y el texto nuevo: /memoria editar <id> <texto>.",
        "fr": "Indique l'identifiant et le nouveau texte : /botmemory edit <id> <texte>.",
        "pt": "Informe o ID e o novo texto: /botmemory edit <id> <texto>.",
        "de": "Gib die ID und den neuen Text an: /botmemory edit <id> <Text>."},
    "forgot": {
        "en": "Forgot {kind} {sid}.", "es": "Olvidada {kind} {sid}.", "fr": "Oublié : {kind} {sid}.",
        "pt": "Esquecido: {kind} {sid}.", "de": "Vergessen: {kind} {sid}."},
    "updated": {
        "en": "Updated {kind} {sid}.", "es": "Actualizada {kind} {sid}.", "fr": "Mis à jour : {kind} {sid}.",
        "pt": "Atualizado: {kind} {sid}.", "de": "Aktualisiert: {kind} {sid}."},
    "confirm": {
        "en": "Type /botmemory clear confirm to erase all long-term memories. This cannot be undone.",
        "es": "Escribe /memoria borrar confirmar para borrar todas las memorias largas. No se puede deshacer.",
        "fr": "Écris /botmemory clear confirm pour effacer toute la mémoire longue. C'est irréversible.",
        "pt": "Digite /botmemory clear confirm para apagar toda a memória longa. Não dá para desfazer.",
        "de": "Tippe /botmemory clear confirm, um das gesamte Langzeitgedächtnis zu löschen. Das lässt sich nicht rückgängig machen."},
    "cleared": {
        "en": "Cleared {n} memories.", "es": "Borradas {n} memorias.", "fr": "{n} souvenirs effacés.",
        "pt": "{n} memórias apagadas.", "de": "{n} Erinnerungen gelöscht."},
    "empty": {"en": "(empty)", "es": "(vacío)", "fr": "(vide)", "pt": "(vazio)", "de": "(leer)"},
    "failed": {
        "en": "Memory command failed ({err}). Nothing was changed.",
        "es": "El comando de memoria falló ({err}). No se cambió nada.",
        "fr": "La commande de mémoire a échoué ({err}). Rien n'a été modifié.",
        "pt": "O comando de memória falhou ({err}). Nada foi alterado.",
        "de": "Der Gedächtnisbefehl ist fehlgeschlagen ({err}). Nichts wurde geändert."},
}


def _msg(lang: str, key: str, **kw: Any) -> str:
    row = _MSG.get(key)
    text = key if row is None else row.get(lang if lang in _LANGS else "en", row["en"])
    try:
        return text.format(**kw)
    except Exception:
        return text


_HELP = {
    "es": (
        "/memoria (alias /botmemory) — lo que el plugin recuerda.\n"
        "  /memoria                listar decisiones, estado, lecciones, resumen y pendientes\n"
        "  /memoria olvidar <id>   olvidar una entrada\n"
        "  /memoria editar <id> <texto>   corregir una entrada\n"
        "  /memoria borrar         pide confirmación\n"
        "  /memoria borrar confirmar   borra toda la memoria larga"
    ),
    "en": (
        "/botmemory (alias /memoria) — what the plugin remembers.\n"
        "  /botmemory                     list decisions, project state, lessons, summary and pending asks\n"
        "  /botmemory forget <id>         forget one entry\n"
        "  /botmemory edit <id> <text>    correct one entry\n"
        "  /botmemory clear               ask for confirmation\n"
        "  /botmemory clear confirm       erase all long-term memories"
    ),
    "fr": (
        "/botmemory (alias /memoria) — ce dont le plugin se souvient.\n"
        "  /botmemory                     lister décisions, état du projet, leçons, résumé et demandes en attente\n"
        "  /botmemory forget <id>         oublier une entrée\n"
        "  /botmemory edit <id> <texte>   corriger une entrée\n"
        "  /botmemory clear               demande confirmation\n"
        "  /botmemory clear confirm       efface toute la mémoire longue"
    ),
    "pt": (
        "/botmemory (alias /memoria) — o que o plugin lembra.\n"
        "  /botmemory                     listar decisões, estado do projeto, lições, resumo e pedidos pendentes\n"
        "  /botmemory forget <id>         esquecer uma entrada\n"
        "  /botmemory edit <id> <texto>   corrigir uma entrada\n"
        "  /botmemory clear               pede confirmação\n"
        "  /botmemory clear confirm       apaga toda a memória longa"
    ),
    "de": (
        "/botmemory (Alias /memoria) — woran sich das Plugin erinnert.\n"
        "  /botmemory                     Entscheidungen, Projektstand, Lektionen, Zusammenfassung und offene Bitten auflisten\n"
        "  /botmemory forget <id>         einen Eintrag vergessen\n"
        "  /botmemory edit <id> <Text>    einen Eintrag korrigieren\n"
        "  /botmemory clear               fragt nach Bestätigung\n"
        "  /botmemory clear confirm       löscht das gesamte Langzeitgedächtnis"
    ),
}


def _help(lang: str) -> str:
    return _HELP.get(lang if lang in _LANGS else "en", _HELP["en"])


_HEADS = {
    "mem": {"en": "[Memory]", "es": "[Memoria]", "fr": "[Mémoire]", "pt": "[Memória]", "de": "[Gedächtnis]"},
    "long": {"en": "Long-term:", "es": "Memoria larga:", "fr": "Mémoire longue :", "pt": "Memória longa:", "de": "Langzeit:"},
    "summary": {"en": "[Session summary]", "es": "[Resumen de sesión]", "fr": "[Résumé de session]",
                "pt": "[Resumo da sessão]", "de": "[Sitzungszusammenfassung]"},
    "pending": {"en": "[Pending asks]", "es": "[Pendientes]", "fr": "[Demandes en attente]",
                "pt": "[Pedidos pendentes]", "de": "[Offene Bitten]"},
}


def _list(store: mem.MemoryStore, session_id: str, lang: str) -> str:
    lang = lang if lang in _LANGS else "en"
    kinds = _KIND[lang]
    lines: list[str] = [_HEADS["mem"][lang]]
    rows = store.list_all()
    if not rows:
        lines.append(f"  {_msg(lang, 'empty')}")
    else:
        lines.append(_HEADS["long"][lang])
        for row in rows:
            kind = kinds.get(str(row.get("kind") or ""), str(row.get("kind") or ""))
            sid = mem.short_id(str(row.get("id") or ""))
            text = " ".join(str(row.get("text") or "").split())
            lines.append(f"  {kind:<22} {sid}  {text}")
    summary = store.load_summary(session_id).get("text") or ""
    lines.append("")
    lines.append(_HEADS["summary"][lang])
    if summary.strip():
        lines.extend(summary.strip().splitlines())
    else:
        lines.append(f"  {_msg(lang, 'empty')}")
    pending = store.load_pending(session_id)
    lines.append("")
    lines.append(_HEADS["pending"][lang])
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
    if token not in {"confirm", "confirmar", "confirmer", "bestätigen", "bestaetigen", "yes", "oui", "ja", "sim", "sí", "si"}:
        return _msg(lang, "confirm")
    n = store.clear_all()
    return _msg(lang, "cleared", n=n)
