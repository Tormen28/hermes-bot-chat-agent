"""Cron bridge for Bot Chat (Fase 3b, v1.2).

A thin, read-mostly adapter over the core's real cron API — no scheduling
of its own, no writes to the core's store except through the core's own
functions.

Two modes, same durable truth:

- ``"registry"``: the plugin runs inside Hermes and the tool registry has
  ``cronjob_manage``. Reads go to the durable files; writes (pause/resume/
  remove) go through that core tool (public ``tools.registry`` surface, no
  private ``cron.*`` imports). This is the production mode.
- ``"disk"``: the tool is not available. Reads go straight to the same durable files
  the core's functions read — ``<home>/cron/executions.db`` (read-only
  SQLite, the same pattern the plugin already uses for ``state.db`` /
  ``kanban.db``) and ``<home>/cron/output/<job_id>/*.md``. Writes raise
  :class:`CronUnavailable` instead of touching ``jobs.json`` by hand: a
  hand-written ``jobs.json`` would repeat E1/E4 (a lost update between
  processes, a failed read wiping state).

The observer never creates core state: every read first checks the file
exists and returns empty when it does not. The executions ledger carries
no output text, so the fire's result is read from the newest output file
the scheduler saved (``cron.jobs.save_job_output``).

Verified against the live core (``2bd378ed``); see ``docs/fase3b-rubric.md``
§2 for the exact API surface used.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

SUMMARY_MAX_CHARS = 500
"""Truncation for the outbox summary; the full output stays in the core's file."""

SILENT_MARKER = "[SILENT]"
"""The core's silence sentinel (``cron.scheduler.SILENT_MARKER``)."""

_TERMINAL_STATUSES = ("completed", "failed", "cancelled", "missed")
_SILENCE_WORDS = {"[silent]", "silent", "no_reply", "no reply"}


class CronUnavailable(Exception):
    """The core is not importable here, so a cron write is refused."""


def extract_response(output_text: Any) -> str:
    """The fire's final response out of the run document the core saved.

    Agent jobs save ``**Response Characters:** N`` + ``## Response`` + the
    final response; ``no_agent`` jobs save the script stdout verbatim after
    the first ``---`` line; silent runs save ``**Status:** silent ...`` and
    their final response IS ``[SILENT]``. Anything else returns the whole
    text, stripped.
    """
    text = str(output_text or "")
    if not text.strip():
        return ""
    if "**Status:** silent" in text:
        return SILENT_MARKER
    frame = text.find("**Response Characters:**")
    if frame >= 0:
        head = text.find("## Response\n\n", frame)
        if head >= 0:
            return text[head + len("## Response\n\n"):].strip()
    head = text.find("## Response\n\n")
    if head >= 0:
        return text[head + len("## Response\n\n"):].strip()
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == "---":
            return "\n".join(lines[i + 1:]).strip()
    return text.strip()


def is_silent_response(text: Any) -> bool:
    """Mirror of the core's silence rule (``_is_cron_silence_response``):
    the sentinel as the whole response, or on its own first/last line —
    never buried mid-sentence."""
    raw = str(text or "").strip()
    if not raw:
        return False
    lowered = raw.lower()
    if lowered in _SILENCE_WORDS:
        return True
    lines = [line.strip().lower() for line in raw.splitlines() if line.strip()]
    return bool(lines) and (lines[0] in _SILENCE_WORDS
                            or lines[-1] in _SILENCE_WORDS)


def _resolve_home(home: Any = None) -> Path:
    if home:
        return Path(home).expanduser()
    try:
        from hermes_constants import get_hermes_home  # documented home helper
        return Path(get_hermes_home())
    except Exception:
        return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))


def _safe_job_id(job_id: Any) -> str:
    """A job id usable as a single path component, or ``""``."""
    text = str(job_id or "").strip()
    if not text or text in {".", ".."} or "/" in text or "\\" in text:
        return ""
    if Path(text).is_absolute() or Path(text).drive:
        return ""
    return text


def _newest_output_file(output_dir: Path) -> Optional[Path]:
    if not output_dir.is_dir():
        return None
    files = sorted(p for p in output_dir.glob("*.md") if p.is_file())
    return files[-1] if files else None


