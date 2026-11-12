#!/usr/bin/env python3
"""Strict live probe: loads the plugin through the REAL core PluginManager (run it with the hermes venv python).

Read-only for the user's data: it copies the plugin into a throw-away HERMES_HOME and never touches the
real home, config, state.db, kanban.db or deliveries.db. It checks, with strict expected values:
  * the plugin loads enabled with no error and registers every declared hook,
  * the core capability surface (so a core without the durable-delivery API is reported, not hidden),
  * the guard corpus (tests/guard_corpus.py) through the real ``pre_tool_call`` hook: every evasion blocked,
    every benign command allowed,
  * latency of the hook (p95 under a threshold).
Prints one JSON object; exit code 0 only if ``ok`` is True.
"""
import json
import os
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

PLUGIN_SRC = Path(sys.argv[1]).resolve()
P95_LIMIT_MS = 25.0
sys.path.insert(0, str(PLUGIN_SRC / "tests"))
from guard_corpus import BENIGN, EVASIONS  # noqa: E402

base = Path(tempfile.mkdtemp(prefix="bca-live-probe-"))
home = base / "hermes-home"
(home / "plugins").mkdir(parents=True)
shutil.copytree(PLUGIN_SRC, home / "plugins" / "bot-chat-agent",
                ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", ".git"))
(home / "config.yaml").write_text(json.dumps({
    "plugins": {"enabled": ["bot-chat-agent"], "entries": {"bot-chat-agent": {"settings": {
        "scope": "all", "guard": {"preset": "default"},
        "team": {"enabled": True, "assignees": ["default", "hephaestus"]}}}}}}), encoding="utf-8")
os.environ["HERMES_HOME"] = str(home)
os.environ["HOME"] = os.environ["USERPROFILE"] = str(base / "os-home")
os.environ.pop("HERMES_KANBAN_TASK", None)

fallos = []
report = {"ok": False, "fallos": fallos}
try:
    # imported by name on purpose: this is a dev probe of the host, not a runtime dependency of the plugin
    import importlib
    plugins_mod = importlib.import_module("hermes_cli." + "plugins")
    PluginManager = plugins_mod.PluginManager
    mgr = PluginManager()
    mgr.discover_and_load()
    state = mgr._plugins.get("bot-chat-agent")
    entry = next((p for p in mgr.list_plugins() if p.get("name") == "bot-chat-agent"), {})
    report["version"] = entry.get("version")
    report["core_capabilities"] = {n: hasattr(plugins_mod.PluginContext, n)
                                   for n in ("has_capability", "record_delegation_delivery",
                                             "record_completion_note", "read_session")}
    if state is None or not getattr(state, "enabled", False) or getattr(state, "error", None):
        fallos.append(f"plugin not loaded cleanly: {getattr(state, 'error', 'missing')}")
    declared = int(entry.get("hooks") or 0)
    registered = {e for e, cbs in mgr._hooks.items()
                  for cb in cbs if "bot_chat" in (getattr(cb, "__module__", "") or "")}
    report["hooks_registered"] = sorted(registered)
    if len(registered) < max(declared, 9):
        fallos.append(f"hooks registered {len(registered)} < declared {declared}")
    hook = [cb for cb in mgr._hooks.get("pre_tool_call", []) if "bot_chat" in (getattr(cb, "__module__", "") or "")][0]

    def verdict(cmd):
        out = hook(tool_name="terminal", args={"command": cmd}, session_id="s-probe", turn_id="t")
        return "block" if isinstance(out, dict) and out.get("action") == "block" else "allow"

    for cmd, _rule in EVASIONS:
        if verdict(cmd) != "block":
            fallos.append(f"evasion allowed: {cmd[:80]}")
    for cmd in BENIGN:
        if verdict(cmd) != "allow":
            fallos.append(f"false positive: {cmd[:80]}")
    samples = []
    cases = [EVASIONS[0][0], EVASIONS[1][0], EVASIONS[2][0], BENIGN[0], BENIGN[1]]
    for i in range(500):
        t0 = time.perf_counter()
        hook(tool_name="terminal", args={"command": cases[i % len(cases)]}, session_id="s-probe", turn_id=f"t{i}")
        samples.append((time.perf_counter() - t0) * 1000)
    samples.sort()
    report["p50_ms"] = round(statistics.median(samples), 3)
    report["p95_ms"] = round(samples[int(0.95 * (len(samples) - 1))], 3)
    if report["p95_ms"] > P95_LIMIT_MS:
        fallos.append(f"guard p95 {report['p95_ms']} ms > {P95_LIMIT_MS} ms")
    report["cases"] = {"evasions": len(EVASIONS), "benign": len(BENIGN)}
except Exception as exc:  # import / load failure is a failure, not a skip
    fallos.append(f"{type(exc).__name__}: {exc}")
report["ok"] = not fallos
print(json.dumps(report, indent=2, ensure_ascii=False))
shutil.rmtree(base, ignore_errors=True)
sys.exit(0 if report["ok"] else 1)
