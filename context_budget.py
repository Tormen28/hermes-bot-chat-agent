"""Token budgets for Fase 2 (bounded context).

The plugin measures every rendered section against the plan budgets
(v1.2 §6, §11): 300 stable tokens, 1,500 dynamic tokens, 2,000 tokens
per piece (B4-2 policy, plugin side). The estimator is ``tiktoken``
(``cl100k_base``) when installed; otherwise ``ceil(chars/4)`` — the same
approximation calibrated in Fase 0 (A0.8, cross-checked against
words x 1.33 within ±5 %). ``tiktoken`` is never installed from the
network by this module; it is used only if already present.
"""
from __future__ import annotations

import math
from typing import Any, Optional

STABLE_BUDGET_TOKENS = 300
DYNAMIC_BUDGET_TOKENS = 1500
PER_PIECE_BUDGET_TOKENS = 2000
# The core caps each hook output at 10_000 characters
# (tools/hook_output_spill.py DEFAULT_MAX_CHARS). Our renders stay far
# below it; the check is a safety net, not the budget.
HOOK_SPILL_MAX_CHARS = 10_000

_ENCODER = None
_ENCODER_TRIED = False


def estimator_name() -> str:
    """Which estimator is in use: 'tiktoken' or 'chars/4'."""
    _ensure_encoder()
    return "tiktoken" if _ENCODER is not None else "chars/4"


def _ensure_encoder() -> None:
    global _ENCODER, _ENCODER_TRIED
    if _ENCODER_TRIED:
        return
    _ENCODER_TRIED = True
    try:
        import tiktoken  # type: ignore

        _ENCODER = tiktoken.get_encoding("cl100k_base")
    except Exception:
        _ENCODER = None


def estimate_tokens(text: str) -> int:
    """Estimated token count for ``text`` (0 for empty text)."""
    if not text:
        return 0
    _ensure_encoder()
    if _ENCODER is not None:
        try:
            return len(_ENCODER.encode(text))
        except Exception:
            pass
    return max(1, math.ceil(len(text) / 4))


def truncate_text(text: str, budget_tokens: int) -> str:
    """Trim ``text`` to roughly ``budget_tokens`` tokens.

    Cuts by characters (4 chars per token) and appends an ellipsis
    marker so the cut is visible, never silent.
    """
    if budget_tokens <= 0:
        return ""
    if estimate_tokens(text) <= budget_tokens:
        return text
    keep_chars = max(0, budget_tokens * 4 - 12)
    return text[:keep_chars].rstrip() + " …[recortado]"


def fit_sections(sections: list[dict[str, Any]], budget_tokens: int) -> dict[str, Any]:
    """Fit ordered sections into ``budget_tokens`` by priority.

    ``sections`` is a list of dicts with keys ``name`` (str), ``text``
    (str) and ``priority`` (int, lower = more important); ``critical``
    (bool, optional): a critical section is never dropped — if the
    critical sections alone exceed the budget they are rendered anyway
    and the result is flagged ``over_budget=True`` (safety beats budget
    for critical constraints; the caller logs a warning).

    Returns ``{"text": str, "tokens": int, "dropped": [names],
    "over_budget": bool}``. Dropped sections are accounted for, never
    lost silently.
    """
    ordered = sorted(sections, key=lambda s: (s.get("priority", 99), s.get("name", "")))
    kept: list[dict[str, Any]] = []
    dropped: list[str] = []
    used = 0
    critical_over = False
    for section in ordered:
        text = str(section.get("text") or "")
        if not text:
            continue
        cost = estimate_tokens(text)
        critical = bool(section.get("critical"))
        if used + cost <= budget_tokens:
            kept.append(section)
            used += cost
            continue
        if critical:
            # Never drop a critical section; render it anyway and flag it.
            kept.append(section)
            used += cost
            critical_over = True
            continue
        # Try trimming this one section to the remaining budget; if even
        # a trimmed stub does not fit, drop it (accounted below).
        remaining = budget_tokens - used
        if remaining >= 8:
            trimmed = truncate_text(text, remaining)
            if trimmed.strip():
                kept.append({**section, "text": trimmed})
                used += estimate_tokens(trimmed)
                dropped.append(f"{section.get('name', '?')} (recortado)")
                continue
        dropped.append(str(section.get("name", "?")))
    rendered = "\n".join(str(s.get("text") or "") for s in kept if str(s.get("text") or "").strip())
    return {
        "text": rendered,
        "tokens": estimate_tokens(rendered),
        "dropped": dropped,
        "over_budget": critical_over or estimate_tokens(rendered) > budget_tokens,
    }


def check_budgets(stable_text: str, dynamic_text: str) -> dict[str, Any]:
    """Measure a render against the Fase 2 budgets.

    Returns ``{"stable_tokens": int, "dynamic_tokens": int,
    "stable_ok": bool, "dynamic_ok": bool, "ok": bool}``.
    """
    stable = estimate_tokens(stable_text)
    dynamic = estimate_tokens(dynamic_text)
    stable_ok = stable <= STABLE_BUDGET_TOKENS
    dynamic_ok = dynamic <= DYNAMIC_BUDGET_TOKENS
    return {
        "stable_tokens": stable,
        "dynamic_tokens": dynamic,
        "stable_ok": stable_ok,
        "dynamic_ok": dynamic_ok,
        "ok": stable_ok and dynamic_ok,
    }
