"""Regression lock (#1325/#1113): the tool executor's outer client carries
CLIO's elicitation handler so a size-guard ``InputRequiredResult`` carried back
from a PROXY-routed namespace (e.g. ``pandas``) is dispatched instead of failing
``MCPError(-32600, 'Elicitation not supported')``.

A plain data server never advertises the SEP-2663 tasks capability, so
``resolve_and_build_direct_client`` demotes it to the proxy path and the
executor builds its client via ``self._client_factory``. Left bare (the default
``make_mcp_client``) that client had the MRTR round cap but no
``elicitation_handler`` — so the whole agent-driven size-guard round-trip died on
the SDK's default callback. ``ClioAgent`` now threads
``_correlated_execution_client_factory()`` into every executor it builds; this
test pins that the factory binds BOTH the correlated handler and the capability
declaration ``build_gateway`` already threads onto the backend/direct paths.
"""

from __future__ import annotations

from clio_agent.agent import _correlated_execution_client_factory
from clio_agent.gact.elicitation_correlation import correlated_elicitation_handler


def test_execution_client_factory_binds_elicitation_handler_and_capabilities() -> None:
    factory = _correlated_execution_client_factory()

    # A functools.partial over make_mcp_client with the execution-path bundle bound.
    bound = factory.keywords

    handlers = bound.get("handlers")
    assert handlers is not None, "executor client_factory must carry an MCPClientHandlers bundle"
    assert handlers.elicitation is correlated_elicitation_handler, (
        "the outer client that drives the MRTR loop for a proxy-routed namespace "
        "must dispatch elicitations through CLIO's correlated handler, not the SDK "
        "default (which raises 'Elicitation not supported')"
    )

    capabilities = bound.get("capabilities")
    assert capabilities is not None, (
        "an explicit capability declaration must be threaded so the advertised "
        "envelope matches the wired handler (form elicitation always advertised)"
    )
