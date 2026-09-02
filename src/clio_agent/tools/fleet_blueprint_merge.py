"""Additive blueprint merge into a resident workspace fleet.

Why this exists: #1232 pt 1 mounted exactly ONE activated blueprint's declared
``mcp_servers`` per workspace fleet and EVICTED (close + rebuild) the resident
executor whenever a resolve arrived under a different active blueprint. That
one-blueprint-at-a-time simplification assumed a workspace is provisioned per
session — but the SPOTTER topology is exactly two sessions sharing one root
with different blueprints BY DESIGN (a workload session plus its standing
watcher child), and the eviction closed the fleet out from under the
workload's LIVE turn: the eviction path checked neither ``busy`` nor the #933
turn lease, and a react turn's DSPy tools hold their executor binding for the
whole turn, so the close poisoned every remaining call of that turn
(``RuntimeError: SyncMCPToolExecutor is closed`` mid-campaign).

The correct shape under the on-demand doctrine (#1237): a second blueprint's
declared servers MERGE into the resident fleet —

* its namespace **specs** join the executor's ``_clio_namespace_specs`` stamp,
  so the on-demand mount (``tools.mcp_discovery.ensure_namespace``) can tell
  "declared but not yet listed" from "genuinely unknown";
* its namespace **proxies** join the async executor's routing table (lazy
  clients — nothing spawns until a call actually routes there);
* its **cached listings** (24h ``listing_cache``) merge append-only into the
  live tool table (#932 ``merge_namespace_tools``), so warm namespaces are
  visible immediately.

Eviction remains only for genuinely-invalidating events (the #1236 federation
epoch). A namespace collision (two blueprints declaring the same namespace
with different specs) keeps the FIRST mounted spec and emits a typed
``workspace_fleet_namespace_spec_conflict`` reason — never a silent override.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from clio_agent.runtime import trace
from clio_agent.tools.gateway import namespace_direct_factories, namespace_proxies, namespace_specs


def merge_blueprint_namespaces(
    executor: Any,
    gateway: Any,
    *,
    blueprint_id: str,
    root: str,
) -> dict[str, Any]:
    """Merge ``gateway``'s declared namespaces into a resident ``executor``.

    ``gateway`` is a freshly-built (declarative, spawn-free) gateway for the
    blueprint being merged; ``executor`` is the resident workspace fleet built
    earlier for a different blueprint (or none). Returns a typed report::

        {"blueprint_id": ..., "merged": [...], "already": [...],
         "conflicts": [...], "cached_tools": <int>}

    Args:
        executor: The resident ``SyncMCPToolExecutor`` for the workspace root.
        gateway: A gateway built with ``blueprint_id``'s declared servers.
        blueprint_id: The blueprint whose namespaces are being merged.
        root: The workspace root (trace context only).
    """

    new_specs = dict(namespace_specs(gateway))
    new_proxies = namespace_proxies(gateway)
    new_direct_factories = namespace_direct_factories(gateway)

    existing_specs = getattr(executor, "_clio_namespace_specs", None)
    if existing_specs is None:
        existing_specs = {}
        _stamp(executor, "_clio_namespace_specs", existing_specs)
    inner = getattr(executor, "_async_executor", None)
    inner_specs = getattr(inner, "_clio_namespace_specs", None) if inner is not None else None
    if inner is not None and inner_specs is None:
        inner_specs = existing_specs
        _stamp(inner, "_clio_namespace_specs", inner_specs)
    # #1281 (C1-S1): the direct-client factory registry merges the SAME way
    # specs do -- a namespace joining a resident fleet via a second blueprint
    # must be routable direct the moment its capability is discovered True.
    existing_factories = getattr(executor, "_clio_namespace_direct_factories", None)
    if existing_factories is None:
        existing_factories = {}
        _stamp(executor, "_clio_namespace_direct_factories", existing_factories)
    inner_factories = (
        getattr(inner, "_clio_namespace_direct_factories", None) if inner is not None else None
    )
    if inner is not None and inner_factories is None:
        inner_factories = existing_factories
        _stamp(inner, "_clio_namespace_direct_factories", inner_factories)

    merged: list[str] = []
    already: list[str] = []
    conflicts: list[str] = []
    cached_count = 0

    for namespace, spec in new_specs.items():
        current = existing_specs.get(namespace)
        if current is not None:
            if _specs_equal(current, spec):
                already.append(namespace)
            else:
                conflicts.append(namespace)
                trace.event(
                    "TOOLS",
                    "workspace_fleet_namespace_spec_conflict root=%s namespace=%s "
                    "blueprint=%s resolution=first_mounted_kept",
                    root,
                    namespace,
                    blueprint_id,
                )
            continue
        existing_specs[namespace] = spec
        if inner_specs is not None and inner_specs is not existing_specs:
            inner_specs[namespace] = spec
        factory = new_direct_factories.get(namespace)
        if factory is not None:
            existing_factories[namespace] = factory
            if inner_factories is not None and inner_factories is not existing_factories:
                inner_factories[namespace] = factory
        proxy = new_proxies.get(namespace)
        if inner is not None and proxy is not None:
            # Lazy per-namespace proxy: joins the routing table without
            # spawning anything until a call actually routes at it (#932).
            inner._namespace_servers.setdefault(namespace, proxy)
        cached_count += _merge_cached_listing(executor, namespace, spec)
        merged.append(namespace)

    mounted_ids = _mounted_blueprint_ids(executor)
    mounted_ids.add(blueprint_id)
    _stamp(executor, "_clio_mounted_blueprint_ids", mounted_ids)
    # Legacy single-id stamp: kept current for external readers; the resolve
    # path decides on the SET above.
    _stamp(executor, "_clio_mounted_blueprint_id", blueprint_id)

    trace.event(
        "TOOLS",
        "workspace_fleet_blueprint_merged root=%s blueprint=%s merged=%s "
        "already=%s conflicts=%s cached_tools=%d",
        root,
        blueprint_id,
        ",".join(merged) or "<none>",
        ",".join(already) or "<none>",
        ",".join(conflicts) or "<none>",
        cached_count,
    )
    return {
        "blueprint_id": blueprint_id,
        "merged": merged,
        "already": already,
        "conflicts": conflicts,
        "cached_tools": cached_count,
    }


def stamp_fresh_fleet(
    executor: Any,
    *,
    blueprint_id: str,
    federation_epoch: Any,
    declared_specs: dict[str, Any],
    direct_factories: Mapping[str, Any] | None = None,
) -> None:
    """Stamp a freshly-rebuilt fleet's bookkeeping (agent.py's rebuild path).

    Sets the legacy single-blueprint stamp, the additive mounted-set, the
    #1236 federation epoch, and the #1237 declared-namespace map — the latter
    on BOTH the sync wrapper (``builders.py`` reads it there) and its inner
    async executor (``mcp_executor.py``'s dispatch-time gate reads it there).
    ``direct_factories`` (#1281 C1-S1) is stamped the same dual way via
    :func:`stamp_direct_factories`. Best-effort on test doubles that refuse
    attribute assignment.
    """

    _stamp(executor, "_clio_mounted_blueprint_id", blueprint_id)
    _stamp(executor, "_clio_mounted_blueprint_ids", {blueprint_id} if blueprint_id else set())
    _stamp(executor, "_clio_federation_epoch", federation_epoch)
    _stamp(executor, "_clio_namespace_specs", declared_specs)
    stamp_direct_factories(executor, direct_factories or {})
    inner = getattr(executor, "_async_executor", None)
    if inner is not None:
        _stamp(inner, "_clio_namespace_specs", declared_specs)


def stamp_direct_factories(executor: Any, direct_factories: Mapping[str, Any]) -> None:
    """Stamp the #1281 (C1-S1) direct-client factory registry onto ``executor``.

    Mirrors the ``_clio_namespace_specs`` dual-stamp (sync wrapper + inner
    async executor) so ``mcp_executor._connect_namespace`` can route a
    namespace direct once its task capability is discovered True -- on
    EVERY executor construction path: the default (no-workspace) gateway
    executor, a freshly rebuilt per-workspace fleet (:func:`stamp_fresh_fleet`),
    and an additively-merged blueprint (:func:`merge_blueprint_namespaces`).
    """

    factories = dict(direct_factories)
    _stamp(executor, "_clio_namespace_direct_factories", factories)
    inner = getattr(executor, "_async_executor", None)
    if inner is not None:
        _stamp(inner, "_clio_namespace_direct_factories", factories)


def _mounted_blueprint_ids(executor: Any) -> set[str]:
    """The executor's mounted-blueprint set, upgraded from the legacy stamp."""

    mounted = getattr(executor, "_clio_mounted_blueprint_ids", None)
    if isinstance(mounted, set):
        return mounted
    legacy = str(getattr(executor, "_clio_mounted_blueprint_id", "") or "")
    return {legacy} if legacy else set()


def _merge_cached_listing(executor: Any, namespace: str, spec: Any) -> int:
    """Merge a warm listing-cache entry for ``namespace`` into the live table."""

    if getattr(spec, "transport", "") != "stdio" or not getattr(spec, "command", ""):
        return 0
    from clio_agent.tools import listing_cache  # noqa: PLC0415

    listed = listing_cache.load_listing(namespace, spec.command, tuple(spec.args), spec.env)
    if not listed:
        return 0
    prefixed = {
        f"{namespace}_{tool.name}": tool.model_copy(update={"name": f"{namespace}_{tool.name}"})
        for tool in listed
    }
    merge = getattr(executor, "merge_namespace_tools", None)
    if callable(merge):
        merge(namespace, prefixed)
        return len(prefixed)
    return 0


def _specs_equal(a: Any, b: Any) -> bool:
    """Whether two namespace specs describe the same server (defensive)."""

    try:
        return bool(a == b)
    except Exception:  # noqa: BLE001 - unequal-on-error keeps the first spec
        return False


def _stamp(obj: Any, name: str, value: Any) -> None:
    """Best-effort attribute stamp (mirrors agent.py's suppress pattern)."""

    try:
        setattr(obj, name, value)
    except (AttributeError, TypeError):
        pass
