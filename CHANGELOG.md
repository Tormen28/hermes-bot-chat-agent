# Changelog

## 0.1.0 - 2026-11-02
- First public release.
- `pre_llm_call`: concise, agentic reply style for canonical "Bot Chat" sessions (English; Spanish via `language: es`).
- `pre_tool_call`: configurable safety guard (secrets, deletes, destructive git, protected-branch push/merge,
  outbound messages, expensive models, one question per turn). Block-only; never approves.
- Desktop: "Working…" status bar above the composer and reasoning collapsed by default, Bot Chat only, SDK-only.
