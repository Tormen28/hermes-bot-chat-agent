"""Bot Chat Agent - agentic style + safety guard for canonical "Bot Chat" sessions.

Scope: only sessions whose title (or compaction-root title) equals the configured
Bot Chat title (default "Bot Chat", the same title Hermes core uses for Bot Mode).
Disclosure: resolves session titles by opening ``<HERMES_HOME>/state.db`` READ-ONLY.
Team wait checks open ``<HERMES_HOME>/kanban.db`` READ-ONLY when gods are configured.
It never writes state.db or kanban.db and never reads .env, auth.json or any credential store.
The guard only ever BLOCKS or adds an idempotency key; it never approves anything.
With ``team.gods`` empty (the default) this stays single-operator and does not invent teammates.
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
import sys
import threading
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Optional

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))
import team_flow  # noqa: E402

logger = logging.getLogger(__name__)

PLUGIN_ID = "bot-chat-agent"
DEFAULT_TITLE = "Bot Chat"
_CACHE_TTL = 30.0

PRESETS: dict[str, dict[str, bool]] = {
    "strict": {
        "secrets": True,
        "deletes": True,
        "destructive_git": True,
        "protected_push": True,
        "external_sends": True,
        "expensive_models": True,
        "one_question_per_turn": True,
        "kanban_lock": True,
        "wait_for_god": True,
    },
    "default": {
        "secrets": True,
        "deletes": True,
        "destructive_git": True,
        "protected_push": True,
        "external_sends": True,
        "expensive_models": True,
        "one_question_per_turn": True,
        "kanban_lock": True,
        "wait_for_god": True,
    },
    "relaxed": {
        "secrets": True,
        "deletes": False,
        "destructive_git": True,
        "protected_push": True,
        "external_sends": False,
        "expensive_models": False,
        "one_question_per_turn": False,
        "kanban_lock": True,
        "wait_for_god": False,
    },
}

DEFAULTS: dict[str, Any] = {
    "bot_chat_title": DEFAULT_TITLE,
    "language": "en",                 # en | es
    "style": True,                    # inject the agentic style contract
    "scope": "bot_chat",              # bot_chat | all  (where the guard applies)
    "preset": "default",              # strict | default | relaxed
    "guard": dict(PRESETS["default"]),
    "protected_branches": ["main", "master"],
    "expensive_model_patterns": [r"\bpro\b", r"opus", r"\bultra\b", r"-max\b"],
    "external_send_tools": ["send_message", "react_to_message"],
    # Empty gods = single-operator. Names are Hermes profile names, never hard-coded.
    "team": {"enabled": True, "gods": []},
    "kanban_db": "",
    "notify_state_path": "",
}

# ---------------------------------------------------------------- i18n
STYLE = {
    "en": (
        "[Bot Chat style]\n"
        "1. Act, don't narrate: do the work, then reply briefly with the result.\n"
        "2. Start with one short acknowledgement (\"On it — …\"), then act. "
        "Do not ask for OK on each safe step.\n"
        "3. Don't ask permission for safe, reversible, in-scope steps. Ask at most ONE "
        "question per turn, only when truly blocked.\n"
        "4. Keep replies short and plain; no inner monologue or tool-by-tool logs.\n"
        "5. Never reveal or paste secrets, tokens, keys, .env or auth files.\n"
        "6. Deleting, destructive git, pushing to protected branches, sending messages "
        "to the outside world and using expensive models require the user's explicit OK.\n"
        "7. If a tool is blocked by the safety guard, stop that step, tell the user in "
        "one line what needs approval, and continue with the rest.\n"
        "8. Report honestly: say what was verified and what was not.\n"
        "9. Follow the team note below. If it says single-operator, do the work in this chat.\n"
        "10. If it names teammates, assign a kanban subcard only to one of those profiles. "
        "If the note says to wait, do not close that card or a parent that still has it open "
        "until the board shows it done, then report the result in one or two lines.\n"
    ),
    "es": (
        "[Estilo Bot Chat]\n"
        "1. Actúa, no narres: haz el trabajo y responde breve con el resultado.\n"
        "2. Empieza con una línea corta («Voy a…») y actúa. No pidas OK en cada paso seguro.\n"
        "3. No pidas permiso para pasos seguros, reversibles y dentro del alcance. Haz como "
        "máximo UNA pregunta por turno, solo si estás bloqueado de verdad.\n"
        "4. Respuestas cortas y claras; sin monólogo interno ni logs de herramientas.\n"
        "5. Nunca muestres secretos, tokens, claves, .env ni archivos de auth.\n"
        "6. Borrar, git destructivo, push a ramas protegidas, enviar mensajes al exterior "
        "y usar modelos caros requieren el OK explícito del usuario.\n"
        "7. Si la guardia bloquea una herramienta, detén ese paso, di en una línea qué "
        "necesita aprobación y sigue con lo demás.\n"
        "8. Reporta con honestidad: qué se verificó y qué no.\n"
        "9. Sigue la nota de equipo. Si dice un solo operador, haz el trabajo en este chat.\n"
        "10. Si nombra compañeros, asigna una subtarjeta solo a uno de esos perfiles. "
        "Si la nota pide esperar, no cierres esa tarjeta ni un padre que aún la tenga abierta "
        "hasta que el tablero la muestre hecha, y luego cuenta el resultado en una o dos líneas.\n"
    ),
}
MSG = {
    "en": {
        "prefix": "[bot-chat-agent] Blocked: ",
        "suffix": " Ask the user for explicit approval before retrying.",
        "secrets": "this touches secrets or credential files.",
        "deletes": "this deletes files or data.",
        "destructive_git": "this is a destructive git operation.",
        "protected_push": "this pushes or merges into a protected branch.",
        "external_sends": "this sends a message to the outside world.",
        "expensive_models": "this uses an expensive model.",
        "one_question_per_turn": "only one question per turn; continue with your best judgement.",
        "kanban_lock": "this bypasses the kanban lock.",
        "wait_for_god": "this closes team work before the assignee delivers.",
    },
    "es": {
        "prefix": "[bot-chat-agent] Bloqueado: ",
        "suffix": " Pide aprobación explícita al usuario antes de reintentar.",
        "secrets": "esto toca secretos o archivos de credenciales.",
        "deletes": "esto borra archivos o datos.",
        "destructive_git": "esto es una operación git destructiva.",
        "protected_push": "esto hace push o merge a una rama protegida.",
        "external_sends": "esto envía un mensaje al exterior.",
        "expensive_models": "esto usa un modelo caro.",
        "one_question_per_turn": "solo una pregunta por turno; sigue con tu mejor criterio.",
        "kanban_lock": "esto se salta el candado del kanban.",
        "wait_for_god": "esto cierra el trabajo de equipo antes de que la persona asignada entregue.",
    },
}

# ---------------------------------------------------------------- config
_ctx: Any = None


def _cfg(key: str) -> Any:
    default = DEFAULTS[key]
    if _ctx is None or not hasattr(_ctx, "get_config"):
        return dict(default) if isinstance(default, dict) else default
    try:
        val = _ctx.get_config(key, None)
    except Exception:
        return dict(default) if isinstance(default, dict) else default
    if key == "guard":
        raw_preset = None
        try:
            raw_preset = _ctx.get_config("preset", None)
        except Exception:
            pass
        pname = str(raw_preset or (val if isinstance(val, str) else "default")).strip().lower()
        base = dict(PRESETS.get(pname, PRESETS["default"]))
        if isinstance(val, dict):
            for k in PRESETS["default"]:
                if k in val and isinstance(val[k], bool):
                    base[k] = val[k]
        return base
    if isinstance(default, dict) and isinstance(val, dict):
        merged = dict(default)
        merged.update(val)
        return merged
    return default if val is None else val


def _lang() -> str:
    lang = str(_cfg("language") or "en").lower()[:2]
    return lang if lang in STYLE else "en"


# ---------------------------------------------------------------- detection
_cache: dict[tuple[str, str, str, bool], tuple[bool, float]] = {}
_lock = threading.Lock()


def _state_db() -> Path:
    try:
        from hermes_constants import get_hermes_home  # documented home helper
        return Path(get_hermes_home()) / "state.db"
    except Exception:
        return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / "state.db"


def is_bot_chat(session_id: Optional[str], db_path: Optional[Path] = None,
                title: Optional[str] = None, include_subagents: bool = False) -> bool:
    """True if session_id is a canonical Bot Chat session (or its compaction/subagent child)."""
    sid = (session_id or "").strip() if isinstance(session_id, str) else ""
    if not sid:
        return False
    want = (title or str(_cfg("bot_chat_title")) or DEFAULT_TITLE).strip()
    db = db_path or _state_db()
    if not db.is_file():
        return False
    inc = bool(include_subagents)
    key = (str(db), sid, want, inc)
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit and hit[1] > now:
            return hit[0]
    result = False
    try:
        with closing(sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=1.5)) as conn:
            cur: Optional[str] = sid
            seen: set[str] = set()
            for _ in range(16):
                if not cur or cur in seen:
                    break
                seen.add(cur)
                row = conn.execute(
                    "SELECT title, parent_session_id, source FROM sessions WHERE id = ? LIMIT 1",
                    (cur,)).fetchone()
                if row is None:
                    break
                t, parent, source = (row[0] or "").strip(), row[1], (row[2] or "").lower()
                if source == "kanban":
                    break
                is_sub = source in ("subagent", "tool") or t.startswith("Subagent:")
                if is_sub:
                    if not inc:
                        break
                    cur = parent
                    continue
                if t == want:
                    result = True
                    break
                if t:
                    break
                cur = parent
    except Exception as exc:  # never break a turn
        logger.debug("%s: detection failed for %s: %s", PLUGIN_ID, sid, exc)
        return False
    with _lock:
        if len(_cache) > 512:
            _cache.clear()
        _cache[key] = (result, now + _CACHE_TTL)
    return result


def _in_scope(session_id: Optional[str], parent_session_id: Optional[str] = None) -> bool:
    if str(_cfg("scope")) == "all":
        return True
    return (is_bot_chat(session_id, include_subagents=True) or
            is_bot_chat(parent_session_id, include_subagents=True))


# ---------------------------------------------------------------- guard rules
_SECRET_RE = re.compile(
    r"(^|[\\/\s'\"=(,:;\[{])(\.env(\.(?!(?:example|sample|template|dist|schema)\b)[\w-]+)?|"
    r"auth\.json|credentials(\.json)?|id_rsa|id_ed25519|id_ecdsa|id_dsa|"
    r"\.npmrc|\.pypirc|\.netrc|[\w-]*secret[\w-]*\.(json|ya?ml|txt)|[\w-]+\.(pem|key|p12|pfx))"
    r"($|[\s'\"`,;:|&)\]}])", re.I)
_SECRET_CMD_RE = re.compile(
    r"(?:^|[;&|()\n]\s*)(printenv|env\s*(?:-[0i]|--null)?\s*(?:$|[;&|>\n])|"
    r"set\s*(?:$|[;&|>\n])|export\s+-p\b|declare\s+-[a-z]*x[a-z]*p\b|"
    r"(?:Get-ChildItem|gci|dir)\s+env:)", re.I)
_DELETE_RE = re.compile(
    r"((?:^|[;&|()\n]\s*|\b(sudo|xargs)\s+)(?:rm\s+-\w*[rf]|rm\s+\S|rmdir\b|del\s|erase\s|"
    r"Remove-Item\b|ri\s|shred\b|unlink\b|rimraf\b|format\s+[a-z]:)|"
    r"\bfind\b[^\n]*\s-delete\b|\bgit\s+rm\b|"
    r"\bDROP\s+(TABLE|DATABASE|SCHEMA)\b|\bTRUNCATE\s+TABLE\b|\bDELETE\s+FROM\b|"
    r"shutil\.rmtree|os\.remove|os\.unlink|\.unlink\()", re.I)
# Short options before a git subcommand. The alternatives are disjoint
# (`-C`/`-c <arg>` vs every other short option) and the repeat is bounded:
# an overlapping unbounded group backtracks exponentially on `git -C -C …`
# and stalls the hook, which holds the GIL so a hook timeout cannot stop it.
_GIT_OPT_PREFIX = r"(?:-[Cc]\s+\S+\s+|-(?![Cc]\s)\S+\s+){0,8}"
_GIT_DESTRUCTIVE_RE = re.compile(
    r"\bgit\s+" + _GIT_OPT_PREFIX +
    r"(push\s+[^\n]*(--force\b|-f\b|--force-with-lease)|reset\s+--hard|clean\s+-\w*f|"
    r"branch\s+-D|checkout\s+--\s|restore\s+[^\n]*--source|rebase\b|filter-branch|"
    r"push\s+[^\n]*--delete|reflog\s+expire|gc\s+--prune|stash\s+(?:drop|clear)\b)", re.I)


def _protected_push(cmd: str) -> bool:
    branches = [re.escape(str(b)) for b in (_cfg("protected_branches") or []) if str(b).strip()]
    if not branches:
        return False
    alt = "|".join(branches)
    if re.search(
            r"\bgit\s+" + _GIT_OPT_PREFIX +
            rf"push\b[^\n]*(?:\s|:|refs/heads/|origin/)({alt})(?=\s|$|[;&|)])",
            cmd, re.I):
        return True
    if re.search(rf"\bgit\s+merge\b", cmd, re.I) and re.search(
            rf"\bgit\s+(checkout|switch)\s+({alt})\b", cmd, re.I):
        return True
    if re.search(rf"\bgh\s+pr\s+merge\b", cmd, re.I):
        return True
    return False


def _text_args(args: Any) -> str:
    if isinstance(args, dict):
        return " ".join(_text_args(v) for v in args.values())
    if isinstance(args, (list, tuple)):
        return " ".join(_text_args(v) for v in args)
    return str(args) if args is not None else ""


_questions: dict[str, str] = {}


def check_tool_call(tool_name: str, args: Any, turn_key: str = "") -> Optional[str]:
    """Return the name of the violated rule, or None if the call is allowed."""
    g = _cfg("guard")
    name = (tool_name or "").strip()
    lname = name.lower()
    a = args if isinstance(args, dict) else {}
    text = _text_args(args)
    cmd = str(a.get("command") or a.get("cmd") or a.get("code") or "") if a else ""
    path = str(a.get("path") or a.get("file_path") or a.get("filename") or a.get("target_file") or a.get("file") or "")

    if g.get("secrets") and (_SECRET_RE.search(" " + path + " ") or
                             (cmd and (_SECRET_RE.search(" " + cmd + " ") or _SECRET_CMD_RE.search(cmd)))):
        return "secrets"
    if cmd:
        if g.get("destructive_git") and _GIT_DESTRUCTIVE_RE.search(cmd):
            return "destructive_git"
        if g.get("protected_push") and _protected_push(cmd):
            return "protected_push"
        if g.get("deletes") and _DELETE_RE.search(cmd):
            return "deletes"
    if g.get("deletes") and (lname in {"delete_file", "remove_file", "rm"} or
                             (lname == "write_file" and a.get("delete"))):
        return "deletes"
    if g.get("external_sends"):
        sends = {str(t).lower() for t in (_cfg("external_send_tools") or [])}
        if lname in sends or (lname.startswith("mcp_") and re.search(
                r"(send|post|reply|publish|tweet|email|message_create|create_message)", lname)):
            return "external_sends"
    if g.get("expensive_models") and lname in {"delegate_task", "spawn_agent", "set_model"}:
        model = str(a.get("model") or a.get("model_name") or "") or text
        for pat in _cfg("expensive_model_patterns") or []:
            try:
                if re.search(str(pat), model, re.I):
                    return "expensive_models"
            except re.error:
                continue
    if g.get("one_question_per_turn") and lname == "clarify" and turn_key:
        with _lock:
            if _questions.get(turn_key):
                return "one_question_per_turn"
            if len(_questions) > 1024:
                _questions.clear()
            _questions[turn_key] = "asked"
    return None


def block_message(rule: str, detail: str = "") -> str:
    m = MSG[_lang()]
    text = m["prefix"] + m.get(rule, rule) + m["suffix"]
    extra = str(detail or "").strip()
    if extra:
        text += " " + extra
    return text


def _hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home  # documented home helper
        return Path(get_hermes_home())
    except Exception:
        return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))


def _team() -> dict[str, Any]:
    raw = _cfg("team")
    if not isinstance(raw, dict):
        raw = {}
    enabled = bool(raw.get("enabled", True))
    gods = team_flow.normalize_gods(raw.get("gods"))
    return {"enabled": enabled, "gods": gods if enabled else []}


def _kanban_db_path() -> Optional[Path]:
    raw = str(_cfg("kanban_db") or "").strip()
    if raw:
        return Path(raw).expanduser()
    env = os.environ.get("HERMES_KANBAN_DB", "").strip()
    if env:
        return Path(env).expanduser()
    home = _hermes_home()
    board = os.environ.get("HERMES_KANBAN_BOARD", "").strip()
    if board and board.lower() != "default":
        return home / "kanban" / "boards" / board / "kanban.db"
    return home / "kanban.db"


def _notify_path() -> Path:
    raw = str(_cfg("notify_state_path") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return _hermes_home() / "bot-chat-agent" / "team-notify.json"


# ---------------------------------------------------------------- hooks
def _on_pre_llm_call(session_id: str = "", **_: Any) -> Optional[dict]:
    if not _cfg("style") or not is_bot_chat(session_id):
        return None
    lang = _lang()
    parts = [STYLE[lang].rstrip()]
    try:
        team = _team()
        guard = _cfg("guard")
        parts.append(team_flow.team_note(
            lang=lang,
            enabled=bool(team["enabled"]),
            gods=list(team["gods"]),
            wait_for_god=bool(guard.get("wait_for_god")),
            session_id=session_id or "",
            db_path=_kanban_db_path(),
            notify_path=_notify_path() if team["gods"] else None,
        ))
    except Exception as exc:
        logger.debug("%s: team note skipped: %s", PLUGIN_ID, exc)
    return {"context": "\n\n".join(part for part in parts if part)}


def _on_pre_tool_call(tool_name: str = "", args: Any = None, session_id: str = "",
                      task_id: str = "", turn_id: str = "", **kw: Any) -> Optional[dict]:
    try:
        if args is None:
            args = kw.get("tool_args") or kw.get("arguments") or {}
        if not _in_scope(session_id, kw.get("parent_session_id")):
            return None
        rule = check_tool_call(tool_name, args, f"{session_id}:{turn_id or task_id}")
        if rule:
            logger.info("%s: blocked %s (%s)", PLUGIN_ID, tool_name, rule)
            return {"action": "block", "message": block_message(rule)}
        team = _team()
        guard = _cfg("guard")
        decision = team_flow.evaluate(
            tool_name, args,
            kanban_lock=bool(guard.get("kanban_lock")),
            wait_for_god=bool(guard.get("wait_for_god")),
            gods=list(team["gods"]),
            db_path=_kanban_db_path(),
            session_id=session_id or "",
            worker_task=os.environ.get("HERMES_KANBAN_TASK", "").strip(),
        )
        if decision.get("rule"):
            logger.info("%s: blocked %s (%s)", PLUGIN_ID, tool_name, decision["rule"])
            return {"action": "block", "message": block_message(decision["rule"], decision.get("detail") or "")}
        if decision.get("modify"):
            return {"action": "modify", "args": decision["modify"]}
    except Exception as exc:
        logger.debug("%s: guard error: %s", PLUGIN_ID, exc)
    return None


def _on_post_tool_call(tool_name: str = "", args: Any = None, result: Any = None,
                       session_id: str = "", status: str = "", **kw: Any) -> None:
    try:
        team = _team()
        if not team["gods"]:
            return None
        if str(status or kw.get("status") or "").lower() in {"error", "blocked"}:
            return None
        if (tool_name or "").strip().lower() != "kanban_create":
            return None
        if not _in_scope(session_id, kw.get("parent_session_id")):
            return None
        if args is None:
            args = kw.get("tool_args") or kw.get("arguments") or {}
        data = args if isinstance(args, dict) else {}
        team_flow.remember_create(
            _notify_path(),
            session_id=session_id or "",
            title=str(data.get("title") or ""),
            assignee=str(data.get("assignee") or ""),
            idempotency=str(data.get("idempotency_key") or data.get("idempotencyKey") or ""),
            result=result,
            gods=list(team["gods"]),
        )
    except Exception as exc:
        logger.debug("%s: create memory skipped: %s", PLUGIN_ID, exc)
    return None


def _on_kanban_completed(task_id: str = "", assignee: str = "", summary: str = "", **kw: Any) -> None:
    try:
        team = _team()
        if not team["gods"]:
            return None
        team_flow.record_delivery(
            _notify_path(),
            task_id=str(task_id or kw.get("task_id") or ""),
            assignee=str(assignee or kw.get("assignee") or ""),
            summary=str(summary or kw.get("summary") or kw.get("result") or ""),
            gods=list(team["gods"]),
        )
    except Exception as exc:
        logger.debug("%s: delivery note skipped: %s", PLUGIN_ID, exc)
    return None


def register(ctx: Any) -> None:
    global _ctx
    _ctx = ctx
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_hook("kanban_task_completed", _on_kanban_completed)
