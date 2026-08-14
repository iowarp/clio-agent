"""codex model-catalog discovery: the real ``model/list`` app-server RPC
(iowarp/clio-agent#1211)."""

from __future__ import annotations

from clio_agent.providers.model_discovery.overlay import (
    CODEX_SOURCE,
    ProviderDiscoveryResult,
    attach_context_limits,
)


def discover_codex(*, timeout: float = 20.0) -> ProviderDiscoveryResult:
    """Refresh codex's live model catalog via the app-server's ``model/list`` RPC.

    Verified live 2026-08-14 against codex-cli 0.147.0: the account's REAL current
    catalog (``gpt-5.6-sol`` default, ``gpt-5.6-terra``, ``gpt-5.6-luna``,
    ``gpt-5.5``, ``gpt-5.4``, ``gpt-5.4-mini``, ``gpt-5.3-codex-spark``) — none of
    which is ``gpt-5.5-codex``/``gpt-5.1`` (#1184's stale, rejected pins).
    Synchronous+blocking (the app-server bridge's JSON-RPC call is a blocking
    socket read); callers on the async refresh path wrap this in
    ``asyncio.to_thread``. Constructs a STANDALONE :class:`CodexAppServerProcess`
    directly — NEVER through ``_APP_SERVER_POOL.process_for`` (#1211 review S1).
    An earlier version keyed into the shared pool at ``(model="", cwd=None)``;
    that key can never collide with a real turn's process today (turns always
    key on a real model id), but going through the pool at all is the wrong
    shape for a rare, explicit, always-immediately-closed action — a bare pool
    key is an aliasing hazard by construction (a future caller that legitimately
    used an empty model string would have its live process closed out from under
    it by a concurrent discovery call). A standalone process can never alias
    with anything the pool serves real turns from. Closed immediately after
    listing (#1211 review R4) — this is a one-off, not a process meant to be
    reused across calls.
    """
    from clio_agent.providers.codex_app_server import CodexAppServerError, CodexAppServerProcess
    from clio_agent.providers.codex_litellm import (  # noqa: PLC0415
        CodexCLIUnavailableError,
        _resolve_codex_binary,
    )

    try:
        binary = _resolve_codex_binary()
    except CodexCLIUnavailableError as exc:
        return ProviderDiscoveryResult(
            provider="codex", discovered=[], source=CODEX_SOURCE, failed_reason=str(exc)
        )
    process = CodexAppServerProcess(binary=binary, model="", cwd=None)
    try:
        rows = process.list_models(timeout=timeout)
    except CodexAppServerError as exc:
        return ProviderDiscoveryResult(
            provider="codex", discovered=[], source=CODEX_SOURCE, failed_reason=str(exc)
        )
    finally:
        process.close()  # one-off discovery process -- never left warm in the pool
    discovered = [
        {
            "id": str(m.get("id") or ""),
            "name": str(m.get("displayName") or m.get("id") or ""),
            "description": str(m.get("description") or ""),
        }
        for m in rows
        if isinstance(m, dict) and m.get("id")
    ]
    if not discovered:
        return ProviderDiscoveryResult(
            provider="codex",
            discovered=[],
            source=CODEX_SOURCE,
            failed_reason="codex app-server model/list returned zero models",
        )
    default_model = next(
        (str(m.get("id")) for m in rows if isinstance(m, dict) and m.get("isDefault")),
        discovered[0]["id"],
    )
    discovered = attach_context_limits(discovered, "codex")
    return ProviderDiscoveryResult(
        provider="codex", discovered=discovered, source=CODEX_SOURCE, default_model=default_model
    )


__all__ = ["discover_codex"]
