"""Shared helpers for MCP runtime pathways.

Three historical wire contracts are preserved as explicit :func:`wire_value`
modes: ``mcp_results``, ``mcp_apps``, and ``gact_runtime``. Converging those
contracts is future wire-work, not part of this slice.

This module also owns :func:`make_mcp_client` (#1106) — the ONE construction
site for **execution-path** FastMCP clients. It carries the :class:`MCPClientHandlers`
slot (typed CLIO hooks; see :mod:`clio_agent.tools.mcp_handlers`) where P1
attaches elicitation/progress/message/cancellation handlers (no-op-absent
today). Execution paths route through it: the ``AsyncMCPToolExecutor`` default
``client_factory``, the gateway proxy backend (``tools/gateway._proxy_for_spec``),
the dynamic-agent external tool call (``gact/agents/builders``), the per-call
dispatch in ``gact/routes/mcp.py``, and the ``providers/handshake/mcp.py`` probe.

**The execution/introspection split (adopted default):** handlers wire on
execution paths only (paths that ``call_tool`` / dispatch a proxy backend), so
**list-only introspection sites do NOT migrate** and keep their bare
``Client()`` — the catalog/blueprint/status/gateway-listing passes
(``routes/catalog.py``, ``routes/blueprints.py``, ``runtime/status.py``,
``tools/gateway.list_gateway_tools``) plus the install/reconnect/inventory
``list_tools`` passes in ``routes/mcp.py``. They never dispatch a tool call, so
they never need the handler slot; forcing them through the factory would be pure
churn.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from clio_agent.tools.mcp_handlers import (
    ElicitationDispatcher,
    MCPClientCapabilities,
    MessageMultiplexer,
    ProgressDispatcher,
)

if TYPE_CHECKING:
    from mcp.client.auth.oauth2 import OAuthClientProvider
    from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

    from clio_agent.tools.mcp_config import MCPAuthConfig
    from clio_agent.tools.mcp_handlers import (
        ElicitationHook,
        MessageHook,
        ProgressHook,
    )

logger = logging.getLogger(__name__)

#: Cache of ``ClientSession`` subclasses keyed by ``(base_session_class, form,
#: url)``, so each distinct (base, declaration) pair yields exactly one session
#: class (see :func:`_capability_session_class`).
_CAPABILITY_SESSION_CLASSES: dict[tuple[type, bool, bool], type] = {}

WireMode = Literal["mcp_results", "mcp_apps", "gact_runtime"]

_MISSING = object()
_VALID_MODES: frozenset[str] = frozenset(("mcp_results", "mcp_apps", "gact_runtime"))


class _MemoryOAuthTokenStorage:
    """Process-local implementation of the MCP SDK ``TokenStorage`` protocol."""

    def __init__(self) -> None:
        self._tokens: OAuthToken | None = None
        self._client_info: OAuthClientInformationFull | None = None

    async def get_tokens(self) -> OAuthToken | None:
        """Return the current OAuth token bundle."""
        return self._tokens

    async def set_tokens(self, tokens: OAuthToken) -> None:
        """Replace the current OAuth token bundle."""
        self._tokens = tokens

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        """Return dynamically registered OAuth client information."""
        return self._client_info

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        """Replace dynamically registered OAuth client information."""
        self._client_info = client_info


def _oauth_provider_from_config(
    server_url: str, config: MCPAuthConfig | None
) -> OAuthClientProvider | None:
    """Build the installed MCP SDK OAuth provider at the client factory boundary.

    Invalid metadata is surfaced as a typed, credential-free transport error.
    The default storage is intentionally process-local; callers that need durable
    refresh tokens provide an SDK ``TokenStorage`` implementation in the auth block.
    """
    if config is None:
        return None

    from mcp.client.auth.oauth2 import OAuthClientProvider  # noqa: PLC0415
    from mcp.shared.auth import OAuthClientMetadata  # noqa: PLC0415
    from pydantic import ValidationError  # noqa: PLC0415

    from clio_agent.tools.mcp_config import MCPTransportError  # noqa: PLC0415

    try:
        metadata = (
            config.client_metadata
            if isinstance(config.client_metadata, OAuthClientMetadata)
            else OAuthClientMetadata.model_validate(config.client_metadata)
        )
        return OAuthClientProvider(
            server_url=server_url,
            client_metadata=metadata,
            storage=config.storage or _MemoryOAuthTokenStorage(),
            redirect_handler=config.redirect_handler,
            callback_handler=config.callback_handler,
            client_metadata_url=config.client_metadata_url,
        )
    except (TypeError, ValueError, ValidationError):
        raise MCPTransportError("invalid MCP OAuth configuration") from None


def _dump_mcp_results_model(value: Any, *, exclude_none: bool) -> Any:
    """Return the historical MCP-results Pydantic projection when available."""

    dump = getattr(value, "model_dump", None)
    if not callable(dump):
        return _MISSING
    attempts: tuple[dict[str, Any], ...] = (
        {"mode": "json", "by_alias": True, "exclude_none": exclude_none},
        {"by_alias": True, "exclude_none": exclude_none},
        {},
    )
    for kwargs in attempts:
        try:
            return dump(**kwargs)
        except TypeError:
            continue
    return _MISSING


def wire_value(
    value: Any,
    *,
    mode: WireMode,
    exclude_none: bool = False,
) -> Any:
    """Convert an SDK or Pydantic value using an explicit historical contract.

    Args:
        value: Value to recursively convert to plain wire data.
        mode: Historical contract to preserve. ``mcp_results`` uses JSON-mode,
            alias-preserving Pydantic dumps; ``mcp_apps`` uses its Python-mode,
            model-None-excluding behavior; ``gact_runtime`` preserves the
            runtime trace's tuple and sorted-set handling.
        exclude_none: Mapping and Pydantic-field filter used only by the
            ``mcp_results`` contract.

    Returns:
        A recursively converted value matching the selected historical output.

    Raises:
        ValueError: If ``mode`` does not name a preserved wire contract.
    """

    if mode not in _VALID_MODES:
        raise ValueError(f"unknown MCP wire mode: {mode!r}")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value

    if mode == "mcp_results":
        if isinstance(value, Mapping):
            return {
                str(key): wire_value(item, mode=mode, exclude_none=exclude_none)
                for key, item in value.items()
                if not (exclude_none and item is None)
            }
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return [wire_value(item, mode=mode, exclude_none=exclude_none) for item in value]
        dumped = _dump_mcp_results_model(value, exclude_none=exclude_none)
        if dumped is not _MISSING:
            return wire_value(dumped, mode=mode, exclude_none=exclude_none)
        return str(value)

    if mode == "mcp_apps":
        if isinstance(value, Mapping):
            return {str(key): wire_value(item, mode=mode) for key, item in value.items()}
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return [wire_value(item, mode=mode) for item in value]
        dump = getattr(value, "model_dump", None)
        if callable(dump):
            return wire_value(dump(by_alias=True, exclude_none=True), mode=mode)
        return str(value)

    if isinstance(value, Mapping):
        return {str(key): wire_value(item, mode=mode) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [wire_value(item, mode=mode) for item in value]
    if isinstance(value, set):
        return sorted(wire_value(item, mode=mode) for item in value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return wire_value(model_dump(exclude_none=True), mode=mode)
        except TypeError:
            return wire_value(model_dump(), mode=mode)
    return str(value)


@dataclass(frozen=True)
class MCPClientHandlers:
    """Typed CLIO hook bundle — a CONSTRUCTION-TIME SLOT, not a live wiring.

    Every hook is absent today (``None`` => that handler is not installed,
    identical to a bare client). The hooks are typed
    :mod:`clio_agent.tools.mcp_handlers` Protocols, not raw callbacks: each
    receives an :class:`MCPInvocationContext` first argument that P1 will
    populate. ``make_mcp_client`` wraps a populated hook in a signature adapter
    and hands it to the matching ``fastmcp.Client`` keyword; ``message`` becomes
    a :class:`MessageMultiplexer` that forwards to the CLIO hook. FastMCP 4
    handles task notifications through client extensions. Cancellation is an
    outbound call-lifecycle operation, not a ``Client`` callback keyword: #1116
    cancels the active ``call_tool`` task so MCP's dispatcher emits the protocol
    request id it allocated. The ``cancellation`` hook remains reserved for a
    future server-originated cancellation policy.

    IMPORTANT: no hook may actually be *wired* until correlation-by-protocol-
    identity lands (clio-agent#1111/#1113). See the ``mcp_handlers`` module
    docstring for the two deferred review findings the P1 implementer must honor.
    """

    elicitation: "ElicitationHook | None" = None
    progress: "ProgressHook | None" = None
    message: "MessageHook | None" = None
    cancellation: "MessageHook | None" = None


def input_required_max_rounds() -> int:
    """Resolve the MRTR round bound (#1114) — config, else the SDK default.

    Bounds the modern-era ``InputRequiredResult`` -> retry-with-``inputResponses``
    loop on every execution-path client. Config key
    ``tools.mcp.input_required_max_rounds`` / env ``CLIO_MCP_INPUT_REQUIRED_MAX_ROUNDS``;
    the default matches the mcp SDK's ``DEFAULT_INPUT_REQUIRED_MAX_ROUNDS`` (10) so a
    server never loops unbounded and exhaustion is a typed, config-tunable degrade.
    """

    from mcp.client._input_required import DEFAULT_INPUT_REQUIRED_MAX_ROUNDS  # noqa: PLC0415

    from clio_agent import conf  # noqa: PLC0415
    from clio_agent.errors import ConfigError  # noqa: PLC0415

    rounds = conf.resolve(
        "tools.mcp.input_required_max_rounds",
        env="CLIO_MCP_INPUT_REQUIRED_MAX_ROUNDS",
        default=DEFAULT_INPUT_REQUIRED_MAX_ROUNDS,
        cast=conf.as_int,
    )
    if rounds < 1:
        # A bound below 1 makes the SDK driver report exhaustion BEFORE dispatching the
        # first input request: ONE legitimate modern-era input request would be
        # misreported as server non-termination, silently disabling HITL. Reject the
        # config at client construction rather than degrade at call time.
        raise ConfigError(
            f"tools.mcp.input_required_max_rounds must be >= 1, got {rounds}: a bound "
            "below 1 disables server-initiated input (MRTR) instead of bounding it.",
            details={
                "key": "tools.mcp.input_required_max_rounds",
                "env": "CLIO_MCP_INPUT_REQUIRED_MAX_ROUNDS",
                "value": rounds,
                "minimum": 1,
            },
        )
    return rounds


def clio_client_info() -> Any:
    """CLIO's client identity, stamped into every execution-path request's ``_meta``.

    The 2026-07-28 revision removed the ``initialize`` handshake: the SDK session
    now stamps per-request ``_meta`` carrying ``clientInfo`` + ``clientCapabilities``
    on every call. FastMCP defaults ``clientInfo`` to ``name='mcp'``/``version='0.1.0'``;
    this declares CLIO's true identity instead so a downstream MCP server can see
    *who* is calling. Client **capabilities** are a separate, explicit concern —
    see :class:`clio_agent.tools.mcp_handlers.MCPClientCapabilities` and the
    ``capabilities`` argument of :func:`make_mcp_client` — decoupled from handler
    wiring so each modeled domain is advertised exactly as declared (elicitation
    pinned absent today; unmodeled domains stay truthfully wiring-derived).
    """

    from mcp.types import Implementation  # noqa: PLC0415

    from clio_agent import __version__  # noqa: PLC0415

    return Implementation(name="clio-agent", title="CLIO Agent", version=__version__)


class _DeclaredCapabilityOverride:
    """Mixin that makes CLIO's declared client capabilities authoritative PER DOMAIN.

    Composed in front of the session class already in effect (plain
    ``ClientSession`` on a direct client, ``_ForwardingClientSession`` on a proxy
    backend) by :func:`_capability_session_class`, so ``super()`` resolves to that
    base's ``_build_capabilities``. The declared elicitation capability is stored
    on the composed class as ``_clio_declared_elicitation``.

    Only the ELICITATION domain is overridden — the one domain CLIO's declaration
    models. Every other capability the base advertises (sampling / roots / log) is
    left untouched: it is the base's truthful wiring-derived value (a direct client
    advertises only what is wired; a proxy backend genuinely forwards sampling/roots
    to the front), and blanking it would sever proxy push-forwarding. A future slice
    that models another domain overrides that field here too.
    """

    _clio_declared_elicitation: Any = None

    def _build_capabilities(self, version: str) -> Any:
        caps = super()._build_capabilities(version)  # type: ignore[misc]
        # Authoritative for elicitation ONLY: pin the declared modes (the SDK
        # otherwise hardcodes both form+url whenever an elicitation callback is
        # wired). ``None`` here pins elicitation absent. All other domains keep the
        # base's value — clearing them would break proxy sampling/roots forwarding.
        return caps.model_copy(update={"elicitation": self._clio_declared_elicitation})


def _capability_session_class(declaration: MCPClientCapabilities, base: type) -> type:
    """Return a ``base`` ``ClientSession`` subclass that advertises ``declaration``.

    The installed MCP SDK derives the advertised ``clientCapabilities`` from
    :meth:`ClientSession._build_capabilities`, which is gated on wired handler
    callbacks and hardcodes elicitation as BOTH ``form`` and ``url``. To pin the
    ELICITATION domain at CLIO's declared granularity independent of handler wiring,
    we compose :class:`_DeclaredCapabilityOverride` in front of the session class
    and override that one method — the sanctioned extension the SDK itself uses
    (``fastmcp``'s proxy installs ``_ForwardingClientSession`` the same way, via
    ``TransportOptions.session_class``). This is a subclass, not a monkeypatch:
    nothing global is mutated, and only the modeled domain is touched.

    ``base`` is the session class that would otherwise be used (plain
    ``ClientSession`` on a direct execution client, ``_ForwardingClientSession`` on
    a proxy backend), so the override COMPOSES with — never discards — the base's
    other capabilities and, on a proxy, its push-forwarding. Subclasses are cached
    per ``(base, form, url)`` so a given (base, declaration) pair yields exactly one
    class.
    """

    key = (base, declaration.elicitation_form, declaration.elicitation_url)
    cached = _CAPABILITY_SESSION_CLASSES.get(key)
    if cached is not None:
        return cached

    session_class = type(
        "_DeclaredCapabilitySession",
        (_DeclaredCapabilityOverride, base),
        {"_clio_declared_elicitation": declaration.elicitation_capability()},
    )
    _CAPABILITY_SESSION_CLASSES[key] = session_class
    return session_class


def make_mcp_client(
    target: Any,
    *,
    handlers: MCPClientHandlers | None = None,
    capabilities: MCPClientCapabilities | None = None,
    client_cls: Callable[..., Any] | None = None,
) -> Any:
    """Construct an execution-path FastMCP client with CLIO identity + the handler slot.

    This is the ONE construction site for clients that actually dispatch MCP
    calls, and the ONE place CLIO stamps its handshake-floor identity: every
    client built here carries :func:`clio_client_info` as its ``client_info`` so
    the per-request ``_meta`` on the 2026-07-28 wire names ``clio-agent`` rather
    than FastMCP's default ``mcp`` (#1111). A populated hook is additionally
    wrapped in a signature adapter and forwarded as the matching ``fastmcp.Client``
    keyword argument — the construction-time slot P1 fills once correlation lands
    (see :mod:`clio_agent.tools.mcp_handlers`).

    Identity scope — a deliberate decision (#1111 review): identity is stamped on
    the EXECUTION paths that route here (tool dispatch, both gateway proxy
    branches, and the handshake probe) plus the readiness probe. The documented
    list-only introspection sites (``routes/catalog.py``, ``routes/blueprints.py``,
    ``runtime/status.py``, ``tools/gateway.list_gateway_tools``, the install/
    reconnect ``list_tools`` passes in ``routes/mcp.py``) keep their bare
    ``Client()``: they only enumerate tools and never dispatch a call or receive a
    server-initiated request, so ``clientInfo`` there is cosmetic. Routing them
    through this factory purely for identity would either re-plumb their list-only
    test doubles for no functional gain or add a per-file import to four modules
    that sit exactly at their size-ratchet baselines — accretion the no-accretion
    guard forbids. The meaningful connection surface (every dispatch path + the
    readiness handshake) carries identity; the display-only enumerations do not.

    Args:
        target: A FastMCP transport / server object (passed straight to the
            client); a CLIO raw ``{transport, command, args, url, env}`` mapping
            spec (resolved via
            :func:`clio_agent.tools.mcp_config.transport_from_spec`); or a native
            FastMCP ``MCPConfig`` mapping (``{"mcpServers": ...}`` or a rootless
            server map), passed unchanged so ``Client`` builds its
            ``MCPConfigTransport``.
        handlers: Optional CLIO hook bundle. ``None`` (or a bundle whose hooks
            are all ``None``) yields an identity-only client (no handler kwargs).
        capabilities: Optional typed client-capability DECLARATION
            (:class:`~clio_agent.tools.mcp_handlers.MCPClientCapabilities`),
            decoupled from handler wiring. A declaration is authoritative PER
            CAPABILITY DOMAIN IT MODELS. Today the type models only elicitation, so
            ANY non-``None`` declaration — including an explicit *empty* one —
            installs a ``ClientSession`` subclass (via ``TransportOptions.session_class``)
            that pins the advertised ``_meta`` elicitation EXACTLY: a form-only
            declaration advertises form without over-advertising url (the SDK's
            elicitation defect the seam exists for; #1113 uses it), and an empty
            declaration advertises elicitation absent even over a wired/forwarding
            elicitation handler. Domains the type does NOT model (sampling / roots /
            log) are deliberately left to the base session's wiring-derived
            advertisement — which is truthful on both paths (a direct client only
            advertises what is actually wired; a proxy backend genuinely forwards
            sampling/roots to the front). Clearing them would sever proxy
            push-forwarding. ``None`` (the default) leaves the whole advertisement
            SDK-derived.
        client_cls: Injection seam for the client class. Defaults to
            ``fastmcp.Client``; tests substitute a fake to inspect the
            construction without spawning a real backend.
    Tasks (#1115): every client built here declares the SEP-2663 tasks extension, so
    a task-serving backend may run a call as a background task and CLIO drives it to
    the real result. There is deliberately no per-call opt-out knob: a client that
    declared nothing would still fold ``fastmcp-tasks``' own internal extension once
    that package is imported, so an "off" switch would not turn the advertisement off
    — it would only swap CLIO's hardened resolver for the un-hardened one. The single
    honest suppression is the one FastMCP itself declares: a ``client_cls`` pinning
    ``_auto_internal_extensions = False`` (``ProxyClient``) folds no extension at all,
    and that path is recorded with the typed reason
    ``mcp_tasks_declaration_suppressed``.

    Returns:
        A constructed (not yet entered) FastMCP client for ``target``.

    Raises:
        ValueError: If ``target`` is a mapping that is neither a CLIO raw spec
            (scalar ``transport`` key) nor a FastMCP ``MCPConfig`` (``mcpServers``
            key or a rootless server map).
    """

    if isinstance(target, Mapping):
        target = _normalize_mapping_target(target)

    if client_cls is None:
        from fastmcp import Client  # noqa: PLC0415

        client_cls = Client

    kwargs: dict[str, Any] = {
        "client_info": clio_client_info(),
        # #1114: the modern-era MRTR loop (InputRequiredResult -> retry with
        # inputResponses) is bounded by this CLIO-config-resolved round cap on EVERY
        # execution-path client (any modern tool call can enter the loop), replacing the
        # SDK's hardcoded default. Exhaustion surfaces the typed degrade in mcp_executor.
        "input_required_max_rounds": input_required_max_rounds(),
    }
    if handlers is not None:
        if handlers.elicitation is not None:
            kwargs["elicitation_handler"] = ElicitationDispatcher(handlers.elicitation)
        if handlers.progress is not None:
            kwargs["progress_handler"] = ProgressDispatcher(handlers.progress)
        if handlers.message is not None:
            kwargs["message_handler"] = MessageMultiplexer(handlers.message)
        # `cancellation` has no fastmcp Client keyword today; P1 owns its wiring.

    # #1115: declare the SEP-2663 tasks extension. CLIO's subclass carries the
    # substrate's identifier, so folding it in REPLACES fastmcp-tasks' internal
    # extension with the hardened one (input-key dedup, `Mcp-Name` on task RPCs,
    # durable task-id persistence). Suppressed — with a typed reason — for client
    # classes that forbid internal extensions (proxy backends; #1119). The extension
    # takes no elicitation callback here on purpose: it reads the SDK-shaped one off
    # the live ClientSession, since fastmcp rewraps the 4-argument handler installed
    # above (see `session_elicitation_callback`).
    from clio_agent.tools.mcp_task_extension import tasks_declaration  # noqa: PLC0415

    declaration = tasks_declaration(client_cls, target)
    if declaration.extensions:
        kwargs["extensions"] = list(declaration.extensions)

    client = client_cls(target, **kwargs)
    if capabilities is not None:
        # ANY non-None declaration (incl. explicit empty) is authoritative for the
        # domain it models (elicitation today): empty pins elicitation absent even
        # over a wired/forwarding handler. Unmodeled domains stay base-derived.
        _install_capability_declaration(client, capabilities)
    return client


def _install_capability_declaration(client: Any, capabilities: MCPClientCapabilities) -> None:
    """Point ``client`` at a capability-declaring ``ClientSession`` subclass.

    Merges the ``session_class`` into the client's ``TransportOptions`` (preserving
    any ``backend_mode``/``forward_incoming_headers`` a proxy already set) rather
    than overwriting, and survives ``Client.new`` (which carries ``_transport_options``
    onto proxy clones). A ``client_cls`` test double without ``_transport_options``
    simply gains the attribute — harmless.
    """

    from dataclasses import replace  # noqa: PLC0415

    from fastmcp.client.transports.base import TransportOptions  # noqa: PLC0415

    existing = getattr(client, "_transport_options", None)
    base_options = existing if existing is not None else TransportOptions()
    # Subclass the session class already in effect (plain ClientSession on a direct
    # client, the proxy's forwarding session on a proxy backend) so the capability
    # override composes with, never discards, that behavior.
    session_class = _capability_session_class(capabilities, base_options.session_class)
    client._transport_options = replace(base_options, session_class=session_class)


def _normalize_mapping_target(target: Mapping[str, Any]) -> Any:
    """Resolve a mapping ``target`` to a transport, or pass a native MCPConfig.

    A CLIO raw spec is recognized ONLY when the top-level ``transport`` value has
    the scalar transport shape (a *string* naming a transport); such specs go
    through :func:`clio_agent.tools.mcp_config.transport_from_spec`. A ``transport``
    key whose value is a *mapping* is a native FastMCP ``MCPConfig`` rootless
    server that merely happens to be named ``transport`` — not a CLIO spec. Native
    ``MCPConfig`` mappings (an ``mcpServers`` wrapper, or a rootless map of server
    configs — values with ``command``/``url``) are returned unchanged so
    ``fastmcp.Client`` builds its own ``MCPConfigTransport``. Anything else is an
    explicit error rather than a silent mis-parse.
    """

    if isinstance(target.get("transport"), str):
        from clio_agent.tools.mcp_config import transport_from_spec  # noqa: PLC0415

        return transport_from_spec(target)
    if "mcpServers" in target or _is_rootless_mcp_config(target):
        return target
    raise ValueError(
        "ambiguous MCP client target mapping: expected a CLIO raw spec (with a "
        "scalar 'transport' key) or a FastMCP MCPConfig (with 'mcpServers' or a "
        f"rootless map of server configs); got keys {sorted(target)!r}"
    )


def _is_rootless_mcp_config(target: Mapping[str, Any]) -> bool:
    """Whether ``target`` is a rootless FastMCP MCPConfig (server map at root).

    Mirrors FastMCP's own ``MCPConfig.wrap_servers_at_root`` heuristic: at least
    one value is a mapping carrying a ``command`` or ``url`` key.
    """

    return any(
        isinstance(value, Mapping) and ("command" in value or "url" in value)
        for value in target.values()
    )


__all__ = [
    "MCPClientCapabilities",
    "MCPClientHandlers",
    "WireMode",
    "clio_client_info",
    "input_required_max_rounds",
    "make_mcp_client",
    "wire_value",
]
