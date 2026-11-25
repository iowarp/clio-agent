"""Resolve the tool executor for the active session workspace (``ClioAgent``).

Moved out of ``agent.py`` (at its size cap) when fleets became keyed by connector
values (CV-D3, :mod:`clio_agent.tools.fleet_key`).

Reads the active workspace root from the tool-execution contextvar. With no active
workspace, returns the default (no-cwd) executor. Otherwise returns a per-workspace
executor over a gateway whose stdio MCP subprocesses are spawned with
``cwd=<workspace root>`` (http MCPs stay shared). The executor is cached per fleet
key -- the root, plus the connector-values variant of the session's server set when
the conversation has values of its own -- so each fleet spawns its stdio MCPs at
most once (lazy, on first tool use).

The gateway also reads the active session's plugin server set
(``tools.execution.get_active_tool_server_set``) and mounts ONLY those servers. A
resolve under a server set the resident fleet has not yet mounted MERGES its
namespaces into the SAME executor (``tools.fleet_server_merge``) instead of evicting
it: two sessions sharing one workspace root with different agents is a real,
designed topology (a workload session + its standing watcher child). Eviction
remains only for genuinely-invalidating events (#1236 federation epoch).
"""

from __future__ import annotations

import threading
from typing import Any

from clio_agent.gact.mcp_gateway_refresh import (
    execution_client_factory as _execution_client_factory,
)
from clio_agent.plugins.servers import server_set_variant, variant_overlay
from clio_agent.runtime import trace
from clio_agent.runtime.source_policy_wait import await_source_policy_settled
from clio_agent.tools.execution import (
    create_sync_tool_executor,
    get_active_tool_server_set,
    get_active_tool_workspace_root,
)
from clio_agent.tools.fleet_key import fleet_key
from clio_agent.tools.gateway import namespace_proxies, namespace_specs
from clio_agent.tools.loop_wait import holding
from clio_agent.tools.workspace_root import canonical_workspace_root

#: Resolves of one fleet before giving up when it keeps being evicted meanwhile.
_RESOLVE_ATTEMPTS = 3


def _build_lock(agent: Any, lock: threading.Lock, key: str) -> threading.Lock:
    """The lock that serializes the resolves of one fleet (code on the loop never takes it)."""

    with lock:
        locks = getattr(agent, "_fleet_build_locks", None)
        if locks is None:
            locks = {}
            agent._fleet_build_locks = locks
        return locks.setdefault(key, threading.Lock())


def resolve_workspace_executor(agent: Any) -> Any:
    """The executor for the active workspace and server set (built lazily).

    The resolve runs under the shared registry lock, but building, reconciling or
    evicting a fleet waits on the application loop, and code on that loop reads the
    registry under the same lock. Each such wait releases the lock
    (:mod:`clio_agent.tools.loop_wait`), so the resolves of one fleet are serialized
    on its own build lock instead, a data-access change refuses while one is in
    flight, and a fleet evicted meanwhile is resolved again.
    """

    # Canonicalized ONCE, so the executor cache, the leases, the reaper and
    # the gateway cwd below all file this workspace under the same key.
    root = canonical_workspace_root(get_active_tool_workspace_root())
    if not root:
        return _rootless_executor(agent, get_active_tool_server_set().strip())
    server_set = get_active_tool_server_set().strip()
    # CV-D3: a conversation with its own connector values has its own fleet at this root.
    key = fleet_key(root, server_set_variant(server_set))
    lock, executors, leases = agent._workspace_state()
    with _build_lock(agent, lock, key):
        for _attempt in range(_RESOLVE_ATTEMPTS):
            closing = None
            with holding(lock):
                await_source_policy_settled(agent, lock)
                agent._fleet_resolves_in_flight = getattr(agent, "_fleet_resolves_in_flight", 0) + 1
                try:
                    executor = _resolve_locked(agent, key, root, server_set, executors, leases)
                finally:
                    agent._fleet_resolves_in_flight -= 1
                if getattr(agent, "_workspace_fleet_closed", False):
                    # The agent shut down while the fleet was built: never leave it running.
                    if executors.get(key) is executor:
                        executors.pop(key, None)
                    closing = executor
                elif executors.get(key) is executor and not getattr(executor, "closed", False):
                    reaper = getattr(agent, "_workspace_reaper", None)
                    # #1230: resolve-for-use counts as activity (resolve-to-busy gap).
                    if reaper is not None:
                        reaper.note_resolved(key)
                    return executor
            if closing is not None:
                closing.close()
                raise RuntimeError("CLIO is shutting down")
            trace.event("TOOLS", "workspace_fleet_resolve_raced root=%s key=%s", root, key)
    raise RuntimeError("CLIO's tools for this workspace kept restarting; retry the request")


