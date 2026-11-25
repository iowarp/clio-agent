"""CLIO client capability advertisements, composed with native SDK Tasks.

FastMCP/fastmcp-tasks owns task declaration and execution driving. CLIO's
explicit extension entries advertise MCP Apps and agent-directed elicitation;
they add no task store, resolver or lifecycle. Proxy clients retain those ads,
while the SDK controls its internal task extension policy.

The read side records server declarations through mcp_connection_era. The
known-extension catalog also describes native Tasks and unsupported auth
extensions; catalog entries alone never enable execution or authentication."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mcp.client.extension import ClientExtension

# Wire identifiers are metadata, not a reason to import the entire MCP client.
# SDK parity is enforced by test_startup_imports.
UI_EXTENSION_ID = "io.modelcontextprotocol/ui"
MCP_APP_MIME_TYPE = "text/html;profile=mcp-app"
TASKS_EXTENSION_ID = "io.modelcontextprotocol/tasks"
#: A2UI client ad (HS1a). A2UI publishes no official MCP extension id yet; if it
#: does, advertise both.
A2UI_EXTENSION_ID = "org.a2ui/a2ui"
#: The mime type of an A2UI message batch in a tool result or a resource (HS1).
A2UI_MIME_TYPE = "application/a2ui+json"

__all__ = [
    "A2UI_EXTENSION_ID",
    "A2UI_MIME_TYPE",
    "AGENT_ELICITATION_EXTENSION_ID",
    "ENTERPRISE_MANAGED_AUTH_EXTENSION_ID",
    "KNOWN_EXTENSIONS",
    "MCP_APPS_PROTOCOL_REVISION",
    "MCP_APP_MIME_TYPE",
    "OAUTH_CLIENT_CREDENTIALS_EXTENSION_ID",
    "RELAY_ASK_EXTENSION_ID",
    "RELAY_CHOICE_EXTENSION_ID",
    "TASKS_EXTENSION_ID",
    "UI_EXTENSION_ID",
    "ExtensionsDeclaration",
    "MCPExtensionDeclaration",
    "MCPKnownExtension",
    "extensions_declaration",
    "known_extension",
]

#: ``gact/mcp_apps.py``'s Apps HOST revision (SEP-1865, built 2026-01-26). The
#: host itself is regression-locked (#1283: "do not touch its behavior") --
#: this constant only RELOCATES the "2026-01-26" literal that used to live
#: twice, hand-typed, inline in that file (~:446 and ~:626) to ONE source, so
#: a future revision bump touches one line instead of two.
MCP_APPS_PROTOCOL_REVISION = "2026-01-26"

#: Obligations doc row J1: M2M auth for headless/CI. Catalog-only (#1283
#: point 3) -- no ClientExtension is built for it in this slice.
OAUTH_CLIENT_CREDENTIALS_EXTENSION_ID = "io.modelcontextprotocol/oauth-client-credentials"

#: Obligations doc row J2: ID-JAG token exchange + org config. Catalog-only
#: (#1283 point 3) -- no ClientExtension is built for it in this slice.
ENTERPRISE_MANAGED_AUTH_EXTENSION_ID = "io.modelcontextprotocol/enterprise-managed-auth"

#: CLIO's OWN vendor extension (#1325 negotiation): declaring it tells a server
#: "this client drives the modern-era MRTR ``InputRequiredResult`` loop and honors
#: the ``x-clio-agent/audience: agent`` hint -- an over-budget tool may hand its
#: narrowing question to the session's agent (agent-first, human as the terminal
#: fallback; see ``gact/agent_elicitation.py`` and
#: ``docs/design/agent-driven-elicitation-and-mrtr.md``)." A server READS it via
#: ``ctx.client_supports_extension(AGENT_ELICITATION_EXTENSION_ID)`` and gates its
#: size-guard on it: present -> return an ``InputRequiredResult`` tagged for the
#: agent; ABSENT (any generic / non-CLIO client) -> return the full result, so the
#: MCP tool still works for everyone. This is the negotiation signal that was
#: missing while only the transport + routing existed -- the reason a size-guard
#: could not tell a CLIO client apart from a generic one. Uses the repo's existing
#: ``x-clio-agent/*`` vendor namespace (the same convention as
#: ``AGENT_AUDIENCE_META_KEY``), which satisfies SEP-2133's reverse-DNS-prefix rule.
AGENT_ELICITATION_EXTENSION_ID = "x-clio-agent/agent-driven-elicitation"

#: clio-relay's typed ask facts: ``_meta["io.iowarp.clio-relay/ask"][ask_id]`` is
#: ``{detail, evidence}`` and ``.../choice`` a choice's options. A client that declares
#: either gets the question sentence alone as the message and shows the facts itself
#: (the web question card's Evidence disclosure); one that does not gets them inlined
#: in the message. See clio-relay ``docs/mcp-tasks.md`` ("what a question says").
RELAY_ASK_EXTENSION_ID = "io.iowarp.clio-relay/ask"
RELAY_CHOICE_EXTENSION_ID = "io.iowarp.clio-relay/choice"


@dataclass(frozen=True)
class MCPExtensionDeclaration:
    """One registry entry's outcome for ONE client construction.

    ``extension`` is ``None`` exactly when this entry opted out for this
    construction (e.g. tasks on a client class that forbids internal
    extensions); ``reason`` then carries the typed reason, mirroring
    :class:`~clio_agent.tools.mcp_task_extension.TasksDeclaration`.
    """

    identifier: str
    extension: ClientExtension | None
    reason: str | None


@dataclass(frozen=True)
class ExtensionsDeclaration:
    """Every :data:`_ACTIVE_ENTRIES` entry's outcome for ONE client construction."""

    #: The flattened, ready-to-pass ``extensions=`` list (every entry whose
    #: ``extension`` was not ``None``, in registry order).
    extensions: tuple[ClientExtension, ...]
    #: EVERY entry's full outcome, active or suppressed -- diagnostics/tests
    #: read this to see WHY an entry did or did not participate.
    entries: tuple[MCPExtensionDeclaration, ...]


