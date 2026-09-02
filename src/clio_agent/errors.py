"""
ClioAgent Structured Error Handling

Structured error types with JSON-serializable output.
Raw tracebacks never reach the user -- all failures are represented as
structured errors instead of fallback assistant responses.

Error hierarchy:
    ClioError (base)
    ├── ProviderError  -- LM provider unavailable/timeout
    ├── RoutingError   -- Router failed to classify
    ├── ExpertError    -- Expert execution failed
    ├── ToolError      -- MCP tool call failed
    ├── ConfigError    -- Configuration invalid
    └── CancellationError -- User-requested cancellation
"""

from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)

MCP_CAPABILITY_REFUSED = "mcp_capability_refused"
MCP_PROTOCOL_REFUSED = "mcp_protocol_refused"
MCP_RESULT_DOWNGRADED_TO_COMPLETE = "mcp_result_downgraded_to_complete"
MCP_WIRE_CANCELLATION_UNAVAILABLE = "mcp_wire_cancellation_unavailable"
#: #1114: the modern-era MRTR loop (InputRequiredResult -> retry with inputResponses)
#: exceeded its config-resolved round bound without reaching a terminal result.
MCP_INPUT_REQUIRED_ROUNDS_EXCEEDED = "mcp_input_required_rounds_exceeded"
#: #1115: no durable task-record home is published, so SEP-2663 task ids survive
#: losing the client but not the process (reconnect-after-crash is degraded).
MCP_TASK_RECORD_STORE_ABSENT = "mcp_task_record_store_absent"
#: #1115: a task kept reporting ``input_required`` while every surfaced input key
#: was already answered — the drive stops rather than polling forever.
MCP_TASK_INPUT_NO_PROGRESS = "mcp_task_input_no_progress"
#: #1115: the tasks extension was NOT declared on this client because its class
#: forbids internal extensions (a proxy backend must not advertise task support).
MCP_TASKS_DECLARATION_SUPPRESSED = "mcp_tasks_declaration_suppressed"
#: #1115: another live driver already holds the lease on this task, so this driver
#: refused to poll/answer it rather than double-prompt and double-update.
MCP_TASK_LEASE_HELD = "mcp_task_lease_held"
#: #1115: the durable write for a task record failed (its session row vanished, or
#: the registry rejected the update), so the record was moved to the process-local
#: holding path — still resumable/cancellable here, but not across a restart.
MCP_TASK_RECORD_HELD_LOCALLY = "mcp_task_record_held_locally"
#: #1115: the session owning live tasks was deleted. Deletion is never blocked; the
#: records are cancel-requested best-effort and migrated to the holding path.
MCP_TASK_SESSION_DELETED = "mcp_task_session_deleted"
#: #1115: a task exists on the server but its id could NOT be made durable. The
#: caller gets this reason plus the taskId and backend identity so the orphan can be
#: reconciled by hand (SEP-2663 has no tasks/list to rediscover it).
MCP_TASK_RECORD_NOT_DURABLE = "mcp_task_record_not_durable"
#: #1231: a backend's registered ``on_poll`` observer factory
#: (``tools/task_observers.py``) raised while being resolved for one task drive.
#: The factory is downgraded to absent (as if nothing were registered) rather than
#: breaking the drive it was about to observe -- mirrors relay_console's own
#: never-break-the-wait discipline one layer up, for the generic registry seam.
MCP_TASK_OBSERVER_FACTORY_FAILED = "mcp_task_observer_factory_failed"
#: #1201: an execution-path client resolved ``tools.mcp.connect_mode=auto`` but
#: negotiated the LEGACY era anyway (the #1186 race: a slow first response burns
#: the modern probe and its one re-probe, so the client falls back to the
#: ``initialize`` handshake even though client and server both speak 2026-07-28).
#: Recorded ONLY under auto mode; a pinned mode (an explicit version or the
#: literal ``"legacy"``) is operator intent and never emits this.
MCP_PROTOCOL_DOWNGRADED_TO_LEGACY = "mcp_protocol_downgraded_to_legacy"
#: #1201: an ``mcp.yaml`` declaration file exists but could not be read/parsed
#: (OS error or malformed YAML). Previously swallowed into ``{}`` -- silently
#: treating "malformed" the same as "no servers declared". A MISSING file is
#: still normal and returns ``{}`` without this reason.
MCP_YAML_DECLARATION_UNREADABLE = "mcp_yaml_declaration_unreadable"
#: #1232 pt 2: a declared MCP namespace did not answer its bounded discovery
#: attempt (connect + list_tools) before the per-namespace deadline. NEVER
#: blocks "agent ready" -- the namespace's tools are simply absent from the
#: catalog until a background re-probe heals it (MCP_NAMESPACE_DISCOVERY_HEALED).
MCP_NAMESPACE_DISCOVERY_TIMEOUT = "mcp_namespace_discovery_timeout"
#: #1232 pt 2: a declared MCP namespace's discovery attempt raised (spawn
#: failure, connection refused, malformed response, ...) rather than timing
#: out. Same non-blocking/heals-in-background contract as the timeout reason.
MCP_NAMESPACE_DISCOVERY_UNREACHABLE = "mcp_namespace_discovery_unreachable"
#: #1232 pt 2: a previously-degraded namespace (either reason above) answered
#: on a background re-probe; its tools are now merged into the live catalog.
MCP_NAMESPACE_DISCOVERY_HEALED = "mcp_namespace_discovery_healed"
#: #1232 pt 3 / #1237 hotfix: a stdio MCP launcher (uv/uvx) waited for the
#: dedicated launcher cache lock past its GENEROUS runaway backstop (default
#: 10 minutes). #1237 owner ruling: this is never the normal path -- a lock
#: held by a live process is waited out no matter how long the holder's
#: legitimate work (a cold uv env build on a slow/NFS filesystem) takes; only
#: a livelocked or unidentifiable holder ever reaches this bound. Feeds the
#: same background re-probe as a discovery degrade.
LAUNCHER_CACHE_LOCK_TIMEOUT = "launcher_cache_lock_timeout"
#: #1237 hotfix: a launcher-cache lock's recorded holder PID was confirmed
#: dead (the process that took the lock is gone), so the lock was an
#: abandoned/stale artifact rather than real contention. It was broken
#: (removed) so acquisition could proceed immediately -- never silently, and
#: never left to block a live waiter for its full runaway backstop.
LAUNCHER_CACHE_LOCK_STALE_BROKEN = "launcher_cache_lock_stale_broken"
#: #1237 hotfix: a declared MCP namespace's server never mounted for a
#: workspace's resident tool fleet (any degrade reason above). Recorded on
#: the executor so a declared tool that resolves to this namespace can name
#: the SERVER and the typed reason in its unavailability error, instead of
#: silently vanishing from the model's tool list with no explanation
#: (gact/agents/toolset_inventory.py::record_tool_unavailable).
MCP_NAMESPACE_MOUNT_FAILED = "mcp_namespace_mount_failed"
#: #1232 pt 4: the boot process-census killed a PROVABLY-orphaned clio-launched
#: child process (dead parent PID + clio launcher identity) instead of only
#: reporting it. The shared clio-core CTE daemon is excluded by construction
#: (see runtime/process_census.py) and never matches this reason.
PROCESS_CENSUS_ORPHAN_REAPED = "process_census_orphan_reaped"
#: #1281 (C1-S1): a namespace connect used the DIRECT task-declaring client
#: route because ``tools/mcp_connection_era.latest_task_capability`` had
#: already recorded this server as task-capable -- the unlock for the #1274
#: defect (every declared server previously suppressed the tasks extension
#: via the proxy path's ``ProxyClient``). Never fires for a v1 server.
MCP_TASKS_DIRECT_ROUTE_SELECTED = "mcp_tasks_direct_route_selected"
#: #1281 (C1-S1): a namespace connect kept today's proxy path because no
#: capability discovery has landed for this server yet (``latest_task_capability``
#: returned ``None``) -- the safe default until a listing pass
#: (``gateway._list_declared_tools``) or an opportunistic real-backend connect
#: (``mcp_connection_era.instrument_client_era``) records a verdict.
#: Self-heals on the next discovery pass; never a permanent classification.
MCP_TASK_CAPABILITY_UNKNOWN = "mcp_task_capability_unknown"
#: #1281 (C1-S1, adversarial-review F4): capability discovery recorded this
#: namespace task-capable, but NO direct-client factory was ever threaded
#: onto this executor (a reserved-namespace mount, or a construction path
#: that predates the C1-S1 stamping) -- the call still routes through the
#: proxy, but this is NEVER silent: the ring records the DECISION ACTUALLY
#: TAKEN (proxy), typed with this reason, rather than the unreachable intent.
MCP_TASK_DIRECT_FACTORY_MISSING = "mcp_task_direct_factory_missing"
#: #1281 (C1-S1, adversarial-review F9): a direct-client factory WAS present
#: and selected, but invoking it raised (e.g. ``transport_for`` refusing an
#: ``MCPSpawnError``-shaped spec at call time). The call falls back to the
#: proxy path -- which the server's own capability declaration proves can
#: still serve it -- rather than hard-failing a call the proxy would serve.
MCP_TASK_DIRECT_FACTORY_CONSTRUCTION_FAILED = "mcp_task_direct_factory_construction_failed"
#: #1281 (C1-S1, adversarial-review F2): a namespace's PERSISTENT client was
#: connected while capability was unknown/False (proxy path), and a LATER
#: discovery pass landed it True -- the stale proxy client is evicted and
#: reconnected through the direct route. Bound STRICTLY to the unknown/False
#: -> direct flip (never the reverse), so a healthy direct connection can
#: never be evicted/thrashed back to the suppressing proxy path.
MCP_TASK_ROUTE_HEALED = "mcp_task_route_healed"
#: #1281 (C1-S1, adversarial-review F7): a task-capability verdict recorded
#: True from the AUTHORITATIVE modern key (``capabilities_extensions``) was
#: legitimately overwritten by an equally-authoritative (modern-negotiated)
#: False -- a real capability demotion (e.g. a server that removed the
#: tasks extension), not a downgrade artifact. Queryable so a demotion is
#: never a silent fact.
MCP_TASK_CAPABILITY_DEMOTED = "mcp_task_capability_demoted"
#: #1281 (C1-S1, adversarial-review F7): an attempted False verdict was
#: REFUSED because it was not equally authoritative as the True verdict it
#: would have overwritten (e.g. a legacy-negotiated read -- possibly just
#: the #1186 downgrade race on a genuinely modern, task-capable server --
#: attempting to clobber a prior ``capabilities_extensions`` True). The
#: existing True record is kept; this reason makes the refusal queryable
#: rather than a silently dropped write.
MCP_TASK_CAPABILITY_DEMOTION_REFUSED = "mcp_task_capability_demotion_refused"


