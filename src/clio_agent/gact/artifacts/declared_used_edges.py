"""``create_artifact``'s own declared ``used=[...]`` input refs (#1191).

Owner module (no-accretion ground rule): split out rather than appended to
:mod:`transform_edges` (the generic used/authority-arg-scan detectors) or
:mod:`transforms` (the record orchestration) — the SAME split rationale
``transform_edges`` itself was carved out of ``transforms`` under.

Owner ruling 2026-08-06 (issue #1191, OPTIONAL semantics — "yes lets add this,
as optional"): the model MAY cite the inputs a ``create_artifact`` mint was
DERIVED FROM — workspace paths and/or artifact ids — so the deliverable's
lineage graph gains a real input chain instead of staying a single node
(before this, every agent-proposed artifact's ``producer_activity_id`` was
always ``""`` — the model's own narration named its inputs, but nothing
recorded them). :func:`detect_declared_used_edges` resolves the refs;
:mod:`transforms`' ``_declared_generated_versions`` (gated on the SAME
non-blank check, :func:`create_artifact_used_refs`) folds this call's own mint
into ``generated`` so the producing activity carries both sides.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional
from urllib.parse import urlsplit

from clio_agent.gact.artifacts.transform_edges import EdgeScan
from clio_agent.gact.artifacts.transform_types import EdgeEvidence, EdgeRole, ProvEdge

if TYPE_CHECKING:
    from fastapi import FastAPI

#: Matched against a call's short (unnamespaced) tool name.
_CREATE_ARTIFACT_TOOL_NAME = "create_artifact"


def create_artifact_used_refs(args: Any) -> list[str]:
    """The non-blank ``used`` ref strings from a ``create_artifact`` call's args.

    A non-list / absent / blank-only value yields no refs (today's behavior,
    unchanged — the regression pin an omitted ``used`` must hold).
    """
    raw = args.get("used") if isinstance(args, dict) else None
    if not isinstance(raw, list):
        return []
    return [str(r).strip() for r in raw if str(r or "").strip()]


def detect_declared_used_edges(
    app: "FastAPI",
    *,
    tool_name: str,
    args: dict[str, Any],
    workspace_id: str,
) -> EdgeScan:
    """Resolve ``create_artifact``'s explicit ``used=[...]`` input refs (#1191).

    Each ref resolves against the registry: an ``artifact_<hex>`` id resolves
    DIRECTLY (:meth:`ArtifactRegistry.get_by_artifact_id`, workspace-agnostic —
    the model may cite an input it read from a sibling workspace); an exact
    HTTP(S) URL becomes an assertion-class external source; anything else is a
    workspace path, containment-checked against the bound root and
    matched by :meth:`ArtifactRegistry.find_version_by_path` (the SAME matcher
    ``transform_edges.detect_used_edges`` uses for the generic arg-scan
    channel — which excludes THIS arg via ``_DECLARED_CHANNEL_ARG_NAMES`` so
    the two never double-edge the same ref). Precision over recall (#966.10):
    an unresolvable ref is NEVER fabricated into an edge — it lands as a typed
    ``used_ref_unresolved`` note so the miss is detectable on the trace, never
    silently dropped. Absent/blank ``used`` -> no edges, no notes at all (the
    regression pin: an ordinary ``create_artifact`` call without declared
    inputs stays exactly as before).
    """
    short = tool_name.rsplit(".", 1)[-1] if "." in tool_name else tool_name
    if short != _CREATE_ARTIFACT_TOOL_NAME:
        return EdgeScan([], [])
    refs = create_artifact_used_refs(args)
    if not refs:
        return EdgeScan([], [])
    from clio_agent.gact.artifacts.minting import _contained, _workspace_root  # noqa: PLC0415
    from clio_agent.gact.artifacts.registry import get_registry  # noqa: PLC0415

    registry = get_registry(app)
    root = _workspace_root(app, workspace_id)
    edges: list[ProvEdge] = []
    notes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for ref in refs:
        if ref in seen:
            continue
        seen.add(ref)
        url_edge = _declared_url_edge(ref)
        if url_edge is not None:
            edges.append(url_edge)
            continue
        resolved = _resolve_declared_used_ref(registry, root, workspace_id, ref, _contained)
        if resolved is None:
            notes.append({"reason": "used_ref_unresolved", "arg": "used", "ref": ref})
            continue
        record, version = resolved
        edges.append(
            ProvEdge(
                role=EdgeRole.USED,
                evidence=(EdgeEvidence.HASH_PAIR if version.sha256 else EdgeEvidence.SCHEMA_ARG),
                artifact_id=version.artifact_id,
                sha256=version.sha256,
                name=record.name,
                version=version.version,
                path=version.path,
                arg="used",
                note=("" if version.sha256 else "stat_pinned"),
            )
        )
    return EdgeScan(edges, notes)


def _declared_url_edge(ref: str) -> ProvEdge | None:
    """Represent an explicitly cited HTTP(S) source without claiming it was fetched."""
    try:
        parsed = urlsplit(ref)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.username or parsed.password:
        return None
    return ProvEdge(
        role=EdgeRole.USED,
        evidence=EdgeEvidence.ASSERTION,
        authority=ref,
        external_ref=f"external:{ref}",
        name=parsed.hostname,
        arg="used",
        note="model_declared_url",
    )


def _resolve_declared_used_ref(
    registry: Any,
    root: Optional[Path],
    workspace_id: str,
    ref: str,
    contained_fn: Any,
) -> Optional[tuple[Any, Any]]:
    """Resolve one declared ``used`` ref to ``(record, version)``, or ``None``.

    An ``artifact_<hex>`` ref resolves DIRECTLY by id; anything else is a
    workspace path, resolved relative to the bound root and matched by its
    recorded ``version.path``. ``None`` means the caller records a typed miss
    — this never raises and never guesses.
    """
    if ref.startswith("artifact_"):
        return registry.get_by_artifact_id(ref)
    if root is None:
        return None
    try:
        candidate = Path(ref)
        if not candidate.is_absolute():
            candidate = root / ref
        if not contained_fn(candidate, root):
            return None
        resolved_path = str(candidate.expanduser().resolve(strict=False))
    except (TypeError, ValueError, OSError):
        return None
    return registry.find_version_by_path(workspace_id, resolved_path)


__all__ = [
    "create_artifact_used_refs",
    "detect_declared_used_edges",
]
