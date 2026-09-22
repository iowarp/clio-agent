"""HTTP and transport routes for infrastructure owned by the active CLIO."""

from __future__ import annotations

import base64
import hmac
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, status

from clio_agent.gact.auth import _header_bearer_token
from clio_agent.gact.infrastructure.models import (
    CreateTargetRequest,
    ExternalServiceConnectionRequest,
    ServiceActionRequest,
    TransportStateRequest,
    UpdateTargetRequest,
)
from clio_agent.gact.infrastructure.runtime import InfrastructureRuntime
from clio_agent.gact.infrastructure.store import InfrastructureStore
from clio_agent.gact.infrastructure.transport import InfrastructureTransportRegistry


def register_infrastructure_routes(app: FastAPI, state_root: Path) -> None:
    """Install CLIO's infrastructure runtime and register its control plane."""

    path = Path(state_root) / "infrastructure.json"
    durable_store = InfrastructureStore(path)

    def update_state(target_id: str, state: str) -> None:
        try:
            durable_store.set_transport_state(target_id, state)
        except KeyError:
            return

    transports = InfrastructureTransportRegistry(update_state)
    app.state.infrastructure_store = durable_store
    app.state.infrastructure_transports = transports
    app.state.infrastructure_runtime = InfrastructureRuntime(durable_store, transports)

    def store() -> InfrastructureStore:
        return app.state.infrastructure_store

    def runtime() -> InfrastructureRuntime:
        return app.state.infrastructure_runtime

    @app.get("/v1/infrastructure/targets")
    async def list_targets() -> dict[str, object]:
        return {"targets": [row.model_dump(mode="json") for row in store().targets()]}

    @app.post("/v1/infrastructure/targets", status_code=status.HTTP_201_CREATED)
    async def create_target(request: CreateTargetRequest) -> dict[str, object]:
        try:
            row = store().create_target(request)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return row.model_dump(mode="json")

    @app.delete("/v1/infrastructure/targets/{target_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_target(target_id: str) -> None:
        await app.state.infrastructure_transports.detach(target_id)
        try:
            store().delete_target(target_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Infrastructure target not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.put("/v1/infrastructure/targets/{target_id}")
    async def update_target(target_id: str, request: UpdateTargetRequest) -> dict[str, object]:
        try:
            row = store().update_target(target_id, request)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Infrastructure target not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return row.model_dump(mode="json")

    @app.put("/v1/infrastructure/targets/{target_id}/transport-state")
    async def set_transport_state(
        target_id: str, request: TransportStateRequest
    ) -> dict[str, object]:
        try:
            row = store().set_transport_state(target_id, request.state)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Infrastructure target not found") from exc
        return row.model_dump(mode="json")

    @app.websocket("/v1/infrastructure/targets/{target_id}/transport")
    async def attach_transport(websocket: WebSocket, target_id: str) -> None:
        target = store().target(target_id)
        if target is None:
            await websocket.close(code=4404)
            return
        if target.kind != "ssh":
            await websocket.close(code=4409)
            return
        expected = getattr(app.state, "bearer_token", None)
        supplied = _header_bearer_token(websocket.scope) or _websocket_protocol_token(websocket)
        if expected is not None and not hmac.compare_digest(supplied, expected):
            await websocket.close(code=4401)
            return
        await app.state.infrastructure_transports.serve(target_id, websocket)

    @app.delete("/v1/infrastructure/targets/{target_id}/transport", status_code=204)
    async def detach_transport(target_id: str) -> None:
        await app.state.infrastructure_transports.detach(target_id)

    @app.get("/v1/infrastructure/catalog")
    async def catalog(target_id: str = "local") -> dict[str, object]:
        try:
            row = await runtime().catalog(target_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Infrastructure target not found") from exc
        except (OSError, RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return row.model_dump(mode="json")

    @app.post(
        "/v1/infrastructure/services/{service_id}/actions",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def service_action(service_id: str, request: ServiceActionRequest) -> dict[str, object]:
        try:
            row = runtime().start_action(service_id, request)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Infrastructure target not found") from exc
        return row.model_dump(mode="json")

    @app.get("/v1/infrastructure/operations/{operation_id}")
    async def operation(operation_id: str) -> dict[str, object]:
        row = store().operation(operation_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Infrastructure operation not found")
        return row.model_dump(mode="json")

    @app.delete("/v1/infrastructure/operations/{operation_id}")
    async def cancel_operation(operation_id: str) -> dict[str, object]:
        try:
            row = await runtime().cancel(operation_id)
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="Infrastructure operation not found"
            ) from exc
        return row.model_dump(mode="json")

    @app.get("/v1/infrastructure/service-connections")
    async def list_connections() -> dict[str, object]:
        return {"connections": [row.model_dump(mode="json") for row in store().connections()]}

    @app.post("/v1/infrastructure/service-connections", status_code=201)
    async def create_connection(
        request: ExternalServiceConnectionRequest,
    ) -> dict[str, object]:
        row = await runtime().create_external_connection(request)
        return row.model_dump(mode="json")

    @app.delete("/v1/infrastructure/service-connections/{connection_id}", status_code=204)
    async def delete_connection(connection_id: str) -> None:
        try:
            store().delete_connection(connection_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Service connection not found") from exc

    @app.put("/v1/infrastructure/service-connections/{connection_id}")
    async def update_connection(
        connection_id: str,
        request: ExternalServiceConnectionRequest,
    ) -> dict[str, object]:
        try:
            row = await runtime().update_external_connection(connection_id, request)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Service connection not found") from exc
        return row.model_dump(mode="json")

    @app.post("/v1/infrastructure/service-connections/{connection_id}/check")
    async def check_connection(connection_id: str) -> dict[str, object]:
        try:
            row = await runtime().check_external_connection(connection_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Service connection not found") from exc
        return row.model_dump(mode="json")


def _websocket_protocol_token(websocket: WebSocket) -> str:
    """Decode a browser-compatible bearer carried as a WebSocket subprotocol."""

    raw = websocket.headers.get("sec-websocket-protocol", "")
    for value in (part.strip() for part in raw.split(",")):
        if not value.startswith("clio-bearer."):
            continue
        encoded = value.removeprefix("clio-bearer.")
        padding = "=" * (-len(encoded) % 4)
        try:
            return base64.urlsafe_b64decode(encoded + padding).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return ""
    return ""
