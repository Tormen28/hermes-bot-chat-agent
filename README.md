# Bot Chat Agent for Hermes

**English** · [Español](#español)

> A calmer, simpler Hermes Bot Chat for people who just want answers.

**0.3.0-dev** on this branch is a private/test cut for a personal Hermes. It is not the Nous catalog release. Installing `Tormen28/hermes-bot-chat-agent` without a pin follows `main` (0.2.0) until this is reviewed and merged. To try this cut, install from a checkout of the branch.

## Why this exists

Hermes is powerful, and that power shows on screen: reasoning blocks, tool cards, diffs, logs, follow-up questions.
For developers that is great. For many people (students, small business owners, family members, anyone
who is not technical) it can feel overwhelming.

**Bot Chat Agent** gives them a friendlier Hermes Bot Chat: you ask, you see a quiet *"Working…"* bar,
and you get a short, direct answer. Less on screen, fewer questions, the same Hermes underneath.
If you want every detail, open a normal session: it stays exactly as it was.

## Screenshots

| Working | Answer | Short and direct |
|---|---|---|
| ![Working bar](docs/screenshots/01-loading.png) | ![Answer, reasoning collapsed](docs/screenshots/02-answer.png) | ![Short answer](docs/screenshots/03-short-answer.png) |

*(Captured on a clean demo profile; the Desktop UI was in Spanish, so the bar reads "Cargando… · Pensando…".)*

## What it does

| Layer | What changes |
|---|---|
| Style (`pre_llm_call`) | A short "act, don't narrate" contract: one acknowledgement (`On it — …`), then the work, then a short result. At most one question per turn. |
| Safety guard (`pre_tool_call`) | **Blocks** risky tool calls (secrets, deletes, destructive git, pushes to protected branches, outbound messages, expensive models, kanban-lock bypasses) and asks you first. Covers both the Bot Chat session and its `delegate_task` / subagent child sessions. It never approves anything. |
| Team | With no `team.assignees`, Bot Chat does the work itself. With profile names configured, it assigns kanban subcards to those profiles and will not close them until the board shows `done`. |
| Desktop | A quiet status bar above the composer (`Working… · <step>`, `Waiting for your OK`, `Waiting on <profile>`, `Done · <summary>`, `Error: <short summary>`), and reasoning collapsed by default while a Bot Chat is focused. Tool and approval cards are untouched. |
| `/memoria` (alias `/memory`) | Lists what the plugin remembers (long-term entries by type, short-term summary, pending asks), forgets or edits an entry by id, and clears all after confirmation. `/memoria` replies in Spanish; `/memory` follows the plugin language (English by default). Registered with the core's `register_command` API; skipped safely if the core does not expose it. |
| Status panel | A compact `[Status]` header in the turn context when there is something to show: running background tasks, pending asks, memories recalled this turn, and the compact token count. The Desktop bar can show the same line as a suffix. Toggle with `status_panel.enabled`. |

Only sessions titled **"Bot Chat"** (the title Hermes Bot Mode uses) and their compaction children get the reply style.
The safety guard also protects `delegate_task` / subagent child sessions spawned from a Bot Chat (inheriting parent Bot Chat protection), while normal sessions and subagents of normal sessions behave exactly as before.
A kanban worker session is outside that scope, so the assigned profile can deliver its own card. `scope: all` still lets that worker finish the card whose id is `HERMES_KANBAN_TASK`.

## Install

Requires Hermes `>= 0.22.5` (verified: that version already has every hook this plugin uses — `post_llm_call`, `agent_loop_stopped`, `kanban_task_blocked` — checked against the core tags; no raise was needed).

```bash
hermes plugins install /path/to/this/checkout
hermes plugins enable bot-chat-agent
```

`hermes plugins install Tormen28/hermes-bot-chat-agent` follows the default branch. Use a local checkout of this branch until it is merged.
Desktop: **Capabilities → Plugins → Bot Chat Agent** → enable. Restart the Desktop app if it does not appear.
Then open **Bots**, pick a bot and chat.

## Disable / uninstall

```bash
hermes plugins disable bot-chat-agent
hermes plugins remove bot-chat-agent
```
Or switch it off in **Capabilities → Plugins**. Disabling restores your previous "collapse reasoning" setting.

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
        preset: default         # strict | default | relaxed
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
        team:
          assignees: []        # Hermes profile names. Empty = single-operator (the default).
                               # `team.gods` is the old key and still works; if both keys
                               # exist and differ, the plugin refuses to guess and blocks
                               # with an explanation. A fresh install is individual; an
                               # existing install that already lists teammates keeps the
                               # team on (no new key needed). `team.enabled: false` turns
                               # the team off even if names are listed.
        kanban_db: ""          # empty = <HERMES_HOME>/kanban.db (or the HERMES_KANBAN_BOARD path)
        notify_state_path: ""  # empty = <HERMES_HOME>/bot-chat-agent/team-notify.json
        delivery:              # Fase 1a delivery store (SQLite, next to team-notify.json)
          backend: sqlite      # sqlite | json (json = legacy path, revert switch)
          offer_lease_seconds: 600   # an offered notice is re-offered after this
          db_path: ""          # empty = <HERMES_HOME>/bot-chat-agent/deliveries.db
          destination: offer   # Fase 1b: offer (default, in-turn only) | auto | b1
                               # auto/b1 write the result durably into the session
                               # transcript when the core exposes B1 (ctx methods
                               # record_delegation_delivery / record_completion_note);
                               # on the live core (no B1) they fall back to offer.
          relay_limit: 50      # max pending rows per durable-relay attempt
          retention_days: 30   # prune only delivered/seen rows older than this
        context:              # Fase 2: bounded context (rubric docs/fase2-rubric.md)
          dynamic_tokens: 1500   # per-turn budget: project state + notices
          stable_tokens: 300     # budget for the stable style instructions
          per_piece_tokens: 2000 # cap per context piece (plugin policy)
          engine_name: bot-chat  # ContextEngine name; activate with context.engine: bot-chat
          incremental_note: true # after the first turn, only what changed
        memory:                # generic memory; local store only, no vault
          enabled: true
          short_term: true     # rolling summary of earlier turns + pending asks
          long_term: true      # decisions / project state / lessons
          recent_messages: 8
          summary_budget_tokens: 400
          long_term_budget_tokens: 400
          max_entries: 500     # prune oldest when the store grows past this
        status_panel:
          enabled: true        # compact [Status] header; false hides it
```
Any `mcp_*` tool whose name contains send/post/reply/publish/tweet/email is also treated as an outbound send.

**Bounded context (Fase 2).** The turn context is split into stable instructions (≤300 tokens), project state, tasks and notices (≤1,500 tokens, 2,000 per piece), measured with the tokenizer. After the first full turn the team note is incremental — only what changed is injected; a turn with no changes injects one short line. Critical constraints are never dropped; anything else that does not fit is listed as dropped. The project card (`<HERMES_HOME>/bot-chat-agent/project-card.md`, wow 7) is the source of truth: manual edits always win, are versioned in the store, and derived content never writes the file. After a cancellation, a compaction or a reopen, the context is rebuilt from the durable store. The plugin also registers a delegating `ContextEngine` named `bot-chat` (no core change: compaction is still delegated to the core's `ContextCompressor`; only `select_context` is overridden) — activate it with `context.engine: bot-chat` in the core config (single slot; without it the `pre_llm_call` hook keeps injecting).

**Memory.** Enabled by default and fully local: a rolling extractive summary of earlier turns (tool payloads skipped) plus the pending-asks reminder, and a long-term store of decisions, project state and lessons at `<HERMES_HOME>/bot-chat-agent/memory.db`. `pre_llm_call` injects only the summary and the entries that overlap the current request, each under its token budget. `post_llm_call` writes new entries back when the turn text contains clear cues (`Decision:`, `Lesson learned:`, `Decidimos`, `Lección:`, …) — no model and no network. Secret-like text is not stored. Fail-open: a missing or corrupt store never breaks the turn. Honest limit on Hermes v0.23: this hook can only *add* a bounded memory block; it cannot replace the core's growing `conversation_history`. The optional `context.engine: bot-chat` path includes the same block in `select_context`. Numbers and method: `docs/memory-bench.md`. Turn the feature off with `memory.enabled: false`.

**`/memoria` and `/memory`.** In a Bot Chat session type `/memoria` (Spanish labels) or `/memory` (English by default) to see the long-term store, the rolling summary and any pending asks. Each long-term row has an 8-character id: `/memory forget ab12cd34` drops one entry, `/memory edit ab12cd34 New text` corrects it, `/memory clear` asks for confirmation and `/memory clear confirm` erases every long-term row. Nothing is changed if the id is unknown or the store cannot be opened.

**Status panel.** When a turn recalls memories, has pending asks or has running background tasks, `pre_llm_call` prepends a one-line `[Status]` header (recalled titles, pending count, task state, compact token count). Turns with nothing to report stay unchanged. Switch it off with `status_panel.enabled: false`. On Desktop the same text can appear as a suffix on the existing status bar when the host forwards a `bot-chat-status` event. A headless walkthrough is `demo/hackathon_demo.py`.


### Guard presets

Presets only package the existing YAML rule groups (no free-form regex allowlists; defaults stay safe):

| Preset | Enabled rule groups |
|---|---|
| `strict` | All original 7 rules on, plus `kanban_lock` and `wait_for_god` |
| `default` | Same safe baseline as `strict` |
| `relaxed` | Keeps `secrets`, `destructive_git`, `protected_push`, and `kanban_lock` on; turns off `deletes`, `external_sends`, `expensive_models`, `one_question_per_turn`, and `wait_for_god` |

You can set `preset: relaxed` (or `guard: relaxed`) and still override individual boolean rules inside `guard:`.

## Guard rules
| Rule | Blocks (examples) |
|---|---|
| secrets | reading `.env`, `auth.json`, SSH keys, `*.pem`; `printenv` |
| deletes | `rm`, `Remove-Item`, `del`, `rd /s`, `DROP TABLE`, `DELETE FROM`, `shutil.rmtree` — also through `/bin/rm`, `\rm`, `env rm`, `bash -c "…"`, `powershell -Command`, `eval`, `$(…)`, inline base64 and `-EncodedCommand` |
| destructive_git | `push --force`, `reset --hard`, `clean -f`, `branch -D`, `rebase` |
| protected_push | `git push … main`, merges into protected branches, `gh pr merge` |
| external_sends | `send_message`, `react_to_message`, outbound MCP tools; a local file leaving through the network (`curl -d @file`, `-F f=@file`, `--post-file`, `… \| nc host`, `scp file user@host:`). Loopback targets are allowed |
| expensive_models | `delegate_task` with a model matching the patterns |
| one_question_per_turn | a second `clarify` in the same turn |
| kanban_lock | writing `kanban.db`, mutating `hermes kanban` from the shell, impersonating the user, or creating a card that is already finished. Missing `idempotency_key` on `kanban_create` is filled in, not blocked |
| obfuscated_exec | decoded or decompressed data piped into a shell/interpreter (`… \| base64 -d \| bash`, `bash <(base64 -d …)`, `eval "$(… \| base64 -d)"`) |
| memory_injection | instruction-like text ("always approve…", override phrases) written into `MEMORY.md`, `USER.md`, `SOUL.md`, `AGENTS.md`, memory tools or `>> MEMORY.md` |
| wait_for_god | `kanban_complete` (and review/archive) of a card assigned to `team.assignees`, or of an ancestor that still has such a card open, until that card's status is `done` |

The guard is a safety net based on patterns, not a sandbox. Keep Hermes's own approval settings on.

**What 0.3.0 hardening covers (and what it does not).** Every command is analysed in several forms: as written, normalised (absolute binary paths, alias backslashes, quote splicing, `sudo`/`env`/`nohup` wrappers) and every nested payload it can recover (`sh -c`, `eval`, `$(…)`, inline base64, PowerShell `-EncodedCommand`). The same rules run on each form. Still NOT covered: payloads built at run time (hex-escaped strings, variables concatenated across lines, scripts written to disk then run), and piping a remote download into a shell. Treat it as a seatbelt.

**Untrusted content (A0.7).** Tool output that contains instruction-like text (instruction-override phrases, "approve everything", role markers…) is wrapped as labelled untrusted data by the `transform_tool_result` hook (core 0.23+; on older cores a one-line notice is added to the next turn instead). Worker summaries that Bot Chat itself puts in the context are neutralised (instruction-like spans removed, length capped). Turn it off with `untrusted.wrap_tool_results: false`. This lowers the risk; it cannot make injection impossible.

## One bot or a team

Both work. Nothing here depends on a particular set of profile names.

**Single-operator** (no `team.assignees`, the default on a fresh install). You ask, Bot Chat says `On it — …` and does the work in that chat. It does not create teammates, and it does not wait on a board. A `kanban_create` naming any profile is blocked: individual mode invents no teammates (an unassigned card is still allowed — that is your own board work, not delegation). The kanban lock still applies if the model tries to write the board database directly.

**Team.** Put existing Hermes profile names in `team.assignees` (the names you already use, whatever they are; `team.gods` is the old key and still works). When work belongs to one of them, Bot Chat creates a kanban subcard assigned to that profile. The guard then refuses to close that card, or a parent/fork that still has it open, until `kanban.db` shows the card `done`. The assigned profile delivers with `kanban_complete` inside its own dispatcher worker. The next Bot Chat turn gets a one-line result note, once, and the Desktop bar can show `Waiting on <profile>` and then `Done · <summary>`. A blocked card (`kanban_task_blocked`) is announced the same way. Deliveries are stored in SQLite (`deliveries.db`, protocol `pending → offered → delivered_in_turn`): a notice is offered to a turn, and only confirmed after that turn generates — an interrupted turn loses nothing, the notice is re-offered next turn or when its 10-minute lease expires. Each notice goes only to the session that created the card (tracked via `kanban_create`); without a known session it waits in an unassigned tray and is never announced in another chat. Delivery failures are logged at `warning`, never swallowed.

A new default never disables an existing team: if your config already lists teammates (under `team.assignees` or the old `team.gods`) and has no `team.enabled` key, the team stays on. `team.enabled: false` turns the team off explicitly, even with names listed.

### Core version and degradation

Minimum core is `>= 0.22.5` (that version already has all three hooks this plugin depends on). On an older core the plugin still loads — the core warns on unknown hook names but stores them — and degrades as follows, with nothing silently lost:

| Missing hook | What degrades | What still holds |
|---|---|---|
| `post_llm_call` | Offers are never confirmed by a generated turn; notices are re-offered next turn or when the 10-minute lease expires (at-least-once, deduplicated by ID in the text) | No notice is lost; the lease is the mechanism |
| `agent_loop_stopped` | No fast release on `/stop`; offers are released only by lease expiry | Same as above — slower, not lost |
| `kanban_task_blocked` | A blocked card is never announced in chat (no notice is invented); the card stays visible on the board | The user sees it on the board; nothing is announced in the wrong session |

A contradictory team config (`team.assignees` and `team.gods` both set and different) fails closed: every tool call in scope is blocked with the explanation, the error is logged at load, and the turn note shows it — the guard and the team are never silently disabled.

`wait_for_god` is off in the `relaxed` preset. `team.enabled: false` also turns the team note and the wait off, even if names are listed.

### How the wait is enforced

It is a `pre_tool_call` decision, not only prompt text. On `kanban_complete`, `kanban_request_review`, and `kanban_archive` the plugin opens the board **read-only**, reads `tasks` and `task_links`, and blocks while a configured assignee (or a descendant assigned to one) is not `done`. There is one hook, shared with the 0.2.0 rules, so a second plugin is not required and cannot disagree.

The worker is allowed to finish only its own card: Hermes sets `HERMES_KANBAN_TASK` to that id, and a kanban-sourced worker session is outside Bot Chat scope. Shell `hermes kanban complete`, SQL against `kanban.db`, a `created_by: user` spoof, and a `kanban_create` that is already `done` or already has a `result` are blocked as the kanban lock.

### What this plugin does not include

Hermes already owns the board. This plugin does not ship `kanban_*` tools, does not start the dispatcher, and does not create or edit profiles.

- The orchestrator profile needs the Hermes `kanban` toolset, or `kanban_create` is not there to call.
- A running gateway dispatcher spawns the assignee. Profile names in `team.assignees` must already exist; this plugin will not invent them.
- Hermes stamps `tasks.session_id` from `HERMES_SESSION_ID`. That is how the turn note lists open cards for this chat. Completing by task id still checks the assignee even when `session_id` is missing.
- Hermes `kanban.auto_subscribe_on_create` (default true) wakes the originating chat when a card finishes. This plugin adds the one-line note and the Desktop phases; it does not replace that subscription.
- Default board path: `<HERMES_HOME>/kanban.db`, or `<HERMES_HOME>/kanban/boards/<HERMES_KANBAN_BOARD>/kanban.db`. Override with `kanban_db` or `HERMES_KANBAN_DB`.
- Delivery store: `<HERMES_HOME>/bot-chat-agent/deliveries.db` (SQLite; task id, kind, session id, assignee, one-line summary, status). Override with `delivery.db_path`. The legacy `team-notify.json` is imported once (pending entries not yet announced) and never modified afterwards; `delivery.backend: json` reverts to the legacy path without uninstalling. The store must live on a local disk, never in a synced folder or on a network path (checked at startup, warning only). Fase 1b: the outbox is complete — relay claim leases, dead-letter after 5 failed attempts, retention prune (delivered/seen only, `delivery.retention_days`), `oldest_pending_age_seconds` in `stats()`, and a switchable durable destination: `delivery.destination: offer` (default — the result is offered in the next turn) or `auto`/`b1`, which writes the result as a durable row in the destination session's transcript when the core exposes B1, with a stable id per delivery (a retry after a crash returns the same message id, never a second row) and a durable mark recorded before confirming, so a crash between the write and the confirm can never re-offer the result.
- It does not write `state.db` or `kanban.db`, and it does not edit `SOUL.md` or profile files.

## Disclosures
- Reads `<HERMES_HOME>/state.db` **read-only** (sessions table: id, title, parent, source) to find Bot Chat sessions. Never writes it.
- Reads the kanban board **read-only** (`tasks`, `task_links`) when team wait or the open-card note needs it. Never writes it.
- Never reads `.env`, `auth.json` or any credential store. No network calls. No telemetry.
- Desktop: changes the `reasoning.collapsedByDefault` setting while a Bot Chat is focused and restores the previous value when you leave it or disable the plugin. If the app crashes inside a Bot Chat, the setting may stay on; toggle it in Settings.

## Known limitation
The public Desktop SDK can collapse reasoning but cannot hide tool cards per session. Full "answer-only" display
needs a new Desktop SDK hook (proposed upstream). Until then tool cards still show in Bot Chat.

## Development
```bash
python -m pytest tests -q
hermes plugins validate . --install-deps
```

Strict stability run (exit code AND `ok is True`, battery compared by test name, `hermes send` checked, one final notification):

```bash
python tools/stability/runner.py --plugin <installed plugin dir> --python <python with pytest> \
    --live-python <hermes venv python> --minutes 60 --notify-final
python tools/stability/r26_audit.py <plugin dir>     # disclosure audit: static + runtime (audit hook)
```

POSIX-only tests (`fork`, `SIGKILL`) are skipped on Windows with a reason; `tests/test_concurrency_spawn.py` covers the same store guarantees there with real `spawn` contention.

## License
MIT. See [LICENSE](LICENSE).

---

## Español

> Un Bot Chat de Hermes más tranquilo y simple, para quien solo quiere respuestas.

### Por qué existe

Hermes es muy potente, y esa potencia se ve en pantalla: bloques de razonamiento, tarjetas de herramientas,
diffs, logs, preguntas de seguimiento. Para un desarrollador eso está genial. Para mucha gente (estudiantes,
emprendedores, familia, cualquiera que no sea técnico) puede ser abrumador.

**Bot Chat Agent** les da un Bot Chat de Hermes más amable: preguntas, ves una barra discreta de
*"Cargando…"* y recibes una respuesta corta y directa. Menos cosas en pantalla, menos preguntas, el mismo
Hermes por debajo. Si quieres ver todo el detalle, abre una sesión normal: queda exactamente igual.

### Capturas

| Cargando | Respuesta | Corto y directo |
|---|---|---|
| ![Barra de carga](docs/screenshots/01-loading.png) | ![Respuesta, razonamiento colapsado](docs/screenshots/02-answer.png) | ![Respuesta corta](docs/screenshots/03-short-answer.png) |

### Qué hace

| Capa | Qué cambia |
|---|---|
| Estilo (`pre_llm_call`) | Un contrato corto de "actúa, no narres": un acuse (`Voy a…`), hace el trabajo, resultado corto. Máximo una pregunta por turno. |
| Guardia de seguridad (`pre_tool_call`) | **Bloquea** acciones riesgosas (secretos, borrados, git destructivo, push a ramas protegidas, mensajes salientes, modelos caros, saltarse el candado del kanban) y te pregunta primero. Cubre tanto el Bot Chat como sus subagentes hijos (`delegate_task`). Nunca aprueba nada por ti. |
| Equipo | Sin `team.assignees`, el Bot Chat hace el trabajo él solo. Con nombres de perfiles, asigna subtareas y no las cierra hasta que el tablero las muestra hechas. |
| Desktop | Una barra de estado discreta (`Cargando… · <paso>`, `Esperando tu OK`, `Esperando a <perfil>`, `Listo · <resumen>`, `Error: <resumen corto>`) y el razonamiento colapsado por defecto mientras estás en un Bot Chat. Las tarjetas de herramientas y de aprobación no se ocultan ni se tocan. |

Solo las sesiones llamadas **"Bot Chat"** (el título que usa el Modo Bot de Hermes) y sus hijas de compactación reciben el estilo de respuesta.
La guardia de seguridad también protege las sesiones hijas `delegate_task` / subagentes lanzadas desde un Bot Chat (heredan la protección del Bot Chat padre), mientras que las sesiones normales, los subagentes de sesiones normales y kanban siguen igual.
Puedes elegir un preset de guardia (`preset: strict | default | relaxed`). `strict` y `default` incluyen el candado del kanban y la espera al compañero. `relaxed` deja el candado puesto y apaga la espera. Sin nombres en `team.assignees` no hay equipo: el bot trabaja solo y no inventa perfiles. El detalle está en inglés, arriba.

### Instalar

Requiere Hermes `>= 0.22.5`.

```bash
hermes plugins install Tormen28/hermes-bot-chat-agent
hermes plugins enable bot-chat-agent
```
Desktop: **Capacidades → Plugins → Bot Chat Agent** → activar. Reinicia la app si no aparece.
Luego abre **Bots**, elige un bot y chatea.

### Desactivar / desinstalar

```bash
hermes plugins disable bot-chat-agent
hermes plugins remove bot-chat-agent
```
O apágalo en **Capacidades → Plugins**. Al desactivarlo se restaura tu ajuste anterior de "colapsar razonamiento".

### Configuración y privacidad

La configuración (`language: es` para respuestas en español), las reglas de la guardia y las divulgaciones
están detalladas arriba en inglés. En resumen: lee `state.db` **solo en modo lectura** para encontrar los Bot Chat,
nunca lee `.env`, `auth.json` ni credenciales, no hace llamadas de red y no envía telemetría. El contexto acotado
(Fase 2) se activa con `context.engine: bot-chat`; la ficha de proyecto vive en
`<HERMES_HOME>/bot-chat-agent/project-card.md` y puedes editarla a mano: tus ediciones siempre mandan.

**Limitación conocida:** el SDK público de Desktop puede colapsar el razonamiento pero aún no puede ocultar las
tarjetas de herramientas por sesión, así que esas tarjetas siguen visibles en Bot Chat.

### Licencia
MIT. Ver [LICENSE](LICENSE).
