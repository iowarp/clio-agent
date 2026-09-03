"""Child-work attribution layered onto normalized execution provenance.

The semantic event log and ``AgentTask`` child sessions remain authoritative.
This module is deliberately a read-side projection: it adds stable session/task
ownership and typed graph edges without copying child transcripts or creating a
second activity store.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from clio_agent.gact.agent_tasks import (
    _DEFAULT_DESCENDANT_DEPTH,
    descendant_sessions,
    display_run_name,
)

if TYPE_CHECKING:
    from fastapi import FastAPI

#: A span whose ``session_id`` is in the lineage only via the session store.
UNOWNED_SESSION_ATTRIBUTION = "session_fork"
#: A span whose ``session_id`` is in NEITHER substrate. Reported, never
#: re-attributed: silently folding it into the root invented ownership and made
#: the root's task_id/task_path appear on work the root never did.
UNATTRIBUTED_SESSION = "unattributed_session"
#: A lineage row whose session the store no longer has (a deleted child, a torn
#: write between the two stores).
LINEAGE_SESSION_MISSING = "lineage_session_missing"

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


def child_session_lineage(
    app: "FastAPI", root_session_id: str, *, max_depth: int = _DEFAULT_DESCENDANT_DEPTH
) -> tuple[list[dict[str, Any]], bool]:
    """Return a bounded, breadth-first lineage plus whether the walk was cut short.

    "Bounded" used to be a docstring claim only: the walk had no depth cap at all,
    while ``descendant_sessions`` -- the substrate the SAME routes use to pick
    which sessions to read -- stopped at ``MAX_SPAWN_DEPTH``. The two therefore
    disagreed about what the tree is. Both now stop at the same constant, and a
    walk that hit the cap says so instead of serving a partial tree as complete.

    The root is included at depth zero; descendants carry their owning task and a
    complete task path so clients can render nested branches without timing
    heuristics. A descendant reached only through the session store (a user fork)
    carries no task identity -- it has none -- and is typed ``session_fork``.
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
            "attribution": "root",
        }
    ]
    task_by_id = _task_index(app)
    path_by_session: dict[str, list[str]] = {root_session_id: []}
    truncated = False
    for row in descendant_sessions(app, root_session_id, max_depth=max_depth):
        task = task_by_id.get(row.task_id) if row.task_id else None
        agent_ref = dict((task or {}).get("agent_ref") or {})
        agent_id = str(agent_ref.get("expert_id") or "") if task else ""
        task_path = [*path_by_session.get(row.parent_session_id, []), *([row.task_id] or [])]
        task_path = [step for step in task_path if step]
        path_by_session[row.session_id] = task_path
        entry: dict[str, Any] = {
            "session_id": row.session_id,
            "parent_session_id": row.parent_session_id,
            "task_id": row.task_id,
            "agent_id": agent_id,
            "label": (
                display_run_name(
                    agent_id or "agent",
                    int((task or {}).get("run_index") or 0),
                    str((task or {}).get("run_label") or ""),
                )
                if task
                else _session_label(app, row.session_id)
            ),
            "depth": row.depth,
            "task_path": task_path,
            "attribution": row.attribution,
        }
        if task is not None:
            entry.update(
                {
                    "status": str(task.get("status") or ""),
                    "created_at": str(task.get("created_at") or ""),
                    "updated_at": str(task.get("updated_at") or ""),
                }
            )
        rows.append(entry)
        if row.depth >= max_depth:
            # The walk stopped here by policy, not because the tree ended: a
            # child of this row would have existed at depth max_depth + 1.
            truncated = truncated or bool(_has_children(app, row.session_id))
    return rows, truncated


