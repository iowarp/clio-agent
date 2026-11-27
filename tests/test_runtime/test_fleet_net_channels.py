"""Net channels belong to the server instance, not to (folder, namespace) (#1033, round 6).

A fleet's teardown closes the channels of its own servers through ``close_fleet_channels``;
two fleets of one folder running a namespace of the same name keep two channels, and a
server another open fleet still routes to (MI1) keeps its channel. Pinned against the REAL
process chokepoint: the channel count actually drops.
"""

from __future__ import annotations

from types import SimpleNamespace

from clio_agent.runtime import net_chokepoint as nc
from clio_agent.tools import namespace_instances
from clio_agent.tools.fleet_serving import (
    close_fleet_channels,
    serving_net_child,
    stamp_net_child,
)


class _Fleet:
    def __init__(self, **servers: object) -> None:
        self._namespace_servers = dict(servers)
        self._closed = False


def _proxy(child: str) -> SimpleNamespace:
    proxy = SimpleNamespace()
    stamp_net_child(proxy, child)
    return proxy


def test_same_named_namespaces_of_two_fleets_keep_their_own_channels(tmp_path) -> None:
    root = str(tmp_path)
    try:
        nc.open_child_channel("plain-fs", workspace_root=root)
        nc.open_child_channel("own-fs", workspace_root=root)
        plain = _Fleet(fs=_proxy("plain-fs"))
        own = _Fleet(fs=_proxy("own-fs"))
        agent = SimpleNamespace(
            tool_executor=None,
            _workspace_state=lambda: (None, {root: plain, f"{root}|cv=x": own}, {}),
        )
        assert serving_net_child(agent, root, "fs") == "plain-fs"
        assert serving_net_child(agent, f"{root}|cv=x", "fs") == "own-fs"

        assert close_fleet_channels(f"{root}|cv=x", own) == 1
        cp = nc.current_chokepoint()
        assert cp is not None
        assert "own-fs" not in cp._channels
        assert "plain-fs" in cp._channels, "the other fleet's same-named server keeps its channel"
    finally:
        nc.shutdown_chokepoint()


def test_a_shared_server_keeps_its_channel_until_its_last_fleet_closes(tmp_path) -> None:
    root = str(tmp_path)
    shared = _proxy("shared-fs")
    first, second = _Fleet(fs=shared), _Fleet(fs=shared)
    try:
        nc.open_child_channel("shared-fs", workspace_root=root)
        namespace_instances._EXECUTORS.add(first)
        namespace_instances._EXECUTORS.add(second)

        first._closed = True
        assert close_fleet_channels(root, first) == 0
        cp = nc.current_chokepoint()
        assert cp is not None and "shared-fs" in cp._channels

        second._closed = True
        assert close_fleet_channels(f"{root}|cv=y", second) == 1
        assert "shared-fs" not in cp._channels
    finally:
        namespace_instances._EXECUTORS.discard(first)
        namespace_instances._EXECUTORS.discard(second)
        nc.shutdown_chokepoint()


def test_a_fleet_without_fenced_servers_closes_nothing() -> None:
    assert close_fleet_channels("", _Fleet(fs=SimpleNamespace())) == 0
    assert close_fleet_channels("", None) == 0
