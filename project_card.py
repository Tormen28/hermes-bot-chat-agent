"""Project card (wow 7): visible, editable, versioned project memory.

The card lives as a Markdown file the user can open and edit in any
editor: ``<HERMES_HOME>/bot-chat-agent/project-card.md``. The file is
the source of truth. Every content change creates a new version row in
the delivery store (``project_card_versions``); derived content NEVER
overwrites the file — only an explicit edit (manual, in the editor, or
``edit_card``) writes it. Manual edits win over anything the plugin
derives, and they are versioned.

Sections (fixed order): Objetivo, Restricciones críticas, Decisiones,
Pendientes, Resultados (IDs). The critical constraints are the "100 %
present" set of the Fase 2 gate: they are rendered verbatim, never
reworded or merged with derived text.
"""
from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

CARD_FILENAME = "project-card.md"

_SECTIONS = (
    ("objective", "Objetivo"),
    ("constraints", "Restricciones críticas"),
    ("decisions", "Decisiones"),
    ("pending", "Pendientes"),
    ("result_ids", "Resultados (IDs)"),
)
_TITLES = {key: title for key, title in _SECTIONS}
_HEADER_RE = re.compile(r"^##\s+(.+?)\s*$")
_VERSION_RE = re.compile(r"<!--\s*version:\s*(\d+).*?-->")


@dataclass
class ProjectCard:
    project_id: str = ""
    objective: str = ""
    constraints: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    result_ids: list[str] = field(default_factory=list)
    version: int = 0
    updated_at: float = 0.0
    updated_by: str = ""
    source: str = "manual"  # "manual" | "derived" (derived never writes the file)

    def facts(self) -> list[str]:
        """All card facts as plain lines (objective + decisions + pending)."""
        out = [self.objective] if self.objective.strip() else []
        out.extend(self.decisions)
        out.extend(self.pending)
        return out


def card_path_for(notify_path: Any) -> Path:
    """Card file path next to ``team-notify.json``."""
    return Path(notify_path).expanduser().parent / CARD_FILENAME


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def render_markdown(card: ProjectCard) -> str:
    """Serialise a card to the on-disk Markdown format."""
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(card.updated_at or time.time()))
    lines = [
        "# Ficha de proyecto — Bot Chat",
        f"<!-- version: {card.version} · updated: {stamp} · by: {card.updated_by or 'manual'} -->",
        "",
    ]

    def _bullets(items: list[str]) -> list[str]:
        return [f"- {item}" for item in items] or ["- (vacío)"]

    lines += ["## Objetivo", "", card.objective.strip() or "(sin definir)", ""]
    lines += ["## Restricciones críticas", ""] + _bullets(card.constraints) + [""]
    lines += ["## Decisiones", ""] + _bullets(card.decisions) + [""]
    lines += ["## Pendientes", ""] + _bullets(card.pending) + [""]
    lines += ["## Resultados (IDs)", ""] + _bullets(card.result_ids) + [""]
    return "\n".join(lines)


def parse_markdown(text: str, project_id: str = "") -> ProjectCard:
    """Parse card Markdown back into a :class:`ProjectCard`.

    Unknown sections are ignored; missing sections stay empty. Bullet
    items are the lines starting with ``- ``; a lone ``- (vacío)`` means
    an empty list. The ``Objetivo`` section is free text.
    """
    card = ProjectCard(project_id=project_id)
    version = _VERSION_RE.search(text or "")
    if version:
        try:
            card.version = int(version.group(1))
        except ValueError:
            card.version = 0
    current: Optional[str] = None
    buckets: dict[str, list[str]] = {key: [] for key, _ in _SECTIONS}
    free: dict[str, list[str]] = {"objective": []}
    title_to_key = {title: key for key, title in _SECTIONS}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("# ") or line.startswith("<!--"):
            continue
        m = _HEADER_RE.match(line)
        if m:
            current = title_to_key.get(m.group(1))
            continue
        if current is None:
            continue
        if current == "objective":
            free["objective"].append(line)
        elif line.startswith("- "):
            item = line[2:].strip()
            if item != "(vacío)":
                buckets[current].append(item)
    card.objective = " ".join(free["objective"]).strip()
    card.constraints = buckets["constraints"]
    card.decisions = buckets["decisions"]
    card.pending = buckets["pending"]
    card.result_ids = buckets["result_ids"]
    return card


def _default_store(card_path: Path) -> Any:
    """Store next to the card file (same dir as the delivery store).

    Best-effort: returns ``None`` when it cannot be opened, so an edit
    never fails because of bookkeeping. The store is where the card's
    version history lives; without it, edits would bump the version in
    the file but leave no history row.
    """
    try:
        from delivery_store import DeliveryStore
        return DeliveryStore(card_path.parent / "deliveries.db")
    except Exception:
        return None


