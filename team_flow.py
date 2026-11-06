"""Team flow for Bot Chat: single-operator unless real profile names are configured.

Kanban reads are read-only. Profile names come from config, never from a pantheon
hard-coded in this plugin. The god's own dispatcher worker is recognized only by
``HERMES_KANBAN_TASK`` matching the card it completes.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
from contextlib import closing
from pathlib import Path
from typing import Any, Optional

_LOCK = threading.Lock()
_TASK_FIELDS = (
    "id", "title", "assignee", "status", "result",
    "started_at", "session_id", "idempotency_key",
)
_CLOSE_TOOLS = {"kanban_complete", "kanban_request_review", "kanban_archive"}
_WRITE_TOOLS = {"write_file", "patch", "delete_file", "remove_file", "rm", "str_replace", "apply_patch"}
_SPOOF_KEYS = (
    "impersonate", "as_user", "as_user_id", "pretend_user",
    "on_behalf_of", "act_as_user", "pre_approved", "preapproved", "already_approved",
)
_USERISH = {"user", "human", "the user", "the_user"}
_PREFINISHED = {"done", "archived", "review", "complete", "completed", "approved"}
_DB_PATH_RE = re.compile(r"kanban\.db|kanban[/\\]+boards[/\\]", re.I)
_PY_OR_SQL_RE = re.compile(r"\b(sqlite3|python3?)\b", re.I)
_SQL_WRITE_RE = re.compile(r"\b(INSERT|UPDATE|DELETE|DROP|REPLACE|ALTER|CREATE|ATTACH)\b", re.I)
_RM_RE = re.compile(r"\b(rm|unlink|rmdir|del|Remove-Item|shutil\.rmtree|os\.remove)\b", re.I)
_CLI_RE = re.compile(r"\bhermes\s+kanban\b([^\n;&|]*)", re.I)
_CLI_MUTATE_RE = re.compile(
    r"\b(complete|create|assign|reassign|link|unlink|unblock|block|archive|"
    r"delete|rm|comment|attach|init|edit|update|move)\b",
    re.I,
)
_SPOOF_CMD_RE = re.compile(
    r"(--as-user\b|--impersonate\b|\bimpersonat\w*|"
    r"--created-by\s+['\"]?(user|human)\b|"
    r"\bcreated_by\s*[:=]\s*['\"]?(user|human)\b)",
    re.I,
)
_TASK_ID_RE = re.compile(r"\b(t_[A-Za-z0-9_-]{4,})\b")


def normalize_gods(raw: Any) -> list[str]:
    if isinstance(raw, str):
        parts = [p.strip() for p in raw.split(",")]
    elif isinstance(raw, (list, tuple)):
        parts = [str(p).strip() for p in raw]
    else:
        parts = []
    out: list[str] = []
    seen: set[str] = set()
    for name in parts:
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(name)
    return out


def god_keys(names: list[str]) -> set[str]:
    return {n.lower() for n in names}


def _is_god(name: Any, keys: set[str]) -> bool:
    return str(name or "").strip().lower() in keys


def _norm_title(title: Any) -> str:
    return re.sub(r"\s+", " ", str(title or "").strip().lower())


def idempotency_key(session_id: str, assignee: str, title: str) -> str:
    raw = "|".join([
        (session_id or "").strip(),
        str(assignee or "").strip().lower(),
        _norm_title(title),
    ])
    return "bca-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _one_line(text: Any, limit: int = 240) -> str:
    line = str(text or "").replace("\r", " ").split("\n", 1)[0].strip()
    if len(line) > limit:
        return line[: limit - 1].rstrip() + "…"
    return line


def _args(args: Any) -> dict[str, Any]:
    return args if isinstance(args, dict) else {}


def _task_id(args: dict[str, Any]) -> str:
    for key in ("task_id", "id", "task"):
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    ids = args.get("ids")
    if isinstance(ids, (list, tuple)) and ids and isinstance(ids[0], str) and ids[0].strip():
        return ids[0].strip()
    return ""


def _paths(args: dict[str, Any]) -> list[str]:
    found = []
    for key in ("path", "file_path", "filename", "target_file", "file", "destination"):
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            found.append(val)
    return found


def _blob(args: dict[str, Any]) -> str:
    return "\n".join(str(args.get(k) or "") for k in ("command", "cmd", "code", "script"))


def _truthy(val: Any) -> bool:
    if isinstance(val, str):
        return val.strip().lower() not in {"", "0", "false", "no", "off"}
    return bool(val)


def lock_detail(tool_name: str, args: Any) -> Optional[str]:
    """Return a short reason when the call bypasses the kanban lock."""
    name = (tool_name or "").strip().lower()
    a = _args(args)
    for key in _SPOOF_KEYS:
        if key in a and _truthy(a.get(key)):
            return "Impersonating the user or pre-approving the card is blocked."
    for key in ("created_by", "actor", "author"):
        if str(a.get(key) or "").strip().lower() in _USERISH:
            return "Claiming to be the user is blocked."
    blob = _blob(a)
    if blob and (_SPOOF_CMD_RE.search(blob) or _db_write(blob)):
        return "Direct kanban.db writes and hermes kanban mutations are blocked."
    if blob:
        for _match in _CLI_RE.finditer(blob):
            if _CLI_MUTATE_RE.search(_match.group(1) or ""):
                return "Direct kanban.db writes and hermes kanban mutations are blocked."
    if name in _WRITE_TOOLS and any(_DB_PATH_RE.search(p) for p in _paths(a)):
        return "Writing kanban.db directly is blocked."
    if name == "kanban_create":
        status = str(a.get("status") or "").strip().lower()
        if status in _PREFINISHED:
            return "Creating a card that is already finished is blocked."
        if _truthy(a.get("result")):
            return "Creating a card with a finished result is blocked."
    return None


def _db_write(blob: str) -> bool:
    if not _DB_PATH_RE.search(blob):
        return False
    return bool(_PY_OR_SQL_RE.search(blob) or _SQL_WRITE_RE.search(blob) or _RM_RE.search(blob))


def _idempotency_patch(args: dict[str, Any], session_id: str) -> Optional[dict[str, str]]:
    existing = args.get("idempotency_key")
    if existing is None:
        existing = args.get("idempotencyKey")
    if isinstance(existing, str) and existing.strip():
        return None
    return {"idempotency_key": idempotency_key(session_id, str(args.get("assignee") or ""), str(args.get("title") or ""))}


class _Board:
    def __init__(self, kind: str, conn: Optional[sqlite3.Connection] = None):
        self.kind = kind  # ok | missing | unreadable
        self.conn = conn


def open_board(path: Optional[Path]) -> _Board:
    if path is None or not Path(path).is_file():
        return _Board("missing")
    try:
        conn = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True, timeout=1.5)
        conn.row_factory = sqlite3.Row
        return _Board("ok", conn)
    except sqlite3.Error:
        return _Board("unreadable")


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1", (table,)
    ).fetchone()
    return row is not None


def _row_dict(row: sqlite3.Row, fields: tuple[str, ...]) -> dict[str, Any]:
    keys = set(row.keys())
    return {name: row[name] for name in fields if name in keys}


def _fetch_task(conn: sqlite3.Connection, task_id: str) -> Optional[dict[str, Any]]:
    if not task_id or not _has_table(conn, "tasks"):
        return None
    have = _columns(conn, "tasks")
    fields = [name for name in _TASK_FIELDS if name in have]
    if "id" not in fields:
        return None
    sql = f"SELECT {', '.join(fields)} FROM tasks WHERE id = ? LIMIT 1"
    row = conn.execute(sql, (task_id,)).fetchone()
    if row is None:
        return None
    return _row_dict(row, _TASK_FIELDS)


def _children(conn: sqlite3.Connection, parent_id: str) -> list[dict[str, Any]]:
    if not _has_table(conn, "task_links") or not _has_table(conn, "tasks"):
        return []
    link_cols = _columns(conn, "task_links")
    if "parent_id" not in link_cols or "child_id" not in link_cols:
        return []
    have = _columns(conn, "tasks")
    fields = [name for name in ("id", "title", "assignee", "status", "result") if name in have]
    if "id" not in fields:
        return []
    select = ", ".join(f"c.{name}" for name in fields)
    rows = conn.execute(
        f"SELECT {select} FROM task_links l JOIN tasks c ON c.id = l.child_id WHERE l.parent_id = ?",
        (parent_id,),
    ).fetchall()
    return [_row_dict(row, ("id", "title", "assignee", "status", "result")) for row in rows]


def _status(row: Optional[dict[str, Any]]) -> str:
    if not row:
        return ""
    return str(row.get("status") or "").strip().lower()


def _open_descendant(conn: sqlite3.Connection, task_id: str, keys: set[str], depth: int = 6) -> Optional[dict[str, Any]]:
    seen: set[str] = set()

    def walk(tid: str, left: int) -> Optional[dict[str, Any]]:
        if left < 0 or not tid or tid in seen:
            return None
        seen.add(tid)
        for child in _children(conn, tid):
            cid = str(child.get("id") or "")
            if _is_god(child.get("assignee"), keys) and _status(child) != "done":
                return child
            found = walk(cid, left - 1)
            if found:
                return found
        return None

    return walk(task_id, depth)


def _wait_detail(tool_name: str, args: dict[str, Any], keys: set[str], board: _Board,
                 worker_task: str) -> Optional[str]:
    if (tool_name or "").strip().lower() not in _CLOSE_TOOLS or not keys:
        return None
    task_id = _task_id(args)
    if worker_task and task_id and worker_task == task_id:
        return None
    assignee_arg = str(args.get("assignee") or "").strip()
    if board.kind != "ok":
        if _is_god(assignee_arg, keys):
            who = assignee_arg
            ident = task_id or "card"
            return f"Waiting for {who} to deliver {ident}."
        return None
    assert board.conn is not None
    root = _fetch_task(board.conn, task_id) if task_id else None
    assignee = str((root or {}).get("assignee") or assignee_arg).strip()
    if _is_god(assignee, keys) and _status(root) != "done":
        who = assignee or assignee_arg
        ident = str((root or {}).get("id") or task_id or "card")
        return f"Waiting for {who} to deliver {ident}."
    if task_id:
        hit = _open_descendant(board.conn, task_id, keys)
        if hit:
            return f"Waiting for {hit.get('assignee')} to deliver {hit.get('id')}."
    return None


def evaluate(tool_name: str, args: Any, *, kanban_lock: bool, wait_for_god: bool,
             gods: list[str], db_path: Optional[Path], session_id: str,
             worker_task: str = "") -> dict[str, Any]:
    """One decision for the shared pre_tool_call: block, modify, or allow."""
    a = _args(args)
    keys = god_keys(gods)
    if kanban_lock:
        detail = lock_detail(tool_name, a)
        if detail:
            return {"rule": "kanban_lock", "detail": detail, "modify": None}
    if wait_for_god and keys:
        board = open_board(db_path)
        try:
            detail = _wait_detail(tool_name, a, keys, board, worker_task)
        except sqlite3.Error:
            detail = None
            if _is_god(a.get("assignee"), keys):
                detail = f"Waiting for {a.get('assignee')} to deliver {_task_id(a) or 'card'}."
        finally:
            if board.conn is not None:
                board.conn.close()
        if detail:
            return {"rule": "wait_for_god", "detail": detail, "modify": None}
    if kanban_lock and (tool_name or "").strip().lower() == "kanban_create":
        patch = _idempotency_patch(a, session_id)
        if patch:
            return {"rule": None, "detail": "", "modify": patch}
    return {"rule": None, "detail": "", "modify": None}


def _empty_state() -> dict[str, Any]:
    return {"pending": {}, "announced": [], "creates": []}


def _load_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return _empty_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty_state()
    if not isinstance(data, dict):
        return _empty_state()
    pending = data.get("pending") if isinstance(data.get("pending"), dict) else {}
    announced = data.get("announced") if isinstance(data.get("announced"), list) else []
    creates = data.get("creates") if isinstance(data.get("creates"), list) else []
    return {"pending": pending, "announced": announced, "creates": creates}


def _save_state(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def record_delivery(path: Optional[Path], *, task_id: str, assignee: str, summary: str,
                    gods: list[str]) -> None:
    if path is None or not str(task_id or "").strip():
        return
    if not _is_god(assignee, god_keys(gods)):
        return
    with _LOCK:
        data = _load_state(path)
        data["pending"][str(task_id).strip()] = {
            "assignee": str(assignee).strip(),
            "summary": _one_line(summary),
        }
        _save_state(path, data)


def take_announcements(path: Optional[Path]) -> list[dict[str, str]]:
    if path is None or not path.is_file():
        return []
    with _LOCK:
        data = _load_state(path)
        announced = {str(item) for item in data["announced"]}
        fresh: list[dict[str, str]] = []
        for task_id, rec in data["pending"].items():
            if task_id in announced or not isinstance(rec, dict):
                continue
            fresh.append({
                "id": str(task_id),
                "assignee": str(rec.get("assignee") or ""),
                "summary": _one_line(rec.get("summary")),
            })
            data["announced"].append(task_id)
        data["announced"] = data["announced"][-500:]
        if fresh:
            _save_state(path, data)
        return fresh


def extract_task_id(result: Any) -> str:
    if isinstance(result, dict):
        for key in ("id", "task_id", "task"):
            val = result.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    match = _TASK_ID_RE.search(str(result or ""))
    return match.group(1) if match else ""


def remember_create(path: Optional[Path], *, session_id: str, title: str, assignee: str,
                    idempotency: str, result: Any, gods: list[str]) -> None:
    if path is None or not _is_god(assignee, god_keys(gods)):
        return
    rec = {
        "session_id": session_id or "",
        "title": _one_line(title, 120),
        "assignee": str(assignee).strip(),
        "task_id": extract_task_id(result),
        "key": idempotency or "",
    }
    with _LOCK:
        data = _load_state(path)
        data["creates"] = [item for item in data["creates"] if isinstance(item, dict)]
        data["creates"].append(rec)
        data["creates"] = data["creates"][-200:]
        _save_state(path, data)


def _open_for_session(conn: sqlite3.Connection, session_id: str, keys: set[str]) -> list[dict[str, Any]]:
    if not session_id or not _has_table(conn, "tasks"):
        return []
    have = _columns(conn, "tasks")
    if "session_id" not in have or "id" not in have:
        return []
    fields = [name for name in ("id", "title", "assignee", "status", "result") if name in have]
    sql = f"SELECT {', '.join(fields)} FROM tasks WHERE session_id = ?"
    rows = conn.execute(sql, (session_id,)).fetchall()
    open_rows = []
    for row in rows:
        item = _row_dict(row, ("id", "title", "assignee", "status", "result"))
        if _is_god(item.get("assignee"), keys) and _status(item) != "done":
            open_rows.append(item)
    return open_rows[:8]


def _card_line(row: dict[str, Any]) -> str:
    title = _one_line(row.get("title") or "card", 80)
    status = _status(row) or "open"
    return f"{row.get('id')} {row.get('assignee')} — {title} ({status})"


def team_note(*, lang: str, enabled: bool, gods: list[str], wait_for_god: bool,
              session_id: str, db_path: Optional[Path], notify_path: Optional[Path]) -> str:
    """Short turn note. Empty gods, or team disabled, stays single-operator."""
    es = lang == "es"
    if not enabled or not gods:
        if es:
            return "[Equipo]\nUn solo operador: no hay compañeros configurados. Haz el trabajo en este chat."
        return "[Team]\nSingle-operator: no teammates configured. Do the work in this chat."
    names = ", ".join(gods)
    if es:
        lines = [f"[Equipo]\nCompañeros que puedes asignar: {names}. No inventes otros perfiles."]
    else:
        lines = [f"[Team]\nTeammates you may assign: {names}. Do not invent other profile names."]
    if wait_for_god:
        lines.append(
            "No cierres esa tarjeta ni a su padre hasta que el tablero la muestre hecha. "
            "Luego cuenta el resultado en una o dos líneas."
            if es else
            "Do not close that card or its parent until the board shows it done. "
            "Then tell the user the result in one or two lines."
        )
    keys = god_keys(gods)
    board = open_board(db_path)
    try:
        if board.kind == "ok" and board.conn is not None:
            for row in _open_for_session(board.conn, session_id, keys):
                prefix = "Abierta" if es else "Open"
                lines.append(f"{prefix}: {_card_line(row)}")
        elif board.kind == "missing":
            lines.append(
                "No hay tablero kanban en la ruta configurada; no inventes entregas."
                if es else
                "No kanban board at the configured path; do not invent a delivery."
            )
    except sqlite3.Error:
        lines.append("Kanban board unreadable; do not invent a delivery." if not es else
                     "El tablero kanban no se pudo leer; no inventes una entrega.")
    finally:
        if board.conn is not None:
            board.conn.close()
    for item in take_announcements(notify_path):
        if es:
            lines.append(
                f"Entregado: {item['id']} por {item['assignee']} — {item['summary']}. "
                "Díselo al usuario en una o dos líneas y para."
            )
        else:
            lines.append(
                f"Delivered: {item['id']} by {item['assignee']} — {item['summary']}. "
                "Tell the user in one or two lines, then stop."
            )
    return "\n".join(lines)
