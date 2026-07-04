"""Synchronous typed client for the GACT v1 API.

:class:`ClioClient` is the SDK's single entry point: a context-manager
httpx client plus small per-resource namespaces (``sessions``,
``messages``, ``workspaces``, ``permissions``). It talks the wire
described by the reconciled contract (``gact-tui/contract/SPEC.md``)
and never imports gact server code.

Example:
    >>> from clio_agent.sdk import ClioClient
    >>> with ClioClient("http://127.0.0.1:8100") as client:
    ...     if not client.capabilities().supports("sessions"):
    ...         raise RuntimeError("backend has no session surface")
    ...     sess = client.sessions.create(title="demo")
    ...     ack = client.messages.post(sess.id, text="hello")
    ...     with client.sessions.events(sess.id) as stream:
    ...         for event in stream:
    ...             if event.type == "message.completed":
    ...                 break
"""

from __future__ import annotations

import logging
from typing import Any, Literal

import httpx

from clio_agent.sdk.errors import ClioConnectionError, error_from_response
from clio_agent.sdk.events import EventStream
from clio_agent.sdk.types import (
    Agent,
    Capabilities,
    Health,
    LMProvider,
    Message,
    Metrics,
    PermissionList,
    PostMessageAck,
    Session,
    Tool,
    Workspace,
)

logger = logging.getLogger("clio_agent.sdk")

DEFAULT_BASE_URL = "http://127.0.0.1:8100"

PermissionAction = Literal["allow", "deny", "allow_session", "allow_workspace"]


def _drop_missing(payload: dict[str, Any]) -> dict[str, Any]:
    """Remove ``None`` entries so PATCH bodies only carry set fields."""

    return {k: v for k, v in payload.items() if v is not None}


class ClioClient:
    """Client for one GACT backend.

    Args:
        base_url: The backend origin (clio's default bind is
            ``http://127.0.0.1:8100``).
        timeout: Per-request timeout in seconds for plain REST calls.
            SSE streams use ``stream_read_timeout`` for reads instead
            (heartbeats arrive at least every 15 s, so the default of
            60 s only fires when the connection is genuinely dead).
        retries: Bounded retry budget for *connection-level* failures
            on idempotent GETs — never for HTTP errors, never for
            writes, and every retry is logged. 0 (default) disables.
        transport: Optional ``httpx.BaseTransport`` override; used by
            the test-suite to drive an in-process ASGI app.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout: float = 30.0,
        stream_read_timeout: float = 60.0,
        retries: int = 0,
        headers: dict[str, str] | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._retries = max(0, retries)
        self._stream_read_timeout = stream_read_timeout
        self._http = httpx.Client(
            base_url=base_url,
            timeout=timeout,
            headers=headers,
            transport=transport,
        )
        self._capabilities: Capabilities | None = None
        self.sessions = SessionsAPI(self)
        self.messages = MessagesAPI(self)
        self.workspaces = WorkspacesAPI(self)
        self.permissions = PermissionsAPI(self)

    # -- lifecycle ---------------------------------------------------- #

    def __enter__(self) -> ClioClient:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        """Release the underlying HTTP connection pool."""

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

    def agents(
        self,
        *,
        tier: int | None = None,
        session_id: str | None = None,
        workspace_id: str | None = None,
    ) -> list[Agent]:
        """GET /v1/agents — the agent catalog (SPEC §6.5).

        Built-in tier-1/2 experts first, then any user/skill agents.
        ``tier`` filters to one tier server-side; ``session_id`` /
        ``workspace_id`` scope which user agents are visible. Backs the
        CLI's ``/experts`` and ``/registry``.
        """

        params = _drop_missing(
            {"tier": tier, "session_id": session_id, "workspace_id": workspace_id}
        )
        response = self._request("GET", "/v1/agents", params=params or None)
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
        mode: str = "chat",
        edit_mode: str = "diff",
        routing_mode: str = "auto",
        metadata: dict[str, Any] | None = None,
    ) -> Session:
        """POST /v1/sessions."""

        response = self._client._request(
            "POST",
            "/v1/sessions",
            json={
                "workspace_id": workspace_id,
                "title": title,
                "mode": mode,
                "edit_mode": edit_mode,
                "routing_mode": routing_mode,
                "metadata": metadata or {},
            },
        )
        return Session.model_validate(response.json())

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
        routing_mode: str | None = None,
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
                "routing_mode": routing_mode,
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
        is inclusive; the fork gets store defaults, not the parent's
        modes/model (SPEC §6.2)."""

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
