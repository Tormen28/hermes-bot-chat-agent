Screenshots in this folder come in two kinds. None of them is a drawing or a mock UI.

**01–03: real Hermes Desktop captures** (taken on 2026-11-03 with a real model on a clean demo profile; the Desktop UI was in Spanish, so the bar reads "Cargando… · Pensando…").

- 01-loading.png: Bot Chat while working (status bar).
- 02-answer.png: full answer, reasoning collapsed.
- 03-short-answer.png: short, direct answer.

**05–12: real Hermes Webapp v0.23.1** (core built from source, the plugin installed as a unified package, throw-away `HERMES_HOME`, plugin language `es`). The **model is a simulated local mock** that follows the plugin's injected instructions, because there is no API key on that machine: it proves the plugin's wiring (offer, profile creation, delegation, status card, memory injection), not how a real LLM would phrase or decide things. Real models are not guaranteed to follow the one-line offer every time.

- 05-real-enable-desktop-half.png: Capabilities → Plugins: the Desktop half of a unified plugin is opt-in; turn it on here.
- 06-real-first-run-offer.png: one-time first-run suggestion of a small starter set (nothing is created).
- 07/08: memory across two separate Bot Chat sessions (session 1 stores `Decisión: …`; the session was then deleted with `hermes sessions delete` and a new one opened; the injected long-term memory reaches the model, and the card shows `Recordadas:`).
- 09-real-panteon-offer.png: the one-line offer to create Daedalus.
- 10-real-panteon-created.png: after a plain "sí": the Daedalus row in the stock Bots panel (no separate panel).
- 11-real-delegation-waiting.png: `Hermes → daedalus: esperando respuesta` (real `message_agent` ack, real in-flight request).
- 12-real-delegation-done.png: `Hermes → daedalus: listo`, only after the real completion notice arrived.
