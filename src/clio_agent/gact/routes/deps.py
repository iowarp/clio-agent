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
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from fastapi import FastAPI

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
