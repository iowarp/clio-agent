"""Cross-concern dependency seam for the GACT route factories (#714).

The router-factory decomposition moves the ``@app.<verb>`` handlers out of the
:func:`clio_agent.gact.app.build_app` closure into ``register_<concern>_routes``
factories (see :mod:`clio_agent.gact.routes`). Handlers keep closing over the
``app`` argument (FastAPI's decorators need it) and reach ``app.state`` directly,
but anything they previously reached as a ``build_app``-local closure now travels
explicitly through :class:`GactDeps`.

``GactDeps`` is built *once* in ``build_app`` and passed to every
``register_<concern>_routes`` call. Keep it minimal: add a field only when a
moved handler genuinely needs a ``build_app``-local helper/closure beyond
``app.state``. Concern-private helpers move with their concern module instead.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.types import AgentDef
    from clio_agent.prompts import PromptRegistry


class _GuardDirectDestructiveAction(Protocol):
    """Callable seam for the shared direct-destructive-action permission guard.

    ``_guard_direct_destructive_action`` (in :mod:`clio_agent.gact.app`) applies
    permission policy + audit semantics before a direct GACT ``DELETE`` mutates
    state. It is a genuinely cross-concern seam: workspace, session, agent,
    blueprint, memory and prompt delete routes all call it. Carrying it on
    ``GactDeps`` lets the moved handlers invoke it without importing back into
    ``gact.app`` (which would violate the no-cycle invariant).
    """

    def __call__(
        self,
        app: "FastAPI",
        *,
        session_id: str = ...,
        workspace_id: str = ...,
        tool_name: str,
        args: Mapping[str, Any],
        summary: str,
        reason: str,
    ) -> None: ...


class _ApplyEditToDisk(Protocol):
    """Callable seam committing an approved file diff to disk.

    ``_apply_edit_to_disk`` (in :mod:`clio_agent.gact.app`) is the GACT-side
    commit step for ``POST /v1/sessions/{sid}/diffs/apply``: it enforces the
    workspace-root + mode + file-policy boundary and records an auto-approved
    permission audit row before writing. It wraps the permission/policy
    machinery that lives in ``gact.app``, so it stays built there and travels
    here; the diff-apply route invokes it without importing back into
    ``gact.app`` (which would violate the no-cycle invariant).
    """

    def __call__(
        self,
        *,
        path: str,
        new_content: str,
        session: Any,
        app: "FastAPI",
    ) -> dict[str, Any]: ...


class _FlushContextFiles(Protocol):
    """Callable seam persisting the context-file ledger to disk.

    ``_flush_context_files`` (in :mod:`clio_agent.gact.app`) atomically writes
    the in-memory context-file ledger to ``app.state.context_files_path`` when
    persistence is configured. It has a second owner -- session deletion's
    ``_delete_session_context_files`` -- so it stays single-sourced in
    ``gact.app`` and travels here for the context-file add/remove routes.
    """

    def __call__(self, app: "FastAPI") -> None: ...


class _PromptRegistryForRequest(Protocol):
    """Callable seam building a request-scoped :class:`PromptRegistry`.

    ``_prompt_registry_for_request`` (in :mod:`clio_agent.gact.app`) layers the
    builtin prompts under the global/workspace/session source roots resolved for
    a request and picks the writable root for the given ``write_scope``. It
    couples to a web of other ``build_app`` closures (source resolution, write
    roots), so it stays built there and travels here; the prompt routes (and the
    agent-run paths that already read ``app.state.prompt_registry_for_request``)
    call it without importing back into ``gact.app``.
    """

    def __call__(
        self,
        *,
        session_id: str = ...,
        workspace_id: str = ...,
        write_scope: str = ...,
    ) -> "PromptRegistry": ...


class _PromptAgentOverlayForRequest(Protocol):
    """Callable seam returning the session-agent prompt overlay for a request.

    ``_prompt_agent_overlay_for_request`` (in :mod:`clio_agent.gact.app`)
    projects any session-scoped agent overlay down to its prompt-affecting
    fields so ``GET /v1/prompts`` can surface where a session has overridden an
    agent's system prompt / prompt id / provider / model.
    """

    def __call__(self, session_id: str = ...) -> dict[str, Any]: ...


class _PromptRenderContextForRequest(Protocol):
    """Callable seam building the template context for ``POST .../render``.

    ``_prompt_render_context_for_request`` (in :mod:`clio_agent.gact.app`)
    assembles the live render context (agent tree/flat list, active expert
    pack/blueprint, agent-invocable commands) used to render a prompt. It reads
    sessions/agent rows/command catalog through other ``build_app`` closures, so
    it stays built there and travels here.
    """

    def __call__(
        self,
        *,
        session_id: str = ...,
        workspace_id: str = ...,
    ) -> dict[str, str]: ...


class _ActiveSessionAgentBlueprintId(Protocol):
    """Callable seam reading a session's *explicitly set* active blueprint id.

    ``_active_session_agent_blueprint_id`` (in :mod:`clio_agent.gact.app`) returns
    only the ``active_agent_blueprint_id`` stored in session metadata -- it does
    NOT apply the default-blueprint fallback that the runtime resolver
    (:func:`~clio_agent.gact.agents.resolution._runtime_active_agent_blueprint_id`)
    layers on. ``GET /v1/sessions/{sid}/agent-blueprint`` must report exactly what
    the session set, so the blueprint route reaches this metadata-only reader
    through ``deps`` rather than the runtime resolver.
    """

    def __call__(self, session_id: str = ...) -> str: ...


class _AgentBlueprintActivationMetadata(Protocol):
    """Callable seam building the session-activation metadata patch for a blueprint.

    ``_agent_blueprint_activation_metadata`` (in :mod:`clio_agent.gact.app`)
    projects a blueprint's wire row plus its on-disk install provenance
    (``read_install_metadata``) into the ``active_agent_blueprint_*`` metadata keys
    persisted on a session when ``POST /v1/sessions/{sid}/agent-blueprint`` sets
    the active blueprint. It stays built in ``build_app`` and travels here.
    """

    def __call__(
        self,
        *,
        blueprint_wire: Mapping[str, Any],
        install_root: "Path | None",
        scope: str,
    ) -> dict[str, str]: ...


class _MirrorWorkspaceSession(Protocol):
    """Callable seam persisting one session row into its owning workspace store.

    ``_mirror_workspace_session`` (in :mod:`clio_agent.gact.app`) writes a single
    session's state into the workspace storage root that owns it. The
    set-active-blueprint route mirrors the session after mutating its metadata so
    the workspace-scoped copy stays in sync; it reaches the mirror through
    ``deps`` rather than importing back into ``gact.app``.
    """

    def __call__(self, app: "FastAPI", session_id: str) -> None: ...


class _AgentRows(Protocol):
    """Callable seam resolving the effective agent catalog for a session/workspace.

    ``_agent_rows`` (in :mod:`clio_agent.gact.app`) merges the active Agent
    Blueprint graph, expert packs, and the built-in/user/skill registry into the
    rows ``GET /v1/agents`` renders, applying the session overlay + prompt
    registry. It is also reached by the prompt render-context builder that stays
    in ``build_app``, so it stays single-sourced there and travels here for the
    agent list/detail routes.
    """

    def __call__(self, session_id: str = ..., workspace_id: str = ...) -> "list[AgentDef]": ...


class _AgentWithCapabilityRefs(Protocol):
    """Callable seam attaching normalized capability metadata to an ``AgentDef``.

    ``_agent_with_capability_refs`` (in :mod:`clio_agent.gact.app`) projects an
    agent's tools/skills/commands (plus the ``main`` backend-command set) into the
    ``capability_refs`` the TUI renders. It has callers that remain in the
    blueprint-row resolution closures, so it stays single-sourced in ``build_app``
    and travels here for the agent create/update routes.
    """

    def __call__(self, agent_def: "AgentDef") -> "AgentDef": ...


class _BaseSessionAgentBlueprintRows(Protocol):
    """Callable seam loading a session's *un-overlaid* active blueprint rows.

    ``_base_session_agent_blueprint_rows`` (in :mod:`clio_agent.gact.app`) loads
    the Agent Blueprint graph a session has activated (by id or on-disk path)
    before any session overlay is applied. The overlay validation + export routes
    re-resolve this base, and so does the live blueprint-row resolver that stays
    in ``build_app``; it stays single-sourced there and travels here.
    """

    def __call__(self, session_id: str = ..., workspace_id: str = ...) -> "list[AgentDef]": ...


class _ApplyAgentOverlayRows(Protocol):
    """Callable seam applying a session overlay patch onto base blueprint rows.

    ``_apply_agent_overlay_rows`` (in :mod:`clio_agent.gact.app`) layers an
    overlay's per-agent field patches onto the base rows, recording the applied
    fields in each row's metadata. The overlay validation + export routes apply it
    to preview the effective hierarchy, and ``_apply_session_agent_overlay`` in
    ``build_app`` reuses it; it stays single-sourced there and travels here.
    """

    def __call__(
        self,
        rows: "list[AgentDef]",
        overlay: Mapping[str, Any],
        *,
        session_id: str = ...,
    ) -> "list[AgentDef]": ...


@dataclass(frozen=True)
class GactDeps:
    """Cross-concern seams the extracted route factories need beyond ``app.state``.

    Built once in ``build_app`` and threaded through every
    ``register_<concern>_routes(app, deps)`` call. Fields are the shared
    ``build_app``-local helpers/closures that more than one concern reaches for;
    concern-private helpers live in the concern module, not here.
    """

    guard_direct_destructive_action: _GuardDirectDestructiveAction
    apply_edit_to_disk: _ApplyEditToDisk
    flush_context_files: _FlushContextFiles
    prompt_registry_for_request: _PromptRegistryForRequest
    prompt_agent_overlay_for_request: _PromptAgentOverlayForRequest
    prompt_render_context_for_request: _PromptRenderContextForRequest
    active_session_agent_blueprint_id: _ActiveSessionAgentBlueprintId
    agent_blueprint_activation_metadata: _AgentBlueprintActivationMetadata
    mirror_workspace_session: _MirrorWorkspaceSession
    agent_rows: _AgentRows
    agent_with_capability_refs: _AgentWithCapabilityRefs
    base_session_agent_blueprint_rows: _BaseSessionAgentBlueprintRows
    apply_agent_overlay_rows: _ApplyAgentOverlayRows
