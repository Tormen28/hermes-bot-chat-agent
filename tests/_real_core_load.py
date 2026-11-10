"""Load the plugin through a REAL Hermes core's PluginManager (no fake ctx).

Usage (with the core's own Python):  python tests/_real_core_load.py <core_checkout> [--grant]
Prints one JSON line: loaded?, hooks, commands, whether the durable-delivery surface exists,
and what the plugin reports as its live delivery path. ``--grant`` consents to the
``state.*`` capabilities the manifest asks for (only meaningful on a core that has them).
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    core = Path(sys.argv[1]).resolve()
    grant = "--grant" in sys.argv
    home = Path(tempfile.mkdtemp(prefix="bca-realcore-"))
    os.environ["HERMES_HOME"] = str(home)
    dst = home / "plugins" / "bot-chat-agent"
    shutil.copytree(ROOT, dst, ignore=shutil.ignore_patterns(".git", "__pycache__", "tests", "docs", "demo"))
    cfg = ["plugins:", "  enabled:", "    - bot-chat-agent"]
    (home / "config.yaml").write_text("\n".join(cfg) + "\n", encoding="utf-8")
    sys.path.insert(0, str(core))
    from hermes_cli import plugins as P  # noqa: E402

    mgr = P.PluginManager()
    mgr.discover_and_load(force=True)
    info = mgr._plugins.get("bot-chat-agent")
    out = {"core": str(core), "loaded": bool(info), "error": getattr(info, "error", None),
           "enabled": getattr(info, "enabled", None)}
    if info:
        out["hooks"] = sorted(h for h, cbs in mgr._hooks.items()
                              if any(getattr(cb, "__module__", "").startswith(("bot_chat", "hermes_plugins")) or True for cb in cbs)
                              and info.manifest.name in str(getattr(info, "hooks_registered", [])) or h in getattr(info, "hooks_registered", []))
        out["commands"] = sorted(mgr._plugin_commands)
    ctx_cls = None
    try:
        from hermes_cli.plugins_context import PluginContext as ctx_cls  # type: ignore
    except Exception:
        pass
    out["core_has_record_delegation_delivery"] = bool(ctx_cls and hasattr(ctx_cls, "record_delegation_delivery"))
    out["core_has_read_session"] = bool(ctx_cls and hasattr(ctx_cls, "read_session"))
    try:
        mod = sys.modules.get("hermes_plugins.bot_chat_agent") or next(
            (m for n, m in sys.modules.items() if n.endswith("bot_chat_agent") and hasattr(m, "delivery_path")), None)
        out["delivery_path"] = mod.delivery_path() if mod else None
    except Exception as exc:
        out["delivery_path"] = f"error: {exc!r}"
    if "--panteon" in sys.argv and info:
        handler = P.get_plugin_command_handler("pantheon") if hasattr(P, "get_plugin_command_handler") else None
        out["panteon_handler"] = bool(handler)
        if handler is None:
            handler = mgr._plugin_commands.get("pantheon", {}).get("handler")
        steps = {}
        steps["plan"] = handler("plan daedalus iris")
        steps["create_no_confirm"] = handler("create daedalus iris")
        steps["team_missing"] = handler("team daedalus confirm")
        steps["create"] = handler("create daedalus iris confirm")
        steps["team"] = handler("team daedalus iris confirm")
        out["panteon"] = steps
        import hermes_yaml as yaml
        out["config_after"] = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
        out["profiles_dirs"] = sorted(p.name for p in (home / "profiles").iterdir()) if (home / "profiles").is_dir() else []
        mod = next((m for n, m in sys.modules.items() if n.endswith("bot_chat_agent") and hasattr(m, "_team")), None)
        try:
            out["team_seen_by_plugin"] = mod._team()["assignees"] if mod else None
        except Exception as exc:
            out["team_seen_by_plugin"] = f"error: {exc!r}"
    print(json.dumps(out, default=str))
    return 0 if out["loaded"] and not out.get("error") else 1


if __name__ == "__main__":
    raise SystemExit(main())