def _build_ui(client_cls: Any, target: Any) -> MCPExtensionDeclaration:  # noqa: ARG001 - uniform entry signature
    """Registry entry #2: the MCP Apps ``ui`` capability ad (#1283 letter (d)).

    Ad-only, unconditional (see the module docstring for why this entry,
    unlike tasks, is never suppressed for a proxy-like client class). The
    ``mimeTypes`` setting is REQUIRED (review round 1, F1): the SDK's own
    ``client_supports_apps`` gate reads ``settings["mimeTypes"]`` and returns
    ``False`` for an ad with no settings at all -- an inert declaration no
    spec-compliant server would ever act on. See
    ``test_mcp_extension_registry.py``'s raw-SDK-server test for the
    before/after proof.
    """

    from mcp.client.extension import advertise  # noqa: PLC0415

    return MCPExtensionDeclaration(
        identifier=UI_EXTENSION_ID,
        extension=advertise(UI_EXTENSION_ID, {"mimeTypes": [MCP_APP_MIME_TYPE]}),
        reason=None,
    )


def _build_agent_elicitation(client_cls: Any, target: Any) -> MCPExtensionDeclaration:  # noqa: ARG001 - uniform entry signature
    """Registry entry #3: CLIO's agent-driven-elicitation ad (#1325 negotiation).

    Ad-only (no claims, no notifications) and unconditional -- like ``ui`` and
    unlike ``tasks``. Every execution-path client carries the MRTR round cap
    (``make_mcp_client`` sets ``input_required_max_rounds`` on ALL of them), so
    every such client genuinely drives an ``InputRequiredResult`` loop and honors
    the ``x-clio-agent/audience`` hint (agent-first, human terminal fallback) --
    the claim is truthful on a direct client and on a proxy backend leg alike (the
    front CLIO answers, exactly as it relays a ui-bearing result). It is NOT gated
    on ``tools.mcp.elicitation.agent_audience.enabled``: disabling that toggle only
    routes the SAME question to the human instead of the agent, so the round-trip
    still completes -- the extension asserts protocol support, not a routing
    guarantee. Declaring it is the negotiation signal a size-guard server reads
    (:data:`AGENT_ELICITATION_EXTENSION_ID`) to prefer the agent over dumping a fat
    result; absence means "generic client, return everything."
    """

    from mcp.client.extension import advertise  # noqa: PLC0415

    return MCPExtensionDeclaration(
        identifier=AGENT_ELICITATION_EXTENSION_ID,
        extension=advertise(AGENT_ELICITATION_EXTENSION_ID),
        reason=None,
    )