class ClioError(Exception):
    """Base error for all CLIO Agent errors.

    All CLIO errors serialize to a structured dict with error_type,
    message, and optional details. Raw tracebacks never reach users.

    Attributes:
        message: Human-readable error description
        error_type: Machine-readable error category
        details: Additional context (never raw tracebacks)
    """

    def __init__(
        self,
        message: str,
        error_type: str = "clio_error",
        details: dict[str, Any] | None = None,
    ) -> None:
        self.message = message
        self.error_type = error_type
        self.details = details or {}
        super().__init__(message)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to structured JSON-compatible dict.

        Returns:
            Dict with error, message, and details keys.
        """
        return {
            "error": self.error_type,
            "message": self.message,
            "details": self.details,
        }


class ProviderError(ClioError):
    """LM provider unavailable, timeout, or authentication failure."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, error_type="provider_error", details=details)


class RoutingError(ClioError):
    """Router failed to classify query to an expert."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, error_type="routing_error", details=details)


class ExpertError(ClioError):
    """Expert execution failed (data, analysis, or visualization)."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, error_type="expert_error", details=details)


class ToolError(ClioError):
    """MCP tool call failed."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, error_type="tool_error", details=details)


class MCPProtocolError(ToolError):
    """Base for typed MCP JSON-RPC capability/protocol refusals."""

    def __init__(
        self,
        message: str,
        *,
        code: int,
        reason: str,
        error_type: str,
        protocol_data: Any = None,
    ) -> None:
        self.code = code
        self.reason = reason
        self.protocol_data = protocol_data
        ClioError.__init__(
            self,
            message,
            error_type=error_type,
            details={
                "reason": reason,
                "json_rpc_code": code,
                "protocol_data": protocol_data,
            },
        )


def _required_extensions_hint(protocol_data: Any) -> str:
    """#1282 D2: extract the -32021 ``requiredCapabilities.extensions`` id list.

    ``protocol_data`` is the raw JSON-RPC error ``data`` payload
    (``{"requiredCapabilities": {"extensions": {"<id>": {...}, ...}}}``, per
    SEP-2663/``fastmcp_tasks.models``) -- a plain mapping, never a model class,
    so this reads defensively and returns ``""`` (no hint appended) on any
    shape that does not match rather than guessing.
    """

    if not isinstance(protocol_data, dict):
        return ""
    required = protocol_data.get("requiredCapabilities")
    extensions = required.get("extensions") if isinstance(required, dict) else None
    if not isinstance(extensions, dict) or not extensions:
        return ""
    names = ", ".join(sorted(str(name) for name in extensions))
    return f" Re-dial declaring the client extension(s): {names}."


def _supported_versions_hint(protocol_data: Any) -> str:
    """#1282 D2: extract the -32022 ``supportedVersions`` list."""

    if not isinstance(protocol_data, dict):
        return ""
    versions = protocol_data.get("supportedVersions")
    if not isinstance(versions, list) or not versions:
        return ""
    named = ", ".join(str(v) for v in versions)
    return f" Re-dial negotiating one of the server's supported protocol version(s): {named}."


