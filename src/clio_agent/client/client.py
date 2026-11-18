"""Synchronous typed client for CLIO: run agents and flows, or use the API directly.

:class:`ClioClient` is the SDK's single entry point, opened as a context manager
with :class:`~clio_agent.client.options.ClioOptions`. Opening it attaches to the
CLIO server at ``options.url`` or starts one on this machine; closing it lets go
(and stops a server it started once no other client uses it). See
:mod:`clio_agent.client.connection`.

``run`` / ``run_flow`` / ``session`` / ``stream_flow`` put an agent or a flow to
work without asking anyone anything (:mod:`clio_agent.client.run`); the per-resource
namespaces (``sessions``, ``messages``, ``workspaces``, ``permissions``,
``documents``) are the typed GACT v1 API (``gact-tui/contract/SPEC.md``).

Example:
    >>> from clio import ClioClient, ClioOptions
    >>> with ClioClient(options=ClioOptions(workspace=".", agent="factorio")) as c:
    ...     result = c.run("Reduce the mass of the bracket in bracket.inp by 30%")
    ...     print(result.text, [a.path for a in result.artifacts])
"""

from __future__ import annotations

import builtins
import logging
from typing import Any, Literal

import httpx

from clio_agent.client.connection import Connection, close_connection, open_connection
from clio_agent.client.errors import ClioConnectionError, error_from_response
from clio_agent.client.events import EventStream
from clio_agent.client.options import ClioOptions
from clio_agent.client.run_api import RunAPI
from clio_agent.client.types import (
    Agent,
    Capabilities,
    DocumentManifest,
    DocumentWorkingCopy,
    Health,
    LMProvider,
    Message,
    Metrics,
    PermissionList,
    PostMessageAck,
    Session,
    Tool,
    UserQuestion,
    Workspace,
)

logger = logging.getLogger("clio_agent.client")


PermissionAction = Literal["allow", "deny", "allow_session", "allow_workspace", "allow_script"]


def _drop_missing(payload: dict[str, Any]) -> dict[str, Any]:
    """Remove ``None`` entries so PATCH bodies only carry set fields."""

    return {k: v for k, v in payload.items() if v is not None}


