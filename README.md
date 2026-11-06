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
| Team | With no `team.gods`, Bot Chat does the work itself. With profile names configured, it assigns kanban subcards to those profiles and will not close them until the board shows `done`. |
| Desktop | A quiet status bar above the composer (`Working… · <step>`, `Waiting for your OK`, `Waiting on <profile>`, `Done · <summary>`, `Error: <short summary>`), and reasoning collapsed by default while a Bot Chat is focused. Tool and approval cards are untouched. |

Only sessions titled **"Bot Chat"** (the title Hermes Bot Mode uses) and their compaction children get the reply style.
The safety guard also protects `delegate_task` / subagent child sessions spawned from a Bot Chat (inheriting parent Bot Chat protection), while normal sessions and subagents of normal sessions behave exactly as before.
A kanban worker session is outside that scope, so the assigned profile can deliver its own card. `scope: all` still lets that worker finish the card whose id is `HERMES_KANBAN_TASK`.

## Install

Requires Hermes `>= 0.22.5`.

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
          enabled: true
          gods: []             # Hermes profile names. Empty = single-operator.
        kanban_db: ""          # empty = <HERMES_HOME>/kanban.db (or the HERMES_KANBAN_BOARD path)
        notify_state_path: ""  # empty = <HERMES_HOME>/bot-chat-agent/team-notify.json
```
Any `mcp_*` tool whose name contains send/post/reply/publish/tweet/email is also treated as an outbound send.

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
| deletes | `rm`, `Remove-Item`, `del`, `DROP TABLE`, `DELETE FROM`, `shutil.rmtree` |
| destructive_git | `push --force`, `reset --hard`, `clean -f`, `branch -D`, `rebase` |
| protected_push | `git push … main`, merges into protected branches, `gh pr merge` |
| external_sends | `send_message`, `react_to_message`, outbound MCP tools |
| expensive_models | `delegate_task` with a model matching the patterns |
| one_question_per_turn | a second `clarify` in the same turn |
| kanban_lock | writing `kanban.db`, mutating `hermes kanban` from the shell, impersonating the user, or creating a card that is already finished. Missing `idempotency_key` on `kanban_create` is filled in, not blocked |
| wait_for_god | `kanban_complete` (and review/archive) of a card assigned to `team.gods`, or of an ancestor that still has such a card open, until that card's status is `done` |

The guard is a safety net based on patterns, not a sandbox. Keep Hermes's own approval settings on.

## One bot or a team

Both work. Nothing here depends on a particular set of profile names.

**Single-operator** (`team.gods: []`, the default). You ask, Bot Chat says `On it — …` and does the work in that chat. It does not create teammates, and it does not wait on a board. The kanban lock still applies if the model tries to write the board database directly.

**Team.** Put existing Hermes profile names in `team.gods` (the names you already use, whatever they are). When work belongs to one of them, Bot Chat creates a kanban subcard assigned to that profile. The guard then refuses to close that card, or a parent/fork that still has it open, until `kanban.db` shows the card `done`. The assigned profile delivers with `kanban_complete` inside its own dispatcher worker. The next Bot Chat turn gets a one-line result note, once, and the Desktop bar can show `Waiting on <profile>` and then `Done · <summary>`.

`wait_for_god` is off in the `relaxed` preset. `team.enabled: false` also turns the team note and the wait off, even if names are listed.

### How the wait is enforced

It is a `pre_tool_call` decision, not only prompt text. On `kanban_complete`, `kanban_request_review`, and `kanban_archive` the plugin opens the board **read-only**, reads `tasks` and `task_links`, and blocks while a configured assignee (or a descendant assigned to one) is not `done`. There is one hook, shared with the 0.2.0 rules, so a second plugin is not required and cannot disagree.

The worker is allowed to finish only its own card: Hermes sets `HERMES_KANBAN_TASK` to that id, and a kanban-sourced worker session is outside Bot Chat scope. Shell `hermes kanban complete`, SQL against `kanban.db`, a `created_by: user` spoof, and a `kanban_create` that is already `done` or already has a `result` are blocked as the kanban lock.

### What this plugin does not include

Hermes already owns the board. This plugin does not ship `kanban_*` tools, does not start the dispatcher, and does not create or edit profiles.

- The orchestrator profile needs the Hermes `kanban` toolset, or `kanban_create` is not there to call.
- A running gateway dispatcher spawns the assignee. Profile names in `team.gods` must already exist; this plugin will not invent them.
- Hermes stamps `tasks.session_id` from `HERMES_SESSION_ID`. That is how the turn note lists open cards for this chat. Completing by task id still checks the assignee even when `session_id` is missing.
- Hermes `kanban.auto_subscribe_on_create` (default true) wakes the originating chat when a card finishes. This plugin adds the one-line note and the Desktop phases; it does not replace that subscription.
- Default board path: `<HERMES_HOME>/kanban.db`, or `<HERMES_HOME>/kanban/boards/<HERMES_KANBAN_BOARD>/kanban.db`. Override with `kanban_db` or `HERMES_KANBAN_DB`.
- Delivery dedup file: `<HERMES_HOME>/bot-chat-agent/team-notify.json` (task id, assignee, one-line summary). Override with `notify_state_path`.
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
| Equipo | Sin `team.gods`, el Bot Chat hace el trabajo él solo. Con nombres de perfiles, asigna subtareas y no las cierra hasta que el tablero las muestra hechas. |
| Desktop | Una barra de estado discreta (`Cargando… · <paso>`, `Esperando tu OK`, `Esperando a <perfil>`, `Listo · <resumen>`, `Error: <resumen corto>`) y el razonamiento colapsado por defecto mientras estás en un Bot Chat. Las tarjetas de herramientas y de aprobación no se ocultan ni se tocan. |

Solo las sesiones llamadas **"Bot Chat"** (el título que usa el Modo Bot de Hermes) y sus hijas de compactación reciben el estilo de respuesta.
La guardia de seguridad también protege las sesiones hijas `delegate_task` / subagentes lanzadas desde un Bot Chat (heredan la protección del Bot Chat padre), mientras que las sesiones normales, los subagentes de sesiones normales y kanban siguen igual.
Puedes elegir un preset de guardia (`preset: strict | default | relaxed`). `strict` y `default` incluyen el candado del kanban y la espera al compañero. `relaxed` deja el candado puesto y apaga la espera. Sin nombres en `team.gods` no hay equipo: el bot trabaja solo y no inventa perfiles. El detalle está en inglés, arriba.

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
nunca lee `.env`, `auth.json` ni credenciales, no hace llamadas de red y no envía telemetría.

**Limitación conocida:** el SDK público de Desktop puede colapsar el razonamiento pero aún no puede ocultar las
tarjetas de herramientas por sesión, así que esas tarjetas siguen visibles en Bot Chat.

### Licencia
MIT. Ver [LICENSE](LICENSE).