def _with_actionable_hint(message: Any, hint_fn: Callable[[Any], str], protocol_data: Any) -> str:
    """Append a D2 actionable hint to ``message``, defensively (#1282 F13).

    ``message`` SHOULD always be a ``str`` (every raw MCP error message this
    codebase builds one from is), but this runs INSIDE exception
    construction — a ``TypeError`` raised here (a non-str ``message``, or a
    ``hint_fn`` that raises on a malformed ``protocol_data`` shape) would
    mask the original protocol refusal entirely with an unrelated crash.
    Coerces defensively and never lets either failure escape. A typed DEBUG
    log distinguishes "hint build failed unexpectedly" from the normal
    not-applicable case (``hint_fn`` returning ``""``, which logs nothing —
    that is not a failure, most refusals simply carry no matching payload
    shape).
    """

    base = message if isinstance(message, str) else str(message)
    try:
        hint = hint_fn(protocol_data)
    except Exception as exc:  # noqa: BLE001 - constructing an exception must never itself raise
        logger.debug(
            "mcp protocol refusal actionable-hint build failed "
            "reason=mcp_refusal_hint_build_failed: %r",
            exc,
        )
        return base
    return base + hint


class MCPMissingRequiredClientCapabilityError(MCPProtocolError):
    """The MCP server refused a request because a required client capability is absent.

    #1282 (C1-S2 D2): the message carries the -32021 payload's own
    ``requiredCapabilities.extensions`` naming exactly what to re-dial with —
    every typed refusal carries what-to-do-next semantics wherever ``message``
    is rendered (a child result, a tool-error observation, a user-facing
    string), with no per-call-site changes needed.
    """

    def __init__(self, message: str, protocol_data: Any = None) -> None:
        super().__init__(
            _with_actionable_hint(message, _required_extensions_hint, protocol_data),
            code=-32021,
            reason=MCP_CAPABILITY_REFUSED,
            error_type="mcp_missing_required_client_capability",
            protocol_data=protocol_data,
        )


