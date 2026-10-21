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

    from clio_agent.gact.types import (
        AgentDef,
        ErrorEnvelope,
        Message,
        Part,
        Session,
        UserQuestion,
    )
    from clio_agent.prompts import PromptRegistry


class _GuardDirectDestructiveAction(Protocol):
    """Callable seam for the shared direct-destructive-action permission guard.

    ``_guard_direct_destructive_action`` (in :mod:`clio_agent.gact.app`) applies
    permission policy + audit semantics before a direct GACT ``DELETE`` mutates
    state. It is a genuinely cross-concern seam: workspace, session, plugin,
    memory and prompt delete routes all call it. Carrying it on
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


class _PromptRenderContextForRequest(Protocol):
    """Callable seam building the template context for ``POST .../render``.

    ``_prompt_render_context_for_request`` (in :mod:`clio_agent.gact.app`)
    assembles the live render context (the session agent and its subagents,
    agent-invocable commands) used to render a prompt. It reads
    sessions, the pinned resolution and the command catalog, so
    it stays built there and travels here.
    """

    def __call__(
        self,
        *,
        session_id: str = ...,
        workspace_id: str = ...,
    ) -> dict[str, str]: ...


class _AppendSessionMessage(Protocol):
    """Callable seam appending one message to a session's ledger (memory + disk).

    ``_append_session_message`` (in :mod:`clio_agent.gact.app`) appends a message
    to ``app.state.messages`` + the message store. It is the message-ledger
    primitive shared across the
    sessions/messages concerns and the command-dispatch route (which materializes
    a synthetic result message); it stays single-sourced in ``gact.app`` and
    travels here so the command route does not import back into it.
    """

    def __call__(
        self, app: "FastAPI", session_id: str, message: "Message", *, atoms_minted: bool = False
    ) -> None: ...


class _DeleteSessionMessages(Protocol):
    """Callable seam dropping a session's message ledger (memory + disk).

    ``_delete_session_messages`` (in :mod:`clio_agent.gact.app`) removes a
    session's messages from ``app.state.messages`` + the message store. The
    ``/clear`` command dispatch
    path calls it; it stays single-sourced in ``gact.app`` and travels here.
    """

    def __call__(self, app: "FastAPI", session_id: str) -> None: ...


class _ResolveSessionAgent(Protocol):
    """Callable seam resolving the session's agent, or a defined subagent, by id.

    Bound to ``app`` in ``build_app`` over
    :func:`clio_agent.gact.agents.resolution.resolve_session_agent`. Returns
    ``None`` when the id names neither the session's agent nor one of its
    defined subagents. The command-dispatch route and the planner-command
    filter use it.
    """

    def __call__(self, agent_id: str, *, session_id: str) -> "AgentDef | None": ...



class _StartBackgroundUserTurn(Protocol):
    """Callable seam staging + driving a user turn off the request thread.

    ``_start_background_user_turn`` is the turn-orchestration entrypoint
    (extracted to :mod:`clio_agent.gact.turn`, #714): it persists the user
    message + parts, flips the session to ``running``, publishes the
    ``session.status_changed`` / ``message.created`` events, and schedules
    :func:`clio_agent.gact.turn._run_turn_in_background` as a tracked task. The
    POST-message, question-answer, retry-attempt and scheduler concerns all kick
    a turn through it. ``build_app`` binds ``app`` into the wrapper it stores
    here, so the moved route handlers invoke it without importing back into
    ``gact.app`` / ``gact.turn`` (preserving the no-cycle invariant).
    """

    def __call__(
        self,
        sid: str,
        sess: "Session",
        user_text: str,
        *,
        request_parts: "list[Part] | None" = ...,
        metadata: "dict[str, Any] | None" = ...,
        prev_status: str = ...,
        turn_agent_id: str = ...,
        user_msg_id: str = ...,
        user_created_at: str = ...,
        supersedes_input: str = ...,
    ) -> "Message": ...


class _DeleteSessionContextFiles(Protocol):
    """Callable seam dropping a session's context-file ledger (memory + disk).

    ``_delete_session_context_files`` (in :mod:`clio_agent.gact.app`) removes a
    session's context-file ledger from ``app.state.context_files`` and the on-disk
    ledger. Session deletion calls it after dropping the session row; it stays
    single-sourced in ``gact.app`` and travels here.
    """

    def __call__(self, app: "FastAPI", session_id: str) -> None: ...


class _ReleaseSessionArc(Protocol):
    """Callable seam releasing a closed session's hot ARC footprint.

    ``_release_session_arc`` (in :mod:`clio_agent.gact.app`) best-effort flushes
    and evicts a deleted session's live ARC working set. Session deletion calls it
    last; it stays single-sourced in ``gact.app`` and travels here.
    """

    def __call__(self, app: "FastAPI", session_id: str) -> None: ...


class _ReplaceSessionMessages(Protocol):
    """Callable seam replacing one session's message ledger (memory + disk).

    ``_replace_session_messages`` (in :mod:`clio_agent.gact.app`) overwrites a
    session's stored messages in ``app.state.messages`` + the message store. The
    rollback (undo/rewind), fork, compact and import session routes all rewrite
    the ledger through it, and
    the message-delete route in ``gact.app`` shares it; it stays single-sourced
    there and travels here.
    """

    def __call__(self, app: "FastAPI", session_id: str, messages: "list[Message]") -> None: ...


class _CancellationAttemptSummary(Protocol):
    """Callable seam projecting a cancellation attempt into its wire summary.

    ``_cancellation_attempt_summary`` (in :mod:`clio_agent.gact.app`) reduces the
    in-memory cancellation-attempt record to the bounded summary published on
    ``session.status_changed``. The turn engine reuses it when settling a cancelled
    envelope, so it stays single-sourced in ``gact.app`` and travels here for the
    session-cancel route.
    """

    def __call__(self, attempt: "Mapping[str, Any] | None") -> dict[str, Any]: ...


class _ActiveLmModelRef(Protocol):
    """Callable seam returning the active global LM as a ModelRef-shaped dict.

    ``_active_lm_model_ref`` (in :mod:`clio_agent.gact.app`) projects the effective
    LM config into the ``{provider_id, model_id, variant}`` shape the retry route
    compares a requested override against. It reads the provider-bind config that
    stays in ``gact.app``, so it travels here.
    """

    def __call__(self, app: "FastAPI") -> dict[str, str]: ...


class _UnsupportedModelRefError(Protocol):
    """Callable seam building the typed error for an unsupported model override.

    ``_unsupported_model_ref_error`` (in :mod:`clio_agent.gact.app`) assembles the
    structured ``not_implemented`` envelope returned when a retry requests a
    model/provider that differs from the active global LM. The retry route and the
    POST-message turn path both raise it; it stays single-sourced in ``gact.app``
    and travels here.
    """

    def __call__(
        self,
        *,
        session_id: str,
        source: str,
        model_ref: Any,
        active_model: Mapping[str, str],
    ) -> "ErrorEnvelope": ...


class _AgentNotAvailableError(Protocol):
    """Callable seam building the typed error when no executable agent is ready.

    ``agent_not_available_error`` (in :mod:`clio_agent.gact.agent_initialization`)
    inspects the agent-construction task/state to return a starting/failed/
    not-configured envelope. The retry route and the POST-message turn path
    both raise it; it stays single-sourced there and travels here.
    """

    def __call__(self, app: "FastAPI", sid: str) -> "ErrorEnvelope": ...


class _AskUserResumeText(Protocol):
    """Callable seam rendering the resume-turn user text for an answered question.

    ``_ask_user_resume_text`` (in :mod:`clio_agent.gact.app`) formats an answered
    :class:`UserQuestion` into the user-turn text that resumes the orchestrator. The
    question-answer route uses it to stage the resume turn; it stays single-sourced
    in ``gact.app`` and travels here.
    """

    def __call__(self, question: "UserQuestion") -> str: ...


class _CompactExactEvidenceIndex(Protocol):
    """Callable seam building the deterministic evidence index for a compaction.

    ``_compact_exact_evidence_index`` (in :mod:`clio_agent.gact.app`) extracts the
    exact scientific identifiers/metrics from a transcript so they survive LM
    compaction. The compact route appends it to the LM summary, and the live
    display path in ``gact.app`` reuses it; it stays single-sourced there and
    travels here.
    """

    def __call__(self, transcript: str) -> str: ...


class _InstallToolRuntimeHooks(Protocol):
    """Callable seam re-installing the tool-runtime permission/observer hooks.

    ``_install_tool_runtime_hooks`` (in :mod:`clio_agent.gact.app`) re-binds the
    pending permission gate + tool observer that the live agent reads off
    ``app.state`` after a provider swap rebuilds the agent. The LM-provider bind
    route (``PUT /v1/providers/lm``) calls it once the new agent is wired; it stays
    single-sourced in ``gact.app`` (the construction lifecycle reuses it) and
    travels here so the moved route does not import back into ``gact.app``.
    """

    def __call__(self, app: "FastAPI") -> None: ...


class _ClearSessionModelRefs(Protocol):
    """Callable seam clearing stale per-session model refs after a provider swap.

    ``_clear_session_model_refs`` (in :mod:`clio_agent.gact.app`) drops any
    per-session GACT ``ModelRef`` overrides so the next turn runs through the
    freshly-bound global LM instead of failing with a per-session override error.
    The LM-provider bind route calls it after swapping the agent; it stays
    single-sourced in ``gact.app`` and travels here.
    """

    def __call__(self, app: "FastAPI") -> None: ...


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
    prompt_render_context_for_request: _PromptRenderContextForRequest
    append_session_message: _AppendSessionMessage
    delete_session_messages: _DeleteSessionMessages
    resolve_session_agent: _ResolveSessionAgent
    start_background_user_turn: _StartBackgroundUserTurn
    delete_session_context_files: _DeleteSessionContextFiles
    release_session_arc: _ReleaseSessionArc
    replace_session_messages: _ReplaceSessionMessages
    cancellation_attempt_summary: _CancellationAttemptSummary
    active_lm_model_ref: _ActiveLmModelRef
    unsupported_model_ref_error: _UnsupportedModelRefError
    agent_not_available_error: _AgentNotAvailableError
    ask_user_resume_text: _AskUserResumeText
    compact_exact_evidence_index: _CompactExactEvidenceIndex
    install_tool_runtime_hooks: _InstallToolRuntimeHooks
    clear_session_model_refs: _ClearSessionModelRefs
