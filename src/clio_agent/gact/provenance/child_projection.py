"""Child-work attribution layered onto normalized execution provenance.

The semantic event log and ``AgentTask`` child sessions remain authoritative.
This module is deliberately a read-side projection: it adds stable session/task
ownership and typed graph edges without copying child transcripts or creating a
second activity store.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from clio_agent.gact.agent_tasks import display_run_name

if TYPE_CHECKING:
    from fastapi import FastAPI

CHILD_ACTIVITY_PROJECTION_CAPABILITY: dict[str, Any] = {
    "include_children": True,
    "session_lineage": True,
    "typed_edges": [
        "delegated",
        "executes_in",
        "contains",
        "used",
        "generated",
        "responded_to",
    ],
}


def child_session_lineage(app: "FastAPI", root_session_id: str) -> list[dict[str, Any]]:
    """Return a bounded, breadth-first lineage for ``root_session_id``.

    Every row is derived from the durable ``AgentTask`` projection.  The root is
    included at depth zero; descendants carry their owning task and a complete
    task path so clients can render nested branches without timing heuristics.
    """

    rows: list[dict[str, Any]] = [
        {
            "session_id": root_session_id,
            "parent_session_id": "",
            "task_id": "",
            "agent_id": "",
            "label": _session_label(app, root_session_id),
            "depth": 0,
            "task_path": [],
        }
    ]
    registry = getattr(app.state, "agent_task_registry", None)
    if registry is None:
        return rows

    seen = {root_session_id}
    frontier: list[tuple[str, int, list[str]]] = [(root_session_id, 0, [])]
    while frontier:
        parent_session_id, parent_depth, parent_path = frontier.pop(0)
        for task in registry.for_parent(parent_session_id):
            child_session_id = str(task.child_session_id or "")
            if not child_session_id or child_session_id in seen:
                continue
            seen.add(child_session_id)
            agent_id = str(task.agent_ref.get("expert_id") or "agent")
            task_path = [*parent_path, task.task_id]
            rows.append(
                {
                    "session_id": child_session_id,
                    "parent_session_id": parent_session_id,
                    "task_id": task.task_id,
                    "agent_id": agent_id,
                    "label": display_run_name(agent_id, task.run_index, task.run_label),
                    "depth": parent_depth + 1,
                    "task_path": task_path,
                    "status": task.status,
                    "created_at": task.created_at,
                    "updated_at": task.updated_at,
                }
            )
            frontier.append((child_session_id, parent_depth + 1, task_path))
    return rows


def project_child_execution(
    app: "FastAPI",
    root_session_id: str,
    result: dict[str, Any],
    *,
    include_children: bool = True,
) -> dict[str, Any]:
    """Add child ownership and causal entity edges to a normalized result.

    Provider-native spans are retained verbatim apart from additive attribution
    fields.  Graph entities are derived from those spans and the authoritative
    ``AgentTask`` lineage.  Hidden reasoning and child message content are never
    read by this projection.
    """

    projected = dict(result)
    lineage = child_session_lineage(app, root_session_id)
    if not include_children:
        lineage = lineage[:1]
    by_session = {str(row["session_id"]): row for row in lineage}
    task_by_id = _task_index(app)

    spans: list[dict[str, Any]] = []
    for raw_span in result.get("spans") or []:
        span = dict(raw_span)
        session_id = str(span.get("session_id") or root_session_id)
        owner = by_session.get(session_id, by_session[root_session_id])
        attributes = dict(span.get("attributes") or {})
        task_id = str(
            span.get("task_id") or attributes.get("task_id") or owner.get("task_id") or ""
        )
        span.update(
            {
                "root_session_id": root_session_id,
                "owner_session_id": session_id,
                "task_id": task_id,
                "task_path": list(owner.get("task_path") or []),
            }
        )
        attributes.update(
            {
                "root_session_id": root_session_id,
                "owner_session_id": session_id,
                "task_id": task_id,
                "task_path": list(owner.get("task_path") or []),
            }
        )
        span["attributes"] = attributes
        spans.append(span)

    nodes = [dict(node) for node in result.get("nodes") or []]
    for node in nodes:
        session_id = str(node.get("session_id") or root_session_id)
        owner = by_session.get(session_id, by_session[root_session_id])
        attributes = dict(node.get("attributes") or {})
        attributes.update(
            {
                "root_session_id": root_session_id,
                "owner_session_id": session_id,
                "task_id": str(attributes.get("task_id") or owner.get("task_id") or ""),
                "task_path": list(owner.get("task_path") or []),
            }
        )
        node["attributes"] = attributes

    entity_nodes, causal_edges = _causal_entities_and_edges(
        root_session_id=root_session_id,
        lineage=lineage,
        spans=spans,
        task_by_id=task_by_id,
    )
    existing_node_ids = {str(node.get("id") or "") for node in nodes}
    nodes.extend(node for node in entity_nodes if node["id"] not in existing_node_ids)

    edges = [dict(edge) for edge in result.get("edges") or []]
    existing_edge_ids = {str(edge.get("id") or "") for edge in edges}
    edges.extend(edge for edge in causal_edges if edge["id"] not in existing_edge_ids)

    projected.update(
        {
            "root_session_id": root_session_id,
            "session_lineage": lineage,
            "spans": spans,
            "nodes": nodes,
            "edges": edges,
        }
    )
    return projected


def _session_label(app: "FastAPI", session_id: str) -> str:
    session = app.state.sessions.get(session_id)
    return str(getattr(session, "title", "") or session_id)


def _task_index(app: "FastAPI") -> dict[str, dict[str, Any]]:
    registry = getattr(app.state, "agent_task_registry", None)
    if registry is None:
        return {}
    return {task.task_id: asdict(task) for task in registry.snapshot()}


def _entity_node(
    node_id: str,
    *,
    kind: str,
    label: str,
    status: str,
    session_id: str,
    agent_id: str = "",
    attributes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": node_id,
        "kind": kind,
        "label": label,
        "status": status,
        "session_id": session_id,
        "agent_id": agent_id,
        "start_time": None,
        "end_time": None,
        "attributes": attributes or {},
    }


def _edge(source: str, target: str, kind: str, **attributes: Any) -> dict[str, Any]:
    return {
        "id": f"{kind}:{source}->{target}",
        "source": source,
        "target": target,
        "kind": kind,
        **attributes,
    }


def _causal_entities_and_edges(
    *,
    root_session_id: str,
    lineage: list[dict[str, Any]],
    spans: list[dict[str, Any]],
    task_by_id: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    nodes: dict[str, dict[str, Any]] = {}
    edges: dict[str, dict[str, Any]] = {}

    for row in lineage:
        session_id = str(row["session_id"])
        session_node = f"session:{session_id}"
        nodes[session_node] = _entity_node(
            session_node,
            kind="session",
            label=str(row.get("label") or session_id),
            status=str(row.get("status") or "active"),
            session_id=session_id,
            agent_id=str(row.get("agent_id") or ""),
            attributes={
                "root_session_id": root_session_id,
                "parent_session_id": str(row.get("parent_session_id") or ""),
                "task_id": str(row.get("task_id") or ""),
                "task_path": list(row.get("task_path") or []),
                "depth": int(row.get("depth") or 0),
            },
        )
        task_id = str(row.get("task_id") or "")
        parent_session_id = str(row.get("parent_session_id") or "")
        if task_id and parent_session_id:
            task = task_by_id.get(task_id, {})
            task_node = f"task:{task_id}"
            nodes[task_node] = _entity_node(
                task_node,
                kind="task",
                label=str(row.get("label") or task_id),
                status=str(task.get("status") or row.get("status") or "unknown"),
                session_id=session_id,
                agent_id=str(row.get("agent_id") or ""),
                attributes={"task_id": task_id, "task_path": list(row.get("task_path") or [])},
            )
            delegated = _edge(f"session:{parent_session_id}", task_node, "delegated")
            executes = _edge(task_node, session_node, "executes_in")
            edges[delegated["id"]] = delegated
            edges[executes["id"]] = executes

    for span in spans:
        span_id = str(span.get("id") or "")
        session_id = str(span.get("owner_session_id") or span.get("session_id") or root_session_id)
        owner_node = (
            f"task:{span.get('task_id')}" if span.get("task_id") else f"session:{session_id}"
        )
        contains = _edge(owner_node, span_id, "contains")
        edges[contains["id"]] = contains

        event_type = str(span.get("event_type") or "")
        relation = "used" if event_type.endswith(".used") else "generated"
        if event_type.startswith("artifact."):
            for artifact in span.get("artifact_refs") or []:
                artifact_id = str(artifact.get("artifact_id") or "")
                if not artifact_id:
                    continue
                artifact_node = f"artifact:{artifact_id}"
                nodes[artifact_node] = _entity_node(
                    artifact_node,
                    kind="artifact",
                    label=artifact_id,
                    status="available",
                    session_id=session_id,
                    attributes={
                        "artifact_id": artifact_id,
                        "sha256": str(artifact.get("sha256") or ""),
                    },
                )
                causal = _edge(owner_node, artifact_node, relation, event_id=span_id)
                edges[causal["id"]] = causal

        attributes = span.get("attributes") or {}
        interaction_id = str(
            attributes.get("interaction_id")
            or attributes.get("question_id")
            or attributes.get("permission_id")
            or ""
        )
        if interaction_id:
            interaction_node = f"interaction:{interaction_id}"
            nodes[interaction_node] = _entity_node(
                interaction_node,
                kind="interaction",
                label=str(attributes.get("interaction_title") or interaction_id),
                status=str(span.get("status") or "unknown"),
                session_id=session_id,
                attributes={"interaction_id": interaction_id},
            )
            # ``.resolved`` is the only settle suffix any emitted semantic event
            # actually carries; ``.answered`` / ``.responded`` matched nothing.
            if event_type.endswith(".resolved"):
                response_edge = _edge(
                    owner_node, interaction_node, "responded_to", event_id=span_id
                )
                edges[response_edge["id"]] = response_edge

    return list(nodes.values()), list(edges.values())
