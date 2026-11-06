# Changelog

## 0.2.0 - 2026-11-05
- Safety guard now covers `delegate_task` / subagent child sessions spawned from a canonical "Bot Chat" session (or its compaction chain), inheriting the parent Bot Chat scope while keeping the reply-style hook limited to the main Bot Chat session.
- Added guard presets (`strict`, `default`, `relaxed`) that package the existing rule groups (`secrets`, `deletes`, `destructive_git`, `protected_push`, `external_sends`, `expensive_models`, `one_question_per_turn`) with optional per-rule boolean overrides.
- Clearer SDK-only Desktop status bar with distinct phases: `Working… · <activity>`, `Waiting for your OK`, and short `Error: <summary>`, without hiding tool or approval cards.

## 0.1.0 - 2026-11-02
- First public release.
- `pre_llm_call`: concise, agentic reply style for canonical "Bot Chat" sessions (English; Spanish via `language: es`).
- `pre_tool_call`: configurable safety guard (secrets, deletes, destructive git, protected-branch push/merge,
  outbound messages, expensive models, one question per turn). Block-only; never approves.
- Desktop: "Working…" status bar above the composer and reasoning collapsed by default, Bot Chat only, SDK-only.
