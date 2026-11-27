"""Per-child network-egress wiring for confined spawns (B4 #978).

Sibling of the :mod:`clio_agent.runtime.sandbox` ladder owner — it holds the B4 egress
attribution glue so the ladder module stays under its file-size ratchet (no accretion). A
confined child gets a PER-CHILD chokepoint channel (:func:`open_child_egress`) whose loopback
port becomes both the srt ``httpProxyPort`` (srt tier — reached via srt's socat bridge) AND
the child's ``HTTP(S)_PROXY``/``ALL_PROXY`` env (floor/Landlock tier). srt refuses to embed a
per-child credential token for an EXTERNAL proxy (``sandbox-manager.js``: ``proxyAuthToken =
httpProxyPort !== undefined ? undefined : …``), so a per-child PORT — not a token — is what
survives srt's composition and lets the listener a connection arrives on deterministically
name the child (no timing heuristic).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from clio_agent.runtime.sandbox import SandboxResult

logger = logging.getLogger(__name__)

#: The loopback host every confined child reaches the chokepoint on (clio-private).
_LOOPBACK_HOST = "127.0.0.1"


def net_mechanism_label(state: "SandboxResult") -> str:
    """Map the tier's ``net_enforcement`` to the honest per-edge egress mechanism label."""
    from clio_agent.runtime.net_chokepoint import (  # noqa: PLC0415
        MECHANISM_ENV_COOPERATIVE,
        MECHANISM_PROXY_ENFORCED,
        MECHANISM_PROXY_ENFORCED_EXTERNAL,
    )
    from clio_agent.runtime.sandbox import NET_ENFORCEMENT_PROXY  # noqa: PLC0415

    enforcement = (
        state.details.get("net_enforcement", "") if isinstance(state.details, dict) else ""
    )
    if enforcement == "proxy-external":
        # MXC forces non-loopback egress through the managed proxy, but permits
        # direct host loopback. Do not claim complete loopback observation.
        return MECHANISM_PROXY_ENFORCED_EXTERNAL
    return (
        MECHANISM_PROXY_ENFORCED
        if enforcement == NET_ENFORCEMENT_PROXY
        else MECHANISM_ENV_COOPERATIVE
    )


def open_child_egress(
    state: "SandboxResult",
    write_roots: Sequence[Path] | Sequence[str],
    *,
    workspace_root: Path | str | None = None,
) -> tuple[str, Optional[int], dict[str, str]]:
    """Open a per-child chokepoint channel for a confined child (B4). Guarded, typed.

    Returns ``(child_id, port, env_overlay)``. ``port`` is ``None`` when no channel could be
    opened — the fence still composes; egress falls back to the shared unattributed listener
    (a typed reason, never a silent hole), and ``env_overlay`` is empty, except on an enforcing
    Linux fence, where it names a closed upstream so the child stays offline. Otherwise the overlay
    points ``HTTP(S)_PROXY``/``ALL_PROXY`` at the per-child channel (the child's route to the
    chokepoint on the floor/Landlock tier; srt overrides it inside the sandbox but the port
    identity is the per-child one). The channel carries the tier's honest net mechanism and
    the child's workspace (``workspace_root``, else its first write root) so deny mode, approvals
    and the ``used web:domain@time`` join scope by workspace without a timing heuristic. The
    first write root is often a session scratch folder, which belongs to no workspace.
    """
    child_id = f"child_{uuid.uuid4().hex[:12]}"
    if workspace_root:
        workspace_root = str(workspace_root)
    else:
        workspace_root = str(write_roots[0]) if write_roots else ""
    port: Optional[int] = None
    try:
        from clio_agent.runtime.net_chokepoint import open_child_channel  # noqa: PLC0415

        port = open_child_channel(
            child_id, mechanism=net_mechanism_label(state), workspace_root=workspace_root
        )
    except Exception as exc:  # noqa: BLE001 — recording wiring must never break a spawn
        logger.warning(
            "egress channel open skipped reason=egress_channel_open_failed child=%s error=%r",
            child_id,
            exc,
        )
    if not port:
        return "", None, _closed_upstream(state)
    overlay = _proxy_overlay(f"http://{_LOOPBACK_HOST}:{port}")
    if _linux_proxy_fence(state):
        # Codex's proxy passes only public hosts on; the SSH helper reaches the rest through
        # the relay tunnel CLIO's chokepoint answers itself (see net_connect).
        from clio_agent.runtime.fence_service import FENCE_CHILD_VARIABLE  # noqa: PLC0415
        from clio_agent.runtime.net_connect import RELAY_AUTHORITY, RELAY_VARIABLE  # noqa: PLC0415

        overlay[RELAY_VARIABLE] = RELAY_AUTHORITY
        # The child's own network namespace is found by this mark, to reach a port it declares.
        overlay[FENCE_CHILD_VARIABLE] = child_id
    return child_id, port, overlay


def _linux_proxy_fence(state: "SandboxResult") -> bool:
    """Whether ``state`` is the Linux fence whose network leaves only through the proxy."""
    from clio_agent.runtime.sandbox import NET_ENFORCEMENT_PROXY  # noqa: PLC0415

    details = state.details if isinstance(state.details, dict) else {}
    return details.get("net_enforcement") == NET_ENFORCEMENT_PROXY and str(
        details.get("platform", "")
    ).startswith("linux")


def _closed_upstream(state: "SandboxResult") -> dict[str, str]:
    """Without a channel, keep an enforcing Linux fence closed instead of letting Codex dial out.

    Codex's managed proxy connects directly when it has no upstream, which would turn a
    chokepoint failure into unrecorded egress. Pointing it at a port nobody listens on makes
    every tunnel fail instead. Other tiers keep the old behavior (no overlay).
    """
    if not _linux_proxy_fence(state):
        return {}
    logger.warning("egress channel unavailable: the Linux fence keeps the child offline")
    return _proxy_overlay(f"http://{_LOOPBACK_HOST}:{_CLOSED_PORT}")


#: The TCP discard port: nothing listens on it, so every proxied connection is refused.
_CLOSED_PORT = 9


def _proxy_overlay(proxy_url: str) -> dict[str, str]:
    overlay: dict[str, str] = {}
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        overlay[key] = proxy_url
        overlay[key.lower()] = proxy_url
    return overlay


__all__ = [
    "net_mechanism_label",
    "open_child_egress",
]
