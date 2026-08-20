"""Idle-TTL + LRU reclamation for per-workspace tool executors (#930 S3/#933).

Each distinct workspace root materializes a resident MCP fleet (executor +
lazily-spawned stdio servers). Before this module they lived until process
shutdown — a day of desktop use accumulated fleets toward OOM (#929). The
reaper closes a workspace executor once it has been IDLE for the TTL, and
enforces an LRU cap on how many stay resident, with three hard guarantees:

- **Drain-aware**: an executor with a call in flight is never closed; the
  in-flight count and idle clock live on the executor itself
  (``SyncMCPToolExecutor.busy`` / ``idle_for``).
- **Resolve-aware** (#1230): ``ClioAgent._active_tool_executor`` resolves an
  executor and returns it to the caller BEFORE the caller ever marks it busy
  (DSPy tool dispatch calls ``call_tool`` after the resolve returns, outside
  the shared registry lock) — the gap between "resolved for an imminent call"
  and "the call actually starts" is otherwise unprotected: an executor already
  idle past TTL at the moment a NEW call resolves it can be popped-and-closed
  by a reap tick landing in that gap, so the caller's very next line raises on
  a closed executor (the live defect: two ``idle_ttl`` reaps during one
  ~220s jarvis dispatch). :meth:`WorkspaceExecutorReaper.note_resolved` closes
  it: resolving a root for use counts as activity for one TTL window, exactly
  like an in-flight call or a live-turn lease.
- **Typed, never silent**: every reap emits a structured trace reason
  (``workspace_fleet_reaped reason=idle_ttl|lru_cap``) to the process
  log/trace plane. A reaped fleet is not a degradation — the next tool call
  rebuilds it lazily (#932 machinery) — but the reason is always recorded.

The registry lock is shared with the executor getter (the agent passes its
own lock) so a reap can never race a concurrent get-or-create handing out a
just-closed executor.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from clio_agent import conf
from clio_agent.runtime import trace

_DEFAULT_TTL_S = 120.0
_DEFAULT_MAX_RESIDENT = 2
_TICK_S = 15.0

#: Typed outcomes of a drain-aware fleet restart request (#1033) — never silent. A resident
#: fleet child is workspace-SHARED and long-lived, so a mid-session write-root grant does not
#: reach it until it respawns; :meth:`WorkspaceExecutorReaper.request_restart` makes that true
#: at a safe boundary and reports which happened.
RESTART_RESTARTED_LIVE = "restarted_live"  # idle+unleased → closed+evicted now; rebuild is live
RESTART_DEFERRED_BUSY = "restart_deferred_busy"  # busy/leased → pending; drains at next idle pass
RESTART_NO_RESIDENT = "no_resident_child"  # nothing resident → next spawn picks up the territory


def _default_channel_closer(root: str) -> int:
    """Close a workspace's per-child net channels (the reap/restart stop step, #1033)."""

    from clio_agent.runtime.sandbox_net import close_namespace_children  # noqa: PLC0415

    return close_namespace_children(root)


def workspace_fleet_ttl_s() -> float:
    """The idle TTL for per-workspace fleets (config/env; #933)."""

    return float(
        conf.resolve(
            "tools.mcp.workspace_ttl_s",
            env="CLIO_MCP_WORKSPACE_TTL_S",
            default=_DEFAULT_TTL_S,
            cast=conf.as_float,
        )
    )


def workspace_fleet_max_resident() -> int:
    """The LRU cap on resident per-workspace fleets (config/env; #933)."""

    return int(
        conf.resolve(
            "tools.mcp.workspace_max_resident",
            env="CLIO_MCP_WORKSPACE_MAX_RESIDENT",
            default=_DEFAULT_MAX_RESIDENT,
            cast=conf.as_int,
        )
    )