class MCPUnsupportedProtocolVersionError(MCPProtocolError):
    """The MCP server refused the negotiated protocol version.

    #1282 (C1-S2 D2): the message carries the -32022 payload's own
    ``supportedVersions`` naming exactly what to re-dial with.
    """

    def __init__(self, message: str, protocol_data: Any = None) -> None:
        super().__init__(
            _with_actionable_hint(message, _supported_versions_hint, protocol_data),
            code=-32022,
            reason=MCP_PROTOCOL_REFUSED,
            error_type="mcp_unsupported_protocol_version",
            protocol_data=protocol_data,
        )


class MCPInputRequiredRoundsExceededError(ToolError):
    """The modern-era MRTR loop exceeded its round bound without terminating (#1114).

    A server kept returning ``InputRequiredResult`` past the config-resolved
    ``tools.mcp.input_required_max_rounds``. Surfaced by the executor as a TYPED
    degrade (``reason`` in the advertised x_clio_stream_fallback_reasons catalog)
    instead of the raw SDK ``InputRequiredRoundsExceededError``, so the model sees
    a typed, recoverable tool error rather than a traceback.
    """

    def __init__(self, max_rounds: int, tool: str = "") -> None:
        self.reason = MCP_INPUT_REQUIRED_ROUNDS_EXCEEDED
        self.max_rounds = max_rounds
        super().__init__(
            f"MCP tool {tool!r} exceeded the input-required round bound ({max_rounds}): "
            "the server kept requesting input without completing. Raise "
            "tools.mcp.input_required_max_rounds or fix the server.",
            details={
                "reason": MCP_INPUT_REQUIRED_ROUNDS_EXCEEDED,
                "max_rounds": max_rounds,
                "tool": tool,
            },
        )


class ConfigError(ClioError):
    """Configuration invalid or missing."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, error_type="config_error", details=details)


class CancellationError(ClioError):
    """User-requested cancellation observed by a cooperative execution path."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, error_type="cancelled", details=details)


def format_error_response(error: Exception) -> dict[str, Any]:
    """Format any exception as a structured JSON-compatible response.

    For ClioError subclasses, returns the structured to_dict() output.
    For all other exceptions, returns a generic internal_error response
    that never exposes raw tracebacks.

    Args:
        error: Any exception instance

    Returns:
        Structured error dict with error, message, and details keys.
    """
    if isinstance(error, ClioError):
        return error.to_dict()
    return {
        "error": "internal_error",
        "message": "An internal error occurred",
        "details": {},
    }
