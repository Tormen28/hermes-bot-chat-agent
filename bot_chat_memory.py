"""Generic short-term and long-term memory for Bot Chat, stored locally.

No external vault, no network, no model. Works for any user with only the
plugin's SQLite file under ``<HERMES_HOME>/bot-chat-agent/memory.db``.

Short-term: a rolling extractive summary of older utterances plus a
fixed window of recent user/assistant turns (tool payloads are skipped)
and the existing pending-asks reminder. Long-term: durable decisions,
project-state notes and lessons, retrieved only when they overlap the
current request, under a token budget.

Every public function fails open (returns empty / None) on error.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable, Optional

import context_budget
import dbscope

try:
    from untrusted import neutralize as _neutralize
except Exception:  # pragma: no cover - sibling missing
    def _neutralize(value, max_chars=600):
        return " ".join(str(value or "").split())[:max_chars]


DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "short_term": True,
    "long_term": True,
    "recent_messages": 8,
    "summary_budget_tokens": 400,
    "long_term_budget_tokens": 400,
    "max_entries": 500,
    "recent_budget_tokens": 400,
}

KINDS = ("decision", "project_state", "lesson")

_HEAD = {
    "en": {
        "summary": "[Session summary]",
        "recent": "[Recent]",
        "ltm": "[Long-term memory]",
    },
    "es": {
        "summary": "[Resumen de sesión]",
        "recent": "[Reciente]",
        "ltm": "[Memoria larga]",
    },
}

_WORD_RE = re.compile(r"[a-z0-9áéíóúüñ]{3,}", re.I)
_SECRET_RE = re.compile(
    r"(?i)(sk-[A-Za-z0-9_\-]{8,}|api[_-]?key\b|bearer\s+\S{8,}"
    r"|-----BEGIN |xox[baprs]-|password\s*[:=]|secret\s*[:=])"
)

_CUES: dict[str, tuple[re.Pattern[str], ...]] = {
    "decision": (
        re.compile(r"(?i)\bdecisi[oó]n\s*:\s*(.+)"),
        re.compile(r"(?i)\bwe decided(?:\s+to)?\s+(.+)"),
        re.compile(r"(?i)\bdecidimos\s+(.+)"),
        re.compile(r"(?i)\bqueda decidido\s*:?\s*(.+)"),
        re.compile(r"(?i)\bla decisión es\s*:?\s*(.+)"),
    ),
    "lesson": (
        re.compile(r"(?i)\blesson learned\s*:\s*(.+)"),
        re.compile(r"(?i)\blesson\s*:\s*(.+)"),
        re.compile(r"(?i)\blección\s*:\s*(.+)"),
        re.compile(r"(?i)\baprendimos(?:\s+que)?\s+(.+)"),
    ),
    "project_state": (
        re.compile(r"(?i)\bproject state\s*:\s*(.+)"),
        re.compile(r"(?i)\bestado del proyecto\s*:\s*(.+)"),
        re.compile(r"(?i)\bcurrent status\s*:\s*(.+)"),
    ),
}

# Explicit "keep this in mind" requests and decision cues in other languages. A user typing
# "Remember that I prefer short answers" expects it to stick; these only fire on the USER's own
# words (an assistant saying "I will remember that ..." must not write memory).
_USER_CUES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("decision", re.compile(r"(?i)(?:^|(?<=[.!?])\s+)remember(?:\s+that)?\s*[:,]?\s+(.+)")),
    ("decision", re.compile(r"(?i)(?:^|(?<=[.!?])\s+)(?:please\s+)?keep in mind(?:\s+that)?\s*[:,]?\s+(.+)")),
    ("decision", re.compile(r"(?i)\bfrom now on\s*,?\s+(.+)")),
    ("decision", re.compile(r"(?i)(?:^|(?<=[.!?])\s+)recuerda(?:\s+que)?\s*[:,]?\s+(.+)")),
    ("decision", re.compile(r"(?i)(?:^|(?<=[.!?])\s+)(?:ten en cuenta|no olvides)(?:\s+que)?\s*[:,]?\s+(.+)")),
    ("decision", re.compile(r"(?i)\bde ahora en adelante\s*,?\s+(.+)")),
    ("decision", re.compile(r"(?i)\bd[ée]cision\s*:\s*(.+)")),      # fr
    ("decision", re.compile(r"(?i)\bnous avons d[ée]cid[ée](?:\s+de)?\s+(.+)")),
    ("decision", re.compile(r"(?i)\bdecis[aã]o\s*:\s*(.+)")),       # pt
    ("decision", re.compile(r"(?i)\bdecidimos\s+(.+)")),
    ("decision", re.compile(r"(?i)\bentscheidung\s*:\s*(.+)")),      # de
    ("decision", re.compile(r"(?i)\bwir haben (?:uns )?entschieden(?:,)?\s+(.+)")),
    ("lesson", re.compile(r"(?i)\bli[cç][aã]o\s*:\s*(.+)")),         # pt
    ("lesson", re.compile(r"(?i)\bli[cç]on\s*:\s*(.+)")),            # fr (leçon)
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  text TEXT NOT NULL,
  keywords TEXT NOT NULL DEFAULT '',
  session_id TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS mem_updated ON memories(updated_at);

CREATE TABLE IF NOT EXISTS session_aux (
  session_id TEXT PRIMARY KEY,
  pending TEXT NOT NULL DEFAULT '[]',
  last_recalled TEXT NOT NULL DEFAULT '[]',
  last_tokens INTEGER NOT NULL DEFAULT 0,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS session_summary (
  session_id TEXT PRIMARY KEY,
  text TEXT NOT NULL DEFAULT '',
  covered INTEGER NOT NULL DEFAULT 0,
  updated_at REAL NOT NULL
);
"""

