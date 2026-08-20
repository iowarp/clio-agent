"""Capability-bound MCP Apps 2026-01-26 host boundary.

The ordinary agent result remains the legacy text projection. Full FastMCP
``CallToolResult`` values (including private ``_meta``) enter this module only
through a dedicated observer and are retained in a bounded, session-local
registry. The transcript carries an opaque ``mcp_app`` capability reference;
the model and durable tool telemetry never receive private app metadata.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse

from clio_agent.gact.mcp_app_sandbox import (
    _SANDBOX_DOCUMENT as _SANDBOX_DOCUMENT,
)
from clio_agent.gact.mcp_app_sandbox import (
    _alternate_loopback_origin as _alternate_loopback_origin,
)
from clio_agent.gact.mcp_app_sandbox import (
    _csp_header as _csp_header,
)
from clio_agent.gact.mcp_app_sandbox import (
    _host_origin as _host_origin,
)
from clio_agent.gact.mcp_app_sandbox import (
    _request_origin as _request_origin,
)
from clio_agent.gact.mcp_app_sandbox import (
    _safe_sources as _safe_sources,
)
from clio_agent.gact.mcp_app_sandbox import (
    _sandbox_url as _sandbox_url,
)
from clio_agent.gact.routes._body import json_body
from clio_agent.gact.runtime.globals import (
    _gact_app_context,
    _resolve_tool_session,
    _tool_session_context,
)
from clio_agent.gact.turn_runner import session_busy_error_payload
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo, Part
from clio_agent.tools.mcp_results import (
    call_tool_result_to_observer as _call_tool_result_to_observer,
)
from clio_agent.tools.mcp_runtime import wire_value

if TYPE_CHECKING:
    from clio_agent.gact.routes.deps import GactDeps


MCP_APP_MIME_TYPE = "text/html;profile=mcp-app"
_REGISTRY_LIMIT = 64
_REGISTRY_TTL_S = 60 * 60
_MAX_PRIVATE_RESULT_BYTES = 1024 * 1024
_MAX_MODEL_CONTEXT_BYTES = 128 * 1024

logger = logging.getLogger(__name__)


def _mapping(value: Any) -> Mapping[str, Any]:
    """Return ``value`` as a mapping when possible."""

    if isinstance(value, Mapping):
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        dumped = dump(by_alias=True, exclude_none=True)
        if isinstance(dumped, Mapping):
            return dumped
    return {}


def _tool_ui(tool: Any) -> dict[str, Any]:
    """Extract stable nested MCP Apps tool metadata."""

    tool_map = _mapping(tool)
    meta = (
        getattr(tool, "meta", None)
        or getattr(tool, "_meta", None)
        or tool_map.get("_meta")
        or tool_map.get("meta")
    )
    meta_map = _mapping(meta)
    ui = _mapping(meta_map.get("ui"))
    if ui:
        return dict(ui)
    flat = meta_map.get("ui/resourceUri")
    return {"resourceUri": flat} if isinstance(flat, str) else {}


def _resource_uri(tool: Any) -> str:
    """Return a validated ``ui://`` resource URI from a tool definition."""

    uri = _tool_ui(tool).get("resourceUri")
    return str(uri) if isinstance(uri, str) and uri.startswith("ui://") else ""


def _tool_visible_to_app(tool: Any) -> bool:
    """Return whether an MCP tool is callable by its bound App."""

    visibility = _tool_ui(tool).get("visibility")
    if not isinstance(visibility, Sequence) or isinstance(visibility, (str, bytes)):
        return True
    return "app" in {str(item) for item in visibility}


def call_tool_result_to_wire(result: Any) -> dict[str, Any]:
    """Serialize a full FastMCP CallToolResult using stable MCP field names."""

    dumped = dict(_mapping(result))
    content = getattr(result, "content", dumped.get("content", [])) or []
    structured = getattr(
        result,
        "structured_content",
        dumped.get("structuredContent", dumped.get("structured_content")),
    )
    meta = getattr(result, "meta", dumped.get("_meta", dumped.get("meta")))
    is_error = getattr(result, "is_error", dumped.get("isError", dumped.get("is_error", False)))

    wire = {str(key): wire_value(value, mode="mcp_apps") for key, value in dumped.items()}
    wire.pop("structured_content", None)
    wire.pop("is_error", None)
    wire.pop("meta", None)
    wire["content"] = wire_value(content, mode="mcp_apps")
    if structured is not None:
        wire["structuredContent"] = wire_value(structured, mode="mcp_apps")
    if meta is not None:
        wire["_meta"] = wire_value(meta, mode="mcp_apps")
    if is_error:
        wire["isError"] = True
    return wire


