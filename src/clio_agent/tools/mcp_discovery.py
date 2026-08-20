"""Concurrent, non-readiness-blocking MCP namespace discovery (#1232 pt 2).

``tools.gateway.list_tool_definitions`` lists every declared namespace
SERIALLY with no per-namespace bound: a namespace that never answers burns
its full retry budget (``tools.mcp.probe_timeout_retries`` x
``tools.mcp.setup_timeout_s``) before the NEXT namespace even starts, and
"agent ready" (``ClioAgent.__init__``) waits for the whole pass. Three dead
namespaces therefore turned into minutes of boot (owner-observed, #1232).

This module is the fix: :func:`discover_declared_tools_bounded` lists every
declared namespace CONCURRENTLY (a bounded thread pool — #942's peak-RSS
concern is a real tradeoff, not dismissed; concurrency is capped rather than
unbounded), each with its OWN per-namespace deadline, so one dead/slow
namespace can never inflate another's cost. A namespace that misses its
deadline is an immediate typed degrade (``MCP_NAMESPACE_DISCOVERY_TIMEOUT`` /
``MCP_NAMESPACE_DISCOVERY_UNREACHABLE``) — never a raise, never a block.

:class:`NamespaceDiscoveryHealer` is the background half: a daemon thread
that re-attempts every degraded namespace on a fixed interval and, on
success, calls back so the owner (``ClioAgent``) can merge the newly-listed
tools into the live catalog and emit the typed
``MCP_NAMESPACE_DISCOVERY_HEALED`` heal event. Boot never waits on this
either — ``ClioAgent.__init__`` starts it and returns.
"""

from __future__ import annotations

import concurrent.futures
import logging
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from clio_agent.errors import (
    LAUNCHER_CACHE_LOCK_TIMEOUT,
    MCP_NAMESPACE_DISCOVERY_HEALED,
    MCP_NAMESPACE_DISCOVERY_TIMEOUT,
    MCP_NAMESPACE_DISCOVERY_UNREACHABLE,
)
from clio_agent.tools.mcp_config import MCPServerSpec

logger = logging.getLogger(__name__)


def _classify_degrade_reason(exc: BaseException) -> str:
    """Typed reason for one namespace's failed discovery attempt (#1232 pt 3/4)."""

    from clio_agent.tools.launcher_cache_lock import LauncherCacheLockTimeoutError  # noqa: PLC0415

    if isinstance(exc, LauncherCacheLockTimeoutError):
        return LAUNCHER_CACHE_LOCK_TIMEOUT
    return MCP_NAMESPACE_DISCOVERY_UNREACHABLE


_DEFAULT_CONCURRENCY = 8
_DEFAULT_HEAL_TICK_S = 20.0
_POLL_INTERVAL_S = 0.1


def discovery_concurrency() -> int:
    """Max simultaneously-live declared-namespace chains during a discovery pass."""

    from clio_agent import conf  # noqa: PLC0415 - avoid import cycle at module load

    return int(
        conf.resolve(
            "tools.mcp.discovery_concurrency",
            env="CLIO_MCP_DISCOVERY_CONCURRENCY",
            default=_DEFAULT_CONCURRENCY,
            cast=conf.as_int,
        )
    )


def discovery_heal_interval_s() -> float:
    """Interval between background re-probes of degraded namespaces."""

    from clio_agent import conf  # noqa: PLC0415 - avoid import cycle at module load

    return float(
        conf.resolve(
            "tools.mcp.discovery_heal_interval_s",
            env="CLIO_MCP_DISCOVERY_HEAL_INTERVAL_S",
            default=_DEFAULT_HEAL_TICK_S,
            cast=conf.as_float,
        )
    )


def _namespace_attempt_timeout_s(namespace: str) -> float:
    """Per-attempt deadline for ONE namespace's discovery listing.

    ``tools.mcp.setup_timeout_s`` times a single negotiate/list round-trip;
    ``mcp_probe_hardening.resolve_timeout_retries`` (per-server override,
    #1232 pt 4) says how many times ``negotiate_auto`` retries a bare
    REQUEST_TIMEOUT INSIDE that attempt. The outer deadline here must cover
    the full retried budget, or a legitimately-retrying namespace would be
    degraded before its own configured retries exhaust.
    """

    from clio_agent import conf  # noqa: PLC0415 - avoid import cycle at module load
    from clio_agent.tools.mcp_probe_hardening import resolve_timeout_retries  # noqa: PLC0415

    setup_timeout_s = float(
        conf.resolve(
            "tools.mcp.setup_timeout_s",
            env="CLIO_MCP_SETUP_TIMEOUT_S",
            default=10.0,
            cast=conf.as_float,
        )
    )
    retries = resolve_timeout_retries(namespace)
    return setup_timeout_s * (retries + 1)


