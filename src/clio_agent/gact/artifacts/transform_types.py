"""Leaf value types for TransformRecords (S5 #971) — enums, edges, instrument.

Split out of :mod:`clio_agent.gact.artifacts.transforms` so both the record
orchestration (``transforms``) and the edge detection (``transform_edges``) share
one dependency-free type module (no import cycle). Pure Pydantic / enums — no I/O,
no app state.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from enum import Enum
from pathlib import Path
from typing import Any, Optional, Union

from pydantic import BaseModel, ConfigDict, Field

#: How many chars of an over-bound value are kept as a human-readable ``head``.
_INSTRUMENT_HEAD_CHARS = 256


class EdgeRole(str, Enum):
    """Which side of the transform an edge sits on (PROV ``used`` / ``generated``)."""

    USED = "used"
    GENERATED = "generated"


class EdgeEvidence(str, Enum):
    """How a provenance edge's identity is known (owner decision #966.5).

    Per-edge, never a single confidence scalar. ``schema-arg`` — the path came
    from a declared arg; ``hash-pair`` — content re-hashed and matched (or gap-
    minted) a registry version; ``lease-window`` — attribution under a clean
    single-writer lease; ``authority`` — a catalog URL/registry id/DOI/ETag;
    ``assertion`` — model-quarantined intent only (weakest).
    """

    SCHEMA_ARG = "schema-arg"
    HASH_PAIR = "hash-pair"
    LEASE_WINDOW = "lease-window"
    AUTHORITY = "authority"
    ASSERTION = "assertion"


class TransformStatus(str, Enum):
    """Whether the producing call succeeded (a failed run that wrote is provenance)."""

    SUCCESS = "success"
    FAILED = "failed"


class AgentRole(str, Enum):
    """Whether the agent EXECUTED the transform or merely ANNOTATED an existing one."""

    EXECUTING = "executing"
    ANNOTATING = "annotating"


class TransformKind(str, Enum):
    """Ordinary vs a ``contended`` record (owner decision #966.10).

    ``ordinary`` — the per-workspace-root executor lock proved a single writer for
    this window. ``contended`` — another active session/task could be writing the
    same workspace, so generated attribution rides a candidate set, never a false
    certainty.
    """

    ORDINARY = "ordinary"
    CONTENDED = "contended"


class ReplayContract(str, Enum):
    """The permanent, honest replay guarantee stamped on a record (owner decision #966.6).

    ``reproducible`` — environment tier ≥ ``lockfile-hash`` AND every used input
    is content-pinned, so a bit-identical replay is guaranteed. ``re-runnable`` —
    the run is fully described but one of those conditions fails; re-running may
    not be bit-identical. Never silently upgraded.
    """

    REPRODUCIBLE = "reproducible"
    RE_RUNNABLE = "re-runnable"


class ProvEdge(BaseModel):
    """One ``used`` / ``generated`` provenance edge with its own evidence.

    A registry-matched edge carries the relay ``artifact_id`` + ``sha256`` (the
    exact :class:`~clio_agent.gact.artifacts.records.ArtifactVersion` pair relay's
    ``ArtifactUse`` keys on); an external edge carries ``external_ref``
    (``external:<path>`` or a catalog URL) instead. ``evidence`` is the per-edge
    basis; ``note`` records a typed qualifier for a changed/degraded input so no
    downgrade is silent. On a hash-mismatch used edge it names the ACTUAL reconcile
    class (finding [3]): ``gap`` (dirty-lease custody gap), ``auto_revision``
    (provably-clean single-writer auto-mint), ``relink`` (revert to a known
    non-head version), or ``stale_fallback`` (reconcile skipped, edge kept the
    stale registered version) — never an unconditional ``gap_first``. Other notes:
    ``stat_pinned`` / ``over_threshold`` (no content hash).

    ``fence_proven`` (B6 #980) is the per-edge upgrade of a GENERATED edge's lease-window
    attribution: ``True`` only where an active OS write fence made this call's output
    territory EXCLUSIVE BY CONSTRUCTION (:func:`fence_proves_exclusivity` — its ``write_roots``
    were disjoint from every OTHER concurrent actor's during the write window), so the
    correlated single-writer window becomes proven, not merely asserted. ``False`` on the
    floor (no fence → correlated only) and on a ``contended`` record (two fenced actors
    legitimately sharing a granted root — the fence NARROWS exclusivity, never FAKES it).
    Stamped at mint, never retroactively; identity evidence (``hash-pair``) is unchanged.
    """

    model_config = ConfigDict(frozen=True)

    role: EdgeRole
    evidence: EdgeEvidence
    #: The relay ``artifact_id`` when this edge points at a registered version.
    artifact_id: str = ""
    sha256: Optional[str] = None
    #: ``external:<path>`` or a catalog URL for an edge NOT in the registry.
    external_ref: str = ""
    #: For ``authority`` evidence: the asserting reference (catalog URL / DOI / id).
    authority: str = ""
    name: str = ""
    version: Optional[int] = None
    path: str = ""
    #: The call arg the edge was discovered on (``""`` for result-derived edges).
    arg: str = ""
    note: str = ""
    #: Network-sourced INGEST evidence (B4 #978), joined from the clio chokepoint's
    #: ``net.egress`` record. ``net_domain`` is the chokepoint-observed remote host; a fresh
    #: web edge names it as ``web:<domain>@<time>`` in ``external_ref``, a joined
    #: staged-download edge keeps its ``sha256`` (hash-pair) AND gains ``net_domain`` (two
    #: evidence bases, one edge). ``net_mechanism`` is the honest per-edge enforcement
    #: (``proxy-enforced`` on the srt tier, ``env-cooperative`` on Landlock/floor — raw
    #: sockets bypass, so the record never claims completeness the tier can't provide).
    #: ``net_at`` is the egress timestamp; ``net_resolved_ip`` the DNS resolution the proxy
    #: performed on the child's behalf (the fenced child issues no raw UDP/53).
    net_domain: str = ""
    net_mechanism: str = ""
    net_at: str = ""
    net_resolved_ip: str = ""
    #: B6 (#980): the lease-window → fence_proven per-edge upgrade on a GENERATED edge. See the
    #: class docstring + :func:`fence_proves_exclusivity`. Default ``False`` (floor / contended).
    fence_proven: bool = False

    def to_artifact_use(self) -> Optional[dict[str, Any]]:
        """Project to the relay ``ArtifactUse {artifact_id, sha256}`` shape, or ``None``.

        Only a registry-matched edge WITH a content hash is a valid relay
        ``ArtifactUse`` (relay requires a 64-hex sha). A stat-pinned or purely
        external/authority edge has no sha and rides ``metadata`` instead — the
        exact convergence gap the clio-relay issue proposes closing.
        """
        if not self.artifact_id or not self.sha256:
            return None
        return {"artifact_id": self.artifact_id, "sha256": self.sha256}


class Instrument(BaseModel):
    """WHAT produced the transform — a tool call, or a ``{cmd, script_hash}`` script.

    A generated script that a shell/exec tool runs is itself a ``script``-kind
    artifact and its own hashed dependency (DVC's move): ``script_hash`` +
    ``script_artifact_id`` pin it, and it ALSO appears as a used edge.
    """

    model_config = ConfigDict(frozen=True)

    tool: str = ""
    args: dict[str, Any] = Field(default_factory=dict)
    cmd: str = ""
    script_hash: str = ""
    script_artifact_id: str = ""


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