def _rootless_executor(agent: Any, server_set: str) -> Any:
    """The default executor, carrying a rootless session's plugin server set.

    A workspace without a root folder has no fleet of its own, so its stdio servers
    run in the process cwd on the default executor. That executor already declares
    the user-scope ``mcp.yaml`` servers (``fleet_server_merge.stamp_default_fleet``);
    the session's plugin servers merge into it here, spawn-free, the same way a
    second server set joins a workspace fleet. The gateway refresh lock keeps the
    merge off an executor that a configuration change is replacing.
    """

    from clio_agent.tools.fleet_server_merge import (  # noqa: PLC0415
        _mounted_server_sets,
        merge_server_set_namespaces,
    )

    refresh_lock = getattr(agent, "_tool_gateway_refresh_lock", None)
    if not server_set or refresh_lock is None:
        return agent.tool_executor
    with refresh_lock:
        executor = agent.tool_executor
        if executor is not None and server_set not in _mounted_server_sets(executor):
            merge_server_set_namespaces(
                executor,
                agent._build_tool_gateway(server_set=server_set),
                server_set=server_set,
                root="<no root folder>",
            )
        return executor


def _resolve_locked(
    agent: Any,
    key: str,
    root: str,
    server_set: str,
    executors: dict[str, Any],
    leases: dict[str, int],
) -> Any:
    """The resolve's critical section (registry lock held, released across loop waits)."""

    reaper = getattr(agent, "_workspace_reaper", None)
    if reaper is not None:
        # The lock is released while this section waits on the loop: keep the reaper's
        # idle pass off the fleet meanwhile (#1230's resolve-activity window).
        reaper.note_resolved(key)
    executor = executors.get(key)
    stale = executor is not None and getattr(executor, "closed", False)
    # #1236: a resident executor minted under an older MCP configuration (the
    # run-15/17 brick) or a plugin's older snapshot must not outlive the change.
    from clio_agent.tools.fleet_server_merge import (  # noqa: PLC0415
        _mounted_server_sets,
        _stamp,
        merge_server_set_namespaces,
        server_set_spec_changed,
        stamp_fresh_fleet,
    )

    current_epoch = getattr(agent, "_mcp_configuration_revision", 0)
    executor_epoch = (
        getattr(executor, "_clio_mcp_configuration_revision", current_epoch)
        if executor is not None
        else current_epoch
    )
    configuration_switched = (
        executor is not None
        and not stale
        and server_set_spec_changed(agent, executor, root, server_set)
    )
    if (
        executor is not None
        and not stale
        and not configuration_switched
        and executor_epoch != current_epoch
    ):
        # A saved configuration change reaches the fleet server by server: only the
        # namespaces whose declaration moved restart; the rest keep their state. A
        # failure raises before the revision is stamped, so the next resolve retries.
        from clio_agent.tools.fleet_reconcile import reconcile_configuration  # noqa: PLC0415

        reconcile_configuration(agent, executor, root, current_epoch)
    if (
        configuration_switched
        and reaper is not None
        and reaper.defer_restart_if_active(key, executor, leases)
    ):
        # #1244: never close under a live turn; reaper drains at idle.
        configuration_switched = False
    # Additive server-set semantics (#1244): an unmounted set merges in
    # below; ``server_set == ""`` reuses the fleet as-is.
    server_set_missing = (
        executor is not None
        and not stale
        and not configuration_switched
        and bool(server_set)
        and server_set not in _mounted_server_sets(executor)
    )
    if executor is None or stale or configuration_switched:
        if configuration_switched:
            assert executor is not None
            # Out of the registry before the close waits on the loop (lock released there).
            if executors.get(key) is executor:
                executors.pop(key, None)
            try:
                executor.close()
            except Exception as exc:  # noqa: BLE001 - typed, never fatal
                trace.event(
                    "TOOLS",
                    "workspace_fleet_federation_evict_close_failed root=%s reason=%s",
                    root,
                    exc,
                )
            from clio_agent.tools.fleet_close_listeners import fleet_closed  # noqa: PLC0415

            fleet_closed(key, executor, "configuration_switched")  # CV-N2
            trace.event(
                "TOOLS",
                "workspace_fleet_mcp_configuration_revision_evict root=%s from_epoch=%s to_epoch=%s",
                root,
                executor_epoch,
                current_epoch,
            )
        # First use, a reaped fleet (#933) or a changed configuration or
        # plugin snapshot: rebuild lazily (spawns nothing until a tool call).
        gateway = agent._build_tool_gateway(cwd=root, server_set=server_set)
        preloaded = agent._tool_definitions
        # Every per-workspace gateway includes user-configured MCP
        # services as well as the plugin servers. Preserve those
        # declarations on the resident executor so root-session
        # services marked ``always_load`` can be mounted by the agent
        # builder.
        declared_specs = dict(namespace_specs(gateway))
        # #1237 owner ruling (2026-08-20): activation mounts NOTHING
        # eagerly. The OLD synchronous discovery pass blocked a
        # workspace's first resolve on every cold server. Only a
        # zero-I/O cache read happens here; a cold namespace remains
        # absent until the builder or dispatch path mounts it on demand.
        from clio_agent.connectors.listing import cached_offered  # noqa: PLC0415

        cached_tools: dict[str, Any] = {}
        for namespace, spec in declared_specs.items():
            if spec.transport != "stdio" or not spec.command:
                continue
            listed = cached_offered(namespace, spec)  # a connector's own tools only
            if listed:
                cached_tools.update(
                    {
                        f"{namespace}_{tool.name}": tool.model_copy(
                            update={"name": f"{namespace}_{tool.name}"}
                        )
                        for tool in listed
                    }
                )
        preloaded = {**(preloaded or {}), **cached_tools}
        executor = create_sync_tool_executor(
            gateway,
            preloaded_tools=preloaded,
            namespace_servers=namespace_proxies(gateway),
            server_id=f"gateway:{key}",
            client_factory=_execution_client_factory(),
        )
        # Stamps (ids/epoch/specs) live with the merge owner module.
        # #1281 F5: direct-client factories are NOT re-stamped here --
        # create_sync_tool_executor's construction above already
        # derived them straight off this SAME `gateway`.
        stamp_fresh_fleet(
            executor,
            server_set=server_set,
            mcp_configuration_revision=current_epoch,
            declared_specs=declared_specs,
        )
        # The conversation values this fleet's connectors run with (rebinds read it).
        _stamp(executor, "_clio_connector_values", variant_overlay(server_set_variant(server_set)))
        executors[key] = executor
    elif server_set_missing:
        # A second session's server set joins the SAME resident fleet:
        # declared specs + lazy proxies + warm cached listings merge in
        # (spawn-free); cold namespaces mount on demand at first need
        # (ensure_namespace), exactly like the rebuild path's.
        assert executor is not None
        merge_server_set_namespaces(
            executor,
            agent._build_tool_gateway(cwd=root, server_set=server_set),
            server_set=server_set,
            root=root,
        )
    return executor
