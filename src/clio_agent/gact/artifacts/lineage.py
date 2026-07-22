"""Lineage traversal over the artifact registry + TransformRecords (S5 #971).

Pure graph builder: given a starting version's relay ``artifact_id`` it walks the
provenance graph the :class:`~clio_agent.gact.artifacts.registry.ArtifactRegistry`
holds — version chains (``revision_of`` edges from ``prior_version``) and
TransformRecords (``used`` / ``generated`` edges) — in either direction.

Nodes are ``artifact | activity | gap`` (a ``gap`` is a version minted with
mechanism ``none`` — an undesignated overwrite whose actor is unknown). Edges are
``used | generated | revision_of``, each carrying its evidence. The traversal is
BFS bounded by a caller ``depth`` (in activity hops) and a hard node cap so a
pathological graph can never produce an unbounded response.

Truncation is TYPED and the graph stays well-formed (findings [9]/[10]): when the
node cap or the caller's depth horizon clips the walk, ``truncated`` carries
``{reason, nodes|at_depth}`` (``None`` when the lineage is complete), and NO edge
is ever emitted that references a node absent from ``nodes`` — a boundary edge is
dropped with the node it could not add, so a strict graph consumer never dangles.

W&B's four verbs / Pachyderm subvenance: ``upstream`` answers "what produced
this" (its activity + inputs, recursively); ``downstream`` answers "what did this
produce" (activities that used it + their outputs); ``both`` is the union.
"""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING, Any, Optional

from clio_agent.gact.artifacts.records import ArtifactRecord, ArtifactVersion, Mechanism

if TYPE_CHECKING:
    from clio_agent.gact.artifacts.registry import ArtifactRegistry

#: Hard cap on nodes in one lineage response (truncation, never unbounded).
_MAX_NODES = 500


def _node_type(version: ArtifactVersion) -> str:
    """``gap`` for an unattributed (mechanism ``none``) version, else ``artifact``."""
    return "gap" if version.mechanism is Mechanism.NONE else "artifact"


def _artifact_node(record: ArtifactRecord, version: ArtifactVersion) -> dict[str, Any]:
    """Wire dict for an artifact / gap node."""
    return {
        "id": version.artifact_id,
        "type": _node_type(version),
        "workspace_id": record.workspace_id,
        "name": record.name,
        "version": version.version,
        "kind": version.kind.value,
        "sha256": version.sha256,
        "mechanism": version.mechanism.value,
        "custody_gap": version.custody_gap,
        "producer_call_id": str(version.producer.get("call_id") or ""),
    }


def _activity_node(transform: Any) -> dict[str, Any]:
    """Wire dict for an activity node (a TransformRecord)."""
    return {
        "id": f"activity:{transform.call_id}",
        "type": "activity",
        "call_id": transform.call_id,
        "tool": transform.instrument.tool,
        "status": transform.status.value,
        "kind": transform.kind.value,
        "replay": transform.replay.value,
        "environment_tier": transform.environment.tier.value,
        "session_id": transform.session_id,
        "turn_id": transform.turn_id,
    }


class _LineageIndex:
    """Reverse indexes over TransformRecords for O(1) neighbour lookup."""

    def __init__(self, registry: "ArtifactRegistry") -> None:
        self._registry = registry
        self._produced_by: dict[str, Any] = {}
        self._used_by: dict[str, list[Any]] = {}
        for transform in registry.all_transforms():
            for edge in transform.generated:
                if edge.artifact_id:
                    self._produced_by[edge.artifact_id] = transform
            for edge in transform.used:
                if edge.artifact_id:
                    self._used_by.setdefault(edge.artifact_id, []).append(transform)

    def produced_by(self, artifact_id: str) -> Optional[Any]:
        """The activity that generated ``artifact_id`` (one producer per version)."""
        return self._produced_by.get(artifact_id)

    def used_by(self, artifact_id: str) -> list[Any]:
        """Activities that used ``artifact_id`` as an input."""
        return self._used_by.get(artifact_id, [])

    def version(self, artifact_id: str) -> Optional[tuple[ArtifactRecord, ArtifactVersion]]:
        """Resolve a version by relay ``artifact_id``."""
        return self._registry.get_by_artifact_id(artifact_id)


