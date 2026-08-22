"""Isolated worker that writes and queries CMF's MLMD representation.

This file is executed directly by :mod:`cmf`; it intentionally imports no CLIO
modules so the isolated CMF environment does not need to install clio-agent.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import sys
import traceback
from collections import deque
from pathlib import Path
from typing import Any, Optional


def _imports() -> dict[str, Any]:
    from cmflib.metadata_helper import (
        associate_child_to_parent_context,
        create_artifact_with_type,
        create_new_execution_in_existing_run_context,
        get_or_create_parent_context,
        get_or_create_run_context,
        value_to_mlmd_value,
    )
    from cmflib.store.sqllite_store import SqlliteStore
    from ml_metadata.proto import metadata_store_pb2 as mlpb

    return {
        "associate": associate_child_to_parent_context,
        "create_artifact": create_artifact_with_type,
        "create_execution": create_new_execution_in_existing_run_context,
        "parent": get_or_create_parent_context,
        "run": get_or_create_run_context,
        "value": value_to_mlmd_value,
        "sqlite": SqlliteStore,
        "mlpb": mlpb,
    }


class CMFEventStore:
    """Small explicit-event mapper over CMF's own SQLite/MLMD primitives."""

    def __init__(self, metadata_path: Path, artifact_root: Path, pipeline_name: str) -> None:
        api = _imports()
        self._api = api
        metadata_path.parent.mkdir(parents=True, exist_ok=True)
        self.store = api["sqlite"]({"filename": str(metadata_path)}).connect()
        self.pipeline_name = pipeline_name
        self.metadata_path = metadata_path
        self.artifact_root = artifact_root
        self.last_publication: dict[str, Any] | None = None
        self.parent = api["parent"](
            self.store,
            pipeline_name,
            {
                "clio_mapping_version": "clio.cmf.v1",
                "clio_source": "arc-semantic-event-highway",
            },
        )
        self.stage = api["run"](
            self.store,
            f"{pipeline_name}/artifacts",
            {"clio_substream": "artifact-provenance"},
        )
        api["associate"](self.store, self.parent, self.stage)

    def publish(self, server_url: str, *, timeout_s: float) -> dict[str, Any]:
        """Serialize cumulative MLMD and publish it through CMF's server protocol."""
        import requests
        from cmflib.cmfquery import CmfQuery

        base_url = server_url.strip().rstrip("/")
        if not base_url:
            raise ValueError("publish requires a CMF server URL")
        if timeout_s <= 0:
            raise ValueError("publish timeout must be greater than zero")
        json_payload = CmfQuery(str(self.metadata_path)).dumptojson(self.pipeline_name, None)
        if not json_payload:
            raise RuntimeError(f"CMF produced no metadata payload for {self.pipeline_name!r}")
        response = requests.post(
            f"{base_url}/api/mlmd_push",
            json={
                "exec_uuid": None,
                "json_payload": json_payload,
                "pipeline_name": self.pipeline_name,
            },
            timeout=timeout_s,
        )
        try:
            body = response.json()
        except ValueError as exc:
            raise RuntimeError(
                f"CMF metadata push returned invalid JSON (status={response.status_code})"
            ) from exc
        status = str(body.get("status") or "") if isinstance(body, dict) else ""
        if response.status_code != 200 or status not in {"success", "exists"}:
            raise RuntimeError(
                "CMF metadata push failed "
                f"(status={response.status_code}, result={status or 'unknown'})"
            )
        self.last_publication = {
            "status": status,
            "status_code": response.status_code,
            "pipeline_name": self.pipeline_name,
        }
        return dict(self.last_publication)

    def record(self, event: dict[str, Any]) -> dict[str, Any]:
        event_type = str(event.get("event_type") or "")
        payload = event.get("payload")
        body = payload if isinstance(payload, dict) else {}
        if event_type in {"artifact.created", "artifact.version.added"}:
            artifact = self._record_artifact(event, body)
            return {"artifact_mlmd_id": int(artifact.id)}
        if event_type == "artifact.transform.recorded":
            execution = self._record_transform(event, body)
            return {"execution_mlmd_id": int(execution.id)}
        if event_type in {"artifact.alias.moved", "artifact.enriched", "artifact.used"}:
            self._update_artifact(event_type, body)
            return {}
        return {"filtered": True}

    def _record_artifact(self, event: dict[str, Any], body: dict[str, Any]) -> Any:
        artifact_id = str(body.get("artifact_id") or "")
        if not artifact_id:
            raise ValueError("artifact event is missing artifact_id")
        existing = self._artifact_by_clio_id(artifact_id)
        if existing is not None:
            return existing
        receipt = _storage_receipt(body)
        digests = receipt.get("digests") if isinstance(receipt, dict) else {}
        digests = digests if isinstance(digests, dict) else {}
        dvc_md5 = str(digests.get("md5") or "")
        sha256 = str(body.get("sha256") or digests.get("sha256") or "")
        kind = str(body.get("kind") or "other")
        custom = {
            "clio_artifact_id": artifact_id,
            "clio_arc_event_id": str(event.get("event_id") or body.get("event_id") or ""),
            "clio_workspace_id": str(body.get("workspace_id") or event.get("workspace_id") or ""),
            "clio_name": str(body.get("name") or artifact_id),
            "clio_version": int(body.get("version") or 1),
            "clio_kind": kind,
            "clio_sha256": sha256,
            "clio_size_bytes": int(body.get("size_bytes") or 0),
            "clio_custody": str(body.get("custody") or ""),
            "clio_mechanism": str(body.get("mechanism") or ""),
            "clio_evidence_class": str(body.get("evidence_class") or ""),
            "clio_prior_version": int(body.get("prior_version") or 0),
            "clio_prior_sha256": str(body.get("prior_sha256") or ""),
            "clio_created_at": str(body.get("created_at") or event.get("occurred_at") or ""),
            "clio_path": str(body.get("path") or ""),
            "clio_annotation": str(body.get("annotation") or ""),
            "clio_producer_json": json.dumps(body.get("producer") or {}, sort_keys=True),
            "clio_storage_receipt_json": json.dumps(receipt, sort_keys=True),
            "clio_mapping_version": "clio.cmf.v1",
        }
        value = self._api["value"]
        mlpb = self._api["mlpb"]
        artifact = self._api["create_artifact"](
            store=self.store,
            uri=f"clio://artifact/{artifact_id}",
            name=f"{custom['clio_name']}:v{custom['clio_version']}",
            type_name=_cmf_artifact_type(kind),
            properties={
                "git_repo": value(""),
                "Commit": value(dvc_md5),
                "url": value(str(receipt.get("object_uri") or body.get("path") or "")),
            },
            type_properties={
                "git_repo": mlpb.STRING,
                "Commit": mlpb.STRING,
                "url": mlpb.STRING,
            },
            custom_properties={key: value(item) for key, item in custom.items()},
        )
        attribution = mlpb.Attribution(context_id=self.stage.id, artifact_id=artifact.id)
        self.store.put_attributions_and_associations([attribution], [])
        return artifact

    def _record_transform(self, event: dict[str, Any], body: dict[str, Any]) -> Any:
        call_id = str(body.get("call_id") or "")
        if not call_id:
            raise ValueError("transform event is missing call_id")
        instrument = body.get("instrument")
        instrument = instrument if isinstance(instrument, dict) else {}
        custom = {
            "clio_call_id": call_id,
            "clio_arc_event_id": str(event.get("event_id") or body.get("event_id") or ""),
            "clio_session_id": str(body.get("session_id") or event.get("session_id") or ""),
            "clio_workspace_id": str(body.get("workspace_id") or event.get("workspace_id") or ""),
            "clio_turn_id": str(body.get("turn_id") or event.get("turn_id") or ""),
            "clio_status": str(body.get("status") or event.get("status") or ""),
            "clio_kind": str(body.get("kind") or "ordinary"),
            "clio_agent_id": str(body.get("agent_id") or ""),
            "clio_agent_role": str(body.get("agent_role") or ""),
            "clio_tool": str(instrument.get("tool") or ""),
            "clio_replay": str(body.get("replay") or ""),
            "clio_replay_reason": str(body.get("replay_reason") or ""),
            "clio_started_at": str(body.get("started_at") or ""),
            "clio_ended_at": str(body.get("ended_at") or ""),
            "clio_instrument_json": json.dumps(instrument, sort_keys=True),
            "clio_environment_json": json.dumps(body.get("environment") or {}, sort_keys=True),
            "clio_mapping_version": "clio.cmf.v1",
        }
        execution = self._api["create_execution"](
            store=self.store,
            execution_type_name=self.stage.name,
            execution_name=f"clio:{call_id}",
            context_id=self.stage.id,
            execution=str(instrument.get("tool") or "clio transform"),
            pipeline_id=self.parent.id,
            pipeline_type=self.parent.name,
            git_repo="",
            git_start_commit="",
            custom_properties=custom,
            create_new_execution=False,
        )
        for key, item in custom.items():
            execution.custom_properties[key].CopyFrom(self._api["value"](item))
        self.store.put_executions([execution])
        self._link_edges(execution.id, body.get("used"), input_event=True)
        self._link_edges(execution.id, body.get("generated"), input_event=False)
        return execution

    def _link_edges(self, execution_id: int, raw: Any, *, input_event: bool) -> None:
        mlpb = self._api["mlpb"]
        for index, edge in enumerate(raw if isinstance(raw, list) else []):
            if not isinstance(edge, dict):
                continue
            artifact_id = str(edge.get("artifact_id") or "")
            artifact = self._artifact_by_clio_id(artifact_id) if artifact_id else None
            if artifact is None:
                external = str(edge.get("external_ref") or edge.get("authority") or "")
                if not external:
                    continue
                artifact = self._external_artifact(external, edge)
            prior = self.store.get_events_by_artifact_ids([artifact.id])
            event_type = mlpb.Event.INPUT if input_event else mlpb.Event.OUTPUT
            if any(item.execution_id == execution_id and item.type == event_type for item in prior):
                continue
            path = mlpb.Event.Path(
                steps=[mlpb.Event.Path.Step(key=str(edge.get("name") or f"edge_{index}"))]
            )
            self.store.put_events(
                [
                    mlpb.Event(
                        execution_id=execution_id,
                        artifact_id=artifact.id,
                        type=event_type,
                        path=path,
                    )
                ]
            )

    def _external_artifact(self, reference: str, edge: dict[str, Any]) -> Any:
        uri = f"clio://external/{reference}"
        existing = self.store.get_artifacts_by_uri(uri)
        if existing:
            return existing[-1]
        value = self._api["value"]
        artifact = self._api["create_artifact"](
            store=self.store,
            uri=uri,
            name=str(edge.get("name") or reference),
            type_name="Dataset",
            custom_properties={
                "clio_external": value(1),
                "clio_external_ref": value(reference),
                "clio_sha256": value(str(edge.get("sha256") or "")),
                "clio_edge_evidence": value(str(edge.get("evidence") or "")),
            },
        )
        mlpb = self._api["mlpb"]
        attribution = mlpb.Attribution(context_id=self.stage.id, artifact_id=artifact.id)
        self.store.put_attributions_and_associations([attribution], [])
        return artifact

    def _update_artifact(self, event_type: str, body: dict[str, Any]) -> None:
        artifact_id = str(body.get("artifact_id") or "")
        artifact = self._artifact_by_clio_id(artifact_id) if artifact_id else None
        if artifact is None and event_type == "artifact.alias.moved":
            artifact = self._artifact_by_record_version(
                str(body.get("workspace_id") or ""),
                str(body.get("name") or ""),
                int(body.get("to_version") or 0),
            )
        if artifact is None:
            return
        if event_type == "artifact.alias.moved":
            key = f"clio_alias_{str(body.get('alias') or 'latest')}"
            artifact.custom_properties[key].CopyFrom(
                self._api["value"](int(body.get("to_version") or 0))
            )
        elif event_type == "artifact.enriched":
            artifact.custom_properties["clio_annotation"].CopyFrom(
                self._api["value"](str(body.get("annotation") or ""))
            )
        elif event_type == "artifact.used":
            artifact.custom_properties["clio_last_used_session_id"].CopyFrom(
                self._api["value"](str(body.get("session_id") or ""))
            )
        self.store.put_artifacts([artifact])

    def _artifact_by_clio_id(self, artifact_id: str) -> Optional[Any]:
        if not artifact_id:
            return None
        matches = self.store.get_artifacts_by_uri(f"clio://artifact/{artifact_id}")
        return matches[-1] if matches else None

    def _artifact_by_record_version(
        self, workspace_id: str, name: str, version: int
    ) -> Optional[Any]:
        for artifact in self.store.get_artifacts():
            props = _custom(artifact)
            if (
                props.get("clio_workspace_id") == workspace_id
                and props.get("clio_name") == name
                and int(props.get("clio_version") or 0) == version
            ):
                return artifact
        return None

    def lineage(
        self, artifact_id: str, *, direction: str, depth: int, complete: bool
    ) -> Optional[dict[str, Any]]:
        root = self._artifact_by_clio_id(artifact_id)
        if root is None:
            return None
        direction = direction if direction in {"upstream", "downstream", "both"} else "both"
        nodes: dict[str, dict[str, Any]] = {}
        artifact_ids: dict[int, str] = {}
        record_versions: dict[tuple[str, str, int], str] = {}
        for artifact in self.store.get_artifacts():
            props = _custom(artifact)
            node = _artifact_node(artifact, props)
            node_id = str(node["id"])
            nodes[node_id] = node
            artifact_ids[int(artifact.id)] = node_id
            if not node.get("external"):
                record_versions[
                    (
                        str(node.get("workspace_id") or ""),
                        str(node.get("name") or ""),
                        int(node.get("version") or 0),
                    )
                ] = node_id
        edges: list[dict[str, Any]] = []
        execution_nodes: dict[int, str] = {}
        for execution in self.store.get_executions():
            props = _custom(execution)
            call_id = str(props.get("clio_call_id") or "")
            if not call_id:
                continue
            node_id = f"activity:{call_id}"
            nodes[node_id] = _execution_node(execution, props)
            execution_nodes[int(execution.id)] = node_id
        mlpb = self._api["mlpb"]
        events = (
            self.store.get_events_by_execution_ids(list(execution_nodes)) if execution_nodes else []
        )
        for event in events:
            activity = execution_nodes.get(int(event.execution_id))
            artifact = artifact_ids.get(int(event.artifact_id))
            if not activity or not artifact:
                continue
            if event.type == mlpb.Event.INPUT:
                edges.append(
                    {"from": artifact, "to": activity, "type": "used", "evidence": "cmf-input"}
                )
            elif event.type == mlpb.Event.OUTPUT:
                edges.append(
                    {
                        "from": activity,
                        "to": artifact,
                        "type": "generated",
                        "evidence": "cmf-output",
                    }
                )
        for node_id, node in list(nodes.items()):
            if node.get("type") not in {"artifact", "gap"} or node.get("external"):
                continue
            prior_version = int(node.get("prior_version") or 0)
            if not prior_version:
                continue
            prior = record_versions.get(
                (
                    str(node.get("workspace_id") or ""),
                    str(node.get("name") or ""),
                    prior_version,
                )
            )
            if prior:
                edges.append(
                    {"from": node_id, "to": prior, "type": "revision_of", "evidence": "hash-pair"}
                )
        selected_nodes, selected_edges, truncated = _bounded_component(
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
            "truncated": truncated,
            "provider": "cmf",
        }


