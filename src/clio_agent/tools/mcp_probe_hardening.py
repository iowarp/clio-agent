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

#1232 pt 4: that retry budget is now PER-SERVER, not only global. Raising
``CLIO_MCP_PROBE_TIMEOUT_RETRIES``/``tools.mcp.probe_timeout_retries`` for one
slow-but-legitimate server used to multiply the connect cost of every OTHER
dead namespace too (they all share the one global knob). A caller that knows
which server/namespace it is connecting on behalf of binds
:func:`probe_server_context` around the connect; :func:`resolve_timeout_retries`
then reads ``tools.mcp.servers.<server_id>.probe_timeout_retries`` first
(config-FILE only — see the dynamic env name below) and falls back to the
unchanged global default when no override is declared for that server.
"""

from __future__ import annotations

import contextvars
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

logger = logging.getLogger(__name__)

_INSTALLED = False

# The MCP server/namespace name the CURRENT connect is being made on behalf of
# (#1232 pt 4). Contextvars propagate correctly across the awaits inside one
# asyncio task, so a bounded-concurrent discovery pass (tools/mcp_discovery.py)
# binding this around each namespace's own task gets the right per-server
# override even though every namespace probes at once.
_CURRENT_PROBE_SERVER_ID: contextvars.ContextVar[str] = contextvars.ContextVar(
    "clio_mcp_probe_server_id", default=""
)
_CURRENT_PROBE_TIMEOUT_RETRIES: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "clio_mcp_probe_timeout_retries", default=None
)


@contextmanager
def probe_server_context(server_id: str, *, timeout_retries: int | None = None) -> Iterator[None]:
    """Bind the MCP server/namespace name for the connect(s) made in this scope.

    Callers that connect on behalf of a KNOWN declared server (the boot
    discovery pass, ``gateway._list_declared_tools``) wrap their connect with
    this so :func:`resolve_timeout_retries` can resolve that server's override.
    An unbound connect (never entered) always uses the global default.
    """

    server_token = _CURRENT_PROBE_SERVER_ID.set(server_id or "")
    retries_token = _CURRENT_PROBE_TIMEOUT_RETRIES.set(timeout_retries)
    try:
        yield
    finally:
        _CURRENT_PROBE_TIMEOUT_RETRIES.reset(retries_token)
        _CURRENT_PROBE_SERVER_ID.reset(server_token)


def resolve_timeout_retries(server_id: str = "") -> int:
    """The probe-timeout retry budget for one server (#1232 pt 4).

    ``server_id`` defaults to the CURRENTLY-BOUND :func:`probe_server_context`
    value. Resolution order: a per-server override
    (``tools.mcp.servers.<server_id>.probe_timeout_retries``, config-FILE only
    — the env name is synthesized per server id, so it is deliberately absent
    from the generated env reference, same convention as
    ``CLIO_CRED_<PROVIDER>_<ACCOUNT>`` in ``providers/credentials.py``) then the
    unchanged global ``tools.mcp.probe_timeout_retries`` default (3).
    """

    from clio_agent import conf  # noqa: PLC0415 - avoid import cycle at module load

    declared_override = _CURRENT_PROBE_TIMEOUT_RETRIES.get()
    if declared_override is not None:
        return declared_override
    resolved_id = (server_id or _CURRENT_PROBE_SERVER_ID.get()).strip()
    if resolved_id:
        env_key = "".join(c if c.isalnum() else "_" for c in resolved_id.upper())
        override = conf.resolve(
            f"tools.mcp.servers.{resolved_id}.probe_timeout_retries",
            env=f"CLIO_MCP_PROBE_TIMEOUT_RETRIES__{env_key}",
            default=None,
            cast=conf.as_int,
        )
        if override is not None:
            return int(override)
    return _resolve_global_timeout_retries()


def _resolve_global_timeout_retries() -> int:
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
    timeout_retries_left = resolve_timeout_retries()

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
        _resolve_global_timeout_retries(),
    )


__all__ = [
    "hardened_negotiate_auto",
    "install_probe_hardening",
    "probe_server_context",
    "resolve_timeout_retries",
]
