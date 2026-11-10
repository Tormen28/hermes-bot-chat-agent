"""Panteón, almost automatic: offer a specialist in one line, create it on a plain yes.

Nothing here creates a profile by itself. The flow, all inside the stock Bots panel:

1. A Bot Chat request clearly fits a specialty (code, design, research ...) and no matching
   profile is in the team. ``step`` returns a short context block telling the coordinator to ask
   ONE line ("I can create Daedalus (code) for this, shall I?"). The offer is remembered.
2. The user answers with a plain yes. ``step`` then queues a creation request. The Desktop half of
   this plugin picks it up from the status feed and calls the stock ``profiles.create`` gateway
   RPC (a public Desktop SDK surface, so the plugin imports no private core module), then reports
   back. The profile appears as a row in the Bots panel and joins ``team.assignees``.
3. A no (or silence) is remembered: the same preset is not offered again for a while.

Offers are only made when the Desktop half has been seen recently (it is the only part that can
create profiles). Everything is pure given its inputs and a state file, so it is unit-tested
without a Hermes core.
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

import panteon as _p

STATE_FILENAME = "panteon-auto.json"
OFFER_TTL_SECONDS = 15 * 60
DECLINE_COOLDOWN_SECONDS = 3 * 24 * 3600
DESKTOP_SEEN_TTL_SECONDS = 90
REQUEST_TTL_SECONDS = 10 * 60
FIRST_RUN_SET = ("daedalus", "iris", "athena")

# What the stock Bots panel stores for a bot (profile.yaml ``ui_meta['hermes-bots']``). Writing it
# makes the profile a real "bot" of Bot Mode, which is also what unlocks the core ``message_agent``
# tool for delegation. Neutral values only.
_BOT_LOOK = {
    "daedalus": ("hexagon", "Code"), "iris": ("cloud", "Design"), "athena": ("circle", "Research"),
    "calliope": ("pill", "Writing"), "dionysus": ("triangle", "Games"), "nike": ("squircle", "QA"),
    "mnemosyne": ("blob", "Docs"),
}


def bot_meta_for(preset: str, now_ms: int | None = None) -> dict[str, Any]:
    shape, _role = _BOT_LOOK.get(preset, ("circle", ""))
    # the Bots panel row shows this title, so it carries the god's name (the role is in the profile description)
    return {"shape": shape, "imageKind": "shape", "title": preset.capitalize(),
            "created": int(now_ms if now_ms is not None else time.time() * 1000)}

# (strong keywords: one is enough, weak keywords: two are needed). Lowercase, EN + ES.
_KEYWORDS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "daedalus": (
        ("refactor", "pull request", "stack trace", "traceback", "unit test", "pytest", "typescript",
         "javascript", "python script", "debug this", "depura", "refactoriza", "implementa la funci",
         "función", "funcion", "endpoint", "bug in", "bug en", "compila", "código", "codigo"),
        ("code", "bug", "api", "script", "function", "test", "repo", "commit", "error", "clase",
         "implementa", "programa")),
    "iris": (
        ("mockup", "wireframe", "color palette", "paleta de colores", "logo", "ui design", "ux ",
         "diseña la pantalla", "diseño de la pantalla", "figma", "tipografía", "tipografia"),
        ("diseño", "design", "layout", "paleta", "palette", "pantalla", "screen", "icono", "icon",
         "interfaz", "interface")),
    "athena": (
        ("investiga", "research ", "compara las opciones", "compare options", "state of the art",
         "estado del arte", "fuentes sobre", "sources on", "market analysis", "análisis de mercado"),
        ("investigar", "análisis", "analisis", "analysis", "fuentes", "sources", "comparativa",
         "benchmark", "estudio")),
    "calliope": (
        ("redacta", "escribe un artículo", "escribe un articulo", "write an article", "draft an email",
         "blog post", "copy para", "ad copy", "carta de presentación", "cover letter"),
        ("redacción", "redaccion", "texto", "text", "artículo", "articulo", "borrador", "draft",
         "newsletter", "post")),
    "dionysus": (
        ("game loop", "godot", "unity ", "unreal", "game design", "diseño de juego", "prototipo jugable",
         "playable prototype", "mecánica de juego", "game mechanic"),
        ("juego", "game", "nivel", "level", "personaje", "character", "mecánica", "mecanica", "sprite")),
    "nike": (
        ("revisa este pr", "review this pr", "code review", "revisión de código", "plan de pruebas",
         "test plan", "reproduce el bug", "reproduce the bug", "qa pass"),
        ("revisa", "review", "verifica", "verify", "reproducir", "reproduce", "qa", "pruebas")),
    "mnemosyne": (
        ("documenta", "document this", "write the readme", "escribe el readme", "changelog",
         "decision log", "registro de decisiones", "notas de traspaso", "handoff notes"),
        ("documentación", "documentacion", "documentation", "readme", "notas", "notes", "wiki")),
}

_YES = re.compile(
    r"^\s*(s[ií]+|sí|si por favor|dale|d[aá]le|ok(ay)?|okey|claro|va|venga|yes|yep|yeah|sure|please do|"
    r"hazlo|cr[eé]alo|cr[eé]alos|adelante|por supuesto|de una|listo|perfecto|go ahead)\b[\s,.!¡]*"
    r"(cr[eé]alo|cr[eé]alos|hazlo|please|por favor|gracias|thanks|do it|go ahead)?[\s.!]*$", re.I)
_NO = re.compile(
    r"^\s*(no+|nop|nope|no gracias|nah|mejor no|ahora no|not now|no thanks|no,? gracias|despu[eé]s)\b.*$", re.I)


def _l(lang: str) -> str:
    return "es" if str(lang or "").lower().startswith("es") else "en"


# ── state ────────────────────────────────────────────────────────────────────────────────────

def state_path(home: Path | None = None) -> Path:
    import status_panel
    return status_panel._home(home) / "bot-chat-agent" / STATE_FILENAME


def load_state(path: Path | None = None) -> dict[str, Any]:
    try:
        data = json.loads((path or state_path()).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_state(state: dict[str, Any], path: Path | None = None) -> bool:
    try:
        dest = path or state_path()
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(dest.parent / f".panteon-auto-{os.getpid()}-{uuid.uuid4().hex[:8]}.json")
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(state, ensure_ascii=False, indent=2))
        os.replace(tmp, dest)
        return True
    except Exception:
        return False


# ── Desktop half handshake ───────────────────────────────────────────────────────────────────

def mark_desktop_seen(*, now: float | None = None, path: Path | None = None) -> None:
    st = load_state(path)
    st["desktop_seen"] = float(now if now is not None else time.time())
    save_state(st, path)


def desktop_ready(*, now: float | None = None, path: Path | None = None) -> bool:
    seen = float(load_state(path).get("desktop_seen") or 0)
    return seen > 0 and (now if now is not None else time.time()) - seen <= DESKTOP_SEEN_TTL_SECONDS


def pending_requests(*, now: float | None = None, path: Path | None = None) -> list[dict[str, Any]]:
    """Creation requests the Desktop half still has to run (fresh, not yet answered)."""
    t = now if now is not None else time.time()
    out = []
    for r in load_state(path).get("requests", []):
        if isinstance(r, dict) and r.get("status") == "pending" and t - float(r.get("ts") or 0) <= REQUEST_TTL_SECONDS:
            out.append({k: r.get(k) for k in ("id", "name", "preset", "soul", "description", "clone_from", "ui_meta")})
    return out


def enqueue_request(preset: str, name: str, *, lang: str = "en", clone_from: str = "default",
                    task: str = "", session_id: str = "", now: float | None = None,
                    path: Path | None = None) -> dict[str, Any]:
    st = load_state(path)
    t = float(now if now is not None else time.time())
    reqs = [r for r in st.get("requests", []) if isinstance(r, dict) and t - float(r.get("ts") or 0) <= 24 * 3600]
    for r in reqs:  # one live request per name
        if r.get("name") == name and r.get("status") in ("pending", "created"):
            return r
    req = {"id": uuid.uuid4().hex[:12], "name": name, "preset": preset, "ts": t, "status": "pending",
           "soul": _p.soul_for(preset, lang), "description": _p.description_for(preset, lang),
           "clone_from": clone_from or "default", "ui_meta": bot_meta_for(preset, int(t * 1000)), "task": (task or "")[:600], "session_id": session_id or "",
           "applied": False}
    reqs.append(req)
    st["requests"] = reqs
    save_state(st, path)
    return req


def record_result(req_id: str, ok: bool, *, error: str = "", now: float | None = None,
                  path: Path | None = None) -> bool:
    """Called by the Desktop half (dashboard API) when ``profiles.create`` finished."""
    st = load_state(path)
    hit = False
    for r in st.get("requests", []):
        if isinstance(r, dict) and r.get("id") == req_id and r.get("status") == "pending":
            r["status"] = "created" if ok else "failed"
            r["error"] = str(error or "")[:300]
            r["done_ts"] = float(now if now is not None else time.time())
            hit = True
    if hit:
        save_state(st, path)
    return hit


def wait_for_result(req_id: str, timeout: float, *, path: Path | None = None,
                    sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
                    interval: float = 0.5) -> str:
    """'created' | 'failed' | 'pending' (timed out). Bounded; never raises."""
    end = clock() + max(0.0, timeout)
    while True:
        for r in load_state(path).get("requests", []):
            if isinstance(r, dict) and r.get("id") == req_id and r.get("status") in ("created", "failed"):
                return str(r["status"])
        if clock() >= end:
            return "pending"
        sleep(interval)


def take_finished(*, path: Path | None = None) -> list[dict[str, Any]]:
    """Requests that finished but whose outcome the coordinator was not told yet (marks them told)."""
    st = load_state(path)
    out = []
    for r in st.get("requests", []):
        if isinstance(r, dict) and r.get("status") in ("created", "failed") and not r.get("applied"):
            r["applied"] = True
            if r.get("status") == "created":
                r["handoff"] = "told"  # the coordinator was told to delegate; later turns re-check it
            out.append(dict(r))
    if out:
        save_state(st, path)
    return out


# ── detection ────────────────────────────────────────────────────────────────────────────────

def detect(text: str) -> Optional[str]:
    """Preset id when the request CLEARLY fits one specialty, else None.

    One strong phrase, or two weak words, for exactly one specialty (a tie between specialties is
    not clear, so nothing is offered)."""
    low = " " + " ".join(str(text or "").lower().split()) + " "
    if len(low) < 12:
        return None
    scores: dict[str, int] = {}
    for pid, (strong, weak) in _KEYWORDS.items():
        s = sum(2 for k in strong if k in low)
        s += sum(1 for w in weak if re.search(r"\b" + re.escape(w.strip()) + r"\b", low))
        if s >= 2:
            scores[pid] = s
    if not scores:
        return None
    best = max(scores.values())
    top = [p for p, s in scores.items() if s == best]
    return top[0] if len(top) == 1 else None


def is_yes(text: str) -> bool:
    t = str(text or "").strip()
    return 0 < len(t) <= 40 and bool(_YES.match(t))


def is_no(text: str) -> bool:
    t = str(text or "").strip()
    return 0 < len(t) <= 40 and bool(_NO.match(t))


# ── the decision ─────────────────────────────────────────────────────────────────────────────

_OFFER = {
    "en": ("[Panteón offer] The request below fits a specialist you do not have yet. Answer it as usual, "
           "and add exactly ONE short closing line offering to create it, in the user's language, like: "
           "\"I can create {Name} ({domain}) for this, shall I?\" Do not create anything and do not "
           "call any tool for this; wait for the user's answer."),
    "es": ("[Oferta Panteón] La petición de abajo encaja con un especialista que aún no tienes. "
           "Respóndela como siempre y añade exactamente UNA línea corta al final ofreciendo crearlo, "
           "en el idioma del usuario, por ejemplo: \"Puedo crear a {Name} ({domain}) para esto, "
           "¿lo creo?\" No crees nada ni llames herramientas por esto; espera la respuesta."),
}
_FIRST_RUN = {
    "en": ("[Panteón offer] This user has no specialists yet. After answering as usual, add exactly ONE "
           "short closing line offering a small starter team, in the user's language, like: \"If you "
           "want, I can create a small team: Daedalus (code), Iris (design) and Athena (research). "
           "Shall I?\" Create nothing until the user says yes."),
    "es": ("[Oferta Panteón] Este usuario aún no tiene especialistas. Tras responder como siempre, "
           "añade exactamente UNA línea corta al final ofreciendo un equipo inicial pequeño, en el "
           "idioma del usuario, por ejemplo: \"Si quieres, puedo crear un equipo pequeño: Daedalus "
           "(código), Iris (diseño) y Athena (investigación). ¿Lo creo?\" No crees nada hasta que "
           "el usuario diga que sí."),
}
_APPROVED = {
    "en": ("[Panteón] The user said yes: {names} is being created now (it shows up as a row in the Bots "
           "panel). Say so in one short line; do not delegate yet.{task}"),
    "es": ("[Panteón] El usuario dijo que sí: {names} se está creando ahora (sale como una fila del panel "
           "Bots). Dilo en una línea corta; todavía no delegues.{task}"),
}
_READY = {
    "en": ("[Panteón] Created and added to the team: {names}. Now hand the user's request to {first} with the "
           "message_agent tool (one message, include the request and what you expect back), then tell the "
           "user in one line that you delegated it. The reply comes back later on its own. A new bot only shows "
           "up in your tools from the next turn: if message_agent is not in your tool list yet, do not "
           "try; tell the user in one line that {first} is ready and that you will hand the request over "
           "on their next message.{task}"),
    "es": ("[Panteón] Creado y añadido al equipo: {names}. Ahora pásale la petición del usuario a {first} con la "
           "herramienta message_agent (un mensaje, con la petición y lo que esperas de vuelta) y dile al "
           "usuario en una línea que se la delegaste. La respuesta vuelve sola más tarde. Un bot nuevo aparece "
           "en tus herramientas desde el turno siguiente: si message_agent aún no está en tu lista, no lo "
           "intentes; dile al usuario en una línea que {first} ya está listo y que le pasas la petición en "
           "su próximo mensaje.{task}"),
}
_REMIND = {
    "en": ("[Panteón] Pending hand-off: the request has not been sent to {name} yet. Send it now with the "
           "message_agent tool (one message), then tell the user in one line.{task}"),
    "es": ("[Panteón] Entrega pendiente: la petición aún no se le envió a {name}. Envíasela ahora con la "
           "herramienta message_agent (un mensaje) y dile al usuario en una línea.{task}"),
}
_FAILED = {
    "en": "[Panteón] Creating {names} failed ({err}). Tell the user briefly; they can run `/pantheon create <id> confirm`.",
    "es": "[Panteón] Falló crear {names} ({err}). Díselo al usuario en corto; puede correr `/pantheon create <id> confirm`.",
}


def _name(preset: str, prefix: str) -> str:
    return _p.profile_name(preset, prefix)


def _cap(preset: str) -> str:
    return preset.capitalize()


def step(session_id: str, user_message: str, *, lang: str = "en", prefix: str = "",
         team: list[str] | None = None, existing: list[str] | None = None,
         coordinator: str = "default", cfg: dict[str, Any] | None = None,
         now: float | None = None, path: Path | None = None,
         wait: Callable[[str, float], str] | None = None,
         delegated: Callable[[str, float], bool] | None = None,
         is_god_chat: bool = False) -> dict[str, Any]:
    """Decide what the coordinator should be told this turn.

    Returns ``{"context": str, "team_add": [names], "queued": [names]}``. Never raises."""
    out: dict[str, Any] = {"context": "", "team_add": [], "queued": []}
    try:
        cfg = dict(cfg or {})
        if not cfg.get("auto", True) or is_god_chat:
            return out
        lg = _l(lang)
        t = float(now if now is not None else time.time())
        team_l = [str(x).lower() for x in (team or [])]
        existing_l = {str(x).lower() for x in (existing or [])}
        st = load_state(path)
        ctx_parts: list[str] = []

        # 0) a hand-off the coordinator was told about earlier but never sent (the message_agent tool
        #    only appears in a turn that starts after the first Bot Mode bot exists): remind, at most twice
        changed = False
        for r in st.get("requests", []):
            if isinstance(r, dict) and r.get("status") == "created" and r.get("handoff") == "told":
                done_ts = float(r.get("done_ts") or r.get("ts") or 0)
                if delegated is not None and delegated(str(r.get("name")), done_ts):
                    r["handoff"] = "done"; changed = True
                elif t - done_ts > OFFER_TTL_SECONDS * 2 or int(r.get("reminders") or 0) >= 2 or not r.get("task"):
                    r["handoff"] = "dropped"; changed = True
                else:
                    r["reminders"] = int(r.get("reminders") or 0) + 1; changed = True
                    ctx_parts.append(_REMIND[lg].format(name=r.get("name"), task=_task_hint(r.get("task"), lg)))
        if changed:
            save_state(st, path)
            st = load_state(path)

        # 1) outcomes of earlier creation requests the coordinator has not heard about
        for r in take_finished(path=path):
            nm = r.get("name") or ""
            if r.get("status") == "created":
                out["team_add"].append(nm)
                ctx_parts.append(_READY[lg].format(
                    names=_cap(r.get("preset") or nm), first=nm, task=_task_hint(r.get("task"), lg)))
            else:
                ctx_parts.append(_FAILED[lg].format(names=nm, err=r.get("error") or "unknown"))
        if ctx_parts:
            st = load_state(path)

        # 2) an open offer: yes / no / ignore
        offers = st.get("offers", {}) if isinstance(st.get("offers"), dict) else {}
        offer = offers.get(session_id or "")
        if isinstance(offer, dict) and t - float(offer.get("ts") or 0) > OFFER_TTL_SECONDS:
            offers.pop(session_id or "", None)
            offer = None
        if offer and is_yes(user_message):
            presets = [p for p in offer.get("presets", []) if p in _p.PRESETS]
            names = []
            for p in presets:
                nm = _name(p, prefix)
                names.append(nm)
                if nm in existing_l:
                    if nm not in team_l and nm not in out["team_add"]:
                        out["team_add"].append(nm)
                    continue
                enqueue_request(p, nm, lang=lang, clone_from=coordinator, task=str(offer.get("task") or ""),
                                session_id=session_id, now=t, path=path)
                out["queued"].append(nm)
            offers.pop(session_id or "", None)
            st = load_state(path)
            st["offers"] = offers
            save_state(st, path)
            if out["queued"]:
                # give the Desktop half a few seconds so the same turn can already delegate
                waiter = wait or (lambda rid, secs: wait_for_result(rid, secs, path=path))
                budget = float(cfg.get("create_wait_seconds", 8))
                for r in load_state(path).get("requests", []):
                    if r.get("name") in out["queued"] and r.get("status") == "pending":
                        waiter(r["id"], budget)
                done = take_finished(path=path)
                created = [r for r in done if r.get("status") == "created"]
                failed = [r for r in done if r.get("status") == "failed"]
                for r in created:
                    out["team_add"].append(r["name"])
                if created:
                    names_txt = ", ".join(_cap(r.get("preset") or r["name"]) for r in created)
                    ctx_parts.append(_READY[lg].format(
                        names=names_txt, first=created[0]["name"], task=_task_hint(offer.get("task"), lg)))
                for r in failed:
                    ctx_parts.append(_FAILED[lg].format(names=r["name"], err=r.get("error") or "unknown"))
                if not created and not failed:
                    ctx_parts.append(_APPROVED[lg].format(
                        names=", ".join(_cap(p) for p in presets), task=""))
            elif out["team_add"]:
                ctx_parts.append(_READY[lg].format(names=", ".join(out["team_add"]), first=out["team_add"][0],
                                                   task=_task_hint(offer.get("task"), lg)))
            out["context"] = "\n".join(ctx_parts)
            return out
        if offer and is_no(user_message):
            for p in offer.get("presets", []):
                st.setdefault("declined", {})[p] = t
            offers.pop(session_id or "", None)
            st["offers"] = offers
            save_state(st, path)
            out["context"] = "\n".join(ctx_parts)
            return out

        # 3) a fresh offer
        if desktop_ready(now=t, path=path):
            preset = detect(user_message)
            declined = st.get("declined", {}) if isinstance(st.get("declined"), dict) else {}
            if preset:
                nm = _name(preset, prefix)
                recent = t - float(declined.get(preset) or 0) < DECLINE_COOLDOWN_SECONDS
                busy = any(r.get("name") == nm and r.get("status") == "pending" for r in st.get("requests", []))
                if nm not in existing_l and nm not in team_l and not recent and not busy:
                    offers[session_id or ""] = {"presets": [preset], "task": " ".join(str(user_message).split())[:600],
                                                 "ts": t}
                    st["offers"] = offers
                    save_state(st, path)
                    ctx_parts.append(_OFFER[lg].format(Name=_cap(preset),
                                                       domain=_p.PRESETS[preset]["domain_" + lg]))
            elif cfg.get("first_run_suggestion", True) and not st.get("first_run_done") \
                    and not team_l and not any(_name(p, prefix) in existing_l for p in _p.PRESET_ORDER) \
                    and len(" ".join(str(user_message or "").split())) >= 12:
                st["first_run_done"] = t
                offers[session_id or ""] = {"presets": list(FIRST_RUN_SET), "task": "", "ts": t}
                st["offers"] = offers
                save_state(st, path)
                ctx_parts.append(_FIRST_RUN[lg])
        out["context"] = "\n".join(ctx_parts)
        return out
    except Exception:
        return out


def _task_hint(task: Any, lg: str) -> str:
    task = " ".join(str(task or "").split())
    if not task:
        return ""
    return (" Original request: " if lg == "en" else " Petición original: ") + task[:400]