def _storage_receipt(body: dict[str, Any]) -> dict[str, Any]:
    producer = body.get("producer")
    producer = producer if isinstance(producer, dict) else {}
    receipt = producer.get("storage_receipt")
    return receipt if isinstance(receipt, dict) else {}


def _cmf_artifact_type(kind: str) -> str:
    if kind == "model":
        return "Model"
    if kind in {"metrics", "table"}:
        return "Metrics"
    if kind in {"environment", "script"}:
        return "Environment"
    return "Dataset"


def _custom(obj: Any) -> dict[str, Any]:
    return {key: _value(value) for key, value in obj.custom_properties.items()}


def _value(value: Any) -> Any:
    field = value.WhichOneof("value")
    return getattr(value, field) if field else None


def _artifact_node(artifact: Any, props: dict[str, Any]) -> dict[str, Any]:
    if props.get("clio_external"):
        return {
            "id": str(props.get("clio_external_ref") or artifact.uri),
            "type": "artifact",
            "external": True,
            "authority": str(props.get("clio_external_ref") or ""),
            "sha256": str(props.get("clio_sha256") or "") or None,
            "evidence": str(props.get("clio_edge_evidence") or ""),
            "name": str(artifact.name or ""),
        }
    mechanism = str(props.get("clio_mechanism") or "")
    return {
        "id": str(props.get("clio_artifact_id") or artifact.uri),
        "type": "gap" if mechanism == "none" else "artifact",
        "workspace_id": str(props.get("clio_workspace_id") or ""),
        "name": str(props.get("clio_name") or artifact.name),
        "version": int(props.get("clio_version") or 0),
        "kind": str(props.get("clio_kind") or "other"),
        "sha256": str(props.get("clio_sha256") or "") or None,
        "mechanism": mechanism,
        "custody_gap": None,
        "producer_call_id": _producer_call_id(str(props.get("clio_producer_json") or "")),
        "prior_version": int(props.get("clio_prior_version") or 0) or None,
        "cmf_artifact_id": int(artifact.id),
    }


