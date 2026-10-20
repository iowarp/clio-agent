"""Existing NDP Endpoint/service operations with immutable response custody."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any

from clio_agent.gact.resource_custody import ResourceStore
from clio_agent.gact.storage.models import now
from clio_agent.gact.storage.ndp_endpoint_capture import (
    MAX_RESPONSE,
    capture_response,
    stream_continuation,
)
from clio_agent.gact.storage.ndp_endpoint_errors import raise_endpoint_error
from clio_agent.gact.storage.ndp_endpoint_models import (
    EndpointCapture,
    EndpointConnection,
    EndpointConnectionInput,
    EndpointConnectionUpdate,
    EndpointRequest,
)
from clio_agent.gact.storage.ndp_endpoint_operations import operations, resolve_request
from clio_agent.gact.storage.ndp_endpoint_transport import endpoint_client
from clio_agent.gact.storage.store import SourceStore


class NDPEndpointService:
    """Own approved destinations; connection metadata never means a stream was consumed."""

    def __init__(self, store: SourceStore, principal: str) -> None:
        self.store, self.principal = store, principal

    def connections(
        self, workspace_id: str, *, include_hidden: bool = False
    ) -> list[EndpointConnection]:
        """List only this workspace and principal's connections."""
        return [
            row
            for row in self.store.list("ndp_endpoint", EndpointConnection)
            if row.workspace_id == workspace_id
            and row.principal == self.principal
            and (include_hidden or not row.hidden)
        ]

    def get(self, workspace_id: str, identifier: str) -> EndpointConnection:
        """Refuse cross-workspace connections, including guessed identifiers."""
        for row in self.connections(workspace_id, include_hidden=True):
            if row.id == identifier:
                return row
        raise KeyError("NDP Endpoint not connected in this workspace")

    @staticmethod
    def _discover(choice: EndpointConnectionInput, token: str | None) -> dict[str, Any]:
        """Read the contract from this explicitly reviewed authentication destination."""
        with endpoint_client(choice) as client:
            headers = (
                {"Authorization": "Bearer " + token}
                if choice.authentication == "ndp" and token
                else {}
            )
            with client.stream("GET", choice.url + "/openapi.json", headers=headers) as response:
                if response.status_code in {401, 403}:
                    raise PermissionError(
                        "NDP Endpoint API discovery requires sign-in and an Endpoint access grant"
                    )
                response.raise_for_status()
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_RESPONSE:
                        raise ValueError("Endpoint OpenAPI exceeds the 2 MiB discovery limit")
        document = json.loads(data)
        if not isinstance(document, dict) or not operations(document):
            raise ValueError("This URL does not expose an OpenAPI service; use its API base URL")
        return document

    def connect(
        self, workspace_id: str, choice: EndpointConnectionInput, token: str | None = None
    ) -> EndpointConnection:
        """Observe the actual API, then save the approved destination without installation."""
        document = self._discover(choice, token)
        previous = next(
            (
                row
                for row in self.connections(workspace_id, include_hidden=True)
                if row.url == choice.url
                and row.ssh_profile == choice.ssh_profile
                and row.ssh_route == choice.ssh_route
                and row.authentication == choice.authentication
            ),
            None,
        )
        record = EndpointConnection(
            **choice.model_dump(),
            id=previous.id if previous else "ndpep_" + uuid.uuid4().hex,
            workspace_id=workspace_id,
            principal=self.principal,
            schema_document=document,
            observed_at=now(),
        )
        self.store.put("ndp_endpoint", record.id, record)
        return record

    def update(
        self,
        workspace_id: str,
        identifier: str,
        update: EndpointConnectionUpdate,
        token: str | None = None,
    ) -> EndpointConnection:
        """Persist edits; rediscover changed destinations before replacing the saved record."""
        record = self.get(workspace_id, identifier)
        changes = update.model_dump(exclude_none=True)
        if "ssh_route" in update.model_fields_set:
            changes["ssh_route"] = update.ssh_route.model_dump() if update.ssh_route else None
        choice = EndpointConnectionInput.model_validate(
            {
                **record.model_dump(include=set(EndpointConnectionInput.model_fields)),
                **{key: value for key, value in changes.items() if key != "hidden"},
            }
        )
        changed_destination = any(
            getattr(choice, key) != getattr(record, key)
            for key in ("url", "ssh_profile", "ssh_route", "authentication")
        )
        document = self._discover(choice, token) if changed_destination else record.schema_document
        edited = EndpointConnection.model_validate(
            {
                **record.model_dump(),
                **choice.model_dump(),
                "hidden": changes.get("hidden", record.hidden),
                "schema_document": document,
                "observed_at": now() if changed_destination else record.observed_at,
            }
        )
        self.store.put("ndp_endpoint", record.id, edited)
        return edited

    @staticmethod
    def describe(record: EndpointConnection) -> dict[str, Any]:
        """Expose useful API semantics; external descriptions remain untrusted data."""
        return {
            **record.model_dump(exclude={"schema_document", "principal"}),
            "api_title": record.schema_document.get("info", {}).get("title", record.name),
            "operations": operations(record.schema_document),
        }

    def disconnect(self, workspace_id: str, identifier: str) -> None:
        """Forget a connection without stopping someone else's remote streams or deleting data."""
        record = self.get(workspace_id, identifier)
        with self.store.transaction() as db:
            db.execute("DELETE FROM records WHERE kind='ndp_endpoint' AND id=?", (record.id,))

    def captures(self, workspace_id: str, identifier: str) -> list[EndpointCapture]:
        """Return retained samples/receipts for one approved connection."""
        self.get(workspace_id, identifier)
        return [
            row
            for row in self.store.list("ndp_capture", EndpointCapture)
            if row.workspace_id == workspace_id and row.endpoint_id == identifier
        ]

    def browse(
        self,
        workspace_id: str,
        identifier: str,
        request: EndpointRequest,
        token: str | None = None,
    ) -> Any:
        """Read bounded catalog/station/session metadata without creating capture files."""
        allowed = {
            "GET /organization",
            "GET /search",
            "GET /resources/search",
            "GET /data/csv-files",
            "GET /active-streams",
            "GET /bridge/stations/catalog",
            "GET /bridge/stations",
            "GET /bridge/sessions",
            "GET /streams",
        }
        if request.operation_id not in allowed or request.body is not None:
            raise ValueError("Choose a supported catalog, station or stream listing")
        record = self.get(workspace_id, identifier)
        _, url, query = resolve_request(record, request)
        headers = {"Accept": "application/json"}
        if record.authentication == "ndp":
            if not token:
                raise PermissionError("Connect your NDP account before browsing this Endpoint")
            headers["Authorization"] = "Bearer " + token
        deadline = time.monotonic() + 30
        with endpoint_client(record, 30) as client:
            with client.stream("GET", url, params=query, headers=headers) as response:
                raise_endpoint_error(response, request, deadline, token)
                data, reason, _ = capture_response(response, request, deadline)
                if reason != "completed":
                    raise ValueError("Listing exceeded its size or time limit; narrow the search")
        return json.loads(data)

    def invoke(
        self,
        workspace_id: str,
        identifier: str,
        request: EndpointRequest,
        resources: ResourceStore,
        token: str | None = None,
    ) -> dict[str, Any]:
        """Run a discovered operation; close streams at finite time/event/byte limits."""
        record = self.get(workspace_id, identifier)
        operation, url, query = resolve_request(record, request)
        headers = {"Accept": "application/json, text/event-stream, */*"}
        if record.authentication == "ndp":
            if not token:
                raise PermissionError(
                    "Sign in to NDP using the private account sign-in before this operation"
                )
            if operation["path"].startswith("/services/redirect/"):
                raise PermissionError(
                    "This Endpoint forwards proxy headers upstream. Connect the service's direct URL to explicitly approve its authentication destination"
                )
            headers["Authorization"] = "Bearer " + token
        started = now()
        deadline = time.monotonic() + request.max_seconds
        with endpoint_client(record, request.max_seconds) as client:
            with client.stream(
                operation["method"], url, params=query, json=request.body, headers=headers
            ) as response:
                raise_endpoint_error(response, request, deadline, token)
                content_type = response.headers.get("content-type", "application/octet-stream")
                sse = "text/event-stream" in content_type
                raw, reason, count = capture_response(response, request, deadline)
        digest = hashlib.sha256(raw).hexdigest()
        name = (
            "ndp-stream.sse"
            if sse
            else ("ndp-response.json" if "json" in content_type else "ndp-response.txt")
        )
        identifier_capture = "ndpcap_" + uuid.uuid4().hex
        resource, _ = resources.create_or_resume(
            workspace_id=workspace_id,
            name=name,
            declared_size=len(raw),
            client_upload_id=identifier_capture,
        )
        if resource.state == "uploading":
            resource = resources.append(resource.id, offset=0, data=bytes(raw))
        if resource.state != "ready" or resource.sha256 != digest:
            raise ValueError(
                "Endpoint response could not be retained in workspace resource custody"
            )
        capture = EndpointCapture(
            id=identifier_capture,
            endpoint_id=record.id,
            endpoint_name=record.name,
            endpoint_url=record.url,
            ssh_profile=record.ssh_profile,
            ssh_route=record.ssh_route,
            workspace_id=workspace_id,
            operation_id=request.operation_id,
            operation_summary=str(operation["summary"])[:200],
            method=operation["method"],
            parameters=request.parameters,
            body=request.body,
            started_at=started,
            completed_at=now(),
            status_code=response.status_code,
            content_type=content_type,
            bytes_received=len(raw),
            stop_reason=reason,
            events=count,
            resource_id=resource.id,
            sha256=digest,
            continuation=stream_continuation(raw, reason, request),
        )
        self.store.put("ndp_capture", capture.id, capture)
        return {
            **capture.model_dump(),
            "preview": bytes(raw[:16000]).decode("utf-8", errors="replace"),
            "preview_truncated": len(raw) > 16000,
        }


def materialize_capture(app: Any, result: dict[str, Any]) -> dict[str, Any]:
    """Publish the retained response through the ordinary Sources/artifact pipeline."""
    from clio_agent.gact.resource_materialization import materialize_once

    record = app.state.resource_store.get(result["workspace_id"], result["resource_id"])
    if record is None:
        raise ValueError("The Endpoint capture resource is unavailable")
    record = materialize_once(app, record)
    capture = app.state.connected_storage.store.get("ndp_capture", result["id"], EndpointCapture)
    capture.workspace_path, capture.source_artifact_id = (
        record.workspace_path,
        record.source_artifact_id,
    )
    app.state.connected_storage.store.put("ndp_capture", capture.id, capture)
    return {
        **result,
        "workspace_path": record.workspace_path,
        "materialization": record.materialization.model_dump(),
        "source_artifact_id": record.source_artifact_id,
    }