def _has_children(app: "FastAPI", session_id: str) -> bool:
    """Whether the bounded walk left descendants of ``session_id`` unvisited."""

    return bool(descendant_sessions(app, session_id, max_depth=1))


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

    A span from a session the lineage does not contain is REPORTED, never
    re-attributed: the old ``by_session.get(sid, by_session[root])`` fallback gave
    such a span the ROOT's ``task_id`` and ``task_path`` -- a fabricated ownership
    the client could not tell from a real one -- and then minted a ``contains``
    edge onto a ``session:<sid>`` node that was never in the graph. Every reason
    the projection is partial rides ``degradations``.
    """

    projected = dict(result)
    lineage, truncated = child_session_lineage(app, root_session_id)
    if not include_children:
        lineage, truncated = lineage[:1], False
    by_session = {str(row["session_id"]): row for row in lineage}
    task_by_id = _task_index(app)
    degradations: list[dict[str, str]] = [
        row for row in (result.get("degradations") or []) if isinstance(row, dict)
    ]
    unattributed: dict[str, str] = {}

    def _owner(session_id: str) -> dict[str, Any]:
        """The lineage row for ``session_id``, or a typed unowned stand-in."""

        row = by_session.get(session_id)
        if row is not None:
            return row
        attribution = unattributed.setdefault(session_id, UNATTRIBUTED_SESSION)
        return {
            "session_id": session_id,
            "parent_session_id": "",
            "task_id": "",
            "agent_id": "",
            "label": session_id,
            "depth": 0,
            "task_path": [],
            "attribution": attribution,
        }

    spans: list[dict[str, Any]] = []
    for raw_span in result.get("spans") or []:
        span = dict(raw_span)
        session_id = str(span.get("session_id") or root_session_id)
        owner = _owner(session_id)
        attributes = dict(span.get("attributes") or {})
        task_id = str(
            span.get("task_id") or attributes.get("task_id") or owner.get("task_id") or ""
        )
        attribution = {
            "root_session_id": root_session_id,
            "owner_session_id": session_id,
            "task_id": task_id,
            "task_path": list(owner.get("task_path") or []),
            "attribution": str(owner.get("attribution") or ""),
        }
        span.update(attribution)
        attributes.update(attribution)
        span["attributes"] = attributes
        spans.append(span)

    nodes = [dict(node) for node in result.get("nodes") or []]
    for node in nodes:
        session_id = str(node.get("session_id") or root_session_id)
        owner = _owner(session_id)
        attributes = dict(node.get("attributes") or {})
        attributes.update(
            {
                "root_session_id": root_session_id,
                "owner_session_id": session_id,
                "task_id": str(attributes.get("task_id") or owner.get("task_id") or ""),
                "task_path": list(owner.get("task_path") or []),
                "attribution": str(owner.get("attribution") or ""),
            }
        )
        node["attributes"] = attributes

    entity_nodes, causal_edges = _causal_entities_and_edges(
        root_session_id=root_session_id,
        # Sessions a span named but the lineage does not own still get a node, so
        # the ``contains`` edge minted for that span lands somewhere.
        lineage=[*lineage, *(_owner(sid) for sid in sorted(unattributed))],
        spans=spans,
        task_by_id=task_by_id,
    )
    existing_node_ids = {str(node.get("id") or "") for node in nodes}
    nodes.extend(node for node in entity_nodes if node["id"] not in existing_node_ids)

    edges = [dict(edge) for edge in result.get("edges") or []]
    existing_edge_ids = {str(edge.get("id") or "") for edge in edges}
    edges.extend(edge for edge in causal_edges if edge["id"] not in existing_edge_ids)

    degradations.extend(
        {
            "reason": UNATTRIBUTED_SESSION,
            "session_id": session_id,
            "detail": "a span named a session that is in neither the task registry nor the "
            "session store's lineage; it is reported unowned rather than folded into the root",
        }
        for session_id in sorted(unattributed)
    )
    degradations.extend(
        {
            "reason": LINEAGE_SESSION_MISSING,
            "session_id": str(row["session_id"]),
            "detail": "the task registry names this child but the session store no longer has it",
        }
        for row in lineage
        if row["depth"] and app.state.sessions.get(str(row["session_id"])) is None
    )

    projected.update(
        {
            "root_session_id": root_session_id,
            "session_lineage": lineage,
            "lineage_depth": max((int(row.get("depth") or 0) for row in lineage), default=0),
            "lineage_truncated": truncated,
            "degradations": degradations,
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
                # HOW this session entered the graph: delegated by a task, reached
                # only through the session store (a fork), or named by a span and
                # owned by neither. A client rendering the tree needs the
                # difference; the old graph stated only the first.
                "attribution": str(row.get("attribution") or ""),
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
