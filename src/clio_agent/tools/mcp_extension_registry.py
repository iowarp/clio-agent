"""Generic MCP CLIENT extension registry (#1283, campaign C1-S3 letters (a)+(d)).

Before this module, ``tools/mcp_runtime.py``'s ``make_mcp_client`` composed its
``extensions=`` kwarg from exactly ONE hardcoded call
(:func:`clio_agent.tools.mcp_task_extension.tasks_declaration`) — the tasks
extension was a special case, not a registered capability. This module makes
extension composition GENERIC: :func:`extensions_declaration` is the single
declare-side entry point ``make_mcp_client`` now calls, folding an ORDERED list
of registry entries (:data:`_ACTIVE_ENTRIES`) into one client construction.

**Entry #1 — tasks.** Wraps :func:`~clio_agent.tools.mcp_task_extension.
tasks_declaration` UNCHANGED: same suppression check
(``client_cls._auto_internal_extensions``), same typed reason
(:data:`~clio_agent.errors.MCP_TASKS_DECLARATION_SUPPRESSED`), same
:class:`~clio_agent.tools.mcp_task_extension.ClioTasksClientExtension`
instance shape. The C1-S1 task-routing test set (``test_mcp_v2_conformance.py``
layer 4, ``test_mcp_tasks.py``) proves this is behaviorally identical to the
pre-registry direct call.

**Entry #2 — MCP Apps `ui` (#1283 letter (d)).** Declares
``io.modelcontextprotocol/ui`` ad-only (:func:`mcp.client.extension.advertise`
— no claims, no behavior). Declared UNCONDITIONALLY, never suppressed for a
client class that forbids internal extensions (unlike tasks): a proxy backend
leg cannot drive a backend TASK on the caller's behalf (the reason tasks is
suppressed there), but it can always relay a ui-bearing result unchanged — ui
rendering happens entirely client-side, in the ALREADY-COMPLETE Apps host
(``gact/mcp_apps.py``, regression-locked by this slice). Declaring it is what
makes a SPEC-COMPLIANT (SEP-1865) server willing to attach ``_meta.ui`` to a
call result in the first place; today's client silently never asked.

**READ side** lives in :mod:`clio_agent.tools.mcp_connection_era` (mirrors the
era / task-capability record/latest idiom already there) — every real client
connect now ALSO records the full set of server-declared extension
identifiers, not only the tasks id. This module only re-exports
:data:`UI_EXTENSION_ID` / :data:`MCP_APPS_PROTOCOL_REVISION` so
``gact/mcp_apps.py`` reads its two "2026-01-26" revision literals from ONE
source instead of hardcoding the string twice.

**Enumerated, not built (#1283 point 3).** :data:`KNOWN_EXTENSIONS` is a
STATIC DATA catalog: the two other official 2026-07-28 extensions
(oauth-client-credentials, enterprise-managed-auth — obligations doc rows
J1/J2) are recorded for documentation/diagnostics only. Neither is folded
into :data:`_ACTIVE_ENTRIES` — building either would be real new auth
behavior, out of this slice's scope.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastmcp.utilities.tasks import TASKS_EXTENSION_ID
from mcp.client.extension import ClientExtension, advertise

logger = logging.getLogger(__name__)

__all__ = [
    "ENTERPRISE_MANAGED_AUTH_EXTENSION_ID",
    "KNOWN_EXTENSIONS",
    "MCP_APPS_PROTOCOL_REVISION",
    "OAUTH_CLIENT_CREDENTIALS_EXTENSION_ID",
    "TASKS_EXTENSION_ID",
    "UI_EXTENSION_ID",
    "ExtensionsDeclaration",
    "MCPExtensionDeclaration",
    "MCPKnownExtension",
    "extensions_declaration",
    "known_extension",
]

#: The MCP Apps extension id (SEP-1865). Re-exported here (fastmcp's own
#: ``fastmcp.apps.config.UI_EXTENSION_ID`` carries the same value) so every
#: consumer of THIS registry imports one name.
UI_EXTENSION_ID = "io.modelcontextprotocol/ui"

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


def _build_tasks(client_cls: Any, target: Any) -> MCPExtensionDeclaration:
    """Registry entry #1: tasks, wrapping ``tasks_declaration`` UNCHANGED.

    The behavior this entry produces (suppression check, backend identity,
    the typed ``mcp_tasks_declaration_suppressed`` reason) is EXACTLY what
    ``make_mcp_client`` called directly before this registry existed -- the
    C1-S1 task-routing test set proves it byte-identical.
    """

    from clio_agent.tools.mcp_task_extension import tasks_declaration  # noqa: PLC0415

    declaration = tasks_declaration(client_cls, target)
    extension = declaration.extensions[0] if declaration.extensions else None
    return MCPExtensionDeclaration(
        identifier=TASKS_EXTENSION_ID, extension=extension, reason=declaration.reason
    )


def _build_ui(client_cls: Any, target: Any) -> MCPExtensionDeclaration:  # noqa: ARG001 - uniform entry signature
    """Registry entry #2: the MCP Apps ``ui`` capability ad (#1283 letter (d)).

    Ad-only, unconditional (see the module docstring for why this entry,
    unlike tasks, is never suppressed for a proxy-like client class).
    """

    return MCPExtensionDeclaration(
        identifier=UI_EXTENSION_ID, extension=advertise(UI_EXTENSION_ID), reason=None
    )


#: Ordered registry: tasks stays entry #1 (fastmcp folds a same-identifier
#: USER extension over an INTERNAL one, so entry order does not change tasks'
#: own behavior, but keeping it first matches its pre-registry precedence and
#: keeps diffs against the C1-S1 test set minimal); ui is entry #2 (#1283
#: letter (d)). Each builder is called FRESH for every construction (never
#: memoized) since a builder may depend on per-construction state
#: (``tasks_declaration``'s ``client_cls``/``target``-derived backend identity).
_ACTIVE_ENTRIES: tuple[Callable[[Any, Any], MCPExtensionDeclaration], ...] = (
    _build_tasks,
    _build_ui,
)


def extensions_declaration(client_cls: Any, target: Any = None) -> ExtensionsDeclaration:
    """Compose the FULL ``extensions=`` list for one client construction.

    The SOLE declare-side entry point :func:`clio_agent.tools.mcp_runtime.
    make_mcp_client` calls (#1283 C1-S3 letter (a)): folds every
    :data:`_ACTIVE_ENTRIES` builder's outcome, in order, into one
    :class:`ExtensionsDeclaration`. Replaces the direct ``tasks_declaration()``
    call that used to be the sole special case at the ``make_mcp_client`` call
    site -- tasks is now registry entry #1 (behavior unchanged); ui is entry #2.

    Args:
        client_cls: The ``fastmcp.Client`` subclass this construction will
            use. Some entries (tasks) gate on its ``_auto_internal_extensions``
            class attribute.
        target: The client's transport/spec target. Tasks binds its backend
            identity to this; other entries today do not use it.

    Returns:
        Every active entry's outcome, plus the flattened list of actually-built
        :class:`~mcp.client.extension.ClientExtension` instances ready for the
        ``extensions=`` kwarg.
    """

    outcomes = tuple(build(client_cls, target) for build in _ACTIVE_ENTRIES)
    built = tuple(entry.extension for entry in outcomes if entry.extension is not None)
    for entry in outcomes:
        if entry.extension is None and entry.reason:
            logger.debug(
                "mcp extension not declared identifier=%s reason=%s", entry.identifier, entry.reason
            )
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
        note="registry entry #1; tasks_declaration() behavior unchanged from the pre-registry call site",
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
