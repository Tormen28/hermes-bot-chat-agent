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
    data: dict = {"enabled": False}
    try:
        import status_panel
        snap = status_panel.read_snapshot(status_panel.status_path())
        if isinstance(snap, dict) and snap:
            data = status_panel.refresh_live(snap)
    except Exception:
        pass
    try:
        import panteon_auto
        data = dict(data)
        data["panteon_requests"] = panteon_auto.pending_requests()
    except Exception:
        pass
    return data


def hello() -> dict:
    """The Desktop half is alive (it can run ``profiles.create``): lets the plugin offer specialists."""
    try:
        import panteon_auto
        panteon_auto.mark_desktop_seen()
        return {"ok": True, "panteon_requests": panteon_auto.pending_requests()}
    except Exception:
        return {"ok": False}


def record_result(body: dict) -> dict:
    try:
        import panteon_auto
        body = body if isinstance(body, dict) else {}
        ok = panteon_auto.record_result(str(body.get("id") or ""), bool(body.get("ok")),
                                        error=str(body.get("error") or ""))
        return {"ok": ok}
    except Exception:
        return {"ok": False}


try:
    from fastapi import APIRouter

    router = APIRouter()

    @router.get("/status")
    async def status() -> dict:
        return get_status()

    @router.post("/panteon/hello")
    async def panteon_hello() -> dict:
        return hello()

    @router.post("/panteon/result")
    async def panteon_result(body: dict) -> dict:
        return record_result(body)
except Exception:  # pragma: no cover - FastAPI is provided by the gateway
    router = None
