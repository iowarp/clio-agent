"""Crash-safe persistence for infrastructure owned by one CLIO instance."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from clio_agent.gact.infrastructure.models import (
    CreateTargetRequest,
    ExternalServiceConnection,
    InfrastructureOperation,
    InfrastructureTarget,
    ServiceRecord,
    UpdateTargetRequest,
    utc_now,
)

_SCHEMA_VERSION = 1


class InfrastructureStore:
    """Persist targets, services, external connections, and operation receipts."""

    def __init__(self, path: Path | None) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._targets: dict[str, InfrastructureTarget] = {
            "local": InfrastructureTarget(
                id="local",
                label="This CLIO's computer",
                kind="local",
                transport_state="connected",
            )
        }
        self._services: dict[str, ServiceRecord] = {}
        self._connections: dict[str, ExternalServiceConnection] = {}
        self._operations: dict[str, InfrastructureOperation] = {}
        self._load()
        self._interrupt_unfinished_operations()
        self._reset_ssh_transport_state()

    def targets(self) -> list[InfrastructureTarget]:
        """Return every target with the built-in local target first."""

        with self._lock:
            return sorted(
                (row.model_copy(deep=True) for row in self._targets.values()),
                key=lambda row: (row.id != "local", row.label.casefold()),
            )

    def target(self, target_id: str) -> InfrastructureTarget | None:
        """Return one target without exposing the mutable stored instance."""

        with self._lock:
            row = self._targets.get(target_id)
            return row.model_copy(deep=True) if row else None

    def create_target(self, request: CreateTargetRequest) -> InfrastructureTarget:
        """Create one target; SSH metadata is required only for SSH targets."""

        if request.kind == "local":
            raise ValueError("The built-in local target already exists")
        if request.kind == "ssh" and request.ssh is None:
            raise ValueError("SSH targets require OpenSSH route metadata")
        if request.kind != "ssh" and request.ssh is not None:
            raise ValueError("Only SSH targets may contain OpenSSH route metadata")
        target_id = self._next_target_id(request.label)
        row = InfrastructureTarget(id=target_id, **request.model_dump())
        with self._lock:
            self._targets[target_id] = row
            self._flush()
        return row.model_copy(deep=True)

    def delete_target(self, target_id: str) -> None:
        """Delete a non-local target and its owned service records."""

        if target_id == "local":
            raise ValueError("The built-in local target cannot be deleted")
        with self._lock:
            if self._targets.pop(target_id, None) is None:
                raise KeyError(target_id)
            self._services = {
                key: value for key, value in self._services.items() if value.target_id != target_id
            }
            self._flush()

    def update_target(self, target_id: str, request: UpdateTargetRequest) -> InfrastructureTarget:
        """Replace non-secret target metadata while preserving its durable identity."""

        if target_id == "local":
            raise ValueError("The built-in local target cannot be edited")
        if request.kind == "ssh" and request.ssh is None:
            raise ValueError("SSH targets require OpenSSH route metadata")
        if request.kind != "ssh" and request.ssh is not None:
            raise ValueError("Only SSH targets may contain OpenSSH route metadata")
        with self._lock:
            previous = self._targets.get(target_id)
            if previous is None:
                raise KeyError(target_id)
            row = InfrastructureTarget(
                id=target_id,
                created_at=previous.created_at,
                transport_state=(
                    previous.transport_state if previous.ssh == request.ssh else "state_unknown"
                ),
                updated_at=utc_now(),
                **request.model_dump(),
            )
            self._targets[target_id] = row
            if previous.ssh != request.ssh:
                self._services = {
                    key: value
                    for key, value in self._services.items()
                    if value.target_id != target_id
                }
            self._flush()
            return row.model_copy(deep=True)

    def set_transport_state(self, target_id: str, state: str) -> InfrastructureTarget:
        """Update transport state without modifying the target's identity."""

        with self._lock:
            target = self._targets.get(target_id)
            if target is None:
                raise KeyError(target_id)
            updated = target.model_copy(update={"transport_state": state, "updated_at": utc_now()})
            self._targets[target_id] = updated
            self._flush()
            return updated.model_copy(deep=True)

    def service(self, target_id: str, service_id: str) -> ServiceRecord | None:
        """Return the durable record for one service on one target."""

        with self._lock:
            row = self._services.get(f"{target_id}:{service_id}")
            return row.model_copy(deep=True) if row else None

    def put_service(self, record: ServiceRecord) -> ServiceRecord:
        """Create or replace one durable service record."""

        with self._lock:
            updated = record.model_copy(update={"updated_at": utc_now()})
            self._services[updated.id] = updated
            self._flush()
            return updated.model_copy(deep=True)

    def delete_service(self, target_id: str, service_id: str) -> None:
        """Remove a service record after a verified uninstall."""

        with self._lock:
            self._services.pop(f"{target_id}:{service_id}", None)
            self._flush()

    def connections(self) -> list[ExternalServiceConnection]:
        """Return connection-only services in stable label order."""

        with self._lock:
            return sorted(
                (row.model_copy(deep=True) for row in self._connections.values()),
                key=lambda row: row.label.casefold(),
            )

    def connection(self, connection_id: str) -> ExternalServiceConnection | None:
        """Return one external connection without exposing stored state."""

        with self._lock:
            row = self._connections.get(connection_id)
            return row.model_copy(deep=True) if row else None

    def put_connection(self, row: ExternalServiceConnection) -> ExternalServiceConnection:
        """Persist a connection-only service."""

        with self._lock:
            self._connections[row.id] = row
            self._flush()
            return row.model_copy(deep=True)

    def delete_connection(self, connection_id: str) -> None:
        """Delete one connection-only record."""

        with self._lock:
            if self._connections.pop(connection_id, None) is None:
                raise KeyError(connection_id)
            self._flush()

    def operation(self, operation_id: str) -> InfrastructureOperation | None:
        """Return one durable operation receipt."""

        with self._lock:
            row = self._operations.get(operation_id)
            return row.model_copy(deep=True) if row else None

    def put_operation(self, row: InfrastructureOperation) -> InfrastructureOperation:
        """Persist an operation state transition."""

        with self._lock:
            updated = row.model_copy(update={"updated_at": utc_now()})
            self._operations[updated.id] = updated
            self._flush()
            return updated.model_copy(deep=True)

    def _next_target_id(self, label: str) -> str:
        base = (
            "-".join(
                part
                for part in "".join(
                    character.casefold() if character.isalnum() else " " for character in label
                ).split()
                if part
            )
            or "target"
        )
        with self._lock:
            candidate = base
            suffix = 2
            while candidate in self._targets:
                candidate = f"{base}-{suffix}"
                suffix += 1
            return candidate

    def _interrupt_unfinished_operations(self) -> None:
        with self._lock:
            changed = False
            for operation_id, row in tuple(self._operations.items()):
                if row.state not in {"queued", "running"}:
                    continue
                self._operations[operation_id] = row.model_copy(
                    update={
                        "state": "failed",
                        "progress": "Interrupted by CLIO restart; inspect actual service state.",
                        "error": "operation_interrupted",
                        "updated_at": utc_now(),
                    }
                )
                changed = True
            if changed:
                self._flush()

    def _reset_ssh_transport_state(self) -> None:
        """A persisted Desktop byte stream cannot survive a CLIO restart."""

        with self._lock:
            changed = False
            for target_id, row in tuple(self._targets.items()):
                if row.kind != "ssh" or row.transport_state == "state_unknown":
                    continue
                self._targets[target_id] = row.model_copy(
                    update={"transport_state": "state_unknown", "updated_at": utc_now()}
                )
                changed = True
            if changed:
                self._flush()

    def _load(self) -> None:
        if self._path is None or not self._path.exists():
            return
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(payload, dict) or payload.get("schema_version") != _SCHEMA_VERSION:
            return
        self._load_rows(payload.get("targets"), InfrastructureTarget, self._targets)
        self._targets["local"] = InfrastructureTarget(
            id="local",
            label="This CLIO's computer",
            kind="local",
            transport_state="connected",
        )
        self._load_rows(payload.get("services"), ServiceRecord, self._services)
        self._load_rows(payload.get("connections"), ExternalServiceConnection, self._connections)
        self._load_rows(payload.get("operations"), InfrastructureOperation, self._operations)

    @staticmethod
    def _load_rows(raw: Any, model: type[Any], destination: dict[str, Any]) -> None:
        if not isinstance(raw, dict):
            return
        for key, value in raw.items():
            if not isinstance(key, str) or not isinstance(value, dict):
                continue
            try:
                destination[key] = model.model_validate(value)
            except (TypeError, ValueError):
                continue

    def _flush(self) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "targets": {key: value.model_dump(mode="json") for key, value in self._targets.items()},
            "services": {
                key: value.model_dump(mode="json") for key, value in self._services.items()
            },
            "connections": {
                key: value.model_dump(mode="json") for key, value in self._connections.items()
            },
            "operations": {
                key: value.model_dump(mode="json") for key, value in self._operations.items()
            },
        }
        temporary = self._path.with_suffix(self._path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self._path)
