"""Idle-TTL + LRU reclamation for per-workspace tool executors (#930 S3/#933).

Each distinct workspace root materializes a resident MCP fleet (executor +
lazily-spawned stdio servers). Before this module they lived until process
shutdown — a day of desktop use accumulated fleets toward OOM (#929). The
reaper closes a workspace executor once it has been IDLE for the TTL, and
enforces an LRU cap on how many stay resident, with two hard guarantees:

- **Drain-aware**: an executor with a call in flight is never closed; the
  in-flight count and idle clock live on the executor itself
  (``SyncMCPToolExecutor.busy`` / ``idle_for``).
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
from typing import Any, Callable

from clio_agent import conf
from clio_agent.runtime import trace

_DEFAULT_TTL_S = 120.0
_DEFAULT_MAX_RESIDENT = 2
_TICK_S = 15.0


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
    ) -> None:
        self._registry = registry
        self._lock = lock
        # Turn-scoped leases (#933): a root leased by a LIVE TURN is never
        # reaped even while idle between tool calls — DSPy tools bind the
        # executor for the whole expert lifetime, so the drain unit is the
        # turn, not the individual call.
        self._leases = leases if leases is not None else {}
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
        try:
            with self._lock:
                # Idle TTL. Leased roots (live turns) are untouchable. A
                # registry entry whose probe raises is skipped with a typed
                # reason rather than aborting the pass — an abort here would
                # orphan already-popped executors (never closed, fleet leaks).
                for root, executor in list(self._registry.items()):
                    try:
                        expired = (
                            not executor.busy
                            and self._leases.get(root, 0) == 0
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
                            if not ex.busy and self._leases.get(root, 0) == 0:
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
                try:
                    executor.close()
                except Exception as exc:  # noqa: BLE001 - close error is typed below
                    trace.event(
                        "TOOLS",
                        "workspace_fleet_reap_close_failed root=%s reason=%s",
                        root,
                        exc,
                    )
                    continue
                reaped.append(root)
                trace.event(
                    "TOOLS",
                    "workspace_fleet_reaped root=%s reason=%s resident=%d",
                    root,
                    reason,
                    len(self._registry),
                )
        return reaped


ReaperFactory = Callable[..., WorkspaceExecutorReaper]
