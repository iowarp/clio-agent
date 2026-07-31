"""Compatibility surface and clio-agent utilities for transform record types."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Union

from clio_schemas import (
    AgentRole,
    EdgeEvidence,
    EdgeRole,
    Instrument,
    ProvEdge,
    ReplayContract,
    TransformKind,
    TransformStatus,
)

_INSTRUMENT_HEAD_CHARS = 256


def _value_blob(value: Any) -> tuple[bytes, str]:
    """Return ``(utf8_bytes, head_text)`` for one arg value (str or JSON-encoded)."""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, default=str, sort_keys=True)
        except (TypeError, ValueError):
            text = str(value)
    return text.encode("utf-8", "replace"), text


def _bound_value(value: Any, arg_max_bytes: int) -> Any:
    """Bound one instrument arg value to ``arg_max_bytes`` (finding [7]).

    A value whose UTF-8/JSON size exceeds the per-arg bound is replaced by
    ``{sha256, size, truncated: true, head}`` — the full-content hash keeps the
    arg's IDENTITY (so a replay/dedup is still exact) without retaining the bytes in
    the process-lifetime registry or the durable trace event.
    """
    raw, text = _value_blob(value)
    if len(raw) <= arg_max_bytes:
        return value
    return {
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size": len(raw),
        "truncated": True,
        "head": text[:_INSTRUMENT_HEAD_CHARS],
    }


def bound_instrument_args(
    args: dict[str, Any], *, arg_max_bytes: int, total_max_bytes: int
) -> dict[str, Any]:
    """Bound instrument args per-arg AND as a whole (finding [7] — bounded memory).

    Every over-bound arg is elided to its content digest (:func:`_bound_value`).
    If the bounded dict is STILL over the whole-instrument ceiling (many medium
    args), it collapses to a single digest of the full args — identity preserved,
    bytes dropped — so one pathological call can never grow the registry / trace
    without bound.
    """
    bounded: dict[str, Any] = {str(k): _bound_value(v, arg_max_bytes) for k, v in args.items()}
    whole, _ = _value_blob(bounded)
    if len(whole) <= total_max_bytes:
        return bounded
    full, _ = _value_blob(args)
    return {
        "__args_truncated__": True,
        "sha256": hashlib.sha256(full).hexdigest(),
        "size": len(full),
        "truncated": True,
        "keys": sorted(str(k) for k in args)[:64],
    }


# --------------------------------------------------------------------------- #
# fence_proven exclusivity math (B6 #980) — the pure, unit-pinnable predicate.
# --------------------------------------------------------------------------- #

#: A write-territory root, given either as a string or a ``Path``.
RootLike = Union[str, Path]


def _root_overlap(a: Path, b: Path) -> bool:
    """Whether two write-territory roots overlap — equal, or one contains the other.

    Containment either way means a child fenced to one root could reach into the other's
    territory, so the two are NOT provably disjoint. Pure + cross-platform (pathlib only);
    an unresolvable path falls back to its literal form rather than raising.
    """
    try:
        a_r = a.expanduser().resolve(strict=False)
    except OSError:
        a_r = a
    try:
        b_r = b.expanduser().resolve(strict=False)
    except OSError:
        b_r = b
    if a_r == b_r:
        return True
    for inner, outer in ((a_r, b_r), (b_r, a_r)):
        try:
            inner.relative_to(outer)
            return True
        except ValueError:
            continue
    return False


def fence_proves_exclusivity(
    output_roots: Sequence[RootLike],
    other_actor_roots: Sequence[Sequence[RootLike]],
) -> bool:
    """Does an active fence PROVE this call's output territory exclusive? (B6 #980, pure).

    The per-edge ``lease-window`` → ``fence_proven`` upgrade predicate. Exclusive BY
    CONSTRUCTION iff no OTHER concurrent actor's write territory overlaps any of this call's
    ``output_roots`` — then, under an OS write fence that confines every actor to its own
    ``write_roots``, it is physically impossible for another actor to have written this call's
    outputs, so correlated single-writer attribution is proven, not merely asserted.

    Any overlap (a legitimately shared / B5-granted root) leaves exclusivity merely
    correlated and returns ``False`` — the fence NARROWS exclusivity, never FAKES it
    (precision over recall #966.10: an ambiguous territory is never a false ``fence_proven``).

    Boundary cases: empty ``output_roots`` → ``False`` (nothing to prove exclusive); no other
    actors → ``True`` (only this fenced actor can write here — vacuously exclusive). Pure —
    the caller (mint) supplies the resolved root sets; this decides only the set-math.
    """
    outs = [Path(r) for r in output_roots if str(r).strip()]
    if not outs:
        return False
    for actor in other_actor_roots:
        for raw in actor:
            if not str(raw).strip():
                continue
            other = Path(raw)
            if any(_root_overlap(out, other) for out in outs):
                return False
    return True


__all__ = [
    "AgentRole",
    "EdgeEvidence",
    "EdgeRole",
    "Instrument",
    "ProvEdge",
    "ReplayContract",
    "RootLike",
    "TransformKind",
    "TransformStatus",
    "bound_instrument_args",
    "fence_proves_exclusivity",
]