def _producer_call_id(raw: str) -> str:
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return ""
    return str(value.get("call_id") or "") if isinstance(value, dict) else ""


def _execution_node(execution: Any, props: dict[str, Any]) -> dict[str, Any]:
    environment: dict[str, Any] = {}
    try:
        parsed = json.loads(str(props.get("clio_environment_json") or "{}"))
        environment = parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        pass
    return {
        "id": f"activity:{props.get('clio_call_id')}",
        "type": "activity",
        "call_id": str(props.get("clio_call_id") or ""),
        "tool": str(props.get("clio_tool") or ""),
        "status": str(props.get("clio_status") or ""),
        "kind": str(props.get("clio_kind") or "ordinary"),
        "replay": str(props.get("clio_replay") or ""),
        "environment_tier": str(environment.get("tier") or ""),
        "session_id": str(props.get("clio_session_id") or ""),
        "turn_id": str(props.get("clio_turn_id") or ""),
        "cmf_execution_id": int(execution.id),
    }


def _bounded_component(
    root: str,
    nodes: dict[str, dict[str, Any]],
    edges: list[dict[str, Any]],
    *,
    direction: str,
    depth: int,
    complete: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], Optional[dict[str, Any]]]:
    incoming: dict[str, list[dict[str, Any]]] = {}
    outgoing: dict[str, list[dict[str, Any]]] = {}
    for edge in edges:
        outgoing.setdefault(str(edge["from"]), []).append(edge)
        incoming.setdefault(str(edge["to"]), []).append(edge)
    selected = {root}
    frontier: deque[tuple[str, int]] = deque([(root, 0)])
    truncated: Optional[dict[str, Any]] = None
    while frontier:
        current, level = frontier.popleft()
        candidates: list[tuple[dict[str, Any], str]] = []
        if direction in {"upstream", "both"}:
            candidates.extend((edge, str(edge["from"])) for edge in incoming.get(current, []))
            candidates.extend(
                (edge, str(edge["to"]))
                for edge in outgoing.get(current, [])
                if edge.get("type") == "revision_of"
            )
        if direction in {"downstream", "both"}:
            candidates.extend((edge, str(edge["to"])) for edge in outgoing.get(current, []))
            candidates.extend(
                (edge, str(edge["from"]))
                for edge in incoming.get(current, [])
                if edge.get("type") == "revision_of"
            )
        if not complete and level >= depth:
            if candidates:
                truncated = {"reason": "depth_horizon", "at_depth": depth}
            continue
        for _edge, neighbour in candidates:
            if neighbour in selected or neighbour not in nodes:
                continue
            if len(selected) >= 500:
                truncated = {"reason": "node_cap", "nodes": 500}
                break
            selected.add(neighbour)
            next_level = level + (1 if nodes[neighbour].get("type") != "activity" else 0)
            frontier.append((neighbour, next_level))
    selected_edges = [edge for edge in edges if edge["from"] in selected and edge["to"] in selected]
    return (
        [nodes[node_id] for node_id in sorted(selected)],
        sorted(selected_edges, key=lambda item: (item["from"], item["to"], item["type"])),
        truncated,
    )


