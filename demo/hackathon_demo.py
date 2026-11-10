#!/usr/bin/env python3
"""Headless Nous-hackathon demo: /memoria and the live status panel.

Runs against the plugin on this box (no Hermes Desktop, no model, no network).
Prints a transcript-like log. Typical use:

    python demo/hackathon_demo.py | tee /workspace/hackathon/bot-chat-demo-output.txt
"""
from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

spec = importlib.util.spec_from_file_location("bot_chat_agent", ROOT / "__init__.py")
bca = importlib.util.module_from_spec(spec)
sys.modules["bot_chat_agent"] = bca
spec.loader.exec_module(bca)

import delivery_store  # noqa: E402
import pending_asks  # noqa: E402


class FakeCtx:
    def __init__(self, cfg=None):
        self.cfg = cfg or {"language": "en", "team": {"assignees": []}}
        self.hooks = {}
        self.commands = {}

    def get_config(self, key, default=None):
        return self.cfg.get(key, default)

    def register_hook(self, name, fn):
        self.hooks[name] = fn

    def register_command(self, name, handler, description="", args_hint="", **k):
        self.commands[str(name).lower().strip().lstrip("/")] = handler

    def register_context_engine(self, *a, **k):
        return None


def setup_home():
    home = Path(tempfile.mkdtemp(prefix="bca-hackathon-"))
    os.environ["HERMES_HOME"] = str(home)
    with sqlite3.connect(home / "state.db") as c:
        c.execute(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, "
            "parent_session_id TEXT, source TEXT)")
        c.execute("INSERT INTO sessions VALUES ('bot','Bot Chat',NULL,'desktop')")
    ctx = FakeCtx()
    bca.register(ctx)
    bca._state_db = lambda: home / "state.db"
    pending_asks.reset()
    return home, ctx


def say(role: str, text: str) -> None:
    prefix = {"user": "Gustavo", "plugin": "Bot Chat", "note": "note"}.get(role, role)
    bar = "─" * 72
    if role == "user":
        print(f"\n{bar}\n{prefix}: {text}")
    elif role == "note":
        print(f"  · {text}")
    else:
        print(f"\n{prefix}:\n{text}")


def main() -> int:
    home, ctx = setup_home()
    pre = ctx.hooks["pre_llm_call"]
    post = ctx.hooks["post_llm_call"]
    memoria = ctx.commands["memoria"]
    memory = ctx.commands["memory"]

    print("Bot Chat Agent — Nous hackathon demo (headless, box only)")
    print(f"HERMES_HOME={home}")
    print("Commands registered:", ", ".join(f"/{n}" for n in sorted(ctx.commands)))
    print("Core shape: v0.23 hook + register_command, no model, no network.")

    say("note", "A first turn writes a decision and a lesson into the local store.")
    history = [
        {"role": "user", "content": "Where should long-term notes live, and what did we learn about steers?"},
    ]
    reply = (
        "Decision: store long-term notes in the plugin local store under HERMES_HOME. "
        "Lesson learned: a mid-turn steer must not drop the earlier ask."
    )
    say("user", history[0]["content"])
    pre(session_id="bot", turn_id="t1", conversation_history=history)
    post(session_id="bot", turn_id="t1", conversation_history=history, final_response=reply)
    say("plugin", reply)

    say("note", "A later ask about auth recalls the relevant memory and shows the status panel.")
    history.append({"role": "assistant", "content": reply})
    history.append({"role": "user", "content": "Remind me: where do long-term notes live? Also, video editor auth?"})
    # seed a second decision that matches this ask
    post(session_id="bot", turn_id="t1b", conversation_history=history,
         final_response="Decision: use token auth for the video editor service.")
    say("user", history[-1]["content"])
    out = pre(session_id="bot", turn_id="t2", conversation_history=history) or {}
    injected = out.get("context") or ""
    # Show only the status line + memory section, not the long style contract.
    visible = []
    for block in injected.split("\n\n"):
        if block.startswith("[Status]") or block.startswith("[Estado]") \
                or block.startswith("[Long-term memory]") or block.startswith("[Memoria larga]") \
                or block.startswith("[Session summary]") or block.startswith("[Resumen") \
                or block.startswith("[Pending"):
            visible.append(block)
    say("plugin", "(injected context, trimmed to status + memory)\n" + "\n\n".join(visible))

    say("user", "/memoria")
    say("plugin", memoria(raw_args="", session_id="bot"))

    say("user", "/memory")
    listed = memory(raw_args="", session_id="bot")
    say("plugin", listed)
    # pick the first 8-char id from the listing
    mid = ""
    for line in listed.splitlines():
        parts = line.split()
        for token in parts:
            if len(token) == 8 and all(c in "0123456789abcdef" for c in token):
                mid = token
                break
        if mid:
            break

    if mid:
        say("user", f"/memory edit {mid} Store long-term notes only in the plugin local store (no vault).")
        say("plugin", memory(raw_args=f"edit {mid} Store long-term notes only in the plugin local store (no vault).",
                             session_id="bot"))
        say("user", "/memoria")
        say("plugin", memoria(raw_args="", session_id="bot"))
        say("user", f"/memory forget {mid}")
        say("plugin", memory(raw_args=f"forget {mid}", session_id="bot"))

    say("user", "/memory clear")
    say("plugin", memory(raw_args="clear", session_id="bot"))
    say("user", "/memory clear confirm")
    say("plugin", memory(raw_args="clear confirm", session_id="bot"))
    say("user", "/memoria")
    say("plugin", memoria(raw_args="", session_id="bot"))

    say("note", "Pending-asks reminder (a mid-turn steer left the arena question unanswered).")
    pending_history = [
        {"role": "user", "content": "revisa bien como esta la arena ahora por favor"},
        {"role": "assistant", "content": "checking", "tool_calls": [{"id": "x"}]},
        {"role": "tool", "content": "ok"},
        {"role": "user", "content": "cuentame como va el plugin de video"},
    ]
    say("user", "/memoria   (after the interrupted arena ask)")
    pre(session_id="bot", turn_id="t3", conversation_history=pending_history)
    say("plugin", memoria(raw_args="", session_id="bot"))

    print("\n" + "─" * 72)
    print("Demo finished. The plugin was not installed on a live Hermes;")
    print("this is a headless box run only. Status panel off: status_panel.enabled: false.")
    print("Memory off: memory.enabled: false.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