class WorkspaceExecutorReaper:
    """Background reaper over a ``{workspace_root: executor}`` registry."""

    def __init__(
        self,
        registry: dict[str, Any],
        lock: threading.Lock,
        *,
        leases: dict[str, int] | None = None,
        ttl_s: float | None = None,
        max_resident: int | None = None,
        tick_s: float = _TICK_S,
        channel_closer: Callable[[str], int] | None = None,
    ) -> None:
        self._registry = registry
        self._lock = lock
        # Turn-scoped leases (#933): a root leased by a LIVE TURN is never
        # reaped even while idle between tool calls — DSPy tools bind the
        # executor for the whole expert lifetime, so the drain unit is the
        # turn, not the individual call.
        self._leases = leases if leases is not None else {}
        # #1033: roots with a deferred fleet restart (grant landed while the
        # executor was busy/leased). The drain-aware pass in ``reap_once`` fires
        # each one at the next safe boundary — NOT a second scheduler, the same
        # idle/lease-gated loop the reaper already runs. Guarded by ``self._lock``.
        self._pending_restarts: set[str] = set()
        # #1230: monotonic timestamp of the last time a root was RESOLVED for an
        # imminent call (``note_resolved``) — the resolve-to-busy gap guard.
        # Bounded by the registry: a root is dropped here the instant it is
        # reaped/evicted (below), so this can never grow past resident roots.
        self._resolved_at: dict[str, float] = {}
        # The net-channel stop step (#1033): closes a workspace's per-child
        # chokepoint listeners when its fleet is torn down (reap OR restart), so
        # the previously-unwired ``close_child_channel`` seam stops leaking.
        self._channel_closer = channel_closer or _default_channel_closer
        self._ttl_s = workspace_fleet_ttl_s() if ttl_s is None else ttl_s
        self._max_resident = (
            workspace_fleet_max_resident() if max_resident is None else max_resident
        )
        self._tick_s = tick_s
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="clio-workspace-fleet-reaper", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.ident is not None:
            self._thread.join(timeout=self._tick_s + 5)

    def _run(self) -> None:
        while not self._stop.wait(self._tick_s):
            try:
                self.reap_once()
            except Exception as exc:  # noqa: BLE001 - the reaper must never die silently
                trace.event("TOOLS", "workspace fleet reaper tick failed: reason=%s", exc)

    def reap_once(self) -> list[str]:
        """One reap pass; returns the roots reaped (test seam)."""

        to_close: list[tuple[str, Any, str]] = []
        now = time.monotonic()
        try:
            with self._lock:
                # #1033 deferred restarts FIRST: a root whose grant landed while
                # busy fires here the moment it goes idle+unleased — reason-tagged
                # ``grant_restart`` so the close+channel-close path (below) rebuilds
                # it lazily with the widened write territory. A pending root no
                # longer resident (already reaped) simply drops its flag: the next
                # spawn reads the widened territory anyway (no restart needed).
                for root in list(self._pending_restarts):
                    executor = self._registry.get(root)
                    if executor is None or getattr(executor, "closed", False):
                        self._pending_restarts.discard(root)
                        continue
                    try:
                        busy = bool(executor.busy) or self._leases.get(root, 0) > 0
                    except Exception as exc:  # noqa: BLE001 - can't prove idle → keep deferred
                        trace.event(
                            "TOOLS",
                            "workspace_fleet_restart_probe_failed root=%s reason=%s",
                            root,
                            exc,
                        )
                        continue
                    if not busy:
                        to_close.append((root, self._registry.pop(root), "grant_restart"))
                        self._pending_restarts.discard(root)
                # Idle TTL. Leased roots (live turns) are untouchable. A
                # registry entry whose probe raises is skipped with a typed
                # reason rather than aborting the pass — an abort here would
                # orphan already-popped executors (never closed, fleet leaks).
                for root, executor in list(self._registry.items()):
                    try:
                        expired = (
                            not executor.busy
                            and self._leases.get(root, 0) == 0
                            and not self._recently_resolved(root, now)
                            and executor.idle_for() >= self._ttl_s
                        )
                    except Exception as exc:  # noqa: BLE001 - typed skip, never abort
                        trace.event(
                            "TOOLS",
                            "workspace_fleet_probe_failed root=%s reason=%s",
                            root,
                            exc,
                        )
                        continue
                    if expired:
                        to_close.append((root, self._registry.pop(root), "idle_ttl"))
                # LRU cap on the remainder (most-recently-active survive).
                if len(self._registry) > self._max_resident:
                    candidates: list[tuple[str, Any, float]] = []
                    for root, ex in self._registry.items():
                        try:
                            if (
                                not ex.busy
                                and self._leases.get(root, 0) == 0
                                and not self._recently_resolved(root, now)
                            ):
                                candidates.append((root, ex, ex.idle_for()))
                        except Exception as exc:  # noqa: BLE001 - typed skip, never abort
                            trace.event(
                                "TOOLS",
                                "workspace_fleet_probe_failed root=%s reason=%s",
                                root,
                                exc,
                            )
                    candidates.sort(key=lambda item: -item[2])
                    overflow = len(self._registry) - self._max_resident
                    for root, _executor, _idle in candidates[:overflow]:
                        to_close.append((root, self._registry.pop(root), "lru_cap"))
        finally:
            # Whatever got popped MUST be closed, even if collection blew up
            # part-way — a popped-but-unclosed executor is an invisible fleet.
            reaped: list[str] = []
            for root, executor, reason in to_close:
                # #1230: a torn-down root's resolve-activity marker goes with it —
                # never left to protect a FUTURE (unrelated) resident at this root.
                self._resolved_at.pop(root, None)
                try:
                    executor.close()
                except Exception as exc:  # noqa: BLE001 - close error is typed below
                    trace.event(
                        "TOOLS",
                        "workspace_fleet_reap_close_failed root=%s reason=%s",
                        root,
                        exc,
                    )
                    # The executor is already POPPED; its per-child net channels must still be
                    # closed on the error path, or their listeners leak toward _MAX_CHILD_CHANNELS
                    # (the next lazy rebuild overwrites the (root,namespace) ids, orphaning them).
                    # Mirrors request_restart, which closes channels even after a close error.
                    self._close_channels(root)
                    continue
                # #1033: a torn-down fleet's per-child net channels go with it —
                # closing them here wires the previously-unused close_child_channel
                # seam so per-child listeners stop leaking. Typed, never fatal.
                self._close_channels(root)
                reaped.append(root)
                trace.event(
                    "TOOLS",
                    "workspace_fleet_reaped root=%s reason=%s resident=%d",
                    root,
                    reason,
                    len(self._registry),
                )
        return reaped

    def _close_channels(self, root: str) -> int:
        """Close the workspace's per-child net channels (guarded, typed) — #1033."""

        try:
            return self._channel_closer(root)
        except Exception as exc:  # noqa: BLE001 - a channel-close error must never abort teardown
            trace.event(
                "TOOLS",
                "workspace_fleet_channel_close_failed root=%s reason=%s",
                root,
                exc,
            )
            return 0

    def note_resolved(self, root: str) -> None:
        """Mark ``root`` as just resolved for an imminent call (#1230).

        MUST be called while the caller already holds the shared registry lock
        (``ClioAgent._active_tool_executor``'s ``with lock:`` block) — this does
        NOT re-acquire it (``self._lock`` is that same lock; it is not
        reentrant). Closes the resolve-to-busy gap: a resolve returns the
        executor to the caller before the caller's tool dispatch marks it busy,
        so a reap tick landing in that window would otherwise see an executor
        already idle past TTL, unleased, and not-yet-busy, and reap it out from
        under the call about to use it.
        """
        self._resolved_at[root] = time.monotonic()

    def _recently_resolved(self, root: str, now: float) -> bool:
        """True while ``root`` is still inside its post-resolve protection window."""
        resolved_at = self._resolved_at.get(root)
        return resolved_at is not None and (now - resolved_at) < self._ttl_s

    def request_restart(self, workspace_root: str) -> str:
        """Request a drain-aware restart of ``workspace_root``'s resident fleet (#1033).

        The primitive behind a mid-session write-root grant taking effect on an already-spawned,
        workspace-shared fleet child. Under the SHARED registry lock (so it can never race a
        concurrent get-or-create or the reaper's pass):

        * idle + unleased → close the executor, close its per-child net channels, and evict it
          from the registry so the next ``_active_tool_executor`` rebuilds lazily with the
          widened ``effective_write_roots`` (returns :data:`RESTART_RESTARTED_LIVE`);
        * busy or leased → NEVER close it mid-call; flag the root for a deferred restart that
          the reaper's idle pass drains at the next safe boundary (returns
          :data:`RESTART_DEFERRED_BUSY`);
        * nothing resident → no-op; the next spawn reads the widened territory anyway (returns
          :data:`RESTART_NO_RESIDENT`).

        The executor + channels are closed OUTSIDE the lock (a channel join can block) after an
        atomic close-and-evict under it — mirroring :meth:`reap_once`.
        """

        root = (workspace_root or "").strip()
        if not root:
            return RESTART_NO_RESIDENT
        to_close: Any = None
        with self._lock:
            executor = self._registry.get(root)
            if executor is None or getattr(executor, "closed", False):
                self._pending_restarts.discard(root)
                return RESTART_NO_RESIDENT
            try:
                busy = bool(executor.busy) or self._leases.get(root, 0) > 0
            except Exception as exc:  # noqa: BLE001 - can't prove idle → defer, never close busy
                trace.event(
                    "TOOLS",
                    "workspace_fleet_restart_probe_failed root=%s reason=%s",
                    root,
                    exc,
                )
                self._pending_restarts.add(root)
                return RESTART_DEFERRED_BUSY
            if busy:
                self._pending_restarts.add(root)
                trace.event("TOOLS", "workspace_fleet_restart_deferred root=%s reason=busy", root)
                return RESTART_DEFERRED_BUSY
            to_close = self._registry.pop(root)
            self._pending_restarts.discard(root)
        try:
            to_close.close()
        except Exception as exc:  # noqa: BLE001 - close error is typed, restart still evicted
            trace.event(
                "TOOLS", "workspace_fleet_restart_close_failed root=%s reason=%s", root, exc
            )
        self._close_channels(root)
        trace.event("TOOLS", "workspace_fleet_restarted root=%s reason=grant_applied_live", root)
        return RESTART_RESTARTED_LIVE


ReaperFactory = Callable[..., WorkspaceExecutorReaper]