_SCHEMA_VERSION = 1

_STOP = {
    "the", "and", "for", "of", "is", "in", "it", "to", "we", "you", "a",
    "an", "on", "at", "as", "be", "or", "by", "this", "that", "with",
    "from", "are", "was", "were", "what", "where", "when", "how", "who",
    "should", "would", "could", "about", "please", "tell", "give", "does",
    "do", "did", "not", "yes", "no", "una", "uno", "que", "los", "las",
    "del", "por", "para", "con", "como", "está", "esto", "esta", "the",
}


def normalize_cfg(raw: Any) -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    if isinstance(raw, dict):
        for key, default in DEFAULTS.items():
            if key not in raw:
                continue
            val = raw[key]
            try:
                if isinstance(default, bool):
                    cfg[key] = bool(val)
                elif isinstance(default, int):
                    cfg[key] = max(0, int(val))
                else:
                    cfg[key] = val
            except (TypeError, ValueError):
                pass
    return cfg


def looks_like_secret(text: str) -> bool:
    return bool(text and _SECRET_RE.search(text))


def _tokens(text: str) -> list[str]:
    return [w.lower() for w in _WORD_RE.findall(text or "")]


def _stem(word: str) -> str:
    w = word.lower()
    for suf in ("mente", "ciones", "ción", "ing", "ers", "ies", "ed", "es", "s"):
        if len(w) > len(suf) + 3 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def _stems(text: str) -> set[str]:
    return {_stem(w) for w in _tokens(text) if w not in _STOP and len(w) >= 3}


def _text_of(content: Any) -> str:
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


def _is_final_assistant(msg: dict) -> bool:
    return (
        str(msg.get("role") or "") == "assistant"
        and not msg.get("tool_calls")
        and bool(_text_of(msg.get("content")).strip())
    )


def recent_utterances(messages: Iterable[Any], n: int = 8) -> list[str]:
    """Last ``n`` user/final-assistant lines; tool payloads are skipped."""
    out: list[str] = []
    try:
        for msg in messages or []:
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role") or "")
            text = " ".join(_text_of(msg.get("content")).split())
            if not text:
                continue
            if role == "user":
                out.append(f"U: {text}")
            elif _is_final_assistant(msg):
                out.append(f"A: {text}")
    except Exception:
        return []
    return out[-max(0, int(n or 0)) :]


def _sentence(text: str, limit: int = 240) -> str:
    text = " ".join((text or "").split())
    for sep in (". ", "! ", "? ", "\n"):
        if sep in text:
            text = text.split(sep, 1)[0] + ("." if sep.strip() == "." else "")
            break
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text.strip()


