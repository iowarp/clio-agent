"""Typed capture + degrade-reason classification for MCP connection era (#1201).

The MCP client is fully v2 (P1 campaign complete on develop), but the ``auto``
connect mode (:mod:`clio_agent.tools.mcp_runtime`) probes the modern
``server/discover`` wire first and falls back to the legacy ``initialize``
handshake when that probe loses the #1186 race (a slow first response --
cold uv env, matplotlib import, launcher cache-lock contention -- burns both
the probe and its one corrective re-probe). A server and client that BOTH
speak 2026-07-28 can therefore land on the legacy era anyway, purely from
timing, not capability. Neither ``mcp_executor.py`` nor ``execution.py`` ever
captured which era a connection actually landed on; only the diagnostic
``/v1/mcp/handshake`` probe recorded ``protocol_version``
(``providers/handshake/mcp.py``). A real execution-path connection silently
downgrading left no typed reason, log line, or trace entry -- the exact
no-silent-fallback violation this module closes.

This is the ONE place execution-path connect sites stamp a
:class:`MCPConnectionEra` immediately after entering a client, mirroring the
``MCP_RESULT_DOWNGRADED_TO_COMPLETE`` (:mod:`clio_agent.tools.mcp_results`) /
``mcp_tasks_declaration_suppressed`` (:mod:`clio_agent.tools.mcp_task_extension`)
degrade-reason conventions already used in this package: a typed catalog
entry, never a silently absorbed fact.

Adversarial review on #1201's first PR (a live probe, ``scripts/diagnostics/
probe_1201_era_detectability.py``) proved the executor's OWN front-leg
capture (``AsyncMCPToolExecutor.start()`` / ``_route()``) is BLIND on the
standard gateway-mounted path: FastMCP's ``_mirror_front_era_mode``
(``fastmcp/server/providers/proxy.py``) forces a proxy's backend leg to adopt
whatever era its FRONT connection negotiated, and the front is always an
in-process connection (to the composite gateway or a mounted
``FastMCPProxy`` object), which negotiates trivially and is therefore always
modern -- independent of what the REAL backend (a real stdio/http
connection, subject to the actual #1186 timing race) would have negotiated
on its own. The probe confirmed this directly: a ``ProxyClient`` instrumented
at its own ``__aenter__`` (the real backend leg) reports the identical
protocol_version as the front, regardless of how slow the real subprocess is.

The fix is :func:`instrument_client_era`, applied at the seam that actually
dials the real backend (``tools/gateway.py::_proxy_for_spec``'s
``_client_factory`` closure, on the per-request clone -- the ONE place the
real, unmirrored connection is entered) and at every other DIRECT
(non-proxied) execution-path connect site: the executor's own front leg
(still meaningful for a directly-targeted executor, e.g. tests, and kept for
the per-server runtime record), the dynamic-agent external tool call
(``gact/elicitation_bridge.py``), the per-call REST dispatch
(``gact/routes/mcp.py``), the relay door (``tools/relay_transport.py``), and
the diagnostic handshake probe (``providers/handshake/mcp.py``, which now
ALSO feeds this module's registry instead of only its own one-off report).

Era determination reads the SDK's own protocol-version registry
(``mcp_types.version``) instead of re-deriving it: a version in
``MODERN_PROTOCOL_VERSIONS`` is "modern", one in ``HANDSHAKE_PROTOCOL_VERSIONS``
is "legacy", anything else (unset/unrecognized) is "unknown" -- never treated
as proven downgrade evidence.

A downgrade is recorded ONLY when ``connect_mode`` resolved to ``auto`` AND
the negotiated era is legacy. A PINNED mode (an explicit modern version, or
the literal ``"legacy"``) is operator intent: legitimate negotiation failure
under a pinned modern mode already raises the typed ``MCP_PROTOCOL_REFUSED``
path (:class:`clio_agent.errors.MCPUnsupportedProtocolVersionError`)
unchanged, and a pinned legacy connection landing on legacy is not a
downgrade at all.

#1281 (C1-S1) piggybacks ONE opportunistic read on this SAME ``__aenter__``
seam: whenever an instrumented client's real negotiation lands, if its
SERVER-declared extensions carry the SEP-2663 tasks id, that is recorded as
a POSITIVE task-capability verdict (:func:`record_task_capability`) under
the DECLARED server/namespace name -- covering the case where a proxy-routed
listing or call reaches the real backend before ``tools/gateway.py::
_list_declared_tools``'s own definitive (connect + full listing) read has
run (see :mod:`clio_agent.tools.mcp_task_routing`, which owns the routing
DECISION and the definitive read; this module owns only the record + this
one opportunistic capture, since it already instruments every real connect).
This capture NEVER writes a negative -- a bare connect carries no guaranteed
tool listing, so extensions absence here is not proof of incapability.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal, TypeVar

from fastmcp.utilities.tasks import TASKS_EXTENSION_ID
from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS, MODERN_PROTOCOL_VERSIONS

from clio_agent.errors import (
    MCP_PROTOCOL_DOWNGRADED_TO_LEGACY,
    MCP_TASK_CAPABILITY_DEMOTED,
    MCP_TASK_CAPABILITY_DEMOTION_REFUSED,
)

logger = logging.getLogger(__name__)

ProtocolEra = Literal["modern", "legacy", "unknown"]


@dataclass(frozen=True)
class MCPConnectionEra:
    """One execution-path client connection's classified protocol era.

    The per-server runtime record scope item 1 asks for: stamped by
    :func:`classify_connection_era` right after a client is entered, and kept
    on the owning executor (``AsyncMCPToolExecutor.connection_era`` for the
    primary/composite connection, ``.namespace_connection_era(namespace)`` for
    a namespace-direct backend).
    """

    server_id: str
    protocol_version: str | None
    era: ProtocolEra
    connect_mode: str
    pinned: bool
    degrade_reason: str | None = None


def resolved_connect_mode() -> str:
    """Return the resolved ``tools.mcp.connect_mode`` value.

    Deliberately a SECOND, independent ``conf.resolve`` of the same key
    :func:`clio_agent.tools.mcp_runtime.make_mcp_client` already reads when
    building the client, rather than threading a new return value through
    ``make_mcp_client``'s many execution-path callers (#1106). It is a pure
    config read with no side effect -- config cannot change mid-connect -- so
    the two reads cannot desync, and this keeps the #1186-race-sensitive
    connect logic in ``mcp_runtime.py`` completely untouched.
    """

    from clio_agent import conf  # noqa: PLC0415 - avoid import cycle at module load

    return conf.resolve(
        "tools.mcp.connect_mode",
        env="CLIO_MCP_CONNECT_MODE",
        default="auto",
        cast=conf.as_str,
    )


def protocol_version_era(protocol_version: str | None) -> ProtocolEra:
    """Classify a raw ``protocol_version`` string into an era -- pure, no recording.

    Shared by :func:`classify_connection_era` and
    :mod:`clio_agent.tools.mcp_task_routing`'s definitive capability read
    (#1281 F7: a capability verdict records the era it was negotiated on, so
    the demotion guard in :func:`record_task_capability` can tell an
    equally-authoritative re-read from a downgraded one) -- kept in exactly
    ONE place so the two never derive era differently.
    """

    if protocol_version in MODERN_PROTOCOL_VERSIONS:
        return "modern"
    if protocol_version in HANDSHAKE_PROTOCOL_VERSIONS:
        return "legacy"
    return "unknown"


def classify_connection_era(
    *, server_id: str, protocol_version: str | None, connect_mode: str
) -> MCPConnectionEra:
    """Classify one negotiated connection, recording a downgrade when real.

    ``protocol_version`` is read straight off ``client.protocol_version``
    after ``__aenter__`` (the SDK populates it for both eras: the
    ``initialize`` handshake, ``server/discover``, or a direct version pin).
    ``connect_mode`` is the resolved ``tools.mcp.connect_mode`` in effect for
    that same client (:func:`resolved_connect_mode`).

    "Pinned" mirrors :func:`clio_agent.tools.mcp_runtime.make_mcp_client`'s
    own pin check exactly (``connect_mode and connect_mode != "auto"``) so the
    two notions of "pinned" cannot drift apart.
    """

    era = protocol_version_era(protocol_version)

    pinned = bool(connect_mode) and connect_mode != "auto"
    degrade_reason = MCP_PROTOCOL_DOWNGRADED_TO_LEGACY if (not pinned and era == "legacy") else None

    record = MCPConnectionEra(
        server_id=server_id,
        protocol_version=protocol_version,
        era=era,
        connect_mode=connect_mode,
        pinned=pinned,
        degrade_reason=degrade_reason,
    )
    _record_latest(record)
    if degrade_reason is not None:
        _record_downgrade(record)
    return record


#: Bounded, queryable in-process ring of real era downgrades -- mirrors
#: ``clio_agent.tools.execution._emit_tool_runtime_reason``'s ring: this
#: tools-layer module imports no gact and cannot reach the per-session
#: semantic-event trace directly, so it retains the same audit-sink pattern
#: every other tools-layer degrade reason in this package uses.
_DOWNGRADES: "deque[MCPConnectionEra]" = deque(maxlen=256)
_DOWNGRADES_LOCK = threading.Lock()

#: Latest classification PER server_id, updated on every classify (not just
#: downgrades) -- the per-server operator surface (doctor/status,
#: /v1/mcp/handshake rows, the gact reader sites) queries "what is this
#: server's era right now", not just "has it ever downgraded".
_LATEST_BY_SERVER: dict[str, MCPConnectionEra] = {}
_LATEST_LOCK = threading.Lock()


def _record_latest(record: MCPConnectionEra) -> None:
    """Update the per-server latest-classification map (no-op for an unlabeled id)."""

    if not record.server_id:
        return
    with _LATEST_LOCK:
        _LATEST_BY_SERVER[record.server_id] = record


def _record_downgrade(record: MCPConnectionEra) -> None:
    """Append a real downgrade to the queryable ring, log it loudly, and audit it."""

    from clio_agent.runtime.stream_audit import stream_audit  # noqa: PLC0415

    with _DOWNGRADES_LOCK:
        _DOWNGRADES.append(record)
    logger.warning(
        "mcp connection downgraded to legacy era reason=%s server=%s protocol_version=%s "
        "connect_mode=%s",
        record.degrade_reason,
        record.server_id,
        record.protocol_version,
        record.connect_mode,
    )
    stream_audit(
        "mcp_connection_downgrade",
        reason=record.degrade_reason,
        server_id=record.server_id,
        protocol_version=record.protocol_version,
        connect_mode=record.connect_mode,
    )


def recorded_mcp_connection_downgrades() -> list[MCPConnectionEra]:
    """Return a snapshot of recorded era-downgrade records (queryable audit)."""

    with _DOWNGRADES_LOCK:
        return list(_DOWNGRADES)


def latest_mcp_connection_era(server_id: str) -> MCPConnectionEra | None:
    """Return the most recent classification observed for ``server_id``, if any."""

    with _LATEST_LOCK:
        return _LATEST_BY_SERVER.get(server_id)


def all_latest_mcp_connection_eras() -> dict[str, MCPConnectionEra]:
    """Return a snapshot of the latest classification for every observed server."""

    with _LATEST_LOCK:
        return dict(_LATEST_BY_SERVER)


# --------------------------------------------------------------------------- #
# #1281 (C1-S1): per-server task capability -- a discovery-time write path,   #
# separate from the __aenter__-time era writes above.                        #
# --------------------------------------------------------------------------- #

#: Where a task-capability verdict was read from. ``capabilities_extensions``
#: is the modern (2026-07-28) key: the SERVER-DECLARED ``ServerCapabilities.
#: extensions`` carries ``io.modelcontextprotocol/tasks``. ``tool_execution``
#: is the legacy (2025-11-25) key: a listed tool's ``execution.task_support``
#: is ``"optional"`` or ``"required"`` (SEP-1686) -- the modern wire has no
#: per-tool ``execution`` field to read at all. ``none`` is a genuine negative:
#: neither key was present on a connect-and-list that saw both.
TaskCapabilitySource = Literal["capabilities_extensions", "tool_execution", "none"]


@dataclass(frozen=True)
class MCPTaskCapability:
    """One declared server's task capability, as negotiated (never probed).

    Recorded by :func:`record_task_capability` at DISCOVERY time (the
    per-namespace listing pass, ``tools/gateway.py::_list_declared_tools``,
    a replayed listing-cache hit, and the opportunistic positive-only read at
    a real backend connect, this module's own ``instrument_client_era``) --
    never inferred from call behavior or timing. Keyed by the DECLARED
    namespace/server name (the same key ``_clio_namespace_specs`` uses), NOT
    the SHA-derived :class:`~clio_agent.tools.mcp_task_extension.
    BackendIdentity.server_id` a task record itself is keyed on -- two
    different identity spaces for two different purposes (routing a
    namespace vs. keying a durable task row).

    ``era`` (#1281 F7, adversarial review) is the protocol era THIS verdict
    was negotiated on -- the demotion guard in :func:`record_task_capability`
    needs it to tell an equally-authoritative re-read (another modern
    negotiation) from a downgraded one (a legacy landing on what may still be
    a modern, task-capable server, e.g. the #1186 race) before letting a
    False overwrite a ``capabilities_extensions`` True.
    """

    server_id: str
    task_capable: bool
    source: TaskCapabilitySource
    era: ProtocolEra = "unknown"


#: Latest task-capability verdict PER declared server, mirroring
#: ``_LATEST_BY_SERVER``'s lock+dict idiom exactly -- a SEPARATE registry
#: (discovery-time writes, not era-classification writes) so the two
#: concerns never share a lock or a stale-overwrite risk.
_LATEST_TASK_CAPABILITY_BY_SERVER: dict[str, MCPTaskCapability] = {}
_LATEST_TASK_CAPABILITY_LOCK = threading.Lock()


def record_task_capability(
    server_id: str,
    *,
    task_capable: bool,
    source: TaskCapabilitySource,
    era: ProtocolEra = "unknown",
) -> MCPTaskCapability:
    """Record one server's task-capability verdict, keyed by declared name.

    Overwrites the prior verdict for ``server_id`` (mirrors
    :func:`_record_latest`: the per-server surface answers "what do we know
    right now", not "has it ever been true") -- WITH ONE GUARD (#1281 F7): a
    False may not overwrite an existing True whose source was
    ``capabilities_extensions`` (the authoritative modern key) unless this
    new read is EQUALLY authoritative (``era == "modern"``). A refused
    overwrite keeps the existing True record and is itself typed + audited
    (:data:`~clio_agent.errors.MCP_TASK_CAPABILITY_DEMOTION_REFUSED`) rather
    than silently dropped; a permitted True -> False transition is likewise
    typed + audited as a real demotion
    (:data:`~clio_agent.errors.MCP_TASK_CAPABILITY_DEMOTED`). A no-op (still
    returns the record) for an unlabeled id.
    """

    candidate = MCPTaskCapability(
        server_id=server_id, task_capable=task_capable, source=source, era=era
    )
    if not server_id:
        return candidate

    with _LATEST_TASK_CAPABILITY_LOCK:
        existing = _LATEST_TASK_CAPABILITY_BY_SERVER.get(server_id)
        refuse = (
            existing is not None
            and existing.task_capable
            and existing.source == "capabilities_extensions"
            and not task_capable
            and era != "modern"
        )
        record = existing if refuse and existing is not None else candidate
        if not refuse:
            _LATEST_TASK_CAPABILITY_BY_SERVER[server_id] = record

    if refuse:
        assert existing is not None  # narrowed by `refuse`'s own condition
        _record_capability_demotion_refused(existing, attempted=candidate)
        return record

    logger.debug(
        "mcp task capability recorded server=%s task_capable=%s source=%s era=%s",
        server_id,
        task_capable,
        source,
        era,
    )
    if existing is not None and existing.task_capable and not task_capable:
        _record_capability_demoted(record, previous=existing)
    return record


def _record_capability_demoted(record: MCPTaskCapability, *, previous: MCPTaskCapability) -> None:
    """A True verdict was legitimately overwritten by an equally-authoritative False."""

    from clio_agent.runtime.stream_audit import stream_audit  # noqa: PLC0415

    logger.warning(
        "mcp task capability demoted server=%s reason=%s previous_source=%s new_source=%s era=%s",
        record.server_id,
        MCP_TASK_CAPABILITY_DEMOTED,
        previous.source,
        record.source,
        record.era,
    )
    stream_audit(
        "mcp_task_capability_demoted",
        reason=MCP_TASK_CAPABILITY_DEMOTED,
        server_id=record.server_id,
        previous_source=previous.source,
        new_source=record.source,
        era=record.era,
    )


def _record_capability_demotion_refused(
    existing: MCPTaskCapability, *, attempted: MCPTaskCapability
) -> None:
    """A False verdict was refused: not equally authoritative as the True it targeted."""

    from clio_agent.runtime.stream_audit import stream_audit  # noqa: PLC0415

    logger.warning(
        "mcp task capability demotion refused server=%s reason=%s existing_source=%s "
        "attempted_source=%s attempted_era=%s",
        existing.server_id,
        MCP_TASK_CAPABILITY_DEMOTION_REFUSED,
        existing.source,
        attempted.source,
        attempted.era,
    )
    stream_audit(
        "mcp_task_capability_demotion_refused",
        reason=MCP_TASK_CAPABILITY_DEMOTION_REFUSED,
        server_id=existing.server_id,
        existing_source=existing.source,
        attempted_source=attempted.source,
        attempted_era=attempted.era,
    )


def latest_task_capability(server_id: str) -> MCPTaskCapability | None:
    """Return the most recent task-capability verdict for ``server_id``, if any.

    ``None`` means capability is genuinely UNKNOWN (no discovery has landed
    yet) -- routing callers must treat this as "keep the proxy path", never
    as a false negative.
    """

    with _LATEST_TASK_CAPABILITY_LOCK:
        return _LATEST_TASK_CAPABILITY_BY_SERVER.get(server_id)


def all_latest_task_capabilities() -> dict[str, MCPTaskCapability]:
    """Return a snapshot of every observed server's latest task-capability verdict."""

    with _LATEST_TASK_CAPABILITY_LOCK:
        return dict(_LATEST_TASK_CAPABILITY_BY_SERVER)


def _server_declares_tasks(client: Any) -> bool:
    """Whether a connected client's SERVER-declared extensions carry the tasks id.

    Reads ``client.server_capabilities`` -- populated by the SDK's own
    negotiation, independent of what THIS client itself declared (a
    ``ProxyClient`` that suppresses its own extension advertisement still
    sees the backend's true capabilities here). Deliberately duplicated
    (rather than imported) from :mod:`clio_agent.tools.mcp_task_routing`'s
    equivalent helper: that module imports FROM this one, so the reverse
    import would cycle. Both are three lines; keep them in sync by hand.
    """

    capabilities = getattr(client, "server_capabilities", None)
    extensions = getattr(capabilities, "extensions", None) or {}
    return TASKS_EXTENSION_ID in extensions


# --------------------------------------------------------------------------- #
# #1283 (C1-S3): generic server-declared extension capture -- the READ side  #
# of the generic extension registry (tools/mcp_extension_registry.py owns    #
# the DECLARE side). Recorded BESIDE the era/capability records above,       #
# mirroring their record/latest idiom exactly, but keyed on the FULL set of  #
# extension identifiers a server declared -- not narrowed to any one id the  #
# way `_server_declares_tasks` is. Before this, server-declared extensions   #
# were never read anywhere (module docstring, #1283 point 1).                #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class MCPServerExtensions:
    """Every extension identifier ONE server declared at its latest negotiation.

    Recorded at the SAME ``__aenter__`` seam as :class:`MCPConnectionEra` (every
    real client connect, direct or a proxy's per-request backend clone), so a
    server's declared extension SET is queryable the moment any execution path
    has connected to it -- never inferred from behavior or timing. CAUTION
    (mirrors the #1281 caution baked into ``_server_declares_tasks``): fastmcp
    splices ``io.modelcontextprotocol/ui: {}`` onto EVERY modern server by
    default, so an id's PRESENCE here is not independent proof a server
    implements that extension's behavior -- only that it was in the
    negotiated capability ad.
    """

    server_id: str
    extensions: tuple[str, ...]
    era: ProtocolEra = "unknown"


#: Latest declared-extension set PER server, mirroring ``_LATEST_BY_SERVER``'s
#: lock+dict idiom exactly -- a SEPARATE registry (a different fact than era
#: or task capability) so none of the three concerns share a lock.
_LATEST_SERVER_EXTENSIONS_BY_SERVER: dict[str, MCPServerExtensions] = {}
_LATEST_SERVER_EXTENSIONS_LOCK = threading.Lock()


def _declared_extension_identifiers(client: Any) -> tuple[str, ...]:
    """Every extension identifier a connected client's SERVER declared, sorted.

    Reads the SAME ``client.server_capabilities.extensions`` source
    :func:`_server_declares_tasks` narrows to one id -- this capture is
    generic on purpose (#1283): it records whatever the server actually
    declared, known identifier or not (the exerciser's synthetic extension
    proves this end to end, ``tests/test_tools/mcp_exerciser.py``).
    """

    capabilities = getattr(client, "server_capabilities", None)
    extensions = getattr(capabilities, "extensions", None) or {}
    return tuple(sorted(str(identifier) for identifier in extensions))


def record_server_extensions(
    server_id: str, *, extensions: Sequence[str], era: ProtocolEra = "unknown"
) -> MCPServerExtensions:
    """Record the FULL server-declared extension set for ``server_id``.

    Overwrites the prior record (mirrors :func:`_record_latest`: "what do we
    know right now", not "has this id ever been declared"). A no-op
    (still returns the record) for an unlabeled id, matching every sibling
    record function in this module.
    """

    record = MCPServerExtensions(server_id=server_id, extensions=tuple(extensions), era=era)
    if not server_id:
        return record
    with _LATEST_SERVER_EXTENSIONS_LOCK:
        _LATEST_SERVER_EXTENSIONS_BY_SERVER[server_id] = record
    return record


def latest_server_extensions(server_id: str) -> MCPServerExtensions | None:
    """Return the most recent declared-extension set observed for ``server_id``.

    ``None`` means genuinely UNOBSERVED (no execution-path connect has landed
    for this server yet) -- never treated as "declares nothing" by a caller.
    """

    with _LATEST_SERVER_EXTENSIONS_LOCK:
        return _LATEST_SERVER_EXTENSIONS_BY_SERVER.get(server_id)


def all_latest_server_extensions() -> dict[str, MCPServerExtensions]:
    """Return a snapshot of every observed server's latest declared-extension set."""

    with _LATEST_SERVER_EXTENSIONS_LOCK:
        return dict(_LATEST_SERVER_EXTENSIONS_BY_SERVER)


_ClientT = TypeVar("_ClientT")

#: Per-base-class instrumented subclasses, cached like
#: ``mcp_runtime.py``'s ``_CAPABILITY_SESSION_CLASSES`` -- a class-COMPOSITION
#: seam the SDK itself uses (``TransportOptions.session_class``), not a
#: monkeypatch. One subclass per distinct base type, reused across every
#: client instance of that type regardless of ``server_id`` (stored PER
#: INSTANCE below, never baked into the shared class).
_INSTRUMENTED_CLASSES: dict[type, type] = {}
_INSTRUMENTED_CLASSES_LOCK = threading.Lock()

#: Instance-``__dict__`` key the composed ``__aenter__`` reads its server_id
#: from. Not a "private name mangled" attribute so it survives ``copy.copy``
#: (FastMCP's ``Client.new()`` clone path) like every other instance attribute.
_SERVER_ID_ATTR = "_clio_mcp_connection_era_server_id"


def _instrumented_class(base_cls: type) -> type:
    """Return (building + caching once) a ``base_cls`` subclass that classifies
    on ``__aenter__``.

    A dynamic subclass, not an instance-level ``__aenter__`` override: Python
    resolves dunder methods invoked via syntax (``async with client:``) on the
    TYPE, never the instance -- an instance attribute named ``__aenter__``
    is silently ignored by that syntax (confirmed by a live regression: the
    proxy backend leg in ``tools/gateway.py::_proxy_for_spec`` is entered via
    ``async with``, not an explicit ``client.__aenter__()`` call, and an
    instance-patched version never fired there). Subclassing makes
    ``type(client).__aenter__`` resolve correctly either way.
    """

    with _INSTRUMENTED_CLASSES_LOCK:
        cached = _INSTRUMENTED_CLASSES.get(base_cls)
        if cached is not None:
            return cached

        async def __aenter__(self: Any) -> Any:  # noqa: N807 - dunder override
            result = await base_cls.__aenter__(self)  # type: ignore[attr-defined]
            server_id = getattr(self, _SERVER_ID_ATTR, "")
            negotiated_era = classify_connection_era(
                server_id=server_id,
                protocol_version=getattr(self, "protocol_version", None),
                connect_mode=resolved_connect_mode(),
            )
            # #1281 (C1-S1): opportunistic POSITIVE-only capability capture --
            # see the module docstring. Never overwrites a verdict with False.
            # era=negotiated_era.era feeds the F7 demotion guard.
            if server_id and _server_declares_tasks(self):
                record_task_capability(
                    server_id,
                    task_capable=True,
                    source="capabilities_extensions",
                    era=negotiated_era.era,
                )
            # #1283 (C1-S3): the GENERIC read side -- every real connect records
            # the server's FULL declared-extension set, not only the tasks id.
            if server_id:
                record_server_extensions(
                    server_id,
                    extensions=_declared_extension_identifiers(self),
                    era=negotiated_era.era,
                )
            return result

        subclass = type(
            f"_EraInstrumented{base_cls.__name__}", (base_cls,), {"__aenter__": __aenter__}
        )
        _INSTRUMENTED_CLASSES[base_cls] = subclass
        return subclass


def instrument_client_era(client: _ClientT, *, server_id: str) -> _ClientT:
    """Make ONE client instance auto-classify its era the moment it connects.

    Applied at the seam that actually enters a client onto a real (possibly
    proxied-behind-mirroring) transport -- see the module docstring for why
    the executor's own front-leg capture is not enough on the gateway-mounted
    path. Swaps ``client.__class__`` to a cached, dynamically composed
    subclass (never mutates ``base_cls`` itself, so every OTHER client of the
    same type in the process is untouched) whose ``__aenter__`` calls the
    real one, then runs :func:`classify_connection_era` against the
    now-negotiated ``client.protocol_version`` and :func:`resolved_connect_mode`.

    Call it on whichever instance is actually entered. FastMCP's
    ``Client.new()`` clone (``copy.copy``) DOES carry an already-swapped
    class and the ``server_id`` attribute forward if the ORIGINAL was
    instrumented first -- but ``tools/gateway.py::_proxy_for_spec`` never
    instruments its long-lived ``base_backend`` (only its per-request
    ``.new()`` clones are ever entered), so it calls this on each fresh clone
    directly instead. Idempotent either way: re-instrumenting an
    already-instrumented instance just re-composes the same cached subclass
    and overwrites ``server_id`` with the same value. Returns ``client``
    unchanged (mutated in place) for chaining.
    """

    client.__class__ = _instrumented_class(type(client))  # type: ignore[misc]
    client.__dict__[_SERVER_ID_ATTR] = server_id
    return client


__all__ = [
    "MCPConnectionEra",
    "MCPServerExtensions",
    "MCPTaskCapability",
    "ProtocolEra",
    "TaskCapabilitySource",
    "all_latest_mcp_connection_eras",
    "all_latest_server_extensions",
    "all_latest_task_capabilities",
    "classify_connection_era",
    "instrument_client_era",
    "latest_mcp_connection_era",
    "latest_server_extensions",
    "latest_task_capability",
    "protocol_version_era",
    "record_server_extensions",
    "record_task_capability",
    "recorded_mcp_connection_downgrades",
    "resolved_connect_mode",
]