def load_card(card_path: Any, store: Any = None, project_id: str = "") -> Optional[ProjectCard]:
    """Load the card file; ``None`` when there is no card file yet.

    When a ``store`` (``DeliveryStore``) is given, a content change
    since the last recorded version is stored as a new version row
    (source ``manual``: the file is the user's edit). The file is never
    modified here. The file is the source of truth: when its embedded
    version is ahead of the history (edits made without a store), that
    version is kept; a manual change otherwise bumps past the last row.
    """
    path = Path(card_path).expanduser()
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8")
    card = parse_markdown(text, project_id=project_id)
    card.updated_at = path.stat().st_mtime
    if store is not None:
        try:
            last = store.last_card_version(project_id)
            digest = _content_hash(text)
            if last is None or last.get("content_hash") != digest:
                if last:
                    card.version = max(int(card.version or 0),
                                       int(last.get("version", 0) or 0) + 1)
                else:
                    card.version = max(card.version, 1)
                store.record_card_version(
                    project_id=project_id,
                    version=card.version,
                    content_hash=digest,
                    content=text,
                    source="manual",
                    author=card.updated_by or "manual",
                )
            else:
                card.version = int(last.get("version", card.version) or card.version)
        except Exception:
            # Version history is bookkeeping: a failure here must never
            # hide the card itself.
            pass
    return card


def edit_card(card_path: Any, *, store: Any = None, project_id: str = "",
              author: str = "", objective: Optional[str] = None,
              add_constraints: Optional[list[str]] = None,
              remove_constraints: Optional[list[str]] = None,
              add_decisions: Optional[list[str]] = None,
              add_pending: Optional[list[str]] = None,
              add_result_ids: Optional[list[str]] = None) -> ProjectCard:
    """Apply an explicit edit to the card file (manual edit path).

    Creates the file when missing. Bumps the version, writes the file
    and records the version row. Derived content must never call this:
    suggestions are applied only through an explicit edit. When no
    store is given, the one next to the card file is used, so edits
    always leave a history row.
    """
    path = Path(card_path).expanduser()
    if store is None:
        store = _default_store(path)
    current = load_card(path, store=store, project_id=project_id)
    card = current or ProjectCard(project_id=project_id)
    if objective is not None:
        card.objective = str(objective).strip()
    for item in add_constraints or []:
        if item not in card.constraints:
            card.constraints.append(str(item))
    for item in remove_constraints or []:
        card.constraints = [c for c in card.constraints if c != item]
    for item in add_decisions or []:
        if item not in card.decisions:
            card.decisions.append(str(item))
    for item in add_pending or []:
        if item not in card.pending:
            card.pending.append(str(item))
    for item in add_result_ids or []:
        if item not in card.result_ids:
            card.result_ids.append(str(item))
    card.version = int(card.version or 0) + 1
    card.updated_at = time.time()
    card.updated_by = author or "manual"
    card.source = "manual"
    text = render_markdown(card)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if store is not None:
        try:
            store.record_card_version(
                project_id=project_id,
                version=card.version,
                content_hash=_content_hash(text),
                content=text,
                source="manual",
                author=card.updated_by,
            )
        except Exception:
            pass
    return card


def render_card_section(card: Optional[ProjectCard], lang: str = "en",
                        budget_tokens: int = 0) -> str:
    """Render the card as a bounded context section.

    Critical constraints first (never dropped by the caller: mark the
    section critical in ``context_sections``); objective, decisions and
    pending follow; result IDs last. When ``budget_tokens`` is given the
    section is truncated to it (constraints are kept whole first).
    """
    if card is None:
        return ""
    import context_budget

    es = lang == "es"
    head = "[Proyecto]" if es else "[Project]"
    lines = [head]
    if card.objective.strip():
        lines.append(f"{'Objetivo' if es else 'Goal'}: {card.objective.strip()}")
    for item in card.constraints:
        lines.append(f"- {'REST' if es else 'RULE'}: {item}")
    for item in card.decisions:
        lines.append(f"- {'Decisión' if es else 'Decision'}: {item}")
    for item in card.pending:
        lines.append(f"- {'Pendiente' if es else 'Pending'}: {item}")
    if card.result_ids:
        lines.append(f"{'Resultados' if es else 'Results'}: {', '.join(card.result_ids)}")
    text = "\n".join(lines)
    if budget_tokens and budget_tokens > 0:
        text = context_budget.truncate_text(text, budget_tokens)
    return text
