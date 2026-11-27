"""Client capability composition retains native SDK ownership of Tasks.

Unit cases cover CLIO's fresh Apps/input advertisements and their composition
with the installed SDK's Tasks extension. Retained real-SDK Apps tests check
mime settings and resource gates. Product task acceptance and delivery are
covered separately by replacement Core/native boundary cases."""

from __future__ import annotations

from fastmcp import Client
from fastmcp.client.extension_hooks import build_internal_client_extensions
from fastmcp.server.providers.proxy import ProxyClient
from fastmcp_tasks.client import TasksClientExtension
from mcp.client.extension import ClientExtension, advertise
from mcp.server.apps import Apps, client_supports_apps
from mcp.server.mcpserver import Context as SDKContext
from mcp.server.mcpserver import MCPServer

from clio_agent.tools.mcp_extension_registry import (
    A2UI_EXTENSION_ID,
    AGENT_ELICITATION_EXTENSION_ID,
    ENTERPRISE_MANAGED_AUTH_EXTENSION_ID,
    KNOWN_EXTENSIONS,
    MCP_APP_MIME_TYPE,
    MCP_APPS_PROTOCOL_REVISION,
    OAUTH_CLIENT_CREDENTIALS_EXTENSION_ID,
    RELAY_ASK_EXTENSION_ID,
    RELAY_CHOICE_EXTENSION_ID,
    TASKS_EXTENSION_ID,
    UI_EXTENSION_ID,
    ExtensionsDeclaration,
    MCPExtensionDeclaration,
    extensions_declaration,
    known_extension,
)

# --------------------------------------------------------------------------- #
# extensions_declaration: pure composition, no real connection needed.        #
# --------------------------------------------------------------------------- #


def test_extensions_declaration_returns_the_typed_shape() -> None:
    declaration = extensions_declaration(Client, object())

    assert isinstance(declaration, ExtensionsDeclaration)
    assert all(isinstance(entry, MCPExtensionDeclaration) for entry in declaration.entries)
    assert all(isinstance(ext, ClientExtension) for ext in declaration.extensions)


def test_clio_extension_order_is_apps_then_agent_elicitation_then_relay_asks() -> None:
    """Native Tasks are contributed by the SDK, outside CLIO's ad-only composition."""
    declaration = extensions_declaration(Client, object())
    assert [entry.identifier for entry in declaration.entries] == [
        UI_EXTENSION_ID,
        AGENT_ELICITATION_EXTENSION_ID,
        RELAY_ASK_EXTENSION_ID,
        RELAY_CHOICE_EXTENSION_ID,
    ]


def test_native_sdk_tasks_compose_with_clio_capability_ads() -> None:
    declaration = extensions_declaration(Client, object())
    internal = build_internal_client_extensions(None)
    native = [ext for ext in internal if ext.identifier == TASKS_EXTENSION_ID]
    assert len(native) == 1 and isinstance(native[0], TasksClientExtension)
    assert {ext.identifier for ext in [*internal, *declaration.extensions]} == {
        TASKS_EXTENSION_ID,
        UI_EXTENSION_ID,
        AGENT_ELICITATION_EXTENSION_ID,
        RELAY_ASK_EXTENSION_ID,
        RELAY_CHOICE_EXTENSION_ID,
    }
    assert all(entry.reason is None for entry in declaration.entries)


def test_proxy_clio_declarations_preserve_apps_and_input_ads() -> None:
    """Native SDK controls its own task suppression; CLIO has no custom task entry."""
    declaration = extensions_declaration(ProxyClient, object())
    by_id = {entry.identifier: entry for entry in declaration.entries}
    assert set(by_id) == {
        UI_EXTENSION_ID,
        AGENT_ELICITATION_EXTENSION_ID,
        RELAY_ASK_EXTENSION_ID,
        RELAY_CHOICE_EXTENSION_ID,
    }
    assert all(entry.extension is not None and entry.reason is None for entry in by_id.values())
    assert [ext.identifier for ext in declaration.extensions] == [
        UI_EXTENSION_ID,
        AGENT_ELICITATION_EXTENSION_ID,
        RELAY_ASK_EXTENSION_ID,
        RELAY_CHOICE_EXTENSION_ID,
    ]