def _write(response: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--metadata", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--pipeline", required=True)
    args = parser.parse_args()
    try:
        database = CMFEventStore(args.metadata, args.artifact_root, args.pipeline)
    except Exception as exc:  # noqa: BLE001 - stderr is diagnostic; parent gets no hello
        traceback.print_exc(file=sys.stderr)
        _write({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        return 1
    for line in sys.stdin:
        try:
            request = json.loads(line)
            operation = str(request.get("operation") or "")
            if operation == "close":
                _write({"ok": True})
                return 0
            if operation == "health":
                _write(
                    {
                        "ok": True,
                        "cmflib_version": importlib.metadata.version("cmflib"),
                        "ml_metadata_version": importlib.metadata.version("ml-metadata"),
                        "metadata_path": str(args.metadata),
                        "artifact_root": str(args.artifact_root),
                        "last_publication": database.last_publication,
                    }
                )
                continue
            if operation == "record":
                event = request.get("event")
                if not isinstance(event, dict):
                    raise ValueError("record requires an event object")
                _write({"ok": True, **database.record(event)})
                continue
            if operation == "publish":
                result = database.publish(
                    str(request.get("server_url") or ""),
                    timeout_s=float(request.get("timeout_s") or 30.0),
                )
                _write({"ok": True, **result})
                continue
            if operation == "lineage":
                graph = database.lineage(
                    str(request.get("artifact_id") or ""),
                    direction=str(request.get("direction") or "both"),
                    depth=int(request.get("depth") or 0),
                    complete=bool(request.get("complete")),
                )
                _write({"ok": True, "graph": graph})
                continue
            raise ValueError(f"unknown operation: {operation}")
        except Exception as exc:  # noqa: BLE001 - each request gets a typed failure response
            traceback.print_exc(file=sys.stderr)
            _write({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
