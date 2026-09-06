"""HTTP routes for capability-bound MCP Apps.

The registry and lifecycle live in :mod:`clio_agent.gact.mcp_apps`; this module
only adapts those domain operations to the GACT wire surface.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from clio_agent.gact.mcp_app_sandbox import (
    _SANDBOX_DOCUMENT,
    _csp_header,
    _host_origin,
    _sandbox_url,
)
from clio_agent.gact.mcp_apps import (
    _MAX_MODEL_CONTEXT_BYTES,
    MCPAppRecord,
    _cleanup_record,
    _not_found,
    _registry,
    _resolve_app_tool,
    _resource_payload,
    _run_bound,
    call_tool_result_to_wire,
    read_resource_result_to_wire,
)
from clio_agent.gact.message_submission import accept_message_async
from clio_agent.gact.routes._body import json_body
from clio_agent.gact.types import Part, PostMessageRequest
from clio_agent.tools.mcp_extension_registry import (
    MCP_APP_MIME_TYPE,
    MCP_APPS_PROTOCOL_REVISION,
)

if TYPE_CHECKING:
    from clio_agent.gact.routes.deps import GactDeps


def register_mcp_app_routes(app: FastAPI, deps: GactDeps) -> None:
    """Register the capability-bound host and separate-origin sandbox routes."""

    def resolve(sid: str, app_id: str, data_ref: str) -> MCPAppRecord:
        if app.state.sessions.get(sid) is None:
            raise _not_found()
        try:
            return _registry(app).get(sid, app_id, data_ref)
        except KeyError as exc:
            raise _not_found() from exc

    async def ensure_resource(record: MCPAppRecord) -> None:
        if record.html is not None:
            return
        result = await _run_bound(
            app,
            record.session_id,
            lambda executor: executor.read_resource(
                record.source_namespace,
                record.resource_uri,
            ),
        )
        html, csp, permissions = _resource_payload(result)
        record.html = html
        record.csp = csp
        record.permissions = permissions

    @app.get("/v1/sessions/{sid}/mcp-apps/{app_id}")
    async def get_mcp_app(sid: str, app_id: str, data_ref: str, request: Request) -> JSONResponse:
        record = resolve(sid, app_id, data_ref)
        try:
            await ensure_resource(record)
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        sandbox_path = (
            f"/v1/sessions/{sid}/mcp-apps/{app_id}/sandbox?{urlencode({'data_ref': data_ref})}"
        )
        return JSONResponse(
            {
                "protocol_version": MCP_APPS_PROTOCOL_REVISION,
                "resource": {
                    "uri": record.resource_uri,
                    "mime_type": MCP_APP_MIME_TYPE,
                    "html": record.html,
                    "csp": record.csp,
                    "permissions": record.permissions,
                },
                "tool_input": record.tool_input,
                "tool_result": record.tool_result,
                "sandbox_url": _sandbox_url(request, sandbox_path),
            },
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    @app.get("/v1/sessions/{sid}/mcp-apps/{app_id}/sandbox")
    async def get_mcp_app_sandbox(
        sid: str, app_id: str, data_ref: str, request: Request
    ) -> HTMLResponse:
        record = resolve(sid, app_id, data_ref)
        await ensure_resource(record)
        origin = _host_origin(request)
        return HTMLResponse(
            _SANDBOX_DOCUMENT,
            headers={
                "Content-Security-Policy": _csp_header(record.csp, origin),
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.post("/v1/sessions/{sid}/mcp-apps/{app_id}/tools/call")
    async def call_mcp_app_tool(
        sid: str, app_id: str, data_ref: str, request: Request
    ) -> dict[str, Any]:
        record = resolve(sid, app_id, data_ref)
        body = await json_body(request, route="POST MCP App tools/call")
        requested = str(body.get("name") or "").strip()
        arguments = body.get("arguments") or {}
        if not requested or not isinstance(arguments, Mapping):
            raise HTTPException(status_code=422, detail="name and object arguments are required")

        def call(executor: Any) -> Any:
            full_name = _resolve_app_tool(executor, record, requested)
            return executor.call_tool_result(full_name, dict(arguments))

        try:
            result = await _run_bound(app, sid, call)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return call_tool_result_to_wire(result)

    @app.post("/v1/sessions/{sid}/mcp-apps/{app_id}/resources/read")
    async def read_mcp_app_resource(
        sid: str, app_id: str, data_ref: str, request: Request
    ) -> dict[str, Any]:
        record = resolve(sid, app_id, data_ref)
        body = await json_body(request, route="POST MCP App resources/read")
        uri = str(body.get("uri") or "").strip()
        if not uri:
            raise HTTPException(status_code=422, detail="uri is required")
        result = await _run_bound(
            app,
            sid,
            lambda executor: executor.read_resource(record.source_namespace, uri),
        )
        return read_resource_result_to_wire(result)

    @app.put("/v1/sessions/{sid}/mcp-apps/{app_id}/model-context")
    async def update_mcp_app_context(
        sid: str, app_id: str, data_ref: str, request: Request
    ) -> dict[str, Any]:
        record = resolve(sid, app_id, data_ref)
        body = await json_body(request, route="PUT MCP App model context")
        encoded = json.dumps(body, sort_keys=True, default=str).encode("utf-8")
        if len(encoded) > _MAX_MODEL_CONTEXT_BYTES:
            raise HTTPException(status_code=413, detail="MCP App model context is too large")
        record.model_context = dict(body)
        record.model_context_digest = hashlib.sha256(encoded).hexdigest()
        record.model_context_bytes = len(encoded)
        return {}

    @app.post("/v1/sessions/{sid}/mcp-apps/{app_id}/messages")
    async def post_mcp_app_message(
        sid: str, app_id: str, data_ref: str, request: Request
    ) -> JSONResponse:
        record = resolve(sid, app_id, data_ref)
        body = await json_body(request, route="POST MCP App ui/message")
        if body.get("role") != "user":
            raise HTTPException(status_code=422, detail="MCP Apps may only submit user messages")
        content = body.get("content") or []
        text = "\n".join(
            str(block.get("text") or "")
            for block in content
            if isinstance(block, Mapping) and block.get("type") == "text"
        ).strip()
        if not text:
            raise HTTPException(status_code=422, detail="ui/message requires text content")
        client_message_id = str(body.get("message_id") or "").strip()
        request_id = str(body.get("request_id") or client_message_id).strip()
        submission = PostMessageRequest(
            parts=[Part(type="text", text=text)],
            client_message_id=client_message_id,
            idempotency_key=(
                f"mcp-app:{record.app_instance_id}:{request_id}" if request_id else ""
            ),
            delivery="auto",
            metadata={
                "mcp_app": {
                    "app_instance_id": record.app_instance_id,
                    "source_server": record.source_namespace or "",
                    "resource_uri": record.resource_uri,
                    "model_context": {
                        "present": bool(record.model_context),
                        "sha256": record.model_context_digest,
                        "bytes": record.model_context_bytes,
                    },
                }
            },
        )
        model_text = text
        if record.model_context:
            model_text = (
                f"{text}\n\nMCP App context from {record.source_namespace or 'server'}:\n"
                f"{json.dumps(record.model_context, sort_keys=True, default=str)}"
            )
        acknowledgement, status_code = await accept_message_async(
            app,
            deps,
            sid,
            submission,
            internal_model_text=model_text,
        )
        return JSONResponse(
            {
                "message_id": acknowledgement.message_id,
                "delivery": acknowledgement.delivery,
                "state": acknowledgement.state,
            },
            status_code=status_code,
        )

    @app.delete("/v1/sessions/{sid}/mcp-apps/{app_id}", status_code=204)
    async def close_mcp_app(sid: str, app_id: str, data_ref: str) -> None:
        try:
            record = _registry(app).claim_close(sid, app_id, data_ref)
        except KeyError as exc:
            raise _not_found() from exc
        try:
            await _cleanup_record(app, record)
        except PermissionError as exc:
            _registry(app).restore_close(record)
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except RuntimeError as exc:
            _registry(app).restore_close(record)
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except Exception:
            _registry(app).restore_close(record)
            raise
        _registry(app).finish_close(app_id)
        return None