def _build_relay_ask(client_cls: Any, target: Any) -> MCPExtensionDeclaration:  # noqa: ARG001 - uniform entry signature
    """Registry entry: CLIO reads clio-relay's typed ask facts (:data:`RELAY_ASK_EXTENSION_ID`).

    Ad-only and unconditional, like ``ui``: a server that does not know the id
    ignores it. The elicitation path keeps a request's namespaced ``_meta`` on the
    question (``metadata.elicitation.meta``) and the question card renders the
    detail and evidence behind a closed disclosure.
    """

    from mcp.client.extension import advertise  # noqa: PLC0415

    return MCPExtensionDeclaration(
        identifier=RELAY_ASK_EXTENSION_ID,
        extension=advertise(RELAY_ASK_EXTENSION_ID),
        reason=None,
    )


def _build_relay_choice(client_cls: Any, target: Any) -> MCPExtensionDeclaration:  # noqa: ARG001 - uniform entry signature
    """Registry entry: CLIO reads clio-relay's typed choices (:data:`RELAY_CHOICE_EXTENSION_ID`).

    The question card draws each declared option with its label, meaning, note and
    recommendation; see :func:`_build_relay_ask`.
    """

    from mcp.client.extension import advertise  # noqa: PLC0415

    return MCPExtensionDeclaration(
        identifier=RELAY_CHOICE_EXTENSION_ID,
        extension=advertise(RELAY_CHOICE_EXTENSION_ID),
        reason=None,
    )


def _build_a2ui(catalogs: tuple[str, ...]) -> MCPExtensionDeclaration:
    """The A2UI client ad (HS1a): the A2UI versions and catalogs this host renders for a server.

    ``catalogs`` are the catalog ids the server's plugin ships; a server whose plugin
    ships none gets no entry, since it has nothing CLIO could render.
    """

    from mcp.client.extension import advertise  # noqa: PLC0415

    from clio_agent.gact.protocol.constants import A2UI_WIRE_VERSIONS  # noqa: PLC0415

    if not catalogs:
        raise ValueError("the A2UI ad needs at least one catalog id")
    versions = sorted(A2UI_WIRE_VERSIONS)
    settings = {
        "versions": versions,
        "clientCapabilities": {
            version: {"supportedCatalogIds": list(catalogs)} for version in versions
        },
    }
    return MCPExtensionDeclaration(
        identifier=A2UI_EXTENSION_ID,
        extension=advertise(A2UI_EXTENSION_ID, settings),
        reason=None,
    )


#: Fresh ad-only entries supplement the SDK's native Tasks extension.
#: They do not own execution identities, polling, results or task storage.
_ACTIVE_ENTRIES: tuple[Callable[[Any, Any], MCPExtensionDeclaration], ...] = (
    _build_ui,
    _build_agent_elicitation,
    _build_relay_ask,
    _build_relay_choice,
)


def extensions_declaration(
    client_cls: Any, target: Any = None, *, a2ui_catalogs: tuple[str, ...] = ()
) -> ExtensionsDeclaration:
    """Build fresh CLIO Apps/input advertisements for one SDK client.

    Native Tasks are folded by FastMCP itself, not by this registry. Client and
    transport arguments are passed to ad builders; no execution identity or task
    state is created here. ``a2ui_catalogs`` are the catalog ids of the server's
    plugin (HS1a)."""

    outcomes = tuple(build(client_cls, target) for build in _ACTIVE_ENTRIES)
    if a2ui_catalogs:
        outcomes += (_build_a2ui(tuple(a2ui_catalogs)),)
    # Suppression is already logged at its source (e.g.
    # ``mcp_task_extension.tasks_declaration``'s own ``logger.debug`` call);
    # this composition site does not re-log it (review round 1: no duplicate
    # log lines per suppression).
    built = tuple(entry.extension for entry in outcomes if entry.extension is not None)
    return ExtensionsDeclaration(extensions=built, entries=outcomes)