class ClioClient(RunAPI):
    """Client for one CLIO server.

    Args:
        options: What the script sets once (:class:`ClioOptions`); the default
            attaches to (or starts) the configured local server.
        timeout: Per-request timeout in seconds for plain REST calls.
            SSE streams use ``stream_read_timeout`` for reads instead
            (heartbeats arrive at least every 15 s, so the default of
            60 s only fires when the connection is genuinely dead).
        retries: Bounded retry budget for *connection-level* failures
            on idempotent GETs — never for HTTP errors, never for
            writes, and every retry is logged. 0 (default) disables.
        transport: Optional ``httpx.BaseTransport`` that serves the API in-process
            (the test-suite drives an ASGI app with it). With one, nothing is
            attached, started or registered.
    """

    def __init__(
        self,
        options: ClioOptions | None = None,
        *,
        timeout: float = 30.0,
        stream_read_timeout: float = 60.0,
        retries: int = 0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.options = options or ClioOptions()
        self._retries = max(0, retries)
        self._stream_read_timeout = stream_read_timeout
        self._in_process = transport is not None
        token = None if self._in_process else self.options.resolved_token()
        self._http = httpx.Client(
            base_url=self.options.base_url,
            timeout=timeout,
            headers={"Authorization": f"Bearer {token}"} if token else None,
            transport=transport,
        )
        self.connection: Connection | None = None
        self._capabilities: Capabilities | None = None
        self.sessions = SessionsAPI(self)
        self.messages = MessagesAPI(self)
        self.workspaces = WorkspacesAPI(self)
        self.permissions = PermissionsAPI(self)
        self.documents = DocumentsAPI(self)
        self._workspace_ids: dict[str, str] = {}

    # -- lifecycle ---------------------------------------------------- #

    def __enter__(self) -> ClioClient:
        self.open()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def open(self) -> ClioClient:
        """Attach to the server or start one, and register (idempotent).

        Raises:
            ServerSetupError: No server answers and none may be started, a server
                could not start, or it has no model provider. The message names the
                problem and the fix.
        """

        if self.connection is None and not self._in_process:
            self.connection = open_connection(self.options, self._http)
        return self

    def close(self) -> None:
        """Let go of the server (stopping one this client started when it was the last
        one attached) and release the connection pool."""

        try:
            if self.connection is not None:
                connection, self.connection = self.connection, None
                close_connection(connection, self._http)
        finally:
            self._http.close()

    # -- transport ---------------------------------------------------- #

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any | None = None,
        params: dict[str, Any] | None = None,
        ok_statuses: tuple[int, ...] = (),
    ) -> httpx.Response:
        """One REST call; non-2xx (minus ``ok_statuses``) raises typed errors."""

        self.open()
        attempts_left = self._retries if method == "GET" else 0
        while True:
            try:
                response = self._http.request(method, path, json=json, params=params)
                break
            except httpx.TransportError as exc:
                if attempts_left <= 0:
                    raise ClioConnectionError(f"{method} {path} failed: {exc}") from exc
                attempts_left -= 1
                logger.warning(
                    "%s %s hit a transport error (%s); retrying (%d attempt(s) left)",
                    method,
                    path,
                    exc,
                    attempts_left,
                )
        if response.is_success or response.status_code in ok_statuses:
            return response
        raise error_from_response(response)

    def _stream(self, method: str, path: str, *, headers: dict[str, str]) -> Any:
        """Open a streaming request (SSE) with stream-appropriate timeouts."""

        self.open()
        timeout = httpx.Timeout(30.0, read=self._stream_read_timeout)
        return self._http.stream(method, path, headers=headers, timeout=timeout)

    # -- root surfaces (§3) -------------------------------------------- #

    def health(self) -> Health:
        """GET /v1/health — typed, including the 503-with-body carve-out
        (SPEC §6.0: an unavailable backend answers 503 with the health
        body, not an error envelope)."""

        response = self._request("GET", "/v1/health", ok_statuses=(503,))
        return Health.model_validate(response.json())

    def capabilities(self, *, refresh: bool = False) -> Capabilities:
        """GET /v1/capabilities — cached after the first call.

        Callers MUST probe :meth:`Capabilities.supports` before using
        optional surfaces: a flag advertised ``False`` (or absent) has
        no route behind it (SPEC §3.3 capability-truth rule).
        """

        if self._capabilities is None or refresh:
            response = self._request("GET", "/v1/capabilities")
            self._capabilities = Capabilities.model_validate(response.json())
        return self._capabilities

    def supports(self, flag: str) -> bool:
        """Shorthand for ``capabilities().supports(flag)``."""

        return self.capabilities().supports(flag)

    # -- catalog + config read surfaces -------------------------------- #

    def agents(self) -> list[Agent]:
        """GET /v1/agents — the installed agents and the user's agents (SPEC §6.5).

        Each row is an agent ref (``<marketplace>/<id>``) with its plugin
        selection; user agents carry the parent they extend and their delta.
        Backs the CLI's ``/experts`` and ``/registry``.
        """

        response = self._request("GET", "/v1/agents")
        return [Agent.model_validate(row) for row in response.json().get("agents", [])]

    def tools(self) -> list[Tool]:
        """GET /v1/tools — the unified live tool catalog (SPEC §6.5).

        Every tool the bundled gateway and any installed third-party
        MCP servers expose, flattened with owner/tags/visibility. Backs
        the CLI's ``/tools``.
        """

        response = self._request("GET", "/v1/tools")
        return [Tool.model_validate(row) for row in response.json().get("tools", [])]

    def metrics(self) -> Metrics:
        """GET /v1/metrics — aggregate runtime counters (SPEC §6.16).

        Session/message rollups, token + cost totals, and per-tool
        latency buckets. Backs the CLI's ``/metrics``.
        """

        response = self._request("GET", "/v1/metrics")
        return Metrics.model_validate(response.json())

    def lm_provider(self) -> LMProvider:
        """GET /v1/providers/lm — the live LM config + presets.

        Reports whether an agent is wired (``configured``), the bound
        provider/model/endpoint, and the discovered context budget.
        Backs the CLI's ``/models``.
        """

        response = self._request("GET", "/v1/providers/lm")
        return LMProvider.model_validate(response.json())


