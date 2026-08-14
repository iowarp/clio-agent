"""Harden the SDK's ``mode='auto'`` era negotiation: timing is never a verdict.

The stock ``mcp.client._probe.negotiate_auto`` treats EVERY ``MCPError`` from
the ``server/discover`` probe — including a client-side ``REQUEST_TIMEOUT`` —
as denylist evidence and falls back to the legacy ``initialize`` handshake.
A slow first response (cold uv env, matplotlib import, launcher cache-lock
contention — the #1186 race) therefore downgrades a modern client talking to
a modern server purely from latency, and the v2-only server rightly refuses
the legacy handshake with ``-32022``, killing the connect after the probe AND
its one corrective re-probe are burned.

Owner ruling (2026-08-13): **protocol selection comes only from the official
negotiation mechanism — the server's typed answers — never from timing.** A
timeout carries zero version information; the correct response is to retry
the SAME probe (the era is warming server-side), bounded, and to fail TYPED
on exhaustion rather than switch dialects.

This module installs a drop-in replacement whose behavior is byte-faithful to
the SDK's for every TYPED answer:

* ``-32022`` naming a mutual modern version → one re-probe at that version;
* ``-32022`` with a disjoint modern-only ``supported`` list → raise (real
  incompatibility);
* any OTHER typed RPC error (method-not-found etc.) → legacy ``initialize``
  (the denylist, unchanged — a server that answers "no such method" has
  spoken);
* an unparseable/legacy-only ``DiscoverResult`` → legacy ``initialize``;
* a non-``MCPError`` exception → propagate (an outage is never an era
  verdict — the SDK already had this right);

and differs in exactly one branch: ``REQUEST_TIMEOUT`` retries the same
probe up to ``tools.mcp.probe_timeout_retries`` (default 3) additional
times, then re-raises the timeout — never ``initialize``.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

_INSTALLED = False

_T = TypeVar("_T")


def namespace_first_call_retries() -> int:
    """Bounded retry budget for a namespace's FIRST real backend request.

    Sibling policy to :func:`_resolve_timeout_retries` above -- see
    :func:`call_with_namespace_first_call_retry`, its one caller
    (``tools/mcp_executor.py::AsyncMCPToolExecutor.call_tool_result``).
    Default 2 (three total attempts): config-tunable like every other bounded
    retry in this package, never hardcoded.
    """

    from clio_agent import conf  # noqa: PLC0415 - avoid import cycle at module load

    return int(
        conf.resolve(
            "tools.mcp.namespace_first_call_retries",
            env="CLIO_MCP_NAMESPACE_FIRST_CALL_RETRIES",
            default=2,
            cast=conf.as_int,
        )
    )


async def call_with_namespace_first_call_retry(
    call: Callable[[], Awaitable[_T]],
    *,
    namespace: str | None,
    first_call: bool,
    tool: str,
) -> _T:
    """Retry a namespace's FIRST real backend request, bounded, on MCPError only.

    A freshly-spawned namespace backend's very first request can lose the
    same #1186-class era-negotiation race this module exists to correct (the
    in-process front always negotiates modern instantly; a legacy-only real
    backend occasionally answers the mirrored-modern attempt with a raw
    session-layer ``MCPError`` -- a rejection BEFORE the request ever reaches
    the tool -- while it is still settling). That is the OFFICIAL negotiation
    mechanism losing a timing race, not evidence about the tool, so a retry
    can never duplicate a tool side effect. A completed call's OWN error
    (fastmcp's ``ToolError``, from a real ``CallToolResult``) is a DIFFERENT
    exception type and is never retried here -- nor is anything once the
    namespace is proven connected (``first_call=False``).

    Args:
        call: Zero-arg awaitable performing ONE attempt (already
            timeout-wrapped by the caller).
        namespace: The routed namespace, or ``None`` for a composite-routed call.
        first_call: Whether this is the namespace's first forwarded call.
        tool: The model-facing tool name, for the retry log line only.
    """

    from mcp.shared.exceptions import MCPError  # noqa: PLC0415

    retries_left = namespace_first_call_retries() if first_call and namespace else 0
    while True:
        try:
            return await call()
        except MCPError as exc:
            if not (first_call and namespace and retries_left > 0):
                raise
            retries_left -= 1
            logger.warning(
                "mcp_namespace_first_call_retry namespace=%s tool=%s retries_left=%d reason=%s",
                namespace,
                tool,
                retries_left,
                exc,
            )


def _resolve_timeout_retries() -> int:
    from clio_agent import conf  # noqa: PLC0415 - avoid import cycle at module load

    return int(
        conf.resolve(
            "tools.mcp.probe_timeout_retries",
            env="CLIO_MCP_PROBE_TIMEOUT_RETRIES",
            default=3,
            cast=conf.as_int,
        )
    )


async def hardened_negotiate_auto(session: Any) -> None:
    """``negotiate_auto`` with the timeout-is-not-evidence correction (see module doc)."""

    import mcp_types as types  # noqa: PLC0415
    from mcp.client._probe import _parse_supported  # noqa: PLC0415
    from mcp.shared.exceptions import MCPError  # noqa: PLC0415
    from mcp_types import REQUEST_TIMEOUT, UNSUPPORTED_PROTOCOL_VERSION  # noqa: PLC0415
    from mcp_types.version import (  # noqa: PLC0415
        HANDSHAKE_PROTOCOL_VERSIONS,
        LATEST_MODERN_VERSION,
        MODERN_PROTOCOL_VERSIONS,
    )
    from pydantic import ValidationError  # noqa: PLC0415

    version = LATEST_MODERN_VERSION
    mutual_retry_used = False
    timeout_retries_left = _resolve_timeout_retries()

    while True:
        try:
            raw = await session.send_discover(version)
        except MCPError as e:
            if e.code == REQUEST_TIMEOUT:
                # THE correction: latency is not version information. Retry the
                # same probe — the slow-starting server is warming and the era
                # may already be locked modern server-side. Typed log per retry.
                if timeout_retries_left > 0:
                    timeout_retries_left -= 1
                    logger.warning(
                        "mcp_probe_timeout_retry reason=discover_timed_out version=%s "
                        "retries_left=%d",
                        version,
                        timeout_retries_left,
                    )
                    continue
                logger.warning(
                    "mcp_probe_timeout_exhausted reason=discover_never_answered version=%s",
                    version,
                )
                raise
            if e.code == UNSUPPORTED_PROTOCOL_VERSION:
                supported = _parse_supported(e.error.data)
                mutual = [v for v in MODERN_PROTOCOL_VERSIONS if v in (supported or ())]
                if mutual and not mutual_retry_used:
                    mutual_retry_used = True
                    version = mutual[-1]
                    continue
                if supported is not None and not any(
                    v in HANDSHAKE_PROTOCOL_VERSIONS for v in supported
                ):
                    raise  # server is modern-only and disjoint — real incompatibility
            # Any other TYPED rpc error (method-not-found, unsupported-with-
            # legacy-advertised, ...) → the denylist fallback, unchanged: the
            # server answered, and its answer says the modern wire is absent.
            try:
                await session.initialize()
            except MCPError as handshake_exc:
                if handshake_exc.code != UNSUPPORTED_PROTOCOL_VERSION or mutual_retry_used:
                    raise
                # -32022 from the handshake is itself modern evidence: the era
                # locked modern server-side before this initialize arrived.
                # Re-probe once at a version the server names (SDK behavior).
                supported = _parse_supported(handshake_exc.error.data)
                mutual = [v for v in MODERN_PROTOCOL_VERSIONS if v in (supported or ())]
                if not mutual:
                    raise
                mutual_retry_used = True
                version = mutual[-1]
                continue
            return
        # A successful raw discover result: identical handling to the SDK.
        try:
            result = types.DiscoverResult.model_validate(raw)
        except ValidationError:
            await session.initialize()  # unparseable result → not modern evidence
            return
        if not any(v in result.supported_versions for v in MODERN_PROTOCOL_VERSIONS):
            await session.initialize()  # explicit legacy advertisement
            return
        session.adopt(result)
        return


def install_probe_hardening() -> None:
    """Swap the hardened policy into BOTH import bindings (idempotent, logged).

    ``fastmcp.client.client`` binds ``negotiate_auto`` by ``from``-import, so
    the SDK module attribute AND fastmcp's module attribute must both point at
    the hardened function.
    """

    global _INSTALLED
    if _INSTALLED:
        return
    import fastmcp.client.client as fastmcp_client  # noqa: PLC0415
    import mcp.client._probe as sdk_probe  # noqa: PLC0415

    sdk_probe.negotiate_auto = hardened_negotiate_auto
    fastmcp_client.negotiate_auto = hardened_negotiate_auto
    _INSTALLED = True
    logger.info(
        "mcp_probe_hardening_installed reason=timeout_is_not_an_era_verdict retries=%d",
        _resolve_timeout_retries(),
    )