def _list_one_namespace(namespace: str, spec: MCPServerSpec) -> dict[str, Any]:
    """List one declared namespace's tools (cache-first), prefixed like the catalog wants.

    Runs on its OWN thread-pool worker thread (see
    :func:`discover_declared_tools_bounded` / :meth:`NamespaceDiscoveryHealer.probe_once`).
    ``probe_server_context`` is bound HERE, before ``_list_declared_tools``
    spins its own event loop via ``asyncio.run`` on this SAME thread, so
    ``hardened_negotiate_auto``'s per-server retry-budget lookup (#1232 pt 4)
    resolves this namespace's override — contextvars propagate into
    ``asyncio.run``'s task on the thread that calls it, no cross-thread hop.

    A cold spawn onto the SHARED dedicated uv cache first acquires the
    bounded launcher-cache lock (#1232 pt 3) — serializing just the spawn
    step against sibling COLD spawns from this same concurrent pass, never
    the whole connect — and raises typed on a wedged lock instead of
    stalling this namespace (and, pre-#1232-pt-2, every namespace after it).
    """

    from clio_agent.tools import listing_cache  # noqa: PLC0415
    from clio_agent.tools.gateway import _list_declared_tools  # noqa: PLC0415
    from clio_agent.tools.launcher_cache_lock import (  # noqa: PLC0415
        acquire_launcher_cache_lock,
        uses_shared_launcher_cache,
    )
    from clio_agent.tools.mcp_probe_hardening import probe_server_context  # noqa: PLC0415

    cacheable = spec.transport == "stdio" and bool(spec.command)
    listed: list[Any] | None = None
    if cacheable:
        listed = listing_cache.load_listing(namespace, spec.command, tuple(spec.args), spec.env)
    if listed is None:
        with probe_server_context(namespace):
            if uses_shared_launcher_cache(spec):
                with acquire_launcher_cache_lock(namespace):
                    listed = _list_declared_tools(spec)
            else:
                listed = _list_declared_tools(spec)
        if cacheable:
            listing_cache.store_listing(namespace, spec.command, tuple(spec.args), listed, spec.env)
    return {
        f"{namespace}_{tool.name}": tool.model_copy(update={"name": f"{namespace}_{tool.name}"})
        for tool in listed
    }


@dataclass
class DiscoveryPass:
    """Result of one bounded-concurrent discovery pass."""

    tools: dict[str, Any] = field(default_factory=dict)
    #: namespace -> typed reason, for every namespace that missed its deadline.
    degraded: dict[str, str] = field(default_factory=dict)


def discover_declared_tools_bounded(
    specs: Mapping[str, MCPServerSpec], *, concurrency: int | None = None
) -> DiscoveryPass:
    """List every declared namespace CONCURRENTLY, each bounded by its own deadline.

    Never raises and never blocks past the SLOWEST namespace's own deadline —
    a dead namespace's cost never compounds onto a sibling's (#1232 pt 2). A
    namespace whose future is still running when its deadline passes is
    dropped from the wait (typed-degraded) but its underlying thread is not
    forcibly killed (Python threads are not cancellable); it either finishes
    naturally in the background and its result is discarded, or the process
    that owns ``specs`` picks it up again via :class:`NamespaceDiscoveryHealer`.
    """

    result = DiscoveryPass()
    if not specs:
        return result

    pool = concurrent.futures.ThreadPoolExecutor(
        max_workers=max(1, concurrency if concurrency is not None else discovery_concurrency()),
        thread_name_prefix="clio-mcp-discovery",
    )
    try:
        now = time.monotonic()
        pending: dict[concurrent.futures.Future, str] = {
            pool.submit(_list_one_namespace, namespace, spec): namespace
            for namespace, spec in specs.items()
        }
        deadlines = {
            namespace: now + _namespace_attempt_timeout_s(namespace) for namespace in specs
        }
        while pending:
            done, _not_done = concurrent.futures.wait(
                list(pending.keys()),
                timeout=_POLL_INTERVAL_S,
                return_when=concurrent.futures.FIRST_COMPLETED,
            )
            now = time.monotonic()
            for future in list(pending.keys()):
                namespace = pending[future]
                if future in done:
                    del pending[future]
                    try:
                        result.tools.update(future.result())
                    except Exception as exc:  # noqa: BLE001 - typed degrade, never sink the pass
                        reason = _classify_degrade_reason(exc)
                        result.degraded[namespace] = reason
                        logger.warning(
                            "mcp_namespace_discovery_degraded namespace=%s reason=%s error=%s",
                            namespace,
                            reason,
                            exc,
                        )
                    continue
                if now >= deadlines[namespace]:
                    del pending[future]
                    result.degraded[namespace] = MCP_NAMESPACE_DISCOVERY_TIMEOUT
                    logger.warning(
                        "mcp_namespace_discovery_degraded namespace=%s reason=%s deadline_s=%.1f",
                        namespace,
                        MCP_NAMESPACE_DISCOVERY_TIMEOUT,
                        _namespace_attempt_timeout_s(namespace),
                    )
        return result
    finally:
        # wait=False: an abandoned (timed-out) namespace's thread is left to
        # finish/die on its own rather than blocking pass teardown on it.
        pool.shutdown(wait=False)


