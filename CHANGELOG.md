# Changelog

## 0.3.0-dev - 2026-11-06

Private/test cut for a personal Hermes. This is not the Nous catalog release. `main` stays 0.2.0 until this branch is reviewed and merged.

- Short ack, then act (`On it — …` / `Voy a…`). With `team.gods` empty, Bot Chat stays single-operator and does not invent teammates.
- When `team.gods` lists real profile names, Bot Chat assigns kanban subcards to those profiles. One `pre_tool_call` refuses to close that card, or an ancestor that still has it open, until the board shows it `done`. The assignee's own dispatcher worker can still deliver (`HERMES_KANBAN_TASK` matches the card).
- The same hook keeps the 0.2.0 rules and adds a kanban lock: no direct `kanban.db` writes, no mutating `hermes kanban` shell calls, no user impersonation, no pre-finished cards, plus an idempotency key on `kanban_create` when one is missing. `strict` and `default` also enforce wait-for-god. `relaxed` keeps the lock and turns wait-for-god off. Subagents of Bot Chat inherit the guard.
- Desktop adds `Waiting on <profile>` and `Done · <summary>` and keeps Working / Waiting for your OK / Error.
- A god delivery is injected once into the next Bot Chat turn as a one-line result note.

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
