# Live-core audit (Hermes core v0.23.0)

Checked on the machine where Bot Chat runs, 2026-11-09. Nothing in the core was modified.

| Item | Value |
|---|---|
| Core | `hermes-agent` tag `v0.23.0`, git `2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19` |
| `PluginContext.has_capability` | present |
| `ctx.read_session` (`read_session`) | **absent** |
| `ctx.record_delegation_delivery` (B1) | **absent** |
| `ctx.record_completion_note` (B1) | **absent** |
| `transform_tool_result` hook | present (`model_tools._apply_transform_tool_result_hook`, runs after `post_tool_call`, first `str` replaces the result, fails open) |
| `SessionDB.append_delegation_delivery` / `append_completion_note` | present in the core, **not reachable from a plugin** through `ctx` (and the core must not be patched) |

How the probe was run: `tools/stability/live_probe.py` with the core's own venv python. It copies the plugin into a throw-away `HERMES_HOME`, loads it through the real `PluginManager`, inspects the context the core hands to `register()`, runs the guard corpus through the real `pre_tool_call` hook and measures p95.

## What the plugin does about it

* Detection is by capability (`hasattr` on `ctx`), never by version, and `has_b1()` treats a context whose attribute access raises as "no B1".
* `delivery.destination: offer` (default) is the live path: a finished card's result is offered in the next Bot Chat turn and confirmed after generation. Nothing is lost if the turn is cancelled (the offer is re-offered).
* `auto` / `b1` fall back to `offer` automatically while B1 is absent; they switch to the durable relay as soon as a core exposes both methods.
* `delivery_path()` returns `{mode, b1_present, active_path}`; `register()` logs the active path at load.
* `tests/test_core_compat.py` exercises a v0.23.0-shaped context, a partial-B1 context and a context that raises, for every mode.

## Known consequences on core 0.23.0

* A delegated result reaches the user only when a Bot Chat turn happens (it is not written into the transcript while the chat is idle). That is the pre-existing `offer` behaviour.
* Without `read_session` the plugin identifies Bot Chat sessions by reading `state.db` read-only.
