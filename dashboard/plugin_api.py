"""Desktop/dashboard API for the live status card.

Mounted by the Hermes gateway at ``/api/plugins/bot-chat-agent`` when
``dashboard/manifest.json`` declares ``"api": "plugin_api.py"`` and the
plugin is in ``plugins.enabled``. The Desktop half polls
``ctx.rest('/status')``. Fail-open: a missing file or import returns a
disabled snapshot, never an exception to the gateway.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def get_status() -> dict:
    try:
        import status_panel
        data = status_panel.read_snapshot(status_panel.status_path())
        if not isinstance(data, dict) or not data:
            return {"enabled": False}
        return data
    except Exception:
        return {"enabled": False}


try:
    from fastapi import APIRouter

    router = APIRouter()

    @router.get("/status")
    async def status() -> dict:
        return get_status()
except Exception:  # pragma: no cover - FastAPI is provided by the gateway
    router = None