def test_extensions_declaration_ui_entry_is_ad_only() -> None:
    """The ui entry carries no claims/notifications -- a bare capability ad
    (:func:`mcp.client.extension.advertise`), never behavior -- but its
    settings are NOT empty (#1283 review round 1, F1): the SDK's own
    ``client_supports_apps`` gate reads ``settings["mimeTypes"]``, so an
    empty-settings ad would be inert against a spec-compliant server. See
    ``test_ui_declaration_mime_types_pass_the_sdk_apps_gate`` below for the
    live SDK-server proof of why this matters."""

    declaration = extensions_declaration(Client, object())
    ui_entry = next(entry for entry in declaration.entries if entry.identifier == UI_EXTENSION_ID)

    assert ui_entry.extension is not None
    assert ui_entry.extension.settings() == {"mimeTypes": [MCP_APP_MIME_TYPE]}
    assert ui_entry.extension.claims() == ()
    assert ui_entry.extension.notifications() == ()


def test_extensions_declaration_agent_elicitation_entry_is_ad_only() -> None:
    """The agent-driven-elicitation entry (#1325) is a bare capability ad -- no
    claims, no notifications, no settings. A size-guard server only needs its
    PRESENCE (``ctx.client_supports_extension``); it carries no behavior of its
    own, so it stays ad-only like ``ui``."""

    declaration = extensions_declaration(Client, object())
    entry = next(e for e in declaration.entries if e.identifier == AGENT_ELICITATION_EXTENSION_ID)

    assert entry.extension is not None
    assert entry.extension.settings() == {}
    assert entry.extension.claims() == ()
    assert entry.extension.notifications() == ()


def test_extensions_declaration_is_called_fresh_every_construction() -> None:
    """Independent client construction receives fresh ad-only extension instances."""
    target = object()
    first = extensions_declaration(Client, target)
    second = extensions_declaration(Client, target)
    assert len(first.entries) == len(second.entries) == 4
    for left, right in zip(first.entries, second.entries, strict=True):
        assert left.identifier == right.identifier
        assert left.extension is not None and right.extension is not None
        assert left.extension is not right.extension


# --------------------------------------------------------------------------- #
# KNOWN_EXTENSIONS: the static, declare-side-only catalog (#1283 point 3).    #
# --------------------------------------------------------------------------- #


def test_known_extensions_catalog_covers_every_active_entry_and_the_two_enumerated_ones() -> None:
    identifiers = {entry.identifier for entry in KNOWN_EXTENSIONS}
    assert identifiers == {
        TASKS_EXTENSION_ID,
        UI_EXTENSION_ID,
        AGENT_ELICITATION_EXTENSION_ID,
        A2UI_EXTENSION_ID,
        RELAY_ASK_EXTENSION_ID,
        RELAY_CHOICE_EXTENSION_ID,
        OAUTH_CLIENT_CREDENTIALS_EXTENSION_ID,
        ENTERPRISE_MANAGED_AUTH_EXTENSION_ID,
    }


def test_known_extensions_flags_match_native_sdk_and_clio_declarations() -> None:
    """Catalog claims cover both native Tasks and CLIO's ad-only capabilities.

    The A2UI ad is built only for a server whose plugin ships catalogs, so the
    declaration here is for such a server."""
    declaration = extensions_declaration(Client, object(), a2ui_catalogs=("example/catalog",))
    native = build_internal_client_extensions(None)
    actively_built = {ext.identifier for ext in [*native, *declaration.extensions]}
    for entry in KNOWN_EXTENSIONS:
        assert entry.actively_declared == (entry.identifier in actively_built), entry.identifier


def test_known_extension_lookup_finds_a_cataloged_identifier() -> None:
    entry = known_extension(OAUTH_CLIENT_CREDENTIALS_EXTENSION_ID)
    assert entry is not None
    assert entry.actively_declared is False
    assert "J1" in entry.spec_reference or "headless" in entry.note.lower()


def test_known_extension_lookup_returns_none_for_an_unknown_identifier() -> None:
    assert known_extension("io.example/not-a-real-extension") is None