class CronAdapter:
    """The protocol the plugin observes/pauses through (both modes)."""

    mode = "base"

    def get_job(self, job_id: str) -> Optional[dict[str, Any]]:
        raise NotImplementedError

    def pause_job(self, job_id: str, reason: Optional[str] = None) -> Optional[dict[str, Any]]:
        raise NotImplementedError

    def resume_job(self, job_id: str) -> Optional[dict[str, Any]]:
        raise NotImplementedError

    def remove_job(self, job_id: str) -> bool:
        raise NotImplementedError

    def terminal_executions(self, job_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """Terminal executions of a job, newest first (core parity)."""
        raise NotImplementedError

    def latest_output(self, job_id: str) -> str:
        """Text of the newest saved output file, or ``""``."""
        raise NotImplementedError


class _DiskAdapter(CronAdapter):
    """Reads the same durable files directly (stdlib only). No writes."""

    mode = "disk"

    def __init__(self, home: Path):
        self.home = home

    def _executions_db(self) -> Path:
        return self.home / "cron" / "executions.db"

    def get_job(self, job_id: str) -> Optional[dict[str, Any]]:
        jid = str(job_id or "").strip()
        if not jid:
            return None
        path = self.home / "cron" / "jobs.json"
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            logger.debug("cron_bridge: jobs.json unreadable: %s", exc)
            return None
        if not isinstance(data, list):
            return None
        for job in data:
            if isinstance(job, dict) and str(job.get("id") or "") == jid:
                return dict(job)
        return None

    def pause_job(self, job_id: str, reason: Optional[str] = None) -> Optional[dict[str, Any]]:
        raise CronUnavailable(
            "cron is not importable here, so pause_job is refused: "
            "the plugin never writes jobs.json by hand")

    def resume_job(self, job_id: str) -> Optional[dict[str, Any]]:
        raise CronUnavailable(
            "cron is not importable here, so resume_job is refused: "
            "the plugin never writes jobs.json by hand")

    def remove_job(self, job_id: str) -> bool:
        raise CronUnavailable(
            "cron is not importable here, so remove_job is refused: "
            "the plugin never writes jobs.json by hand")

    def terminal_executions(self, job_id: str, limit: int = 50) -> list[dict[str, Any]]:
        db = self._executions_db()
        if not db.is_file():
            return []
        jid = str(job_id or "").strip()
        if not jid:
            return []
        try:
            with closing(sqlite3.connect(
                    f"file:{db.as_posix()}?mode=ro", uri=True, timeout=1.5)) as conn:
                conn.row_factory = sqlite3.Row
                marks = ",".join("?" for _ in _TERMINAL_STATUSES)
                rows = conn.execute(
                    "SELECT * FROM executions WHERE job_id = ? AND status IN "
                    f"({marks}) ORDER BY julianday(claimed_at) DESC, "
                    "claimed_at DESC, id DESC LIMIT ?",
                    (jid, *_TERMINAL_STATUSES, max(1, int(limit))),
                ).fetchall()
        except sqlite3.Error as exc:
            logger.debug("cron_bridge: executions.db unreadable: %s", exc)
            return []
        return [_normalize_execution(dict(r)) for r in rows]

    def latest_output(self, job_id: str) -> str:
        jid = _safe_job_id(job_id)
        if not jid:
            return ""
        return _read_newest_output(self.home / "cron" / "output" / jid)


class _RegistryAdapter(_DiskAdapter):
    """Reads like the disk adapter; writes (pause/resume/remove) go through the core's own
    ``cronjob_manage`` tool, called via the public ``tools.registry`` surface. The plugin never
    writes ``jobs.json`` itself and imports no private ``cron.*`` module."""

    mode = "registry"

    def __init__(self, home: Path, registry: Any):
        super().__init__(home)
        self._registry = registry

    def _call(self, args: dict[str, Any]) -> dict[str, Any]:
        raw = self._registry.dispatch("cronjob_manage", args)
        try:
            data = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
        except (TypeError, ValueError):
            data = {}
        if not isinstance(data, dict) or data.get("error") or data.get("success") is False:
            detail = data.get("error") if isinstance(data, dict) else ""
            raise CronUnavailable(f"cronjob_manage {args.get('action')} failed: {detail or raw!r}")
        return data

    def pause_job(self, job_id: str, reason: Optional[str] = None) -> Optional[dict[str, Any]]:
        self._call({"action": "pause", "job_id": str(job_id or ""), "reason": reason or ""})
        return self.get_job(job_id)

    def resume_job(self, job_id: str) -> Optional[dict[str, Any]]:
        self._call({"action": "resume", "job_id": str(job_id or "")})
        return self.get_job(job_id)

    def remove_job(self, job_id: str) -> bool:
        self._call({"action": "remove", "job_id": str(job_id or "")})
        return True


def _normalize_execution(row: Any) -> dict[str, Any]:
    data = dict(row) if isinstance(row, dict) else {}
    return {
        "id": str(data.get("id") or ""),
        "job_id": str(data.get("job_id") or ""),
        "status": str(data.get("status") or ""),
        "claimed_at": str(data.get("claimed_at") or ""),
        "started_at": str(data.get("started_at") or ""),
        "finished_at": str(data.get("finished_at") or ""),
        "error": str(data.get("error") or ""),
        "scheduled_instant": str(data.get("scheduled_instant") or ""),
        "delivery_outcome": str(data.get("delivery_outcome") or ""),
    }


def _read_newest_output(output_dir: Path) -> str:
    newest = _newest_output_file(output_dir)
    if newest is None:
        return ""
    try:
        return newest.read_text(encoding="utf-8-sig")
    except OSError as exc:
        logger.debug("cron_bridge: output unreadable: %s", exc)
        return ""


def default_adapter(home: Any = None) -> CronAdapter:
    """Registry-backed writes when the host's tool registry has ``cronjob_manage``, else disk mode
    (reads only; writes raise :class:`CronUnavailable`)."""
    resolved = _resolve_home(home)
    try:
        from tools.registry import registry  # public plugin surface (catalog rule 9)
        if registry.get_entry("cronjob_manage") is not None:
            return _RegistryAdapter(resolved, registry)
    except Exception as exc:  # noqa: BLE001 - disk mode is the fallback
        logger.debug("cron_bridge: tool registry not available (%s); disk mode", exc)
    return _DiskAdapter(resolved)
