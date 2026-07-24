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
import threading
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from clio_agent.runtime.sandbox import SandboxResult

logger = logging.getLogger(__name__)

#: The loopback host every confined child reaches the chokepoint on (clio-private).
_LOOPBACK_HOST = "127.0.0.1"


# --------------------------------------------------------------------------- #
# Fleet namespace → serving child map (B5 #979.7 — the deferred B4 WRITER).
# --------------------------------------------------------------------------- #
#
# A confined MCP-fleet proxy is ONE persistent child per (workspace, namespace): its
# ``net_child_id`` is minted at ``transport_for`` (proxy build) and is stable for the child's
# life. The gact tool-observer mints the ``call_id`` on a DIFFERENT thread and only knows the
# tool NAME (``<namespace>_<tool>``), so this runtime-layer registry bridges the two: the
# spawn seam registers ``(workspace_root, namespace) -> net_child_id`` here, and the observer
# resolves it to call ``register_serving_child(app, call_id, net_child_id)`` — completing the
# ``egress -> child -> call-window -> transform`` join (#978 pt 5). Keyed by workspace_root +
# namespace (NOT process-global by namespace alone), so two workspaces' identically-named
# namespaces never cross-attribute; the fleet child is workspace-shared, so a workspace key is
# the child's real scope (a session key would be too narrow — sessions share the child). A
# rebuilt gateway overwrites the entry with the fresh child; the floor leaves it empty
# (net_child_id="" → the observer no-ops → the mint abstains: precision preserved).
_NAMESPACE_CHILD: dict[tuple[str, str], str] = {}
_NAMESPACE_CHILD_LOCK = threading.Lock()


def _ns_key(workspace_root: Optional[str], namespace: str) -> tuple[str, str]:
    root = ""
    if workspace_root:
        try:
            root = str(Path(workspace_root).expanduser().resolve(strict=False))
        except OSError:
            root = str(Path(workspace_root).expanduser())
    return root, (namespace or "").strip()


def register_namespace_child(
    workspace_root: Optional[str], namespace: str, net_child_id: str
) -> None:
    """Associate a fleet namespace's persistent confined child with its ``net_child_id`` (B5).

    No-op for an empty ``net_child_id`` (the floor / unfenced case) or empty ``namespace``.
    """
    child = (net_child_id or "").strip()
    ns = (namespace or "").strip()
    if not child or not ns:
        return
    with _NAMESPACE_CHILD_LOCK:
        _NAMESPACE_CHILD[_ns_key(workspace_root, ns)] = child


def resolve_namespace_child(workspace_root: Optional[str], namespace: str) -> str:
    """Return the confined ``net_child_id`` serving ``namespace`` in ``workspace_root``, or ``""``."""
    ns = (namespace or "").strip()
    if not ns:
        return ""
    with _NAMESPACE_CHILD_LOCK:
        return _NAMESPACE_CHILD.get(_ns_key(workspace_root, ns), "")


def clear_namespace_children() -> None:
    """Drop all namespace→child associations (test isolation seam)."""
    with _NAMESPACE_CHILD_LOCK:
        _NAMESPACE_CHILD.clear()


def close_namespace_children(workspace_root: Optional[str]) -> int:
    """Close + drop every per-child net channel serving ``workspace_root`` (#1033).

    The stop half of a drain-aware fleet restart: when a workspace's resident fleet is
    torn down (reaped for idle/LRU, or restarted to pick up a widened write territory), its
    per-child chokepoint listeners must go with it — otherwise they leak toward the bounded
    ``_MAX_CHILD_CHANNELS`` cap (the previously-UNWIRED :func:`close_child_channel` seam). Pops
    each ``(root, namespace) -> net_child_id`` entry for the root and closes that specific
    channel by id (compare-and-close is unnecessary here because the pop removes exactly the
    ids we then close; a subsequent lazy respawn registers fresh ids). Returns the count of
    channels closed. Guarded per channel with a typed reason — a close error never leaves a
    half-popped registry.
    """
    root = _ns_key(workspace_root, "")[0]
    if not root:
        return 0
    with _NAMESPACE_CHILD_LOCK:
        keys = [key for key in _NAMESPACE_CHILD if key[0] == root]
        children = [_NAMESPACE_CHILD.pop(key) for key in keys]
    if not children:
        return 0
    from clio_agent.runtime.net_chokepoint import close_child_channel  # noqa: PLC0415

    closed = 0
    for child in children:
        try:
            close_child_channel(child)
        except Exception as exc:  # noqa: BLE001 — a channel-close error is typed, never silent
            logger.warning(
                "net child channel close skipped reason=net_child_close_failed child=%s error=%r",
                child,
                exc,
            )
            continue
        closed += 1
    return closed


def net_mechanism_label(state: "SandboxResult") -> str:
    """Map the tier's ``net_enforcement`` to the honest per-edge egress mechanism label."""
    from clio_agent.runtime.net_chokepoint import (  # noqa: PLC0415
        MECHANISM_ENV_COOPERATIVE,
        MECHANISM_PROXY_ENFORCED,
    )
    from clio_agent.runtime.sandbox import NET_ENFORCEMENT_PROXY  # noqa: PLC0415

    enforcement = (
        state.details.get("net_enforcement", "") if isinstance(state.details, dict) else ""
    )
    return (
        MECHANISM_PROXY_ENFORCED
        if enforcement == NET_ENFORCEMENT_PROXY
        else MECHANISM_ENV_COOPERATIVE
    )


def open_child_egress(
    state: "SandboxResult", write_roots: Sequence[Path] | Sequence[str]
) -> tuple[str, Optional[int], dict[str, str]]:
    """Open a per-child chokepoint channel for a confined child (B4). Guarded, typed.

    Returns ``(child_id, port, env_overlay)``. ``port`` is ``None`` when no channel could be
    opened — the fence still composes; egress falls back to the shared unattributed listener
    (a typed reason, never a silent hole), and ``env_overlay`` is empty. Otherwise the overlay
    points ``HTTP(S)_PROXY``/``ALL_PROXY`` at the per-child channel (the child's route to the
    chokepoint on the floor/Landlock tier; srt overrides it inside the sandbox but the port
    identity is the per-child one). The channel carries the tier's honest net mechanism and
    the child's primary write territory so the ``used web:domain@time`` join can scope by
    workspace without a timing heuristic.
    """
    child_id = f"child_{uuid.uuid4().hex[:12]}"
    workspace_root = str(write_roots[0]) if write_roots else ""
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
        return "", None, {}
    if not port:
        return "", None, {}
    proxy_url = f"http://{_LOOPBACK_HOST}:{port}"
    overlay: dict[str, str] = {}
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        overlay[key] = proxy_url
        overlay[key.lower()] = proxy_url
    return child_id, port, overlay


__all__ = [
    "clear_namespace_children",
    "close_namespace_children",
    "net_mechanism_label",
    "open_child_egress",
    "register_namespace_child",
    "resolve_namespace_child",
]