# --------------------------------------------------------------------------- #
# MCP_APPS_PROTOCOL_REVISION: the single source gact/mcp_apps.py reads from   #
# instead of two hand-typed "2026-01-26" literals.                            #
# --------------------------------------------------------------------------- #


def test_mcp_apps_protocol_revision_is_the_documented_host_revision() -> None:
    assert MCP_APPS_PROTOCOL_REVISION == "2026-01-26"


def test_mcp_apps_reads_the_revision_constant_not_a_literal() -> None:
    """Regression pin for the #1283 relocation: both former hardcoded sites
    in ``gact/mcp_apps.py`` now reference the registry constant."""

    from clio_agent.gact import mcp_apps

    assert mcp_apps.MCP_APPS_PROTOCOL_REVISION is MCP_APPS_PROTOCOL_REVISION


def test_mcp_apps_and_artifacts_wire_share_the_same_mime_object() -> None:
    """Regression pin for the #1283 review-round F1 residual: the MIME
    literal that used to be hand-typed independently in ``gact/mcp_apps.py``
    AND ``gact/artifacts/wire.py`` now both alias the SAME registry constant."""

    from clio_agent.gact import mcp_apps
    from clio_agent.gact.artifacts import wire

    assert mcp_apps.MCP_APP_MIME_TYPE is MCP_APP_MIME_TYPE
    assert wire.UI_PAYLOAD_MIME is MCP_APP_MIME_TYPE


# --------------------------------------------------------------------------- #
# #1283 review round 1, F1 (MUST-FIX): the SDK's own MCP Apps compliance     #
# gate (mcp.server.apps.client_supports_apps) requires settings["mimeTypes"] #
# -- an empty-settings ad (the pre-fix shape) is INERT: no spec-compliant    #
# server would ever attach _meta.ui for it. fastmcp's own servers stamp ui   #
# in ServerCapabilities unconditionally (no client_supports_apps equivalent  #
# exists in fastmcp), so this gap was invisible to every fastmcp-backed      #
# test in this suite -- it needs the RAW SDK server to prove.                #
# --------------------------------------------------------------------------- #


def _build_sdk_apps_gate_server() -> MCPServer:
    """A minimal raw-SDK ``MCPServer`` with the real ``Apps`` extension and one
    tool that reports back whatever the SDK's own gate decides."""

    apps = Apps()
    server = MCPServer("sdk-apps-gate", extensions=[apps])

    @server.tool()
    def gate_probe(ctx: SDKContext) -> bool:
        return client_supports_apps(ctx)

    return server


async def test_ui_declaration_mime_types_pass_the_sdk_apps_gate() -> None:
    """RED-FIRST PROOF (review round 1): an ad-only ``ui`` declaration with NO
    ``mimeTypes`` setting (the pre-fix shape -- what ``advertise(UI_EXTENSION_ID)``
    alone produces) reads ``client_supports_apps(ctx) is False`` on a REAL SDK
    server; the registry's actual ui entry (``mimeTypes`` set, post-fix) reads
    ``True`` on the SAME server. This is the SDK's own MUST -- not something
    the fastmcp-backed exerciser/conformance suites could ever catch, since
    fastmcp declares no such gate at all.
    """

    server = _build_sdk_apps_gate_server()

    # Pre-fix shape: an ad-only declaration with NO settings -- what this
    # registry's ui entry declared before the F1 fix.
    async with Client(server, extensions=[advertise(UI_EXTENSION_ID)]) as client:
        pre_fix = await client.call_tool("gate_probe", {})
    assert pre_fix.data.result is False, (
        "an empty-settings ui ad must never pass the SDK's client_supports_apps gate"
    )

    # Post-fix: the registry's actual, currently-declared ui entry.
    declaration = extensions_declaration(Client, object())
    async with Client(server, extensions=list(declaration.extensions)) as client:
        post_fix = await client.call_tool("gate_probe", {})
    assert post_fix.data.result is True, (
        "the registry's ui entry must declare mimeTypes so a spec-compliant "
        "server actually attaches _meta.ui for this client"
    )
