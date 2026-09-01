"""Read CMF lineage over the server's REST surface, with no local MLMD store.

The query half of deployment shape (a). Adopted from the spotter-ai pack's
read-only CMF provider (``spotter_ai/providers/cmf.py``), which was written
against the REST surface a *running* cmf-server exposes -- note that surface is
NOT the one in the cmflib 0.1.0 sdist (which has no ``/api/pipeline-stages``);
the deployed server is newer, and the live endpoints are the contract here.

What this maps, and how faithfully:

* **nodes** come from the row listings, which carry CLIO's own ``clio_*``
  custom properties, so an artifact/activity node is rebuilt from CLIO's
  vocabulary rather than reverse-engineered from CMF labels.
* **generated** edges come from each artifact's ``clio_producer_json.call_id``
  -- the transform that made it, which CLIO stamps at write time.
* **revision_of** edges come from ``clio_prior_version`` matched within
  ``(workspace_id, name)``, exactly as the local worker derives them.
* **used** edges come from CMF's ``artifact-lineage/tangled-tree``: a parent of
  an artifact is an input to the call that produced that artifact, so the
  parent is routed through the child's producing activity. Where a child has no
  producing activity the relation cannot be expressed in CLIO's node vocabulary
  and the graph is marked truncated rather than being quietly flattened.

The node/edge shapes are identical to the local worker's
(``cmf_worker.lineage``) so ``GET /v1/artifacts/{id}/lineage`` answers the same
graph whichever lane wrote the metadata.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

import httpx

# One-way dependency: CLIO may import the worker module (its top-level imports
# are stdlib only and cmflib is loaded lazily), but the worker must never import
# CLIO -- it runs inside the isolated CMF interpreter. Sharing the traversal
# keeps both lanes bounding the graph identically instead of drifting.
from clio_agent.gact.artifacts.provenance.cmf_encoding import decode_property_value
from clio_agent.gact.artifacts.provenance.cmf_worker import _bounded_component

#: Row cap per listing call, mirroring the pack's bound.
_ROW_LIMIT = 10_000


class CMFRestLineageReader:
    """Answer CLIO lineage queries from a cmf-server's REST read surface."""

    name = "cmf-rest"

    def __init__(
        self,
        server_url: str,
        pipeline_name: str,
        *,
        timeout_s: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._pipeline = pipeline_name
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=server_url.strip().rstrip("/"), timeout=timeout_s
        )

    def lineage(
        self,
        artifact_id: str,
        *,
        direction: str,
        depth: int,
        complete: bool = False,
    ) -> dict[str, Any] | None:
        """Return CLIO's provider-neutral graph rooted at ``artifact_id``.

        Args:
            artifact_id: The CLIO artifact id to root the graph at.
            direction: ``upstream``, ``downstream`` or ``both``.
            depth: Maximum artifact hops; ignored when ``complete``.
            complete: Return the whole reachable component.

        Returns:
            The bounded graph, or ``None`` when the artifact is unknown to the
            server (an honest 404 at the route).
        """
        direction = direction if direction in {"upstream", "downstream", "both"} else "both"
        artifact_rows = self._artifacts()
        nodes: dict[str, dict[str, Any]] = {}
        by_display: dict[str, str] = {}
        for row in artifact_rows:
            node = _artifact_node(row)
            nodes[str(node["id"])] = node
            name = str(row.get("name") or "")
            # The tangled-tree endpoint labels artifacts with a DERIVED display
            # id, not the stored name, so both spellings are registered.
            for key in (name, lineage_display_id(name, str(row.get("artifact_type") or ""))):
                if key:
                    by_display.setdefault(key, str(node["id"]))
        if artifact_id not in nodes:
            return None
        activities: dict[str, dict[str, Any]] = {}
        for row in self._executions():
            node = _execution_node(row)
            call_id = str(node["call_id"])
            if call_id:
                activities[call_id] = node
        edges: list[dict[str, Any]] = []
        truncated = self._link(nodes, activities, by_display, edges)
        nodes.update({str(node["id"]): node for node in activities.values()})
        selected_nodes, selected_edges, bound_truncation = _bounded_component(
            artifact_id,
            nodes,
            edges,
            direction=direction,
            depth=max(0, int(depth)),
            complete=complete,
        )
        return {
            "root": artifact_id,
            "direction": direction,
            "depth": max(0, int(depth)),
            "nodes": selected_nodes,
            "edges": selected_edges,
            "truncated": bound_truncation or truncated,
            "provider": "cmf",
        }

    def _link(
        self,
        nodes: dict[str, dict[str, Any]],
        activities: dict[str, dict[str, Any]],
        by_display: dict[str, str],
        edges: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Derive generated / revision_of / used edges. Returns any truncation."""
        versions: dict[tuple[str, str, int], str] = {}
        for node_id, node in nodes.items():
            versions[
                (
                    str(node.get("workspace_id") or ""),
                    str(node.get("name") or ""),
                    int(node.get("version") or 0),
                )
            ] = node_id
        for node_id, node in nodes.items():
            call_id = str(node.get("producer_call_id") or "")
            if call_id and call_id in activities:
                edges.append(
                    {
                        "from": f"activity:{call_id}",
                        "to": node_id,
                        "type": "generated",
                        "evidence": "cmf-producer",
                    }
                )
            prior_version = int(node.get("prior_version") or 0)
            if prior_version:
                prior = versions.get(
                    (
                        str(node.get("workspace_id") or ""),
                        str(node.get("name") or ""),
                        prior_version,
                    )
                )
                if prior:
                    edges.append(
                        {
                            "from": node_id,
                            "to": prior,
                            "type": "revision_of",
                            "evidence": "hash-pair",
                        }
                    )
        return self._used_edges(nodes, activities, by_display, edges)

    def _used_edges(
        self,
        nodes: dict[str, dict[str, Any]],
        activities: dict[str, dict[str, Any]],
        by_display: dict[str, str],
        edges: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Route CMF's artifact->artifact parents through the child's activity."""
        unresolved = 0
        unmapped = 0
        for layer in self._artifact_lineage_layers():
            for row in layer if isinstance(layer, list) else []:
                if not isinstance(row, dict):
                    continue
                child = by_display.get(str(row.get("id") or ""))
                if child is None:
                    unmapped += 1
                    continue
                call_id = str(nodes[child].get("producer_call_id") or "")
                for parent_display in row.get("parents") or []:
                    parent = by_display.get(str(parent_display))
                    if parent is None:
                        unmapped += 1
                        continue
                    if not call_id or call_id not in activities:
                        unresolved += 1
                        continue
                    edges.append(
                        {
                            "from": parent,
                            "to": f"activity:{call_id}",
                            "type": "used",
                            "evidence": "cmf-lineage",
                        }
                    )
        if unresolved or unmapped:
            # No silent flattening: an input CLIO cannot attribute to a call is
            # reported, not turned into an artifact->artifact edge CLIO's node
            # vocabulary does not have; and a CMF lineage label that matches no
            # known artifact is reported rather than dropped.
            return {
                "reason": "cmf_lineage_edges_unmapped",
                "producer_unresolved": unresolved,
                "label_unmapped": unmapped,
            }
        return None

    def _get(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        response = self._client.get(path, params=params)
        response.raise_for_status()
        return response.json()

    def _stages(self) -> list[str]:
        raw = self._get(f"/api/pipeline-stages/{quote(self._pipeline, safe='')}")
        if isinstance(raw, dict):
            return [str(value) for value in raw.get("stages") or [] if value]
        return []

    def _artifact_types(self, stage: str) -> list[str]:
        raw = self._get(
            f"/api/artifact-types-by-stage/{quote(self._pipeline, safe='')}",
            params={"stage_name": stage},
        )
        return [str(value) for value in raw if value] if isinstance(raw, list) else []

    def _artifacts(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for stage in self._stages():
            for artifact_type in self._artifact_types(stage):
                raw = self._get(
                    f"/api/artifacts-by-stage/{quote(self._pipeline, safe='')}",
                    params={
                        "stage_name": stage,
                        "artifact_type": artifact_type,
                        "active_page": 1,
                        "record_per_page": _ROW_LIMIT,
                        "sort_field": "name",
                        "sort_order": "asc",
                        "filter_value": "",
                    },
                )
                rows.extend(_items(raw))
        return rows

    def _executions(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for stage in self._stages():
            raw = self._get(
                f"/api/executions-by-stage/{quote(self._pipeline, safe='')}",
                params={
                    "stage_name": stage,
                    "active_page": 1,
                    "record_per_page": _ROW_LIMIT,
                    "sort_order": "DESC",
                    "filter_value": "",
                },
            )
            rows.extend(_items(raw))
        return rows

    def _artifact_lineage_layers(self) -> list[Any]:
        raw = self._get(f"/api/artifact-lineage/tangled-tree/{quote(self._pipeline, safe='')}")
        return raw if isinstance(raw, list) else []

    def close(self) -> None:
        """Close the HTTP client this reader owns."""
        if self._owns_client:
            self._client.close()


def _items(raw: Any) -> list[dict[str, Any]]:
    rows = raw.get("items") if isinstance(raw, dict) else raw
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def cmf_property(row: dict[str, Any], name: str) -> Any:
    """Read one CMF row property, decoded, whatever container it arrived in.

    Values are stored percent-encoded so the cmf-server cannot discard the
    entity (see :mod:`cmf_encoding`); every read path decodes, so a caller sees
    the original bytes and never a ``%5C``.

    The dashboard API has shipped several row shapes (top-level keys, a nested
    ``*_properties`` dict, a list of ``{name, value}`` pairs, and prefixed flat
    keys). Reading all of them keeps the reader working across server versions
    instead of silently returning empty CLIO metadata.
    """
    direct = row.get(name)
    if direct is not None:
        return decode_property_value(direct)
    for container_name in ("artifact_properties", "execution_properties", "properties"):
        container = row.get(container_name)
        if isinstance(container, dict) and name in container:
            return decode_property_value(container[name])
        if isinstance(container, list):
            for item in container:
                if isinstance(item, dict) and (item.get("name") or item.get("key")) == name:
                    return decode_property_value(item.get("value"))
    for prefix in ("custom_properties_", "properties_"):
        if f"{prefix}{name}" in row:
            return decode_property_value(row[f"{prefix}{name}"])
    return None


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _artifact_node(row: dict[str, Any]) -> dict[str, Any]:
    """Project one CMF artifact row onto CLIO's artifact/gap node shape."""
    artifact_id = str(cmf_property(row, "clio_artifact_id") or row.get("artifact_id") or "")
    mechanism = str(cmf_property(row, "clio_mechanism") or "")
    return {
        "id": artifact_id,
        "type": "gap" if mechanism == "none" else "artifact",
        "workspace_id": str(cmf_property(row, "clio_workspace_id") or ""),
        "name": str(cmf_property(row, "clio_name") or row.get("name") or ""),
        "version": _int(cmf_property(row, "clio_version")),
        "kind": str(cmf_property(row, "clio_kind") or "other"),
        "sha256": str(cmf_property(row, "clio_sha256") or "") or None,
        "mechanism": mechanism,
        "custody_gap": None,
        "producer_call_id": _producer_call_id(str(cmf_property(row, "clio_producer_json") or "")),
        "prior_version": _int(cmf_property(row, "clio_prior_version")) or None,
        "cmf_artifact_id": _int(row.get("artifact_id") or row.get("id")),
    }


def lineage_display_id(name: str, artifact_type: str) -> str:
    """Reproduce the artifact label CMF's tangled-tree endpoint emits.

    The lineage endpoint identifies artifacts by a label derived from the stored
    name and type, not by the name itself, so a reader has to derive the same
    label to join the two listings. Adopted verbatim in behaviour from the
    spotter-ai pack's reader, which was calibrated against the deployed server.

    Args:
        name: The artifact's stored CMF name.
        artifact_type: Its CMF artifact type.

    Returns:
        The display label, or ``name`` when it does not fit the type's pattern.
    """
    try:
        segments = name.split(":")
        if artifact_type == "Metrics":
            return f"{segments[0]}:{segments[1][:4]}:{segments[2]}"
        if artifact_type == "Model":
            return f"{segments[-3].split('/')[-1]}:{segments[-2][:4]}"
        if artifact_type == "Dataset":
            parts = name.rsplit(":")[0].split("/")
            return f"{parts[-1] or parts[-2]}:{segments[-1][:4]}"
        if artifact_type == "Dataslice":
            path, lineage_id = name.split("/", 1)[1].rsplit(":", 1)
            return f"{path}:{lineage_id[:4]}"
        if artifact_type == "Step_Metrics":
            relative = name.split("/", 1)[1]
            values = name.rsplit(":")
            return f"{relative.rsplit(':', 3)[0]}:{values[-3][:4]}:{values[-2]}:{values[-1][:4]}"
    except (IndexError, ValueError):
        return name
    return name


def _producer_call_id(raw: str) -> str:
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return ""
    return str(value.get("call_id") or "") if isinstance(value, dict) else ""


def _execution_node(row: dict[str, Any]) -> dict[str, Any]:
    """Project one CMF execution row onto CLIO's activity node shape."""
    call_id = str(cmf_property(row, "clio_call_id") or "")
    environment: dict[str, Any] = {}
    try:
        parsed = json.loads(str(cmf_property(row, "clio_environment_json") or "{}"))
        environment = parsed if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, TypeError):
        environment = {}
    return {
        "id": f"activity:{call_id}",
        "type": "activity",
        "call_id": call_id,
        "tool": str(cmf_property(row, "clio_tool") or ""),
        "status": str(cmf_property(row, "clio_status") or ""),
        "kind": str(cmf_property(row, "clio_kind") or "ordinary"),
        "replay": str(cmf_property(row, "clio_replay") or ""),
        "environment_tier": str(environment.get("tier") or ""),
        "session_id": str(cmf_property(row, "clio_session_id") or ""),
        "turn_id": str(cmf_property(row, "clio_turn_id") or ""),
        "cmf_execution_id": _int(row.get("execution_id") or row.get("id")),
    }


__all__ = ["CMFRestLineageReader", "cmf_property", "lineage_display_id"]