def _revision_neighbours(
    index: _LineageIndex,
    record: ArtifactRecord,
    version: ArtifactVersion,
    *,
    direction: str,
) -> list[ArtifactVersion]:
    """Version-chain ``revision_of`` neighbours in the requested direction.

    Upstream: the version this one revises (``prior_version``). Downstream: the
    versions that revise this one (their ``prior_version`` == this version).
    """
    out: list[ArtifactVersion] = []
    if direction in ("upstream", "both") and version.prior_version is not None:
        prior = next((v for v in record.versions if v.version == version.prior_version), None)
        if prior is not None:
            out.append(prior)
    if direction in ("downstream", "both"):
        for candidate in record.versions:
            if candidate.prior_version == version.version:
                out.append(candidate)
    return out


def build_lineage(
    registry: "ArtifactRegistry",
    artifact_id: str,
    *,
    direction: str = "both",
    depth: int = 3,
) -> Optional[dict[str, Any]]:
    """Build the lineage graph rooted at ``artifact_id`` (S5 #971).

    ``direction`` ∈ ``{upstream, downstream, both}``; ``depth`` bounds the activity
    hops. Returns ``{root, direction, depth, nodes, edges, truncated}`` or ``None``
    when the root artifact id is unknown. ``truncated`` is ``None`` for a complete
    graph, else ``{reason: "node_cap", nodes}`` or ``{reason: "depth_horizon",
    at_depth}`` (findings [9]/[10]). Deterministic + idempotent: nodes and edges are
    de-duplicated by id, the BFS visits each artifact once at its shallowest depth,
    and no edge is emitted whose endpoint node was clipped by the cap (no dangling).
    """
    if direction not in ("upstream", "downstream", "both"):
        direction = "both"
    depth = max(0, int(depth))
    index = _LineageIndex(registry)
    root = index.version(artifact_id)
    if root is None:
        return None

    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []
    edge_keys: set[tuple[str, str, str]] = set()
    truncated: Optional[dict[str, Any]] = None

    def add_node(node: dict[str, Any]) -> bool:
        nonlocal truncated
        node_id = node["id"]
        if node_id in nodes:
            return True
        if len(nodes) >= _MAX_NODES:
            # Node cap wins over a depth horizon (the harder bound). Only a node
            # that was actually ADDED can be an edge endpoint (finding [9]).
            truncated = {"reason": "node_cap", "nodes": _MAX_NODES}
            return False
        nodes[node_id] = node
        return True

    def add_edge(src: str, dst: str, etype: str, evidence: str) -> None:
        key = (src, dst, etype)
        if key in edge_keys:
            return
        edge_keys.add(key)
        edges.append({"from": src, "to": dst, "type": etype, "evidence": evidence})

    def note_depth_horizon() -> None:
        nonlocal truncated
        if truncated is None:
            truncated = {"reason": "depth_horizon", "at_depth": depth}

    rec0, ver0 = root
    add_node(_artifact_node(rec0, ver0))
    frontier: deque[tuple[str, int]] = deque([(artifact_id, 0)])
    visited: set[str] = {artifact_id}

    while frontier:
        current_id, current_depth = frontier.popleft()
        resolved = index.version(current_id)
        if resolved is None:
            continue
        if current_depth >= depth:
            # At the caller's depth horizon: if this node still has neighbours in the
            # requested direction, the graph CONTINUES past the bound (finding [10]).
            if _has_frontier_beyond(index, resolved, direction=direction):
                note_depth_horizon()
            continue
        record, version = resolved

        # --- revision_of (version chain) ---
        for neighbour in _revision_neighbours(index, record, version, direction=direction):
            if not add_node(_artifact_node(record, neighbour)):
                break  # node cap — drop the edge with the node it references
            if neighbour.version < version.version:
                add_edge(current_id, neighbour.artifact_id, "revision_of", "hash-pair")
            else:
                add_edge(neighbour.artifact_id, current_id, "revision_of", "hash-pair")
            if neighbour.artifact_id not in visited:
                visited.add(neighbour.artifact_id)
                frontier.append((neighbour.artifact_id, current_depth + 1))

        # --- upstream: the producing activity + its used inputs ---
        if direction in ("upstream", "both"):
            producer = index.produced_by(current_id)
            # Only emit the activity's edge + expand its inputs when its node was
            # actually added (finding [9] — never an edge to an absent activity).
            if producer is not None and add_node(_activity_node(producer)):
                gen_ev = _edge_evidence(producer.generated, current_id)
                add_edge(f"activity:{producer.call_id}", current_id, "generated", gen_ev)
                _expand_activity_inputs(
                    index, producer, current_depth, frontier, visited, add_node, add_edge
                )

        # --- downstream: activities that consumed this + their outputs ---
        if direction in ("downstream", "both"):
            for consumer in index.used_by(current_id):
                if not add_node(_activity_node(consumer)):
                    break  # node cap — drop this consumer's edge + expansion
                used_ev = _edge_evidence(consumer.used, current_id)
                add_edge(current_id, f"activity:{consumer.call_id}", "used", used_ev)
                _expand_activity_outputs(
                    index, consumer, current_depth, frontier, visited, add_node, add_edge
                )

    return {
        "root": artifact_id,
        "direction": direction,
        "depth": depth,
        "nodes": list(nodes.values()),
        "edges": edges,
        "truncated": truncated,
    }