class NamespaceDiscoveryHealer:
    """Background re-prober for namespaces degraded during a discovery pass (#1232 pt 2).

    Never blocks boot or a caller: :meth:`mark_degraded` just records the
    namespace; a daemon thread retries each pending namespace on a fixed
    interval and calls ``on_healed`` (with the newly-listed tools) the moment
    one answers, emitting the typed heal event either way.
    """

    def __init__(
        self,
        *,
        spec_provider: Callable[[], Mapping[str, MCPServerSpec]],
        on_healed: Callable[[str, dict[str, Any]], None],
        tick_s: float | None = None,
    ) -> None:
        self._spec_provider = spec_provider
        self._on_healed = on_healed
        self._tick_s = tick_s if tick_s is not None else discovery_heal_interval_s()
        self._degraded: dict[str, str] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="clio-mcp-discovery-healer", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        """Signal + wait (bounded) for the thread to exit — the orderly shutdown path."""

        self.request_stop()
        if self._thread.ident is not None:
            self._thread.join(timeout=self._tick_s + 5)

    def request_stop(self) -> None:
        """Signal the thread to exit WITHOUT waiting — for a caller that must not block.

        ``ClioAgent._start_mcp_namespace_discovery`` uses this to retire a
        stale healer from a PRIOR default-gateway build (e.g. a periodic
        relay-catalog refresh): that call runs on the gact event loop, so a
        bounded :meth:`stop` join (up to ``tick_s`` + 5s) would freeze the
        whole server for that window. ``threading.Event.set`` wakes a thread
        parked in ``wait()`` immediately, so the stale thread exits promptly
        regardless — this just does not BLOCK the caller confirming that; it
        is daemon=True either way, so an unconfirmed exit is never a leak at
        process shutdown.
        """

        self._stop.set()

    def mark_degraded(self, namespace: str, reason: str) -> None:
        """Record ``namespace`` as degraded so the background loop retries it.

        No-silent-fallback: the FIRST time a namespace degrades this is both
        logged AND emitted to the queryable audit sink (mirrors
        ``mcp_connection_era._record_downgrade``'s log+stream_audit pairing),
        not just a log line — a re-degrade on an already-pending namespace
        (e.g. a healer re-probe that failed again) updates the reason without
        re-emitting the audit event every tick.
        """

        with self._lock:
            already = namespace in self._degraded
            self._degraded[namespace] = reason
        if not already:
            logger.warning(
                "mcp_namespace_discovery_degraded namespace=%s reason=%s "
                "(background re-probe scheduled every %.0fs)",
                namespace,
                reason,
                self._tick_s,
            )
            from clio_agent.runtime.stream_audit import stream_audit  # noqa: PLC0415

            stream_audit("mcp_namespace_discovery_degraded", reason=reason, namespace=namespace)

    def pending(self) -> dict[str, str]:
        """Snapshot of currently-degraded namespaces (test/status seam)."""

        with self._lock:
            return dict(self._degraded)

    def _run(self) -> None:
        while not self._stop.wait(self._tick_s):
            try:
                self.probe_once()
            except Exception as exc:  # noqa: BLE001 - the healer must never die silently
                logger.warning("mcp_namespace_discovery_healer_tick_failed reason=%s", exc)

    def probe_once(self) -> list[str]:
        """One re-probe pass over every pending namespace; returns those healed."""

        with self._lock:
            pending = dict(self._degraded)
        if not pending:
            return []
        specs = self._spec_provider()
        healed: list[str] = []
        for namespace in pending:
            spec = specs.get(namespace)
            if spec is None:
                # No longer declared (config changed since it degraded) — drop it;
                # nothing to heal toward.
                with self._lock:
                    self._degraded.pop(namespace, None)
                continue
            try:
                tools = _list_one_namespace(namespace, spec)
            except Exception as exc:  # noqa: BLE001 - stays degraded, retried next tick
                reason = _classify_degrade_reason(exc)
                with self._lock:
                    self._degraded[namespace] = reason
                logger.debug(
                    "mcp_namespace_discovery_reprobe_failed namespace=%s reason=%s error=%s",
                    namespace,
                    reason,
                    exc,
                )
                continue
            with self._lock:
                self._degraded.pop(namespace, None)
            logger.warning(
                "mcp_namespace_discovery_healed reason=%s namespace=%s tool_count=%d",
                MCP_NAMESPACE_DISCOVERY_HEALED,
                namespace,
                len(tools),
            )
            from clio_agent.runtime.stream_audit import stream_audit  # noqa: PLC0415

            stream_audit(
                "mcp_namespace_discovery_healed",
                reason=MCP_NAMESPACE_DISCOVERY_HEALED,
                namespace=namespace,
                tool_count=len(tools),
            )
            self._on_healed(namespace, tools)
            healed.append(namespace)
        return healed


__all__ = [
    "DiscoveryPass",
    "NamespaceDiscoveryHealer",
    "discover_declared_tools_bounded",
    "discovery_concurrency",
    "discovery_heal_interval_s",
]
