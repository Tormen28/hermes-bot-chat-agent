# Bot Chat Agent for Hermes

[Español](README.es.md)

Makes a bot's **canonical "Bot Chat" session** (Bot Mode / hermes-bots) feel like a focused assistant,
without changing any other session, profile or the core.

| Layer | What it does |
|---|---|
| Style (`pre_llm_call`) | Adds a short "act, don't narrate" contract: brief acknowledgement, work, short result; at most one question per turn. |
| Safety guard (`pre_tool_call`) | **Blocks** risky tool calls and tells the model to ask you first. It never approves anything. |
| Desktop | A quiet "Working… · Reading files" bar above the composer, and reasoning collapsed by default while a Bot Chat is focused. Approval cards are untouched. |

Only sessions titled **"Bot Chat"** (the title Hermes Bot Mode uses) and their compaction children are affected.
Subagent sessions and every other session behave exactly as before.

## Requirements
Hermes `>= 0.22.5`.

## Install
```bash
hermes plugins install Tormen28/hermes-bot-chat-agent   # or copy this folder to ~/.hermes/plugins/bot-chat-agent
hermes plugins enable bot-chat-agent
```
Desktop: **Capabilities → Plugins → Bot Chat Agent** → enable. Restart the Desktop app if it does not appear.

## Disable / uninstall
```bash
hermes plugins disable bot-chat-agent
hermes plugins remove bot-chat-agent
```
Disabling the Desktop part restores your previous "collapse reasoning" setting.

## Configuration
In `config.yaml` (all optional; defaults shown):
```yaml
plugins:
  entries:
    bot-chat-agent:
      settings:
        bot_chat_title: "Bot Chat"
        language: en            # en | es
        style: true
        scope: bot_chat         # bot_chat | all  (where the guard runs)
        guard:
          secrets: true
          deletes: true
          destructive_git: true
          protected_push: true
          external_sends: true
          expensive_models: true
          one_question_per_turn: true
        protected_branches: [main, master]
        expensive_model_patterns: ['\bpro\b', 'opus', '\bultra\b', '-max\b']
        external_send_tools: [send_message, react_to_message]
```
Any `mcp_*` tool whose name contains send/post/reply/publish/tweet/email is also treated as an outbound send.

## Guard rules
| Rule | Blocks (examples) |
|---|---|
| secrets | reading `.env`, `auth.json`, SSH keys, `*.pem`; `printenv` |
| deletes | `rm`, `Remove-Item`, `del`, `DROP TABLE`, `DELETE FROM`, `shutil.rmtree` |
| destructive_git | `push --force`, `reset --hard`, `clean -f`, `branch -D`, `rebase` |
| protected_push | `git push … main`, merges into protected branches, `gh pr merge` |
| external_sends | `send_message`, `react_to_message`, outbound MCP tools |
| expensive_models | `delegate_task` with a model matching the patterns |
| one_question_per_turn | a second `clarify` in the same turn |

The guard is a safety net based on patterns, not a sandbox. Keep Hermes's own approval settings on.

## Disclosures
- Reads `<HERMES_HOME>/state.db` **read-only** (sessions table: id, title, parent, source) to find Bot Chat sessions. Never writes it.
- Never reads `.env`, `auth.json` or any credential store. No network calls. No telemetry.
- Desktop: changes the `reasoning.collapsedByDefault` setting while a Bot Chat is focused and restores the previous value when you leave it or disable the plugin. If the app crashes inside a Bot Chat, the setting may stay on; toggle it in Settings.

## Known limitation
The public Desktop SDK can collapse reasoning but cannot hide tool cards per session. Full "answer-only" display
needs a new SDK hook (a feature request to Nous is planned). Until then tool cards still show in Bot Chat.

## Development
```bash
python -m pytest tests -q
hermes plugins validate . --install-deps
```

## License
MIT. See [LICENSE](LICENSE).
