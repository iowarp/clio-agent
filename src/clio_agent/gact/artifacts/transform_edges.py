"""Provenance edge detection for TransformRecords (S5 #971) — used + authority.

Split out of :mod:`clio_agent.gact.artifacts.transforms` (no-accretion — the record
model + recording orchestration own that file; edge DISCOVERY owns this one). Two
detectors, both **precision over recall** (owner decision #966.10):

* :func:`detect_used_edges` — walk the call args for strings that resolve to an
  existing file inside the workspace root and match a registered artifact by path,
  then re-hash under the threshold (hash equal → ``schema-arg`` + ``hash-pair``;
  hash differs → mint a GAP version FIRST and point the edge at it; over threshold
  → ``stat-pinned`` labeled). An existing contained file NOT in the registry → an
  ``external:<path>`` edge; anything else → no edge.
* :func:`detect_authority_edges` — NDP catalog inputs (``stage_resource`` /
  ``search_datasets`` / ``get_dataset_details``) register their catalog resource
  ``url`` / ``id`` as ``authority``-asserted inputs. NDP results carry no
  checksum/ETag/DOI (verified against the real server), so the catalog URL/UUID IS
  the authority; absent any recognized NDP shape → a typed skip.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from clio_agent.gact.artifacts.designation import OUTPUT_PATH_ARG_NAMES
from clio_agent.gact.artifacts.records import ArtifactVersion, Mechanism
from clio_agent.gact.artifacts.transform_types import EdgeEvidence, EdgeRole, ProvEdge

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

#: The NDP MCP tool names whose results carry authoritative catalog identity
#: (item 4). Verified against the real server
#: (``clio-kit/.../ndp/src/ndp_mcp/server.py``): these return dataset/resource
#: dicts with ``id`` + download ``url`` (no checksum/ETag/DOI — the URL/UUID is the
#: authority). ``stage_resource`` additionally downloads to a local path.
_NDP_CATALOG_TOOLS: frozenset[str] = frozenset(
    {"search_datasets", "get_dataset_details", "list_organizations"}
)
_NDP_STAGE_TOOL = "stage_resource"

#: Bound on how many authority-asserted catalog resources one result contributes,
#: so a large search result cannot grow a transform record unboundedly.
_MAX_AUTHORITY_EDGES = 32
#: Bound on recursion + fan-out when walking call args for candidate paths.
_MAX_ARG_STRINGS = 64
_MAX_ARG_DEPTH = 4


# --------------------------------------------------------------------------- #
# Used-edge detection (item 3) — precision over recall (owner decision #966.10).
# --------------------------------------------------------------------------- #


def _candidate_arg_strings(args: Any) -> list[tuple[str, str]]:
    """Walk call args for candidate path strings as ``(arg_name, value)`` pairs.

    Output-path args (:data:`OUTPUT_PATH_ARG_NAMES`) are EXCLUDED — those are the
    generated side, not used inputs. Nested dicts/lists are walked to a bounded
    depth and fan-out so a pathological arg blob cannot explode the scan (precision
    over recall — a missed deep path is a lost edge, never a false one).
    """
    out: list[tuple[str, str]] = []

    def walk(name: str, value: Any, depth: int) -> None:
        if len(out) >= _MAX_ARG_STRINGS or depth > _MAX_ARG_DEPTH:
            return
        if isinstance(value, str):
            if value.strip():
                out.append((name, value))
            return
        if isinstance(value, dict):
            for key, sub in value.items():
                if str(key) in OUTPUT_PATH_ARG_NAMES:
                    continue
                walk(str(key), sub, depth + 1)
        elif isinstance(value, (list, tuple)):
            for sub in value:
                walk(name, sub, depth + 1)

    if isinstance(args, dict):
        for key, value in args.items():
            if str(key) in OUTPUT_PATH_ARG_NAMES:
                continue
            walk(str(key), value, 0)
    return out


def detect_used_edges(
    app: "FastAPI",
    sid: str,
    *,
    args: dict[str, Any],
    workspace_id: str,
    turn_id: str,
    trace_id: str,
) -> list[ProvEdge]:
    """Detect ``used`` edges from call args (item 3 — precision over recall).

    For each candidate arg string: it must resolve to an existing file INSIDE the
    workspace root (else NO edge). A registry match by path re-hashes under the
    threshold — hash equal → ``schema-arg`` + ``hash-pair``; hash differs → mint a
    GAP version FIRST (S4 machinery) and point the edge at the gap; over threshold
    → ``stat-pinned`` labeled. An existing contained file NOT in the registry →
    an ``external:<path>`` edge (hashed when under threshold). Everything else →
    no edge.
    """
    from clio_agent.gact.artifacts.minting import (  # noqa: PLC0415
        _contained,
        _workspace_root,
        compute_identity,
    )
    from clio_agent.gact.artifacts.registry import get_registry  # noqa: PLC0415

    root = _workspace_root(app, workspace_id)
    if root is None:
        return []
    registry = get_registry(app)
    edges: list[ProvEdge] = []
    seen: set[str] = set()
    for arg_name, raw in _candidate_arg_strings(args):
        try:
            path = Path(raw)
        except (TypeError, ValueError):
            continue
        if not _contained(path, root):
            continue
        try:
            if not path.is_file():
                continue
        except OSError:
            continue
        resolved = str(path.expanduser().resolve(strict=False))
        if resolved in seen:
            continue
        seen.add(resolved)
        try:
            evidence = compute_identity(resolved)
        except OSError:
            continue
        match = registry.find_version_by_path(workspace_id, resolved)
        if match is None:
            # Existing contained file, not registered → an external:path edge.
            edges.append(
                ProvEdge(
                    role=EdgeRole.USED,
                    evidence=EdgeEvidence.SCHEMA_ARG,
                    external_ref=f"external:{resolved}",
                    sha256=evidence.sha256,
                    path=resolved,
                    arg=arg_name,
                    note=("" if evidence.sha256 else "stat_pinned"),
                )
            )
            continue
        record, version = match
        edges.append(
            _matched_used_edge(
                app,
                sid,
                record_name=record.name,
                workspace_id=workspace_id,
                version=version,
                on_disk=evidence,
                path=resolved,
                arg=arg_name,
                turn_id=turn_id,
                trace_id=trace_id,
            )
        )
    return edges


def _matched_used_edge(
    app: "FastAPI",
    sid: str,
    *,
    record_name: str,
    workspace_id: str,
    version: ArtifactVersion,
    on_disk: Any,
    path: str,
    arg: str,
    turn_id: str,
    trace_id: str,
) -> ProvEdge:
    """Build the used edge for a registry-matched path (re-hash under threshold)."""
    disk_sha = on_disk.sha256
    if disk_sha is None:
        # Over the hash threshold → stat-pinned, labeled (never a silent hash-skip).
        return ProvEdge(
            role=EdgeRole.USED,
            evidence=EdgeEvidence.SCHEMA_ARG,
            artifact_id=version.artifact_id,
            name=record_name,
            version=version.version,
            path=path,
            arg=arg,
            note="over_threshold",
        )
    if disk_sha == version.sha256:
        # Content unchanged since registration → schema-arg + hash-pair.
        return ProvEdge(
            role=EdgeRole.USED,
            evidence=EdgeEvidence.HASH_PAIR,
            artifact_id=version.artifact_id,
            sha256=disk_sha,
            name=record_name,
            version=version.version,
            path=path,
            arg=arg,
        )
    # Content DIFFERS — never silently pin the stale registered version. Mint a GAP
    # version FIRST (S4 reconcile machinery) and point the edge at the gap.
    gap = _mint_gap_for_changed_input(
        app,
        sid,
        name=record_name,
        workspace_id=workspace_id,
        path=path,
        turn_id=turn_id,
        trace_id=trace_id,
    )
    target = gap if gap is not None else version
    return ProvEdge(
        role=EdgeRole.USED,
        evidence=EdgeEvidence.HASH_PAIR,
        artifact_id=target.artifact_id,
        sha256=target.sha256 or disk_sha,
        name=record_name,
        version=target.version,
        path=path,
        arg=arg,
        note="gap_first",
    )


def _mint_gap_for_changed_input(
    app: "FastAPI",
    sid: str,
    *,
    name: str,
    workspace_id: str,
    path: str,
    turn_id: str,
    trace_id: str,
) -> Optional[ArtifactVersion]:
    """Mint a GAP/new version for an input that changed since registration (S4).

    Routes through :func:`reconcile_designated_path` with ``producing=False``: the
    content is an undesignated overwrite observed at use, so it becomes a GAP
    version (dirty lease) / auto revision (clean lease) / re-link (known non-head)
    — never a false producing mint, and the old version is never mutated. Returns
    the operative version to point the used edge at, or ``None`` on a typed skip.
    """
    from clio_agent.gact.artifacts.versions import reconcile_designated_path  # noqa: PLC0415

    outcome = reconcile_designated_path(
        app,
        sid,
        name=name,
        workspace_id=workspace_id,
        path=path,
        mechanism=Mechanism.NONE,
        turn_id=turn_id,
        trace_id=trace_id,
        session_id=sid,
    )
    if outcome is None:
        logger.info(
            "used-edge gap mint skipped reason=reconcile_skipped session=%s name=%s", sid, name
        )
        return None
    return outcome.version


# --------------------------------------------------------------------------- #
# Authority-asserted identity (item 4) — NDP catalog inputs.
# --------------------------------------------------------------------------- #


def _structured_result(result: Any) -> Optional[dict[str, Any]]:
    """Extract the structured-content dict from a raw tool result, or ``None``."""
    if isinstance(result, dict):
        sc = result.get("structuredContent")
        if isinstance(sc, dict):
            return sc
        return result
    return None


def detect_authority_edges(
    app: "FastAPI",
    *,
    tool_name: str,
    result: Any,
    workspace_id: str,
) -> list[ProvEdge]:
    """Register NDP catalog inputs as ``authority-asserted`` used edges (item 4).

    NDP tool results carry NO checksum/ETag/DOI (verified against the real
    server); the catalog resource ``url`` / ``id`` IS the authority. A
    ``stage_resource`` result names a downloaded ``local_path`` (hashed when it
    lands in the workspace) plus its source ``url``; ``search_datasets`` /
    ``get_dataset_details`` name catalog resources by ``url`` + ``id``. Absent any
    recognized NDP shape → a typed skip (no edges).
    """
    structured = _structured_result(result)
    if structured is None:
        return []
    short = tool_name.rsplit(".", 1)[-1] if "." in tool_name else tool_name
    if short == _NDP_STAGE_TOOL:
        return _stage_resource_edges(app, structured, workspace_id)
    if short in _NDP_CATALOG_TOOLS:
        return _catalog_resource_edges(structured)
    return []


def _stage_resource_edges(
    app: "FastAPI", structured: dict[str, Any], workspace_id: str
) -> list[ProvEdge]:
    """Authority edge for a ``stage_resource`` download (url is the authority)."""
    url = str(structured.get("url") or "").strip()
    if not url:
        return []
    local_path = str(structured.get("local_path") or "").strip()
    sha: Optional[str] = None
    if local_path:
        sha = _hash_if_contained(app, local_path, workspace_id)
    return [
        ProvEdge(
            role=EdgeRole.USED,
            evidence=EdgeEvidence.AUTHORITY,
            authority=url,
            external_ref=f"external:{url}",
            sha256=sha,
            path=local_path,
            note="ndp_stage_resource",
        )
    ]


def _catalog_resource_edges(structured: dict[str, Any]) -> list[ProvEdge]:
    """Authority edges for catalog resources named in a search/details result."""
    datasets: list[Any] = []
    if isinstance(structured.get("datasets"), list):
        datasets = list(structured["datasets"])
    elif isinstance(structured.get("dataset"), dict):
        datasets = [structured["dataset"]]
    edges: list[ProvEdge] = []
    seen: set[str] = set()
    for dataset in datasets:
        if not isinstance(dataset, dict):
            continue
        for resource in dataset.get("resources") or ():
            if not isinstance(resource, dict) or len(edges) >= _MAX_AUTHORITY_EDGES:
                continue
            authority = str(resource.get("url") or resource.get("id") or "").strip()
            if not authority or authority in seen:
                continue
            seen.add(authority)
            edges.append(
                ProvEdge(
                    role=EdgeRole.USED,
                    evidence=EdgeEvidence.AUTHORITY,
                    authority=authority,
                    external_ref=f"external:{authority}",
                    name=str(resource.get("name") or ""),
                    note="ndp_catalog_resource",
                )
            )
    return edges


def _hash_if_contained(app: "FastAPI", raw_path: str, workspace_id: str) -> Optional[str]:
    """Hash a staged file when it exists inside the workspace root, else ``None``."""
    from clio_agent.gact.artifacts.minting import (  # noqa: PLC0415
        _contained,
        _workspace_root,
        compute_identity,
    )

    root = _workspace_root(app, workspace_id)
    if root is None:
        return None
    path = Path(raw_path)
    if not _contained(path, root):
        return None
    try:
        if not path.is_file():
            return None
        return compute_identity(path).sha256
    except OSError:
        return None


__all__ = ["detect_authority_edges", "detect_used_edges"]
