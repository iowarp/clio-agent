"""The generic MCP client extension registry (#1283, campaign C1-S3).

Pure, I/O-free unit coverage for ``tools/mcp_extension_registry.py`` in
isolation from any real connection: the composition logic
(:func:`extensions_declaration`), the static :data:`KNOWN_EXTENSIONS` catalog
(#1283 point 3), and the ``MCP_APPS_PROTOCOL_REVISION`` constant
``gact/mcp_apps.py`` now reads instead of hand-typing the "2026-01-26"
literal twice. End-to-end proof (a real negotiated connection, the ui-serving
arm admitted through the Apps host) lives in
``tests/test_tools/test_mcp_v2_conformance.py``'s "Layer 6" section; the
tasks-entry-unchanged regression proof lives in ``test_mcp_tasks.py``
(untouched aside from the two justified ``len(extensions)``/``"extensions"
not in captured`` updates this slice's ui entry required -- see those tests'
updated docstrings).
"""

from __future__ import annotations

from fastmcp import Client
from fastmcp.server.providers.proxy import ProxyClient
from mcp.client.extension import ClientExtension

from clio_agent.errors import MCP_TASKS_DECLARATION_SUPPRESSED
from clio_agent.tools.mcp_extension_registry import (
    ENTERPRISE_MANAGED_AUTH_EXTENSION_ID,
    KNOWN_EXTENSIONS,
    MCP_APPS_PROTOCOL_REVISION,
    OAUTH_CLIENT_CREDENTIALS_EXTENSION_ID,
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


def test_extensions_declaration_entry_order_is_tasks_then_ui() -> None:
    """Registry order: tasks stays entry #1 (matches its pre-registry
    precedence), ui is entry #2 (#1283 letter (d))."""

    declaration = extensions_declaration(Client, object())
    assert [entry.identifier for entry in declaration.entries] == [
        TASKS_EXTENSION_ID,
        UI_EXTENSION_ID,
    ]


def test_extensions_declaration_a_plain_client_declares_both() -> None:
    declaration = extensions_declaration(Client, object())

    assert len(declaration.extensions) == 2
    assert {ext.identifier for ext in declaration.extensions} == {
        TASKS_EXTENSION_ID,
        UI_EXTENSION_ID,
    }
    assert all(entry.reason is None for entry in declaration.entries)


def test_extensions_declaration_a_proxy_client_suppresses_only_tasks() -> None:
    """The tasks-specific suppression (#1119) is unchanged; ui is never
    suppressed for a client class forbidding internal extensions (#1283)."""

    declaration = extensions_declaration(ProxyClient, object())
    by_id = {entry.identifier: entry for entry in declaration.entries}

    assert by_id[TASKS_EXTENSION_ID].extension is None
    assert by_id[TASKS_EXTENSION_ID].reason == MCP_TASKS_DECLARATION_SUPPRESSED
    assert by_id[UI_EXTENSION_ID].extension is not None
    assert by_id[UI_EXTENSION_ID].reason is None
    assert [ext.identifier for ext in declaration.extensions] == [UI_EXTENSION_ID]


def test_extensions_declaration_ui_entry_is_ad_only() -> None:
    """The ui entry carries no claims/notifications -- a bare capability ad
    (:func:`mcp.client.extension.advertise`), never behavior."""

    declaration = extensions_declaration(Client, object())
    ui_entry = next(entry for entry in declaration.entries if entry.identifier == UI_EXTENSION_ID)

    assert ui_entry.extension is not None
    assert ui_entry.extension.settings() == {}
    assert ui_entry.extension.claims() == ()
    assert ui_entry.extension.notifications() == ()


def test_extensions_declaration_is_called_fresh_every_construction() -> None:
    """No memoization: two calls for the SAME client class/target each build
    fresh extension instances (tasks binds a per-construction backend
    identity; reusing an instance across constructions would be wrong)."""

    target = object()
    first = extensions_declaration(Client, target)
    second = extensions_declaration(Client, target)

    first_tasks = next(e.extension for e in first.entries if e.identifier == TASKS_EXTENSION_ID)
    second_tasks = next(e.extension for e in second.entries if e.identifier == TASKS_EXTENSION_ID)
    assert first_tasks is not second_tasks


# --------------------------------------------------------------------------- #
# KNOWN_EXTENSIONS: the static, declare-side-only catalog (#1283 point 3).    #
# --------------------------------------------------------------------------- #


def test_known_extensions_catalog_covers_every_active_entry_and_the_two_enumerated_ones() -> None:
    identifiers = {entry.identifier for entry in KNOWN_EXTENSIONS}
    assert identifiers == {
        TASKS_EXTENSION_ID,
        UI_EXTENSION_ID,
        OAUTH_CLIENT_CREDENTIALS_EXTENSION_ID,
        ENTERPRISE_MANAGED_AUTH_EXTENSION_ID,
    }


def test_known_extensions_actively_declared_flag_matches_the_active_registry() -> None:
    """The catalog's ``actively_declared`` flag must not silently drift from
    what :func:`extensions_declaration` actually builds."""

    declaration = extensions_declaration(Client, object())
    actively_built = {
        entry.identifier for entry in declaration.entries if entry.extension is not None
    }

    for entry in KNOWN_EXTENSIONS:
        if entry.actively_declared:
            assert entry.identifier in actively_built, (
                f"{entry.identifier} is marked actively_declared but extensions_declaration "
                "did not build it for a plain client"
            )
        else:
            assert entry.identifier not in actively_built, (
                f"{entry.identifier} is marked catalog-only but extensions_declaration built it "
                "-- #1283 point 3 forbids new auth behavior in this slice"
            )


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