def _has_frontier_beyond(
    index: _LineageIndex,
    resolved: tuple[ArtifactRecord, ArtifactVersion],
    *,
    direction: str,
) -> bool:
    """Whether a node at the depth horizon still has neighbours (graph continues)."""
    record, version = resolved
    if direction in ("upstream", "both"):
        if index.produced_by(version.artifact_id) is not None:
            return True
        if version.prior_version is not None:
            return True
    if direction in ("downstream", "both"):
        if index.used_by(version.artifact_id):
            return True
        if any(cand.prior_version == version.version for cand in record.versions):
            return True
    return False


def _edge_evidence(edges: list[Any], artifact_id: str) -> str:
    """The evidence label of the edge for ``artifact_id`` (``""`` when absent)."""
    for edge in edges:
        if edge.artifact_id == artifact_id:
            return edge.evidence.value
    return ""


def _expand_activity_inputs(
    index: _LineageIndex,
    activity: Any,
    depth: int,
    frontier: deque,
    visited: set[str],
    add_node: Any,
    add_edge: Any,
) -> None:
    """Add an activity's used-input artifacts as upstream neighbours."""
    for edge in activity.used:
        if not edge.artifact_id:
            # External / authority-only input — surface it as a leaf node, no recursion.
            # Emit the edge ONLY when the leaf node was actually added (finding [9]).
            if _add_external_node(edge, add_node) and edge.external_ref:
                add_edge(
                    edge.external_ref, f"activity:{activity.call_id}", "used", edge.evidence.value
                )
            continue
        resolved = index.version(edge.artifact_id)
        if resolved is None:
            continue
        rec, ver = resolved
        if not add_node(_artifact_node(rec, ver)):
            return
        add_edge(edge.artifact_id, f"activity:{activity.call_id}", "used", edge.evidence.value)
        if edge.artifact_id not in visited:
            visited.add(edge.artifact_id)
            frontier.append((edge.artifact_id, depth + 1))


def _expand_activity_outputs(
    index: _LineageIndex,
    activity: Any,
    depth: int,
    frontier: deque,
    visited: set[str],
    add_node: Any,
    add_edge: Any,
) -> None:
    """Add an activity's generated-output artifacts as downstream neighbours."""
    for edge in activity.generated:
        if not edge.artifact_id:
            continue
        resolved = index.version(edge.artifact_id)
        if resolved is None:
            continue
        rec, ver = resolved
        if not add_node(_artifact_node(rec, ver)):
            return
        add_edge(f"activity:{activity.call_id}", edge.artifact_id, "generated", edge.evidence.value)
        if edge.artifact_id not in visited:
            visited.add(edge.artifact_id)
            frontier.append((edge.artifact_id, depth + 1))


def _add_external_node(edge: Any, add_node: Any) -> bool:
    """Add a leaf node for an external / authority-asserted input (no recursion).

    Returns whether the node is present (added or already there) so the caller only
    emits the edge when its endpoint exists (finding [9] — never a dangling edge).
    """
    ref = edge.external_ref or edge.authority
    if not ref:
        return False
    return bool(
        add_node(
            {
                "id": ref,
                "type": "artifact",
                "external": True,
                "authority": edge.authority,
                "sha256": edge.sha256,
                "evidence": edge.evidence.value,
                "name": edge.name,
            }
        )
    )


__all__ = ["build_lineage"]
