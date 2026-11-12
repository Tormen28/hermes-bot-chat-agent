"""Team configuration for Bot Chat (Fase 4: una versión pública y personal).

The user-facing key is ``team.assignees``. ``team.gods`` is the old key,
kept as a legacy alias; the compatibility is defined from scratch here:

- Only ``assignees`` set -> use it.
- Only ``gods`` set -> use it (legacy; the caller may warn once).
- Both set and equal (case-insensitive, order-insensitive) -> use it.
- Both set and different -> :class:`TeamConfigError` with an explanatory
  message. Never a silent pick: a contradictory config must fail loudly
  where the user can see it, and the guard fails closed on it.

``enabled`` (whether the team is on at all):

- Explicit value wins, whatever it is.
- Absent -> derived: on iff at least one assignee is listed. That is the
  migration rule for the new default (fresh installs are individual):
  a new default never disables the team of an existing install, because
  an install that already lists teammates keeps them, and keeps the team
  on, without writing any new key.

This module is pure (no I/O, no logging): the plugin decides where the
error and the deprecation warning surface.
"""
from __future__ import annotations

from typing import Any

from team_flow import god_keys, normalize_gods


class TeamConfigError(ValueError):
    """The team config is contradictory and cannot be resolved."""


def resolve_team(raw: Any) -> dict[str, Any]:
    """Resolve a raw ``team`` config dict into effective team settings.

    Returns ``{"enabled", "assignees", "configured", "source",
    "legacy_gods"}`` where ``assignees`` is the effective list (empty
    when the team is disabled) and ``configured`` is the list as
    configured (kept for diagnostics even when disabled).
    """
    if not isinstance(raw, dict):
        raw = {}
    assignees_present = "assignees" in raw
    gods_present = "gods" in raw
    assignees = normalize_gods(raw.get("assignees"))
    gods = normalize_gods(raw.get("gods"))
    if assignees_present and gods_present:
        if god_keys(assignees) != god_keys(gods):
            raise TeamConfigError(
                "team.assignees and team.gods both exist but differ: "
                f"assignees={assignees!r}, gods={gods!r}. "
                "Use only team.assignees (the new key) and remove team.gods, "
                "or make both lists identical."
            )
        chosen, source, legacy = assignees, "assignees", False
    elif assignees_present:
        chosen, source, legacy = assignees, "assignees", False
    elif gods_present:
        chosen, source, legacy = gods, "gods", True
    else:
        chosen, source, legacy = [], "none", False
    if "enabled" in raw:
        enabled = bool(raw.get("enabled"))
    else:
        # New default is individual; an existing install that already
        # lists teammates keeps the team on without any new key.
        enabled = bool(chosen)
    return {
        "enabled": enabled,
        "assignees": list(chosen) if enabled else [],
        "configured": list(chosen),
        "source": source,
        "legacy_gods": legacy,
    }