def extract_memories(user_text: str, assistant_text: str) -> list[tuple[str, str]]:
    """Pull decision / lesson / project-state cues out of a turn (no model)."""
    blob = " ".join(p for p in (user_text or "", assistant_text or "") if p).strip()
    if not blob or looks_like_secret(blob):
        return []
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    ublob = " ".join((user_text or "").split())
    for kind, cue in _USER_CUES:
        for match in cue.finditer(ublob):
            body = _sentence(match.group(1))
            if len(body) < 8 or looks_like_secret(body):
                continue
            key = f"{kind}:{body.lower()}"
            if key in seen:
                continue
            seen.add(key)
            found.append((kind, body))
    for kind, cues in _CUES.items():
        for cue in cues:
            for match in cue.finditer(blob):
                body = _sentence(match.group(1))
                if len(body) < 8 or looks_like_secret(body):
                    continue
                key = f"{kind}:{body.lower()}"
                if key in seen:
                    continue
                seen.add(key)
                found.append((kind, body))
    return found


class MemoryStore:
    """SQLite-backed memory; one short-lived connection per operation."""

    def __init__(self, path: Any):
        self.path = Path(path)

    _CORRUPT_MARKERS = ("not a database", "malformed", "disk image", "file is encrypted")

    def _quarantine(self) -> None:
        """Move an unreadable store aside (never delete the user's file) so memory starts fresh
        instead of being silently dead forever."""
        stamp = time.strftime("%Y%m%dT%H%M%S")
        for suffix in ("", "-wal", "-shm"):
            src = Path(str(self.path) + suffix)
            if src.exists():
                try:
                    src.replace(Path(f"{self.path}.corrupt-{stamp}{suffix}"))
                except OSError:
                    pass

    def _conn(self) -> sqlite3.Connection:
        def _connect(path: Any, factory: Any) -> sqlite3.Connection:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            kw = {"factory": factory} if factory is not None else {}
            return sqlite3.connect(path, timeout=5.0, **kw)

        def _setup(conn: sqlite3.Connection) -> None:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.execute("PRAGMA journal_mode = WAL")
            # Memory is rebuilt from the conversation and a lost last commit after a
            # power cut is harmless: WAL + NORMAL never corrupts and skips one fsync
            # per commit (~2 ms each on Windows, several commits per turn).
            conn.execute("PRAGMA synchronous = NORMAL")
            # The schema script only runs once per database file (user_version),
            # not on every connection.
            if conn.execute("PRAGMA user_version").fetchone()[0] != _SCHEMA_VERSION:
                conn.executescript(_SCHEMA)
                conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")

        for attempt in (0, 1):
            try:
                return dbscope.open_conn(self.path, _connect, _setup)
            except sqlite3.DatabaseError as exc:
                corrupt = any(m in str(exc).lower() for m in self._CORRUPT_MARKERS)
                if attempt == 0 and corrupt:
                    dbscope.forget(self.path)
                    self._quarantine()
                    continue
                raise
        raise sqlite3.DatabaseError("memory store unavailable")  # pragma: no cover

    def _open(self):
        """Connection that always closes (``with conn`` does not close on 3.12+)."""
        from contextlib import contextmanager

        @contextmanager
        def _cm():
            conn = self._conn()
            try:
                yield conn
                conn.commit()
            finally:
                conn.close()

        return _cm()

    def upsert(self, kind: str, text: str, *, session_id: str = "",
               keywords: Iterable[str] | None = None) -> Optional[str]:
        try:
            kind = str(kind or "").strip()
            text = _neutralize(_sentence(text, 400), max_chars=400)
            if kind not in KINDS or len(text) < 8 or looks_like_secret(text):
                return None
            words = " ".join(keywords) if keywords is not None else " ".join(_tokens(text))
            mid = hashlib.sha1(f"{kind}\0{text.lower()}".encode("utf-8", "replace")).hexdigest()
            now = time.time()
            with self._open() as conn:
                conn.execute(
                    "INSERT INTO memories (id, kind, text, keywords, session_id, "
                    "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET text=excluded.text, "
                    "keywords=excluded.keywords, updated_at=excluded.updated_at",
                    (mid, kind, text, words, session_id or "", now, now),
                )
            return mid
        except Exception:
            return None

    def retrieve(self, query: str, *, budget_tokens: int, limit: int = 8) -> list[dict[str, Any]]:
        try:
            qstems = _stems(query or "")
            if not qstems or budget_tokens <= 0:
                return []
            with self._open() as conn:
                rows = list(conn.execute(
                    "SELECT id, kind, text, keywords, session_id, updated_at "
                    "FROM memories ORDER BY updated_at DESC"))
            scored: list[tuple[int, dict[str, Any]]] = []
            for row in rows:
                item = dict(row)
                hay = _stems(f"{item.get('text') or ''} {item.get('keywords') or ''}")
                score = len(qstems & hay)
                if score <= 0:
                    continue
                scored.append((score, item))
            scored.sort(key=lambda p: (-p[0], -float(p[1].get("updated_at") or 0)))
            out: list[dict[str, Any]] = []
            used = 0
            for score, item in scored:
                cost = context_budget.estimate_tokens(str(item.get("text") or ""))
                if out and used + cost > budget_tokens:
                    continue
                if not out and cost > budget_tokens:
                    item["text"] = context_budget.truncate_text(
                        str(item.get("text") or ""), budget_tokens)
                    cost = context_budget.estimate_tokens(item["text"])
                out.append(item)
                used += cost
                if len(out) >= limit or used >= budget_tokens:
                    break
            return out
        except Exception:
            return []

    def load_summary(self, session_id: str) -> dict[str, Any]:
        try:
            if not session_id:
                return {"text": "", "covered": 0}
            with self._open() as conn:
                row = conn.execute(
                    "SELECT text, covered FROM session_summary WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
            if not row:
                return {"text": "", "covered": 0}
            return {"text": str(row["text"] or ""), "covered": int(row["covered"] or 0)}
        except Exception:
            return {"text": "", "covered": 0}

    def save_summary(self, session_id: str, text: str, covered: int) -> None:
        try:
            if not session_id:
                return
            with self._open() as conn:
                conn.execute(
                    "INSERT INTO session_summary (session_id, text, covered, updated_at) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT(session_id) DO UPDATE SET "
                    "text=excluded.text, covered=excluded.covered, "
                    "updated_at=excluded.updated_at",
                    (session_id, text or "", int(covered or 0), time.time()),
                )
        except Exception:
            return

    def count(self) -> int:
        try:
            with self._open() as conn:
                row = conn.execute("SELECT COUNT(*) AS n FROM memories").fetchone()
            return int(row["n"] if row else 0)
        except Exception:
            return 0

    def prune(self, max_entries: int) -> int:
        try:
            cap = max(0, int(max_entries))
            with self._open() as conn:
                n = int(conn.execute("SELECT COUNT(*) AS n FROM memories").fetchone()["n"])
                extra = n - cap
                if extra <= 0:
                    return 0
                conn.execute(
                    "DELETE FROM memories WHERE id IN ("
                    "SELECT id FROM memories ORDER BY updated_at ASC, created_at ASC "
                    "LIMIT ?)",
                    (extra,),
                )
                return extra
        except Exception:
            return 0


    def list_all(self, limit: int = 500) -> list[dict[str, Any]]:
        try:
            with self._open() as conn:
                rows = conn.execute(
                    "SELECT id, kind, text, keywords, session_id, updated_at "
                    "FROM memories ORDER BY kind, updated_at DESC LIMIT ?",
                    (max(1, int(limit)),),
                ).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []

    def resolve(self, token: str) -> tuple[Optional[dict[str, Any]], str]:
        """Look up by full id or unique prefix. Error is empty on success."""
        try:
            key = str(token or "").strip().lower()
            if len(key) < 4:
                return None, "id too short"
            with self._open() as conn:
                rows = [dict(r) for r in conn.execute(
                    "SELECT id, kind, text, keywords, session_id, updated_at "
                    "FROM memories WHERE id LIKE ?",
                    (key + "%",),
                ).fetchall()]
            if not rows:
                return None, "not found"
            exact = [r for r in rows if r["id"] == key]
            if exact:
                return exact[0], ""
            if len(rows) > 1:
                return None, "ambiguous"
            return rows[0], ""
        except Exception:
            return None, "not found"

    def get(self, token: str) -> Optional[dict[str, Any]]:
        row, _err = self.resolve(token)
        return row

    def delete(self, token: str) -> Optional[dict[str, Any]]:
        row, err = self.resolve(token)
        if not row:
            return None
        try:
            with self._open() as conn:
                conn.execute("DELETE FROM memories WHERE id = ?", (row["id"],))
            return row
        except Exception:
            return None

    def update(self, token: str, text: str) -> Optional[dict[str, Any]]:
        row, err = self.resolve(token)
        if not row:
            return None
        try:
            body = _neutralize(_sentence(text, 400), max_chars=400)
            if len(body) < 8 or looks_like_secret(body):
                return None
            now = time.time()
            words = " ".join(_tokens(body))
            with self._open() as conn:
                conn.execute(
                    "UPDATE memories SET text = ?, keywords = ?, updated_at = ? "
                    "WHERE id = ?",
                    (body, words, now, row["id"]),
                )
            row = dict(row)
            row["text"] = body
            row["keywords"] = words
            row["updated_at"] = now
            return row
        except Exception:
            return None

    def clear_all(self) -> int:
        try:
            with self._open() as conn:
                n = int(conn.execute("SELECT COUNT(*) AS n FROM memories").fetchone()["n"])
                conn.execute("DELETE FROM memories")
            return n
        except Exception:
            return 0

    def _aux_write(self, session_id: str, **fields: Any) -> None:
        if not session_id:
            return
        now = time.time()
        pending = fields.get("pending")
        recalled = fields.get("recalled")
        tokens = fields.get("tokens")
        with self._open() as conn:
            conn.execute(
                "INSERT INTO session_aux (session_id, pending, last_recalled, "
                "last_tokens, updated_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET "
                "pending = COALESCE(?, pending), "
                "last_recalled = COALESCE(?, last_recalled), "
                "last_tokens = COALESCE(?, last_tokens), "
                "updated_at = excluded.updated_at",
                (
                    session_id,
                    json.dumps(pending) if pending is not None else "[]",
                    json.dumps(recalled) if recalled is not None else "[]",
                    int(tokens or 0),
                    now,
                    json.dumps(pending) if pending is not None else None,
                    json.dumps(recalled) if recalled is not None else None,
                    None if tokens is None else int(tokens),
                ),
            )

    def save_pending(self, session_id: str, asks: list[str]) -> None:
        try:
            self._aux_write(session_id, pending=list(asks or []))
        except Exception:
            return

    def load_pending(self, session_id: str) -> list[str]:
        try:
            if not session_id:
                return []
            with self._open() as conn:
                row = conn.execute(
                    "SELECT pending FROM session_aux WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
            if not row:
                return []
            data = json.loads(row["pending"] or "[]")
            return [str(x) for x in data if str(x).strip()]
        except Exception:
            return []

    def save_recall(self, session_id: str, recalled: list[dict[str, Any]],
                    tokens: int = 0) -> None:
        try:
            slim = []
            for item in recalled or []:
                title = short_title(str(item.get("title") or item.get("text") or ""))
                if not title:
                    continue
                slim.append({
                    "id": str(item.get("id") or ""),
                    "kind": str(item.get("kind") or ""),
                    "title": title,
                })
            self._aux_write(session_id, recalled=slim, tokens=int(tokens or 0))
        except Exception:
            return

    def load_recall(self, session_id: str) -> list[dict[str, Any]]:
        try:
            if not session_id:
                return []
            with self._open() as conn:
                row = conn.execute(
                    "SELECT last_recalled FROM session_aux WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
            if not row:
                return []
            data = json.loads(row["last_recalled"] or "[]")
            return [d for d in data if isinstance(d, dict)]
        except Exception:
            return []


def short_id(mid: str) -> str:
    return str(mid or "")[:8]


def short_title(text: str, n: int = 40) -> str:
    t = " ".join(str(text or "").split())
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


def update_summary(store: Optional[MemoryStore], session_id: str,
                   messages: Iterable[Any], recent_n: int,
                   budget_tokens: int) -> str:
    """Fold older utterances into a budgeted extractive summary."""
    try:
        utts = recent_utterances(messages, n=10_000)
        keep = max(0, int(recent_n or 0))
        older = utts[:-keep] if keep else utts
        bullets = []
        for line in older:
            short = line if len(line) <= 120 else line[:119].rstrip() + "…"
            bullets.append(f"- {short}")
        # Keep the newest overflow when the budget is tight.
        text = ""
        for line in reversed(bullets):
            candidate = (line + ("\n" + text if text else ""))
            if context_budget.estimate_tokens(candidate) > budget_tokens:
                break
            text = candidate
        if store is not None and session_id:
            store.save_summary(session_id, text, len(older))
        return text
    except Exception:
        return ""


def remember_turn(store: Optional[MemoryStore], session_id: str, *,
                  messages: Iterable[Any] | None = None,
                  final_response: str = "", user_text: str = "",
                  cfg: dict[str, Any] | None = None) -> int:
    """Extract long-term entries from a finished turn. Returns how many written."""
    try:
        if store is None:
            return 0
        options = normalize_cfg(cfg)
        if not options["enabled"] or not options["long_term"]:
            return 0
        user = user_text
        if not user:
            for msg in reversed(list(messages or [])):
                if isinstance(msg, dict) and str(msg.get("role") or "") == "user":
                    user = _text_of(msg.get("content"))
                    break
        written = 0
        for kind, text in extract_memories(user, final_response or ""):
            if store.upsert(kind, text, session_id=session_id or ""):
                written += 1
        store.prune(options["max_entries"])
        return written
    except Exception:
        return 0


def _ltm_block(store: Optional[MemoryStore], query: str, lang: str,
               budget_tokens: int, with_hits: bool = False):
    empty = ("", []) if with_hits else ""
    if store is None or not query or budget_tokens <= 0:
        return empty
    hits = store.retrieve(query, budget_tokens=budget_tokens)
    if not hits:
        return empty
    head = _HEAD.get(lang, _HEAD["en"])["ltm"]
    lines = [head]
    slim = []
    for item in hits:
        kind = str(item.get("kind") or "note")
        text = _neutralize(item.get("text") or "", max_chars=400)
        if text:
            lines.append(f"- ({kind}) {text}")
            slim.append({"id": str(item.get("id") or ""), "kind": kind,
                         "title": short_title(text), "text": text})
    block = "\n".join(lines)
    return (block, slim) if with_hits else block


def _last_user(messages: Iterable[Any]) -> str:
    for msg in reversed(list(messages or [])):
        if isinstance(msg, dict) and str(msg.get("role") or "") == "user":
            return _text_of(msg.get("content"))
    return ""


def render_for_turn(store: Optional[MemoryStore], session_id: str,
                    messages: Iterable[Any], query: str = "",
                    lang: str = "en", pending_text: str = "",
                    cfg: dict[str, Any] | None = None) -> str:
    """Hook-side memory block: rolling summary + optional pending + relevant LTM.

    Recent messages are not repeated here: the core already sends them in
    conversation_history on v0.23.
    """
    try:
        options = normalize_cfg(cfg)
        if not options["enabled"]:
            return ""
        lang = lang if lang in _HEAD else "en"
        parts: list[str] = []
        if options["short_term"]:
            summary = update_summary(
                store, session_id, messages,
                recent_n=options["recent_messages"],
                budget_tokens=options["summary_budget_tokens"],
            )
            if summary.strip():
                parts.append(_HEAD[lang]["summary"] + "\n" + summary.strip())
            if pending_text and pending_text.strip():
                parts.append(pending_text.strip())
        if options["long_term"]:
            q = query or _last_user(messages)
            block, hits = _ltm_block(store, q, lang, options["long_term_budget_tokens"],
                                     with_hits=True)
            if block:
                parts.append(block)
            if store is not None and session_id:
                store.save_recall(session_id, hits)
        return "\n\n".join(parts)
    except Exception:
        return ""


def bounded_view(messages: Iterable[Any], *, session_id: str = "",
                 lang: str = "en", store: Optional[MemoryStore] = None,
                 store_path: Any = None, pending: str = "",
                 cfg: dict[str, Any] | None = None) -> str:
    """Compact session view for measurement / a future context-engine path.

    Includes the rolling summary, the recent utterance window, pending
    asks and relevant long-term hits. This is NOT what v0.23 sends to
    the model (the core still attaches the full history).
    """
    try:
        options = normalize_cfg(cfg)
        if store is None and store_path:
            store = MemoryStore(store_path)
        lang = lang if lang in _HEAD else "en"
        parts: list[str] = []
        body = render_for_turn(
            store, session_id, messages, query=_last_user(messages),
            lang=lang, pending_text=pending, cfg=options)
        if body:
            # render_for_turn already has summary + pending + ltm; prepend recent.
            parts.append(body)
        recent = recent_utterances(messages, n=options["recent_messages"])
        if recent:
            lines = [(_HEAD[lang]["recent"])]
            used = 0
            budget = options["recent_budget_tokens"]
            for line in recent:
                short = line if len(line) <= 160 else line[:159].rstrip() + "…"
                cost = context_budget.estimate_tokens(short)
                if used + cost > budget and lines[1:]:
                    break
                lines.append(f"- {short}")
                used += cost
            # Insert recent after the summary (or first if no summary).
            recent_block = "\n".join(lines)
            if parts:
                parts[0] = parts[0] + "\n\n" + recent_block
            else:
                parts.append(recent_block)
        return "\n\n".join(p for p in parts if p.strip())
    except Exception:
        return ""