def call_tool_result_to_observer(result: Any) -> dict[str, Any]:
    """Return the public MCP result fields safe for ordinary tool telemetry.

    MCP Apps may carry private ``_meta`` capability data.  The ordinary tool
    observer needs exact public ``structuredContent`` for durable execution
    evidence, but must never receive that private metadata.
    """

    return _call_tool_result_to_observer(result)


def read_resource_result_to_wire(result: Any) -> dict[str, Any]:
    """Serialize FastMCP's list-or-result resource response shape."""

    if isinstance(result, Sequence) and not isinstance(result, (str, bytes, bytearray)):
        contents = list(result)
    else:
        dumped = _mapping(result)
        contents = getattr(result, "contents", dumped.get("contents", [])) or []
    return {"contents": wire_value(contents, mode="mcp_apps")}


def _content_meta(content: Mapping[str, Any]) -> dict[str, Any]:
    meta = _mapping(content.get("_meta") or content.get("meta"))
    return dict(_mapping(meta.get("ui")))


def _resource_payload(result: Any) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """Return validated app HTML plus CSP and permissions metadata."""

    wire = read_resource_result_to_wire(result)
    contents = wire.get("contents")
    if not isinstance(contents, list) or len(contents) != 1:
        raise ValueError("MCP App resource must return exactly one content item")
    content = contents[0]
    if not isinstance(content, Mapping):
        raise ValueError("MCP App resource content must be an object")
    mime = str(content.get("mimeType") or content.get("mime_type") or "")
    if mime != MCP_APP_MIME_TYPE:
        raise ValueError(f"MCP App resource has unsupported MIME type {mime!r}")
    text = content.get("text")
    if not isinstance(text, str):
        raise ValueError("MCP App resource must contain text HTML")
    ui = _content_meta(content)
    csp = dict(_mapping(ui.get("csp")))
    permissions = dict(_mapping(ui.get("permissions")))
    return text, csp, permissions


