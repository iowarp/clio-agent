"""Chromium's subresource egress in a deny-mode workspace: through CLIO's chokepoint.

The browser's own requests are not the kernel's, so they do not pass the kernel's fence.
Top-level navigation is gated per host (:mod:`.host_gate`). When the workspace that starts
a profile's browser runs deny-mode egress, Chromium is launched with a per-child channel of
:mod:`clio_agent.runtime.net_chokepoint` as its proxy, so subresources meet the same
chokepoint and the same egress records. The channel is attributed to that workspace; a
profile later shared by a session of another workspace keeps it (BR24). Outside deny mode
subresources load as in any browser.
"""

from __future__ import annotations

import uuid
from typing import Any

from clio_agent.runtime import trace

__all__ = ["close_channel", "proxy_for"]


def _workspace(app: Any, session_id: str) -> tuple[str, str]:
    sessions = getattr(getattr(app, "state", None), "sessions", None)
    session = sessions.get(session_id) if sessions is not None else None
    workspace_id = str(getattr(session, "workspace_id", "") or "")
    workspaces = getattr(app.state, "workspaces", None) if app is not None else None
    workspace = workspaces.get(workspace_id) if workspaces is not None and workspace_id else None
    return workspace_id, str(getattr(workspace, "root_path", "") or "")


def proxy_for(app: Any, session_id: str) -> tuple[dict[str, str] | None, str]:
    """``(proxy, channel_id)`` for a launch: a chokepoint channel in deny mode, else none.

    Raises:
        BrowserError: ``browser_egress_unavailable`` when deny mode is on but no channel
            could be opened (the browser never runs unproxied in deny mode).
    """
    if app is None:
        return None, ""
    from clio_agent.gact.runtime.grants import workspace_deny_mode  # noqa: PLC0415

    workspace_id, root = _workspace(app, session_id)
    if not workspace_deny_mode(app, workspace_id):
        return None, ""
    from clio_agent.gact.browser.settings import BrowserError  # noqa: PLC0415
    from clio_agent.runtime import net_chokepoint  # noqa: PLC0415

    channel = f"browser_{uuid.uuid4().hex[:12]}"
    port = net_chokepoint.open_child_channel(
        channel, mechanism=net_chokepoint.MECHANISM_ENV_COOPERATIVE, workspace_root=root
    )
    if not port:
        raise BrowserError(
            "browser_egress_unavailable",
            "this workspace runs deny-mode egress and CLIO could not open a proxy channel",
        )
    trace.event("BROWSER", "browser egress through chokepoint channel=%s port=%s", channel, port)
    # Chromium bypasses the proxy for loopback unless told not to.
    return {"server": f"http://127.0.0.1:{port}", "bypass": "<-loopback>"}, channel


def close_channel(channel: str) -> None:
    """Close a profile's chokepoint channel when its browser closes."""
    from clio_agent.runtime import net_chokepoint  # noqa: PLC0415

    net_chokepoint.close_child_channel(channel)