class SessionsAPI:
    """Session lifecycle (SPEC §6.2) + the per-session SSE feed (§7)."""

    def __init__(self, client: ClioClient) -> None:
        self._client = client

    def create(
        self,
        *,
        workspace_id: str = "ws_default",
        title: str = "",
        mode: str = "edit",  # P1.1 #1063: ``chat`` mode removed (unenforced == edit)
        edit_mode: str = "diff",
        agent: str | None = None,
        model: str | None = None,
        approval_mode: str | None = None,
        flow: dict[str, Any] | None = None,
        unattended: bool = False,
        max_turns: int | None = None,
        max_budget_usd: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Session:
        """POST /v1/sessions.

        ``agent`` is an installed agent id (or ``marketplace/id``), ``model`` an
        ``account/model`` id; unset, the server's defaults apply. ``flow``
        (``{"id", "options"}``) makes the session run that flow as its own work: the
        first message is its input. ``unattended`` marks a run nobody watches
        (questions answered "not available", asks denied, plugins trusted);
        ``max_turns`` / ``max_budget_usd`` limit its whole session tree.
        """

        body: dict[str, Any] = {
            "workspace_id": workspace_id,
            "title": title,
            "mode": mode,
            "edit_mode": edit_mode,
            "metadata": metadata or {},
            "unattended": unattended,
        }
        if agent:
            body["agent"] = {"id": agent}
        if model:
            account, _, model_id = model.partition("/")
            body["model"] = {"provider_id": account, "model_id": model_id}
        optional = {
            "approval_mode": approval_mode,
            "flow": flow,
            "max_turns": max_turns,
            "max_budget_usd": max_budget_usd,
        }
        body.update(_drop_missing(optional))
        response = self._client._request("POST", "/v1/sessions", json=body)
        return Session.model_validate(response.json())

    def usage(self, session_id: str) -> dict[str, Any]:
        """GET /v1/sessions/{id}/usage — model turns, tokens and cost over the session
        and everything it spawned (the flow route's spend, summed)."""

        return dict(self._client._request("GET", f"/v1/sessions/{session_id}/usage").json())

    def list(
        self,
        *,
        workspace_id: str | None = None,
        include_all_workspaces: bool | None = None,
        archived: bool | None = None,
    ) -> list[Session]:
        """GET /v1/sessions — newest-first; active-only unless
        ``archived`` is set (SPEC §6.2)."""

        params = _drop_missing(
            {
                "workspace_id": workspace_id,
                "include_all_workspaces": include_all_workspaces,
                "archived": archived,
            }
        )
        response = self._client._request("GET", "/v1/sessions", params=params or None)
        return [Session.model_validate(row) for row in response.json().get("sessions", [])]

    def get(self, session_id: str) -> Session:
        """GET /v1/sessions/{id}."""

        response = self._client._request("GET", f"/v1/sessions/{session_id}")
        return Session.model_validate(response.json())

    def update(
        self,
        session_id: str,
        *,
        title: str | None = None,
        mode: str | None = None,
        edit_mode: str | None = None,
        approval_mode: str | None = None,
        metadata: dict[str, Any] | None = None,
        archived: bool | None = None,
    ) -> Session:
        """PATCH /v1/sessions/{id} — only set fields are sent;
        ``metadata`` merges shallowly server-side (SPEC §6.2)."""

        body = _drop_missing(
            {
                "title": title,
                "mode": mode,
                "edit_mode": edit_mode,
                "approval_mode": approval_mode,
                "metadata": metadata,
                "archived": archived,
            }
        )
        response = self._client._request("PATCH", f"/v1/sessions/{session_id}", json=body)
        return Session.model_validate(response.json())

    def delete(self, session_id: str) -> None:
        """DELETE /v1/sessions/{id} — policy-gated (a policy deny is a
        403 ``permission_error``); cascades messages + memory."""

        self._client._request("DELETE", f"/v1/sessions/{session_id}")

    def fork(
        self,
        session_id: str,
        *,
        at_message_id: str | None = None,
        title: str | None = None,
    ) -> Session:
        """POST /v1/sessions/{id}/fork — ``at_message_id`` truncation
        is inclusive. The independent branch preserves the parent's model,
        behavior settings and narrowing permission policies. Only user conversations
        and user branches can be forked; managed subagent/lookup sessions are rejected."""

        body = _drop_missing({"at_message_id": at_message_id, "title": title})
        response = self._client._request("POST", f"/v1/sessions/{session_id}/fork", json=body)
        return Session.model_validate(response.json())

    def events(
        self,
        session_id: str,
        *,
        last_event_id: int | None = None,
        reconnect_attempts: int = 0,
        reconnect_wait_s: float = 1.0,
    ) -> EventStream:
        """Open the session's SSE feed (SPEC §7.1).

        ``last_event_id`` resumes from the replay buffer; only events
        with a greater id are delivered (replayed copies carry
        ``replay=True``). ``reconnect_attempts`` bounds the explicit,
        logged reconnect budget — the default of 0 never retries.
        """

        return EventStream(
            self._client,
            session_id,
            last_event_id=last_event_id,
            reconnect_attempts=reconnect_attempts,
            reconnect_wait_s=reconnect_wait_s,
        )

    # -- ask-user clarifying questions (#333) -------------------------- #

    # NB: ``builtins.list`` because the ``list`` method above shadows the builtin in
    # annotations that follow it in this class body (a class-scope name collision).
    def questions(
        self, session_id: str, *, status: str | None = None
    ) -> builtins.list[UserQuestion]:
        """GET /v1/sessions/{sid}/questions — the ask-user question log.

        Asking posts immediately; the agent may continue or explicitly wait
        on the Question handle. ``status`` filters server-side; rows come
        back newest-first.
        """

        params = _drop_missing({"status": status})
        response = self._client._request(
            "GET", f"/v1/sessions/{session_id}/questions", params=params or None
        )
        return [UserQuestion.model_validate(row) for row in response.json().get("questions", [])]

    def question(self, session_id: str, question_id: str) -> UserQuestion:
        """Read one authorized Question, including its confirmed answer-input identity."""
        response = self._client._request(
            "GET", f"/v1/sessions/{session_id}/questions/{question_id}"
        )
        return UserQuestion.model_validate(response.json())

    def answer_question(
        self,
        session_id: str,
        question_id: str,
        *,
        answer: str = "",
        option_id: str | None = None,
        selected_options: builtins.list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> UserQuestion:
        """POST /v1/sessions/{sid}/questions/{qid}/answer — resolve a pending
        ask-user question.

        Pass ``answer`` for a freeform reply and/or ``option_id`` (or the
        richer ``selected_options`` list) to pick a choice. Answering the
        question stores its answer for shared collection or automatic input
        delivery. A busy agent receives it at an eligible iteration; an idle
        agent may wake in a later segment. Read ``answer_input`` to correlate
        that segment with its completion event.
        """

        selected = list(selected_options or [])
        if option_id:
            selected.append(option_id)
        body: dict[str, Any] = {
            "answer": answer,
            "selected_options": selected,
            "metadata": metadata or {},
        }
        response = self._client._request(
            "POST",
            f"/v1/sessions/{session_id}/questions/{question_id}/answer",
            json=body,
        )
        return UserQuestion.model_validate(response.json())


class MessagesAPI:
    """Messages within a session (SPEC §6.3)."""

    def __init__(self, client: ClioClient) -> None:
        self._client = client

    def post(
        self,
        session_id: str,
        text: str | None = None,
        *,
        parts: list[dict[str, Any]] | None = None,
        agent_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> PostMessageAck:
        """POST /v1/sessions/{sid}/messages — returns the ack; the
        assistant turn streams asynchronously over SSE.

        ``text`` is the convenience form (sent as a single text part,
        the canonical shape); pass ``parts`` for anything richer.
        """

        wire_parts = list(parts or [])
        if text is not None:
            wire_parts.append({"type": "text", "text": text})
        body: dict[str, Any] = {"parts": wire_parts}
        if agent_id:
            body["agent_id"] = agent_id
        if metadata:
            body["metadata"] = metadata
        response = self._client._request("POST", f"/v1/sessions/{session_id}/messages", json=body)
        return PostMessageAck.model_validate(response.json())

    def list(self, session_id: str) -> list[Message]:
        """GET /v1/sessions/{sid}/messages — the full ledger,
        newest-first; may include one live in-flight assistant message
        (``metadata.live: true``) while a turn streams."""

        response = self._client._request("GET", f"/v1/sessions/{session_id}/messages")
        return [Message.model_validate(row) for row in response.json().get("messages", [])]

    def get(self, session_id: str, message_id: str) -> Message:
        """GET /v1/sessions/{sid}/messages/{msg_id}."""

        response = self._client._request("GET", f"/v1/sessions/{session_id}/messages/{message_id}")
        return Message.model_validate(response.json())


class WorkspacesAPI:
    """Workspace CRUD (SPEC §6.1)."""

    def __init__(self, client: ClioClient) -> None:
        self._client = client

    def list(self) -> list[Workspace]:
        """GET /v1/workspaces."""

        response = self._client._request("GET", "/v1/workspaces")
        return [Workspace.model_validate(row) for row in response.json().get("workspaces", [])]

    def create(
        self,
        name: str,
        *,
        root_path: str = "",
        storage_root: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> Workspace:
        """POST /v1/workspaces (201)."""

        response = self._client._request(
            "POST",
            "/v1/workspaces",
            json={
                "name": name,
                "root_path": root_path,
                "storage_root": storage_root,
                "metadata": metadata or {},
            },
        )
        return Workspace.model_validate(response.json())

    def get(self, workspace_id: str) -> Workspace:
        """GET /v1/workspaces/{id}."""

        response = self._client._request("GET", f"/v1/workspaces/{workspace_id}")
        return Workspace.model_validate(response.json())

    def update(
        self,
        workspace_id: str,
        *,
        name: str | None = None,
        root_path: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Workspace:
        """PATCH /v1/workspaces/{id} — metadata merges (no key removal)."""

        body = _drop_missing({"name": name, "root_path": root_path, "metadata": metadata})
        response = self._client._request("PATCH", f"/v1/workspaces/{workspace_id}", json=body)
        return Workspace.model_validate(response.json())

    def delete(self, workspace_id: str) -> None:
        """DELETE /v1/workspaces/{id} — ``ws_default`` is undeletable
        (409 ``permission_error``, SPEC §6.1)."""

        self._client._request("DELETE", f"/v1/workspaces/{workspace_id}")


class DocumentsAPI:
    """Document artifact manifests, reviews, renditions, and working copies."""

    def __init__(self, client: ClioClient) -> None:
        self._client = client

    def manifest(self, artifact_id: str) -> DocumentManifest:
        """Resolve format-aware viewer routing for one immutable version."""

        response = self._client._request("GET", f"/v1/artifacts/{artifact_id}/document")
        return DocumentManifest.model_validate(response.json())

    def content(self, artifact_id: str) -> bytes:
        """Fetch hash-verified document bytes."""

        response = self._client._request("GET", f"/v1/artifacts/{artifact_id}/document/content")
        return response.content

    def render_pdf(self, artifact_id: str, *, session_id: str) -> DocumentManifest:
        """Create a derived PDF rendition."""

        response = self._client._request(
            "POST",
            f"/v1/artifacts/{artifact_id}/renditions",
            json={"format": "pdf"},
            params={"session_id": session_id},
        )
        return DocumentManifest.model_validate(response.json()["artifact"])

    def create_working_copy(
        self,
        artifact_id: str,
        *,
        session_id: str,
        writable: bool = True,
        auto_checkpoint: bool = True,
    ) -> DocumentWorkingCopy:
        """Materialize an exact version under a confined mutable lease."""

        response = self._client._request(
            "POST",
            f"/v1/artifacts/{artifact_id}/working-copies",
            json={
                "session_id": session_id,
                "writable": writable,
                "auto_checkpoint": auto_checkpoint,
            },
        )
        return DocumentWorkingCopy.model_validate(response.json())

    def get_working_copy(self, working_copy_id: str) -> DocumentWorkingCopy:
        """Resolve a working copy."""

        response = self._client._request("GET", f"/v1/document-working-copies/{working_copy_id}")
        return DocumentWorkingCopy.model_validate(response.json())

    def close_working_copy(self, working_copy_id: str) -> DocumentWorkingCopy:
        """Close a writable lease without deleting its file."""

        response = self._client._request("DELETE", f"/v1/document-working-copies/{working_copy_id}")
        return DocumentWorkingCopy.model_validate(response.json())

    def resolve_conflict(
        self,
        working_copy_id: str,
        *,
        resolution: Literal["keep-current", "use-working-copy"],
        expected_head_artifact_id: str,
    ) -> DocumentWorkingCopy:
        """Apply an explicit stale-save conflict decision."""

        response = self._client._request(
            "POST",
            f"/v1/document-working-copies/{working_copy_id}/conflict",
            json={
                "resolution": resolution,
                "expected_head_artifact_id": expected_head_artifact_id,
            },
        )
        return DocumentWorkingCopy.model_validate(response.json())


class PermissionsAPI:
    """Permission requests + replies (SPEC §6.11)."""

    def __init__(self, client: ClioClient) -> None:
        self._client = client

    def list(
        self,
        *,
        session_id: str | None = None,
        status: str | None = None,
        limit: int | None = None,
    ) -> PermissionList:
        """GET /v1/permissions — ``status`` accepts any lifecycle value
        or ``"all"``; rows are desc by created_at."""

        params = _drop_missing({"session_id": session_id, "status": status, "limit": limit})
        response = self._client._request("GET", "/v1/permissions", params=params or None)
        return PermissionList.model_validate(response.json())

    def respond(self, permission_id: str, action: PermissionAction) -> None:
        """POST /v1/permissions/{id} — resolve a pending request.

        Idempotent server-side: responding to an already-resolved row
        is a silent 204. ``allow_session`` / ``allow_workspace``
        additionally derive a sticky allow policy (SPEC §6.11).
        """

        self._client._request("POST", f"/v1/permissions/{permission_id}", json={"action": action})
