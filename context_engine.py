"""BotChat delegating context engine (Fase 2, B4 Opción 0).

No core change: Bot Chat registers a ``ContextEngine`` (public ``agent.context_engine``)
that compacts with its own deterministic ``LocalCompactor`` (no private core import, no LLM
call) and overrides ``select_context`` (bounded plugin context per request, never persisted
in the transcript) and ``on_turn_complete``.

Activation: the user sets ``context.engine: bot-chat`` in the core
config. Without that, the engine stays registered but inactive and the
``pre_llm_call`` path remains the active one (fail-open).

The core ABC is imported lazily: when the core is not on ``sys.path``
(unit tests, offline use) a local shim with the same interface is
used, so this module always imports. ``clone_for_agent`` builds a
fresh engine with a fresh delegate (the engine holds no connections
or locks, but explicit fresh construction is safer than deepcopy).
"""
from __future__ import annotations

import copy
import logging
from abc import ABC, abstractmethod
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

ENGINE_NAME = "bot-chat"

try:
    from agent.context_engine import ContextEngine as _CoreEngine  # type: ignore
except Exception:  # noqa: BLE001 - the core is not importable offline
    _CoreEngine = None  # type: ignore


class _ShimEngine(ABC):
    """Local stand-in for the core ABC (same interface, offline use)."""

    last_prompt_tokens: int = 0
    last_completion_tokens: int = 0
    last_total_tokens: int = 0
    threshold_tokens: int = 0
    context_length: int = 0
    compression_count: int = 0
    threshold_percent: float = 0.75
    protect_first_n: int = 3
    protect_last_n: int = 6
    emit_automatic_compaction_status: bool = True

    @property
    @abstractmethod
    def name(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def update_from_response(self, usage: dict[str, Any]) -> None:
        raise NotImplementedError

    @abstractmethod
    def should_compress(self, prompt_tokens: int | None = None) -> bool:
        raise NotImplementedError

    @abstractmethod
    def compress(self, messages: list[dict[str, Any]], current_tokens: Optional[int] = None,
                 focus_topic: Optional[str] = None, force: bool = False,
                 memory_context: str = "") -> list[dict[str, Any]]:
        raise NotImplementedError

    def select_context(self, request_messages: list[dict[str, Any]], *,
                       conversation_messages: list[dict[str, Any]] | None = None,
                       incoming_message: dict[str, Any] | None = None,
                       budget_tokens: int = 0) -> Optional[list[dict[str, Any]]]:
        return None

    def on_turn_complete(self, messages: list[dict[str, Any]],
                         usage: dict[str, Any] | None = None, **kwargs: Any) -> None:
        return None

    def on_session_start(self, session_id: str, **kwargs: Any) -> None:
        return None

    def on_session_end(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        return None

    def on_session_reset(self) -> None:
        self.last_prompt_tokens = 0
        self.last_completion_tokens = 0

    def clone_for_agent(self) -> "_ShimEngine":
        return copy.deepcopy(self)

    def update_model(self, model: str, context_length: int, base_url: str = "",
                     api_key: str = "", provider: str = "", api_mode: str = "") -> None:
        return None


_Base = _CoreEngine if _CoreEngine is not None else _ShimEngine

#: Sessions the host started with this engine (in-memory only: engine
#: activity is a property of the running host, not durable state).
_ENGINE_SESSIONS: set[str] = set()


def is_engine_active(session_id: str) -> bool:
    """True when the host started ``session_id`` with this engine.

    The ``pre_llm_call`` hook uses this to stand down (the engine
    supplies the context via ``select_context``). When False, the hook
    path is the active one.
    """
    return bool(session_id) and session_id in _ENGINE_SESSIONS


def _style_markers() -> tuple[str, ...]:
    return ("[Bot Chat style]", "[Estilo Bot Chat]")


class LocalCompactor:
    """Deterministic, dependency-free compaction (no LLM call).

    Keeps the first ``protect_first_n`` and last ``protect_last_n`` messages and replaces the middle
    with ONE user note: the protected block / memory context plus a one-line digest per dropped
    message. It never splits an assistant tool call from its tool results. This is weaker than a
    model-written summary: it trades quality for staying inside public plugin surfaces and for never
    making a network call. Whether it speeds up a long session is NOT measured yet.
    """

    NOTE_HEADING = "[Bot Chat: earlier turns compacted]"
    DIGEST_LINE_CHARS = 140
    NOTE_MAX_CHARS = 6000

    def __init__(self, *, model: str = "", context_length: int = 0, threshold_percent: float = 0.75,
                 protect_first_n: int = 3, protect_last_n: int = 6):
        self.model = model
        self.context_length = int(context_length or 0)
        self.threshold_percent = float(threshold_percent)
        self.protect_first_n = int(protect_first_n)
        self.protect_last_n = int(protect_last_n)
        self.last_prompt_tokens = 0
        self.last_completion_tokens = 0
        self.last_total_tokens = 0
        self.compression_count = 0

    @property
    def threshold_tokens(self) -> int:
        return int(self.context_length * self.threshold_percent) if self.context_length > 0 else 0

    def update_model(self, model: str, context_length: int, **_kw: Any) -> None:
        self.model = str(model or self.model)
        self.context_length = int(context_length or 0)

    def update_from_response(self, usage: dict[str, Any]) -> None:
        usage = usage or {}
        self.last_prompt_tokens = int(usage.get("prompt_tokens") or 0)
        self.last_completion_tokens = int(usage.get("completion_tokens") or 0)
        self.last_total_tokens = int(usage.get("total_tokens")
                                     or self.last_prompt_tokens + self.last_completion_tokens)

    def should_compress(self, prompt_tokens: int | None = None) -> bool:
        limit = self.threshold_tokens
        used = int(prompt_tokens if prompt_tokens is not None else self.last_prompt_tokens)
        return limit > 0 and used >= limit

    @staticmethod
    def _text(msg: Any) -> str:
        content = msg.get("content") if isinstance(msg, dict) else ""
        if isinstance(content, list):
            content = " ".join(str(p.get("text") or "") for p in content if isinstance(p, dict))
        return " ".join(str(content or "").split())

    def compress(self, messages: list[dict[str, Any]], current_tokens: Optional[int] = None,
                 focus_topic: Optional[str] = None, force: bool = False,
                 memory_context: str = "") -> list[dict[str, Any]]:
        msgs = list(messages or [])
        head_n = max(0, self.protect_first_n)
        tail_start = len(msgs) - max(1, self.protect_last_n)
        # never start the kept tail on a tool result: pull its assistant call in with it
        while 0 < tail_start < len(msgs) and isinstance(msgs[tail_start], dict) \
                and msgs[tail_start].get("role") == "tool":
            tail_start -= 1
        if tail_start <= head_n:
            return msgs
        # never end the head on an assistant message whose tool results fall in the dropped middle
        while head_n > 0 and isinstance(msgs[head_n - 1], dict) and msgs[head_n - 1].get("tool_calls"):
            head_n -= 1
        middle = msgs[head_n:tail_start]
        if not middle:
            return msgs
        lines = []
        for m in middle:
            role = str(m.get("role") or "?") if isinstance(m, dict) else "?"
            text = self._text(m)
            if not text and isinstance(m, dict) and m.get("tool_calls"):
                text = "(tool call)"
            if text:
                lines.append(f"- {role}: {text[:self.DIGEST_LINE_CHARS]}")
        parts = [self.NOTE_HEADING]
        if focus_topic:
            parts.append(f"Focus: {focus_topic}")
        if memory_context.strip():
            parts.append(memory_context.strip())
        parts.append(f"{len(middle)} earlier messages were dropped. One-line digest:")
        note = "\n".join(parts) + "\n" + "\n".join(lines)
        if len(note) > self.NOTE_MAX_CHARS:
            note = note[:self.NOTE_MAX_CHARS].rstrip() + "\n- …"
        self.compression_count += 1
        return msgs[:head_n] + [{"role": "user", "content": note}] + msgs[tail_start:]


class BotChatContextEngine(_Base):  # type: ignore[misc]
    """Engine: bounded plugin context, plugin-local compaction."""

    def __init__(self, *, model: str = "",
                 build_context: Optional[Callable[..., dict[str, Any]]] = None,
                 protected_block: Optional[Callable[..., str]] = None,
                 clear_fingerprint: Optional[Callable[..., None]] = None):
        self._model = str(model or "")
        # Remembered so a lazily created compactor gets the real window (a delegate created with
        # context_length=0 has a threshold of 0 and NEVER compacts: the history grows unbounded).
        self._context_length = 0
        # Callables supplied by the plugin (resolved lazily per call, so
        # the engine never holds paths, DB connections or locks).
        self._build_context = build_context
        self._protected_block = protected_block
        self._clear_fingerprint = clear_fingerprint
        self._delegate: Any = None
        self._delegate_failed = False
        self._session_id = ""
        self.last_prompt_tokens = 0
        self.last_completion_tokens = 0
        self.last_total_tokens = 0
        self.compression_count = 0
        self.threshold_percent = 0.75
        self.protect_first_n = 3
        self.protect_last_n = 6

    # ------------------------------------------------------------------ identity
    @property
    def name(self) -> str:
        return ENGINE_NAME

    # ------------------------------------------------------------------ lifecycle
    def on_session_start(self, session_id: str, **kwargs: Any) -> None:
        self._session_id = str(session_id or "")
        if self._session_id:
            _ENGINE_SESSIONS.add(self._session_id)

    def on_session_end(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        _ENGINE_SESSIONS.discard(str(session_id or ""))
        if str(session_id or "") == self._session_id:
            self._session_id = ""

    def on_session_reset(self) -> None:
        _ENGINE_SESSIONS.clear()
        self._session_id = ""
        self.last_prompt_tokens = 0
        self.last_completion_tokens = 0

    def clone_for_agent(self) -> "BotChatContextEngine":
        """Fresh engine with a fresh delegate (no shared state)."""
        clone = BotChatContextEngine(
            model=self._model,
            build_context=self._build_context,
            protected_block=self._protected_block,
            clear_fingerprint=self._clear_fingerprint,
        )
        clone._context_length = self._context_length
        clone.last_prompt_tokens = self.last_prompt_tokens
        clone.last_completion_tokens = self.last_completion_tokens
        clone.last_total_tokens = self.last_total_tokens
        clone.threshold_percent = self.threshold_percent
        return clone

    def update_model(self, model: str, context_length: int, base_url: str = "",
                     api_key: str = "", provider: str = "", api_mode: str = "") -> None:
        self._model = str(model or "")
        try:
            self._context_length = int(context_length or 0)
        except (TypeError, ValueError):
            self._context_length = 0
        if self._delegate is not None:
            try:
                self._delegate.update_model(
                    model, context_length, base_url=base_url, api_key=api_key,
                    provider=provider, api_mode=api_mode)
                return
            except Exception:
                pass
        # Drop the delegate: it is re-created lazily with the new model.
        self._delegate = None

    # ------------------------------------------------------------------ delegate
    def _delegate_compressor(self) -> Any:
        """Compactor used by ``compress`` (lazy). A host or test may inject ``_delegate``; otherwise
        the plugin's own deterministic :class:`LocalCompactor` is used. The core's private
        ``ContextCompressor`` is deliberately not imported (catalog rule 9: public surfaces only)."""
        if self._delegate is None:
            self._delegate = LocalCompactor(
                model=self._model or "unknown", context_length=self._context_length,
                threshold_percent=self.threshold_percent,
                protect_first_n=self.protect_first_n, protect_last_n=self.protect_last_n)
        return self._delegate

    # ------------------------------------------------------------------ core API
    def update_from_response(self, usage: dict[str, Any]) -> None:
        usage = usage or {}
        self.last_prompt_tokens = int(usage.get("prompt_tokens") or 0)
        self.last_completion_tokens = int(usage.get("completion_tokens") or 0)
        self.last_total_tokens = int(
            usage.get("total_tokens") or self.last_prompt_tokens + self.last_completion_tokens)
        delegate = self._delegate_compressor()
        if delegate is not None:
            try:
                delegate.update_from_response(usage)
                for attr in ("last_prompt_tokens", "last_completion_tokens",
                             "last_total_tokens", "threshold_tokens",
                             "context_length", "compression_count"):
                    value = getattr(delegate, attr, None)
                    if isinstance(value, int):
                        setattr(self, attr, value)
            except Exception as exc:  # noqa: BLE001 - counters are best effort
                logger.debug("bot-chat-agent: delegate usage update skipped: %s", exc)

    def should_compress(self, prompt_tokens: int | None = None) -> bool:
        delegate = self._delegate_compressor()
        if delegate is None:
            return False
        try:
            return bool(delegate.should_compress(prompt_tokens))
        except Exception:
            return False

    def compress(self, messages: list[dict[str, Any]], current_tokens: Optional[int] = None,
                 focus_topic: Optional[str] = None, force: bool = False,
                 memory_context: str = "") -> list[dict[str, Any]]:
        """Compact via the internal compressor, protecting plugin state.

        The protected block (critical constraints + result IDs + minimal
        tool-effects log) is passed as ``memory_context`` so the core
        summary prompt includes it. After a compaction the fingerprint
        is cleared: the next render rebuilds the full note.
        """
        protected = ""
        if self._protected_block is not None and self._session_id:
            try:
                protected = str(self._protected_block(self._session_id) or "")
            except Exception:
                protected = ""
        merged = (protected + "\n\n" + memory_context).strip() if protected else (memory_context or "")
        delegate = self._delegate_compressor()
        if delegate is None:
            return list(messages)
        try:
            out = delegate.compress(
                messages, current_tokens=current_tokens, focus_topic=focus_topic,
                force=force, memory_context=merged)
        except Exception as exc:  # noqa: BLE001 - compaction must not break the turn
            logger.warning("bot-chat-agent: delegated compaction failed (%s); "
                           "messages unchanged", exc)
            return list(messages)
        try:
            self.compression_count = int(getattr(delegate, "compression_count", 0) or 0)
        except Exception:
            pass
        if self._clear_fingerprint is not None and self._session_id:
            try:
                self._clear_fingerprint(self._session_id)
            except Exception:
                pass
        return out

    def select_context(self, request_messages: list[dict[str, Any]], *,
                       conversation_messages: list[dict[str, Any]] | None = None,
                       incoming_message: dict[str, Any] | None = None,
                       budget_tokens: int = 0) -> Optional[list[dict[str, Any]]]:
        """Bounded plugin context for THIS request (not persisted).

        Appends one user message with the stable + dynamic sections.
        Returns ``None`` (leave unchanged) when the engine has no
        session, no builder, or the request already carries the style
        (the hook path injected it this turn) — the two paths never
        duplicate.
        """
        if not self._session_id or self._build_context is None:
            return None
        try:
            last_user = ""
            for msg in reversed(list(request_messages or [])):
                if isinstance(msg, dict) and str(msg.get("role") or "") == "user":
                    content = msg.get("content")
                    if isinstance(content, str):
                        last_user = content
                    elif isinstance(content, list):
                        last_user = "".join(
                            str(part.get("text") or "")
                            for part in content if isinstance(part, dict))
                    break
            if any(marker in last_user for marker in _style_markers()):
                return None
            msgs = conversation_messages or request_messages
            try:
                built = self._build_context(
                    self._session_id, f"engine:{self._session_id}", msgs) or {}
            except TypeError:
                built = self._build_context(self._session_id) or {}
            stable = str(built.get("stable") or "")
            dynamic = str(built.get("dynamic") or "")
            pending = str(built.get("pending") or "")
            memory = str(built.get("memory") or "")
            text = "\n\n".join(part for part in (stable, dynamic, pending, memory)
                                 if part.strip())
            if not text.strip():
                return None
            out = [dict(m) if isinstance(m, dict) else m for m in (request_messages or [])]
            out.append({"role": "user", "content": text})
            return out
        except Exception as exc:  # noqa: BLE001 - selection must not break the turn
            logger.debug("bot-chat-agent: select_context skipped: %s", exc)
            return None

    def on_turn_complete(self, messages: list[dict[str, Any]],
                         usage: dict[str, Any] | None = None, **kwargs: Any) -> None:
        """Observe a finished turn; an interrupted turn resets the note."""
        try:
            if usage:
                self.update_from_response(usage)
            if kwargs.get("interrupted") and self._clear_fingerprint is not None and self._session_id:
                self._clear_fingerprint(self._session_id)
        except Exception:
            pass