def _private_cleanup(result_wire: Mapping[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """Extract an optional namespaced cleanup call from private result metadata."""

    meta = _mapping(result_wire.get("_meta"))
    for value in meta.values():
        namespace_meta = _mapping(value)
        cleanup = _mapping(namespace_meta.get("cleanup"))
        tool = cleanup.get("tool")
        arguments = cleanup.get("arguments", cleanup.get("args", {}))
        if isinstance(tool, str) and tool.strip() and isinstance(arguments, Mapping):
            return tool.strip(), dict(arguments)
    return None


@dataclass
class MCPAppRecord:
    """One private, session-bound MCP App instance."""

    app_instance_id: str
    data_ref: str
    session_id: str
    source_namespace: str | None
    tool_name: str
    resource_uri: str
    tool_input: dict[str, Any]
    tool_result: dict[str, Any]
    cleanup: tuple[str, dict[str, Any]] | None
    created_at: float = field(default_factory=time.monotonic)
    last_access: float = field(default_factory=time.monotonic)
    html: str | None = None
    csp: dict[str, Any] = field(default_factory=dict)
    permissions: dict[str, Any] = field(default_factory=dict)
    model_context: dict[str, Any] = field(default_factory=dict)


class MCPAppAdmissionError(RuntimeError):
    """An App result that cannot be exposed but may still require cleanup."""

    def __init__(self, message: str, record: MCPAppRecord) -> None:
        super().__init__(message)
        self.record = record


class MCPAppRegistry:
    """Bounded active-App registry plus a lossless cleanup ownership ledger."""

    def __init__(self) -> None:
        self._records: OrderedDict[str, MCPAppRecord] = OrderedDict()
        self._cleanup_records: OrderedDict[str, MCPAppRecord] = OrderedDict()
        self._lock = threading.RLock()

    def register(
        self,
        *,
        session_id: str,
        source_namespace: str | None,
        tool_name: str,
        resource_uri: str,
        tool_input: Mapping[str, Any],
        tool_result: Mapping[str, Any],
    ) -> MCPAppRecord:
        """Store one private result after enforcing size and count bounds."""

        namespace = source_namespace.strip() if isinstance(source_namespace, str) else ""
        encoded = json.dumps(tool_result, sort_keys=True, default=str).encode("utf-8")
        record = MCPAppRecord(
            app_instance_id=secrets.token_urlsafe(18),
            data_ref=secrets.token_urlsafe(32),
            session_id=session_id,
            source_namespace=namespace or None,
            tool_name=tool_name,
            resource_uri=resource_uri,
            tool_input=dict(tool_input),
            tool_result=dict(tool_result),
            cleanup=_private_cleanup(tool_result),
        )
        with self._lock:
            self._expire_locked()
            rejection: str | None = None
            if not namespace:
                rejection = "MCP App result has no exact originating server namespace"
            elif len(encoded) > _MAX_PRIVATE_RESULT_BYTES:
                rejection = "MCP App private result exceeds the 1 MiB admission limit"
            elif len(self._records) >= _REGISTRY_LIMIT:
                rejection = f"MCP App active-instance limit ({_REGISTRY_LIMIT}) has been reached"
            if rejection is not None:
                # The remote tool has already succeeded. Never silently discard
                # ownership metadata for a resource-bearing result: retain the
                # minimum cleanup identity until cleanup succeeds. Full private
                # payload bytes are not retained for a rejected UI admission.
                if record.cleanup is not None:
                    record.tool_input = {}
                    record.tool_result = {}
                    self._cleanup_records[record.app_instance_id] = record
                raise MCPAppAdmissionError(rejection, record)
            self._records[record.app_instance_id] = record
        return record

    def get(self, session_id: str, app_instance_id: str, data_ref: str) -> MCPAppRecord:
        """Resolve one record only when every capability binding matches."""

        with self._lock:
            self._expire_locked()
            record = self._records.get(app_instance_id)
            if (
                record is None
                or record.session_id != session_id
                or not secrets.compare_digest(record.data_ref, data_ref)
            ):
                raise KeyError(app_instance_id)
            record.last_access = time.monotonic()
            self._records.move_to_end(app_instance_id)
            return record

    def remove(self, app_instance_id: str) -> MCPAppRecord | None:
        """Remove and return an App record."""

        with self._lock:
            record = self._records.pop(app_instance_id, None)
            return record or self._cleanup_records.pop(app_instance_id, None)

    def drop_session(self, session_id: str) -> list[MCPAppRecord]:
        """Remove every App record owned by ``session_id``."""

        with self._lock:
            removed = [
                record
                for records in (self._records, self._cleanup_records)
                for record in records.values()
                if record.session_id == session_id
            ]
            for record in removed:
                self._records.pop(record.app_instance_id, None)
                self._cleanup_records.pop(record.app_instance_id, None)
            return removed

    def records_for_session(self, session_id: str) -> list[MCPAppRecord]:
        """Return the live App records owned by ``session_id``."""

        with self._lock:
            self._expire_locked()
            return [
                record
                for records in (self._records, self._cleanup_records)
                for record in records.values()
                if record.session_id == session_id
            ]

    def session_ids(self) -> list[str]:
        """Return every session with an active or cleanup-only App record."""

        with self._lock:
            self._expire_locked()
            return list(
                dict.fromkeys(
                    record.session_id
                    for records in (self._records, self._cleanup_records)
                    for record in records.values()
                )
            )

    def _expire_locked(self) -> None:
        deadline = time.monotonic() - _REGISTRY_TTL_S
        # A cleanup-bearing record represents an owned remote attachment. It
        # cannot be TTL-evicted because doing so would discard the only cleanup
        # identity. Records without cleanup semantics may expire normally.
        expired = [
            key
            for key, record in self._records.items()
            if record.cleanup is None and record.last_access < deadline
        ]
        for key in expired:
            self._records.pop(key, None)


def _not_found() -> HTTPException:
    """Return a non-oracular capability lookup error."""

    return HTTPException(
        status_code=404,
        detail=ErrorEnvelope(
            error=ErrorInfo(
                error="not_found",
                message="MCP App instance not found",
                recoverable=False,
            )
        ).model_dump(exclude_none=True),
    )


def _registry(app: FastAPI) -> MCPAppRegistry:
    registry = getattr(app.state, "mcp_app_registry", None)
    if not isinstance(registry, MCPAppRegistry):
        registry = MCPAppRegistry()
        app.state.mcp_app_registry = registry
    return registry


def _make_mcp_app_observer(app: FastAPI):
    """Build the raw-result observer installed beside durable telemetry."""

    def observe(
        name: str,
        args: Mapping[str, Any],
        tool: Any,
        raw_result: Any,
        source_namespace: str | None,
    ) -> None:
        uri = _resource_uri(tool)
        if not uri:
            return
        result_wire = call_tool_result_to_wire(raw_result)
        if result_wire.get("isError") is True:
            return
        sid, _session = _resolve_tool_session(app)
        if not sid:
            return
        try:
            record = _registry(app).register(
                session_id=sid,
                source_namespace=source_namespace,
                tool_name=name,
                resource_uri=uri,
                tool_input=args,
                tool_result=result_wire,
            )
        except MCPAppAdmissionError as exc:
            _schedule_rejected_cleanup(app, exc.record)
            raise

        # Import lazily to preserve the tools <- runtime <- gact direction and
        # avoid a module cycle during gact startup.
        from clio_agent.gact.tool_observer import (  # noqa: PLC0415
            _agent_tool_owner,
            _append_live_assistant_part,
        )

        _public, owner = _agent_tool_owner(app, name)
        _append_live_assistant_part(
            app,
            sid,
            Part(
                id=f"mcp_app_{record.app_instance_id}",
                type="mcp_app",
                agent_id=owner,
                app_instance_id=record.app_instance_id,
                resource_uri=record.resource_uri,
                source_server=record.source_namespace or "",
                data_ref=record.data_ref,
                mime_type=MCP_APP_MIME_TYPE,
                metadata={"stream_source": "live", "protocol": "2026-01-26"},
            ),
        )

    return observe


def install_mcp_app_runtime(app: FastAPI) -> None:
    """Install the per-app registry and private raw-result observer."""

    _registry(app)
    app.state.pending_mcp_app_observer = _make_mcp_app_observer(app)


def _bound_executor(app: FastAPI, sid: str) -> Any:
    agent = getattr(app.state, "agent", None)
    if agent is None:
        raise RuntimeError("agent is not available")
    resolver = getattr(agent, "_active_tool_executor", None)
    executor = resolver() if callable(resolver) else getattr(agent, "tool_executor", None)
    if executor is None:
        raise RuntimeError("agent tool executor is not available")
    # #1201: surface any recorded era downgrade for this executor's servers
    # into the session's semantic-event trace.
    from clio_agent.gact.mcp_connection_observability import (  # noqa: PLC0415
        emit_downgrade_events_for_executor,
    )

    emit_downgrade_events_for_executor(app, sid, executor)
    return executor


async def _run_bound(app: FastAPI, sid: str, operation: Any) -> Any:
    """Run a blocking executor operation in the exact session context."""

    def invoke() -> Any:
        with _gact_app_context(app), _tool_session_context(sid):
            return operation(_bound_executor(app, sid))

    return await asyncio.to_thread(invoke)


def _resolve_app_tool(executor: Any, record: MCPAppRecord, requested: str) -> str:
    """Resolve an app call without allowing cross-namespace tool access."""

    definitions = executor.get_all_tool_definitions()
    namespace = record.source_namespace
    if not namespace:
        raise PermissionError("MCP App has no exact originating server namespace; tool call denied")
    full_name = requested
    if not requested.startswith(f"{namespace}_"):
        full_name = f"{namespace}_{requested}"
    if not full_name.startswith(f"{namespace}_"):
        raise PermissionError("MCP App cannot call a different server namespace")
    tool = definitions.get(full_name)
    if tool is None or not _tool_visible_to_app(tool):
        raise PermissionError(f"tool {requested!r} is not exposed to this MCP App")
    return full_name


async def _cleanup_record(app: FastAPI, record: MCPAppRecord) -> None:
    """Run one declared App cleanup through the normal tool boundary."""

    if record.cleanup is None:
        return
    cleanup_tool, cleanup_args = record.cleanup

    def cleanup(executor: Any) -> Any:
        full_name = _resolve_app_tool(executor, record, cleanup_tool)
        return executor.call_tool_result(full_name, cleanup_args)

    result = await _run_bound(app, record.session_id, cleanup)
    if call_tool_result_to_wire(result).get("isError") is True:
        raise RuntimeError(f"MCP App cleanup tool {cleanup_tool!r} returned an error result")


def _schedule_rejected_cleanup(app: FastAPI, record: MCPAppRecord) -> None:
    """Immediately clean a post-call result whose UI admission was rejected.

    The compact cleanup record remains in the registry until this succeeds. If
    no running host loop is available, normal session/process teardown retries
    it instead of losing the ownership identity.
    """

    if record.cleanup is None:
        return
    loop = getattr(app.state, "mcp_app_loop", None)
    if not isinstance(loop, asyncio.AbstractEventLoop) or not loop.is_running():
        logger.error(
            "MCP App admission rejected; cleanup retained for teardown app_instance_id=%s",
            record.app_instance_id,
        )
        return

    async def attempt() -> None:
        try:
            await _cleanup_record(app, record)
        except Exception:  # noqa: BLE001 - retain ownership and retry on teardown
            logger.exception(
                "MCP App rejected-admission cleanup failed; retained app_instance_id=%s",
                record.app_instance_id,
            )
        else:
            _registry(app).remove(record.app_instance_id)

    asyncio.run_coroutine_threadsafe(attempt(), loop)


async def cleanup_session_mcp_apps(app: FastAPI, session_id: str) -> None:
    """Close every App owned by a session before destroying that session.

    Each cleanup runs through the same namespace routing, permission gate,
    observers, and session context as an ordinary tool call. Successfully
    closed records are removed immediately; failed records remain available so
    the operator can retry instead of silently leaking the remote attachment.
    """

    registry = _registry(app)
    failures: list[str] = []
    for record in registry.records_for_session(session_id):
        try:
            await _cleanup_record(app, record)
        except Exception as exc:  # noqa: BLE001 - attempt every owned App cleanup
            failures.append(f"{record.app_instance_id}: {exc}")
        else:
            registry.remove(record.app_instance_id)
    if failures:
        raise RuntimeError("; ".join(failures))


async def cleanup_all_mcp_apps(app: FastAPI) -> None:
    """Close every retained App before the host tears down MCP transports."""

    registry = _registry(app)
    failures: list[str] = []
    for session_id in registry.session_ids():
        try:
            await cleanup_session_mcp_apps(app, session_id)
        except RuntimeError as exc:
            failures.append(f"{session_id}: {exc}")
    if failures:
        raise RuntimeError("; ".join(failures))


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
    async def get_mcp_app(sid: str, app_id: str, data_ref: str, request: Request) -> dict[str, Any]:
        record = resolve(sid, app_id, data_ref)
        try:
            await ensure_resource(record)
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        sandbox_path = f"/v1/sessions/{sid}/mcp-apps/{app_id}/sandbox?data_ref={data_ref}"
        return {
            "protocol_version": "2026-01-26",
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
        }

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
        return {}

    @app.post("/v1/sessions/{sid}/mcp-apps/{app_id}/messages")
    async def post_mcp_app_message(
        sid: str, app_id: str, data_ref: str, request: Request
    ) -> dict[str, Any]:
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
        session = app.state.sessions.get(sid)
        if session is None:
            raise _not_found()
        # #948 S1: the MCP App is a turn producer too — gate it through the canonical
        # within-session busy check (the actual in-flight task), not a status
        # projection, so every producer refuses a concurrent turn identically.
        busy_payload = session_busy_error_payload(getattr(app.state, "turn_runner", None), sid)
        if busy_payload is not None:
            raise HTTPException(status_code=409, detail=busy_payload)

        model_context = record.model_context
        effective_text = text
        if model_context:
            effective_text = (
                f"{text}\n\nMCP App context from {record.source_namespace or 'server'}:\n"
                f"{json.dumps(model_context, sort_keys=True, default=str)}"
            )
        message = deps.start_background_user_turn(
            sid,
            session,
            effective_text,
            request_parts=[Part(type="text", text=text)],
            metadata={
                "mcp_app": {
                    "app_instance_id": record.app_instance_id,
                    "source_server": record.source_namespace or "",
                    "resource_uri": record.resource_uri,
                }
            },
            prev_status=str(getattr(session, "status", "idle")),
        )
        return {"message_id": message.id}

    @app.delete("/v1/sessions/{sid}/mcp-apps/{app_id}", status_code=204)
    async def close_mcp_app(sid: str, app_id: str, data_ref: str) -> None:
        record = resolve(sid, app_id, data_ref)
        try:
            await _cleanup_record(app, record)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        _registry(app).remove(app_id)
        return None


__all__ = [
    "MCP_APP_MIME_TYPE",
    "MCPAppAdmissionError",
    "MCPAppRegistry",
    "call_tool_result_to_observer",
    "call_tool_result_to_wire",
    "cleanup_all_mcp_apps",
    "cleanup_session_mcp_apps",
    "install_mcp_app_runtime",
    "read_resource_result_to_wire",
    "register_mcp_app_routes",
]
