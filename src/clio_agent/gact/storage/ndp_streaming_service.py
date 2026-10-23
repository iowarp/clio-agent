"""Workspace-owned finite SDK readers and canonical, durable capture checkpoints."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from clio_agent.gact.resource_custody import ResourceStore
from clio_agent.gact.storage.models import now
from clio_agent.gact.storage.ndp_endpoint_models import (
    EndpointCapture,
    EndpointConnection,
    EndpointContinuation,
    EndpointRequest,
)
from clio_agent.gact.storage.ndp_streaming_models import PREFIX, StreamBatch, StreamReader
from clio_agent.gact.storage.ndp_streaming_transport import run_sdk
from clio_agent.gact.storage.store import SourceStore


class NDPStreamingService:
    """Keep SDK execution outside ledger transactions and source producers independent."""

    def __init__(self, store: SourceStore, principal: str) -> None:
        self.store, self.principal = store, principal

    def readers(self, connection: EndpointConnection) -> list[StreamReader]:
        """List only readers owned by this workspace, connection and CLIO principal."""
        return [
            row
            for row in self.store.list("ndp_sdk_reader", StreamReader)
            if (row.workspace_id, row.endpoint_id, row.principal)
            == (connection.workspace_id, connection.id, self.principal)
        ]

    def _get(self, connection: EndpointConnection, identifier: str) -> StreamReader:
        for row in self.readers(connection):
            if row.id == identifier:
                if row.connection_fingerprint != self._fingerprint(connection):
                    raise ValueError(
                        "Endpoint or SDK runtime changed; open a new reader after inspecting its source"
                    )
                return row
        raise KeyError("NDP stream reader does not belong to this workspace and connection")

    @staticmethod
    def _batch_key(identifier: str, cursor: str) -> str:
        return hashlib.sha256(json.dumps([identifier, cursor]).encode()).hexdigest()

    @staticmethod
    def _fingerprint(connection: EndpointConnection) -> str:
        values = connection.model_dump(
            include={"url", "ssh_profile", "ssh_route", "authentication", "streaming_runtime"}
        )
        return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()

    def invoke(
        self,
        connection: EndpointConnection,
        request: EndpointRequest,
        resources: ResourceStore,
        token: str | None,
    ) -> dict[str, Any]:
        """Open/read/control a logical reader without installing software on the Endpoint."""
        action = request.operation_id.removeprefix(PREFIX)
        if connection.streaming_runtime is None:
            raise ValueError("Configure an installed streaming_v2 runtime for this Endpoint")
        started = now()
        previous: StreamReader | None = None
        batch_key = ""
        if action == "open":
            result = run_sdk(
                connection,
                {
                    "action": "open",
                    "resource_id": request.parameters["resource_id"],
                    "start": request.parameters.get("start", "earliest"),
                },
                token,
                request.max_seconds,
            )
            view = StreamReader(
                id="ndpstream_" + uuid.uuid4().hex,
                endpoint_id=connection.id,
                workspace_id=connection.workspace_id,
                principal=self.principal,
                topic=result["topic"],
                positions=result["positions"],
                cursor=uuid.uuid4().hex,
                created_at=started,
                earliest_positions=result["earliest_positions"],
                topic_identity=result["topic_identity"],
                connection_fingerprint=self._fingerprint(connection),
            )
            payload = {
                **view.model_dump(exclude={"principal"}),
                "history": result["history"],
                "latest_positions": result["latest_positions"],
            }
        elif action == "list":
            view = None
            payload = {
                "streams": [
                    row.model_dump(exclude={"principal"}) for row in self.readers(connection)
                ]
            }
        else:
            previous = self._get(connection, request.parameters["stream_id"])
            view = previous.model_copy(deep=True)
            if action in {"stop", "resume"}:
                view.state = "stopped" if action == "stop" else "active"
                view.generation += 1
                payload = view.model_dump(exclude={"principal"})
            elif action == "read":
                cursor = str(request.parameters.get("cursor", view.cursor))
                batch_key = self._batch_key(view.id, cursor)
                try:
                    return self.store.get("ndp_sdk_batch", batch_key, StreamBatch).result
                except KeyError:
                    pass
                if cursor != view.cursor:
                    raise ValueError(
                        "The supplied SDK stream cursor is not the reader's current position"
                    )
                if view.state != "active":
                    raise ValueError("This NDP SDK reader is stopped; resume it before capturing")
                result = run_sdk(
                    connection,
                    {
                        "action": "read",
                        "topic": view.topic,
                        "positions": view.positions,
                        "topic_identity": view.topic_identity,
                        "max_records": request.max_events,
                        "max_bytes": 2 * 1024 * 1024 - 8192,
                        "poll_seconds": min(5.0, request.max_seconds / 3),
                    },
                    token,
                    request.max_seconds,
                )
                positions = {**result["earliest_positions"], **view.positions}
                if (
                    result["count"] != len(result["records"])
                    or result["count"] > request.max_events
                ):
                    raise ValueError("SDK runtime returned an invalid bounded record count")
                for row in result["records"]:
                    partition = str(row["partition"])
                    if row["topic"] != view.topic or row["offset"] != positions.get(partition):
                        raise ValueError(
                            "SDK capture has a gap or duplicate Kafka offset; saved positions were not advanced"
                        )
                    positions[partition] = row["offset"] + 1
                if result["next_positions"] != positions:
                    raise ValueError(
                        "SDK runtime positions do not match the actual captured records"
                    )
                view.positions, view.cursor = positions, uuid.uuid4().hex
                payload = {**result, "stream_id": view.id, "next_cursor": view.cursor}
            else:
                raise ValueError("Choose a returned streaming_v2 SDK operation")
        capture, result = self._retain(connection, request, resources, payload, started)
        # SDK waits and resource writes happen before this short optimistic commit.
        with self.store.transaction() as db:
            if previous is not None:
                current = db.execute(
                    "SELECT body FROM records WHERE kind='ndp_sdk_reader' AND id=?", (previous.id,)
                ).fetchone()
                if current is None:
                    raise ValueError("NDP stream reader was removed while the capture ran")
                current_view = StreamReader.model_validate_json(current[0])
                if (current_view.generation, current_view.cursor) != (
                    previous.generation,
                    previous.cursor,
                ):
                    cached = (
                        db.execute(
                            "SELECT body FROM records WHERE kind='ndp_sdk_batch' AND id=?",
                            (batch_key,),
                        ).fetchone()
                        if batch_key
                        else None
                    )
                    if cached:
                        return StreamBatch.model_validate_json(cached[0]).result
                    raise ValueError(
                        "Reader was stopped, resumed or advanced while capture ran; its saved positions were not changed"
                    )
            for kind, identifier, body in [
                ("ndp_capture", capture.id, capture.model_dump_json()),
                *(
                    ([("ndp_sdk_reader", view.id, view.model_dump_json())])
                    if view is not None
                    else []
                ),
                *(
                    ([("ndp_sdk_batch", batch_key, StreamBatch(result=result).model_dump_json())])
                    if batch_key
                    else []
                ),
            ]:
                db.execute(
                    "INSERT OR REPLACE INTO records VALUES (?,?,?)", (kind, identifier, body)
                )
        return result

    @staticmethod
    def _retain(
        connection: EndpointConnection,
        request: EndpointRequest,
        resources: ResourceStore,
        payload: dict[str, Any],
        started: str,
    ) -> tuple[EndpointCapture, dict[str, Any]]:
        """Retain SDK data through the same resource custody and presentation as HTTP data."""
        raw = json.dumps(payload, ensure_ascii=True).encode()
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError("SDK response exceeded the 2 MiB resource limit; narrow the capture")
        identifier = "ndpcap_" + uuid.uuid4().hex
        resource, _ = resources.create_or_resume(
            workspace_id=connection.workspace_id,
            name="ndp-stream.json",
            declared_size=len(raw),
            client_upload_id=identifier,
        )
        if resource.state == "uploading":
            resource = resources.append(resource.id, offset=0, data=raw)
        digest = hashlib.sha256(raw).hexdigest()
        if resource.state != "ready" or resource.sha256 != digest:
            raise ValueError("NDP SDK capture could not be retained in workspace resource custody")
        continuation = (
            EndpointContinuation(
                stream_id=payload["stream_id"],
                next_cursor=payload["next_cursor"],
                next_positions=payload["next_positions"],
                count=payload["count"],
            )
            if request.operation_id == PREFIX + "read"
            else None
        )
        capture = EndpointCapture(
            id=identifier,
            endpoint_id=connection.id,
            endpoint_name=connection.name,
            endpoint_url=connection.url,
            ssh_profile=connection.ssh_profile,
            ssh_route=connection.ssh_route,
            workspace_id=connection.workspace_id,
            operation_id=request.operation_id,
            operation_summary=request.operation_id.removeprefix(PREFIX).capitalize()
            + " Kafka reader with streaming_v2",
            method="SDK",
            parameters=request.parameters,
            body=None,
            started_at=started,
            completed_at=now(),
            status_code=200,
            content_type="application/json",
            bytes_received=len(raw),
            stop_reason="completed",
            resource_id=resource.id,
            sha256=digest,
            events=payload.get("count", 0),
            continuation=continuation,
        )
        return capture, {
            **capture.model_dump(),
            "preview": raw[:16000].decode("utf-8"),
            "preview_truncated": len(raw) > 16000,
            "transport": "streaming_v2",
            "stream": {key: value for key, value in payload.items() if key != "records"},
        }
