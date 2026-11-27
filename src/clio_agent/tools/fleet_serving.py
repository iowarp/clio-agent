"""Which resident fleet serves a conversation, and each of its servers' network channel.

A conversation's tools run on one fleet: its workspace's plain fleet, a fleet of its own
at that root (connector values of its own, or a plugin keeping a person's server name;
:mod:`clio_agent.tools.fleet_own_set`), or, without a root folder, the default executor or
a rootless fleet of its own. :func:`serving_fleet_key` is the key the turn-bound resolve
(``workspace_fleet.resolve_workspace_executor``) files that fleet under, so every reader
of "the fleet serving this conversation" agrees with the turn.

A confined stdio server gets its own egress channel on the chokepoint (``net_child_id``,
:mod:`clio_agent.runtime.sandbox_net`). The channel belongs to that server instance: it is
stamped on the server's transport and namespace proxy (:func:`stamp_net_child`) when the
gateway builds them, and read back through the proxy the fleet routes the namespace to
(:func:`serving_net_child`). Two fleets of one folder running a namespace of the same
name therefore keep two channels, and a fleet sharing a sibling's server (MI1 adoption)
reads that server's channel. A fleet's teardown closes the channels of the servers no
other open fleet routes to (:func:`close_fleet_channels`).
"""

from __future__ import annotations

import logging
from typing import Any

from clio_agent.tools.fleet_key import fleet_key
from clio_agent.tools.fleet_own_set import fleet_variant
from clio_agent.tools.workspace_root import canonical_workspace_root

logger = logging.getLogger(__name__)

#: The attribute a transport and its namespace proxy carry their server's channel on.
NET_CHILD_ATTR = "_clio_net_child"


def serving_fleet_key(root: str, server_set: str) -> str:
    """The key of the fleet serving ``server_set`` in workspace ``root``.

    ``""`` is the default executor, which serves a workspace without a root folder when
    the set has no fleet of its own.
    """

    root = canonical_workspace_root(root)
    return fleet_key(root, fleet_variant(server_set.strip(), root))


def resident_executor(agent: Any, key: str) -> Any:
    """The resident executor filed under ``key`` (``None`` when none), never creating one.

    Safe on the event loop: it never waits for the registry lock (whose holder may be
    waiting on the loop); a dict copy is atomic under the GIL.
    """

    if not key:
        return getattr(agent, "tool_executor", None)
    state = getattr(agent, "_workspace_state", None)
    if not callable(state):
        return None
    result = state()
    executors = result[1] if isinstance(result, tuple) and len(result) > 1 else None
    return dict(executors).get(key) if isinstance(executors, dict) else None


def stamp_net_child(target: Any, net_child: str) -> None:
    """Record on ``target`` (a transport or a namespace proxy) its server's channel."""

    if not net_child or target is None:
        return
    try:
        setattr(target, NET_CHILD_ATTR, net_child)
    except (AttributeError, TypeError):  # a test double that takes no attributes
        logger.debug("net child not stamped reason=target_refuses_attributes")


def net_child_of(target: Any) -> str:
    """The channel ``target`` carries (``""`` for an unfenced or remote server)."""

    return str(getattr(target, NET_CHILD_ATTR, "") or "")


def own_server_channel(transport: Any, confined: Any) -> None:
    """Make the confined spawn's egress channel (and its release) the transport's own.

    The channel closes when the transport goes (``ConfinedSpawn.release``), or earlier
    with its fleet's teardown (:func:`close_fleet_channels`); closing twice is a no-op.
    """

    import weakref  # noqa: PLC0415

    details = getattr(getattr(confined, "result", None), "details", None) or {}
    stamp_net_child(transport, str(details.get("net_child_id") or ""))
    weakref.finalize(transport, confined.release)


def serving_net_child(agent: Any, key: str, namespace: str) -> str:
    """The channel of the server fleet ``key`` routes ``namespace`` to, or ``""``."""

    executor = resident_executor(agent, key)
    inner = getattr(executor, "_async_executor", executor)
    servers = getattr(inner, "_namespace_servers", None)
    if not isinstance(servers, dict) or not namespace:
        return ""
    return net_child_of(servers.get(namespace))


def close_fleet_channels(key: str, executor: Any) -> int:
    """Close the channels of ``executor``'s servers that no other open fleet routes to.

    A server another fleet shares (MI1) keeps its channel; it closes with the last fleet
    routing to it, or when its transport goes (``ConfinedSpawn.release``). Closing is
    idempotent. Returns the count closed; a close error is logged with a typed reason.
    """

    from clio_agent.runtime.net_chokepoint import close_child_channel  # noqa: PLC0415
    from clio_agent.tools import namespace_instances  # noqa: PLC0415
    from clio_agent.tools.fleet_close_listeners import fleet_proxies  # noqa: PLC0415

    closed = 0
    for proxy in namespace_instances.unshared(fleet_proxies(executor), executor):
        child = net_child_of(proxy)
        if not child:
            continue
        try:
            close_child_channel(child)
        except Exception as exc:  # noqa: BLE001 - a channel-close error is typed, never silent
            logger.warning(
                "net child channel close skipped reason=net_child_close_failed fleet=%s "
                "child=%s error=%r",
                key,
                child,
                exc,
            )
            continue
        closed += 1
    return closed


__all__ = [
    "NET_CHILD_ATTR",
    "close_fleet_channels",
    "net_child_of",
    "own_server_channel",
    "resident_executor",
    "serving_fleet_key",
    "serving_net_child",
    "stamp_net_child",
]
