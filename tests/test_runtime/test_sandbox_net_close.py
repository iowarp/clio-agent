"""close_namespace_children (#1033): the reap/restart stop step that wires close_child_channel.

Before #1033 ``net_chokepoint.close_child_channel`` had ZERO runtime callers, so a per-child
egress listener lived until process exit — a leak bounded only by ``_MAX_CHILD_CHANNELS``. The
fleet-restart / reap teardown now closes a workspace's channels through this seam. This pins it
against the REAL process chokepoint: the channel count actually drops.
"""

from __future__ import annotations

from clio_agent.runtime import net_chokepoint as nc
from clio_agent.runtime import sandbox_net


def test_close_namespace_children_closes_channels_and_drops_registry(tmp_path) -> None:
    sandbox_net.clear_namespace_children()
    root = str(tmp_path)
    try:
        port_a = nc.open_child_channel("child-A", workspace_root=root)
        port_b = nc.open_child_channel("child-B", workspace_root=root)
        assert port_a and port_b
        sandbox_net.register_namespace_child(root, "fs", "child-A")
        sandbox_net.register_namespace_child(root, "shell", "child-B")

        cp = nc.current_chokepoint()
        assert cp is not None
        assert {"child-A", "child-B"} <= set(cp._channels), "channels open before restart"

        closed = sandbox_net.close_namespace_children(root)

        assert closed == 2, "both per-child channels closed on restart"
        assert "child-A" not in cp._channels and "child-B" not in cp._channels
        # The namespace→child associations for the root are gone (a fresh spawn re-registers).
        assert sandbox_net.resolve_namespace_child(root, "fs") == ""
        assert sandbox_net.resolve_namespace_child(root, "shell") == ""
    finally:
        sandbox_net.clear_namespace_children()
        nc.shutdown_chokepoint()


def test_close_namespace_children_only_touches_the_targeted_root(tmp_path) -> None:
    sandbox_net.clear_namespace_children()
    root_a = str(tmp_path / "a")
    root_b = str(tmp_path / "b")
    try:
        nc.open_child_channel("a-fs", workspace_root=root_a)
        nc.open_child_channel("b-fs", workspace_root=root_b)
        sandbox_net.register_namespace_child(root_a, "fs", "a-fs")
        sandbox_net.register_namespace_child(root_b, "fs", "b-fs")

        assert sandbox_net.close_namespace_children(root_a) == 1
        cp = nc.current_chokepoint()
        assert cp is not None
        assert "a-fs" not in cp._channels
        assert "b-fs" in cp._channels, "an adjacent workspace's channel must be untouched"
        assert sandbox_net.resolve_namespace_child(root_b, "fs") == "b-fs"
    finally:
        sandbox_net.clear_namespace_children()
        nc.shutdown_chokepoint()


def test_close_namespace_children_no_children_is_zero(tmp_path) -> None:
    sandbox_net.clear_namespace_children()
    assert sandbox_net.close_namespace_children(str(tmp_path)) == 0
    assert sandbox_net.close_namespace_children("") == 0