@dataclass(frozen=True)
class MCPKnownExtension:
    """One entry in the STATIC catalog of MCP extensions clio_agent knows about.

    Distinct from :data:`_ACTIVE_ENTRIES` (what a client construction actually
    folds into its ``extensions=`` kwarg): every entry here is DATA (an
    identifier, the spec reference it tracks, and whether clio_agent actively
    declares it), so ``actively_declared=False`` entries are documentation of
    a known gap, never live behavior (#1283 point 3).
    """

    identifier: str
    spec_reference: str
    actively_declared: bool
    note: str


KNOWN_EXTENSIONS: tuple[MCPKnownExtension, ...] = (
    MCPKnownExtension(
        identifier=TASKS_EXTENSION_ID,
        spec_reference="SEP-2663",
        actively_declared=True,
        note="native FastMCP/fastmcp-tasks client extension; no CLIO execution driver",
    ),
    MCPKnownExtension(
        identifier=UI_EXTENSION_ID,
        spec_reference="SEP-1865 (MCP Apps)",
        actively_declared=True,
        note=(
            "registry entry #2, declared ad-only and unconditionally; the Apps HOST "
            f"(gact/mcp_apps.py, revision {MCP_APPS_PROTOCOL_REVISION}) is unchanged/regression-locked"
        ),
    ),
    MCPKnownExtension(
        identifier=AGENT_ELICITATION_EXTENSION_ID,
        spec_reference="#1325 (agent-driven-elicitation negotiation signal)",
        actively_declared=True,
        note=(
            "registry entry #3, declared ad-only and unconditionally; a size-guard "
            "server reads it via ctx.client_supports_extension to prefer the agent "
            "over dumping a fat result (absent -> return everything)"
        ),
    ),
    MCPKnownExtension(
        identifier=A2UI_EXTENSION_ID,
        spec_reference="A2UI v0.9 (host semantics HS1a)",
        actively_declared=True,
        note=(
            "declared only for a server whose plugin ships A2UI catalogs; settings name "
            "the versions and the plugin's catalog ids"
        ),
    ),
    MCPKnownExtension(
        identifier=RELAY_ASK_EXTENSION_ID,
        spec_reference="clio-relay docs/mcp-tasks.md (what a question says)",
        actively_declared=True,
        note=(
            "declared ad-only and unconditionally; relay then sends the question sentence "
            "alone and its detail and evidence typed in _meta, which the question card shows"
        ),
    ),
    MCPKnownExtension(
        identifier=RELAY_CHOICE_EXTENSION_ID,
        spec_reference="clio-relay docs/mcp-tasks.md (what a question says)",
        actively_declared=True,
        note="declared ad-only and unconditionally; the question card draws relay's typed choices",
    ),
    MCPKnownExtension(
        identifier=OAUTH_CLIENT_CREDENTIALS_EXTENSION_ID,
        spec_reference="obligations doc row J1 (M2M auth for headless/CI)",
        actively_declared=False,
        note="enumerated per #1283 point 3; declaration path unverified, no ClientExtension built yet",
    ),
    MCPKnownExtension(
        identifier=ENTERPRISE_MANAGED_AUTH_EXTENSION_ID,
        spec_reference="obligations doc row J2 (ID-JAG token exchange + org config)",
        actively_declared=False,
        note="enumerated per #1283 point 3; not in fastmcp, only relevant if enterprise demands it",
    ),
)


def known_extension(identifier: str) -> MCPKnownExtension | None:
    """Return the :data:`KNOWN_EXTENSIONS` catalog entry for ``identifier``, if any."""

    for entry in KNOWN_EXTENSIONS:
        if entry.identifier == identifier:
            return entry
    return None
