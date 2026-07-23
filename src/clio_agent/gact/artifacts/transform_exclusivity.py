"""The ``fence_proven`` mint-time wiring (B6 #980) — owner module (no accretion).

Split out of :mod:`clio_agent.gact.artifacts.transforms` (the record orchestration owner) so
that file stays under its size ratchet. The PURE exclusivity set-math is
:func:`clio_agent.gact.artifacts.transform_types.fence_proves_exclusivity`; THIS module holds
the app-state wiring that gathers its inputs at mint — the current sandbox state, this call's
output territory (the workspace root), and every OTHER in-flight actor's write territory — and
decides the per-edge lease-window → fence_proven upgrade for the GENERATED (written) side.

Upgraded PER EDGE at mint, NEVER retroactively; ``False`` on the floor (no fence → correlated
only) and on a ``contended`` record (two fenced actors legitimately sharing a granted root —
the fence NARROWS exclusivity, never FAKES it). Precision over recall (#966.10): any ambiguity
yields plain lease-window, never a false fence.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from clio_agent.gact.artifacts.transform_types import TransformKind, fence_proves_exclusivity

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)


def _other_actor_write_roots(app: "FastAPI", session_id: str) -> list[list[Path]]:
    """Each OTHER in-flight actor's effective write territory (the exclusivity-math input).

    Enumerates the write_roots every concurrent session (≠ this one) is fenced to, so
    :func:`fence_proves_exclusivity` can decide whether any of them could reach this call's
    output territory. Best-effort — an unresolvable peer contributes no roots (it cannot narrow
    the proof). Single-session is the common case → an empty list → vacuously exclusive under
    an active fence.
    """
    from clio_agent.gact.artifacts.minting import _workspace_root  # noqa: PLC0415
    from clio_agent.gact.artifacts.versions import _session_workspace  # noqa: PLC0415
    from clio_agent.runtime.sandbox import PROFILE_FLEET, effective_write_roots  # noqa: PLC0415

    out: list[list[Path]] = []
    in_flight = getattr(app.state, "in_flight_turns", None)
    if not in_flight:
        return out
    # A ``RuntimeError`` here means the in-flight map MUTATED during enumeration — i.e. peers
    # exist but could not be listed (maximal ambiguity). It MUST NOT be swallowed into the
    # empty accumulator (which the caller reads as "no other actors → vacuously exclusive → a
    # FALSE fence_proven"). Let it propagate to :func:`generated_fence_proven`'s outer guard,
    # which fails safe to plain lease-window (precision over recall #966.10 — the same posture
    # as ``versions.py::_workspace_single_writer``). "Cannot enumerate" ≠ "no peers".
    others = [s for s in list(in_flight.keys()) if s and s != session_id]
    for other in others:
        other_ws = _session_workspace(app, other)
        other_root = _workspace_root(app, other_ws) if other_ws else None
        roots = effective_write_roots(
            PROFILE_FLEET, workspace_root=str(other_root) if other_root is not None else None
        )
        out.append(list(roots))
    return out


def generated_fence_proven(
    app: "FastAPI", workspace_id: str, session_id: str, *, kind: TransformKind
) -> bool:
    """Whether the active OS fence PROVES this call's output territory exclusive (B6 #980).

    The lease-window → fence_proven upgrade, computed at mint (never retroactively). ``True``
    only when ALL hold: the record is ``ordinary`` (a ``contended`` record rides a candidate
    set — two fenced actors sharing a granted root stay contended); an OS write fence is ACTIVE
    this process (floor tiers are correlated-only); and the exclusivity set-math holds — no
    OTHER in-flight actor's territory overlaps this call's workspace output territory
    (:func:`fence_proves_exclusivity`). Guarded: any failure yields ``False`` (plain
    lease-window), never a false ``fence_proven`` (precision over recall).
    """
    if kind is not TransformKind.ORDINARY:
        return False
    try:
        from clio_agent.gact.artifacts.minting import _workspace_root  # noqa: PLC0415
        from clio_agent.runtime.sandbox import current_state  # noqa: PLC0415

        state = current_state()
        if state is None or not state.active:
            return False
        root = _workspace_root(app, workspace_id)
        if root is None:
            return False
        others = _other_actor_write_roots(app, session_id)
        return fence_proves_exclusivity([root], others)
    except Exception:  # noqa: BLE001 — a provenance upgrade must never break a turn
        logger.debug("fence_proven upgrade skipped reason=fence_proven_probe_failed", exc_info=True)
        return False


__all__ = ["generated_fence_proven"]
