"""The run surface of :class:`~clio_agent.client.ClioClient`: ``run``, ``run_flow``,
``stream_flow`` and ``session``.

Every run creates (or continues) an *unattended* session: the server answers its
questions "the user is not available", denies anything the approval mode would ask a
person, and trusts the plugins it needs (:mod:`clio_agent.gact.unattended`). The
options' ``agent``, ``model``, ``approval_mode``, ``max_turns`` and
``max_budget_usd`` apply unless a call overrides them.

A refused flow (an unknown flow, options that do not load) and every failed run
return ``status="failed"`` with the server's typed error. Errors in the script's own
use of the library raise: an agent no marketplace provides is
:class:`AgentNotAvailable`, naming the agent and how to get it.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from clio_agent.client.connection import ServerSetupError
from clio_agent.client.errors import ClioAPIError, ConflictError
from clio_agent.client.run import RunEngine, RunResult

if TYPE_CHECKING:
    from clio_agent.client.client import ClioClient
    from clio_agent.client.options import ClioOptions
    from clio_agent.client.types import Session

#: ``run_flow`` refusals: returned as a failed result, the way ``run_flow`` refuses an agent.
FLOW_REFUSALS = frozenset(
    {
        "flow_not_resolved",
        "flow_options_invalid",
        "flow_import_failed",
        "flow_invalid",
        "flow_not_trusted",
        "flow_not_added",
    }
)


class AgentNotAvailable(ServerSetupError):
    """The requested agent is not installed in any trusted marketplace."""


class RunSession:
    """A session for several runs: ``run`` and ``stream`` again on the same conversation."""

    def __init__(self, client: ClioClient, session: Session, workspace_root: str) -> None:
        self.client = client
        self.id = session.id
        self.session = session
        self._engine = RunEngine(client, session.id, workspace_root)

    def run(self, prompt: str, *, files: list[str] | None = None) -> RunResult:
        """Run ``prompt`` in this session and return its result."""

        return self._engine.run(prompt, files)

    def stream(self, prompt: str, *, files: list[str] | None = None) -> Iterator[Any]:
        """Run ``prompt`` and yield its typed events, ending with the :class:`RunResult`."""

        return self._engine.stream(prompt, files)


class RunAPI:
    """Mixin: the run methods of :class:`ClioClient` (it provides ``options`` and the API)."""

    options: ClioOptions

    # -- public ------------------------------------------------------------- #

    def run(
        self,
        prompt: str,
        *,
        files: list[str] | None = None,
        agent: str | None = None,
        model: str | None = None,
        approval_mode: str | None = None,
        max_turns: int | None = None,
        max_budget_usd: float | None = None,
    ) -> RunResult:
        """Run an agent on ``prompt`` in a new session and wait for it to finish.

        ``files`` are workspace paths attached as workspace-file references, the way
        the composer attaches them.
        """

        handle = self.session(
            agent=agent,
            model=model,
            approval_mode=approval_mode,
            max_turns=max_turns,
            max_budget_usd=max_budget_usd,
            title=prompt,
        )
        return handle.run(prompt, files=files)

    def run_flow(
        self,
        flow_id: str,
        input: str,  # noqa: A002 - the flow's input, as run_flow names it
        *,
        agent: str | None = None,
        options: dict[str, Any] | None = None,
        model: str | None = None,
        approval_mode: str | None = None,
        max_turns: int | None = None,
        max_budget_usd: float | None = None,
    ) -> RunResult:
        """Run flow ``flow_id`` on ``input`` as a session's own work and wait for it."""

        result: Any = None
        for item in self.stream_flow(
            flow_id,
            input,
            agent=agent,
            options=options,
            model=model,
            approval_mode=approval_mode,
            max_turns=max_turns,
            max_budget_usd=max_budget_usd,
        ):
            result = item
        return result

    def stream_flow(
        self,
        flow_id: str,
        input: str,  # noqa: A002
        *,
        agent: str | None = None,
        options: dict[str, Any] | None = None,
        model: str | None = None,
        approval_mode: str | None = None,
        max_turns: int | None = None,
        max_budget_usd: float | None = None,
    ) -> Iterator[Any]:
        """Run a flow and yield its typed events (its children as :class:`TaskStarted` /
        :class:`TaskSettled`), ending with the :class:`RunResult`."""

        try:
            handle = self.session(
                agent=agent,
                model=model,
                approval_mode=approval_mode,
                max_turns=max_turns,
                max_budget_usd=max_budget_usd,
                flow={"id": flow_id, "options": dict(options or {})},
                title=f"{flow_id}: {input}",
            )
        except ClioAPIError as exc:
            if exc.error not in FLOW_REFUSALS:
                raise
            yield RunResult(status="failed", text="", session_id="", error=exc.info)
            return
        yield from handle.stream(input)

    def session(
        self,
        *,
        id: str | None = None,  # noqa: A002 - the session id to continue
        agent: str | None = None,
        model: str | None = None,
        approval_mode: str | None = None,
        max_turns: int | None = None,
        max_budget_usd: float | None = None,
        flow: dict[str, Any] | None = None,
        title: str = "",
    ) -> RunSession:
        """A new session for several runs, or ``id``: continue a session made earlier
        (by this script or in the UI)."""

        client: ClioClient = self  # type: ignore[assignment]
        if id is not None:
            existing = client.sessions.get(id)
            root = client.workspaces.get(existing.workspace_id).root_path
            return RunSession(client, existing, root)
        chosen = self.options.with_overrides(
            agent=agent,
            model=model,
            approval_mode=approval_mode,
            max_turns=max_turns,
            max_budget_usd=max_budget_usd,
        )
        workspace_id, root = self._workspace(chosen.workspace_path())
        try:
            created = client.sessions.create(
                workspace_id=workspace_id,
                title=" ".join(title.split())[:80],
                agent=chosen.agent,
                model=chosen.model,
                approval_mode=chosen.approval_mode,
                flow=flow,
                unattended=True,
                max_turns=chosen.max_turns,
                max_budget_usd=chosen.max_budget_usd,
            )
        except ClioAPIError as exc:
            if exc.error == "agent_unavailable":
                raise AgentNotAvailable(_agent_fix(chosen.agent, exc)) from exc
            raise
        return RunSession(client, created, root)

    # -- workspace ----------------------------------------------------------- #

    def _workspace(self, path: str) -> tuple[str, str]:
        """``(workspace id, root)`` of the workspace rooted at ``path``, made if needed."""

        client: ClioClient = self  # type: ignore[assignment]
        cached = client._workspace_ids.get(path)
        if cached:
            return cached, path
        found = self._find_workspace(path)
        if found is None:
            name = os.path.basename(path.rstrip("/\\")) or path
            try:
                found = client.workspaces.create(name, root_path=path).id
            except ConflictError:
                found = self._find_workspace(path)  # created meanwhile by another client
                if found is None:
                    raise
        client._workspace_ids[path] = found
        return found, path

    def _find_workspace(self, path: str) -> str | None:
        client: ClioClient = self  # type: ignore[assignment]
        wanted = os.path.normcase(os.path.normpath(path))
        for workspace in client.workspaces.list():
            root = workspace.root_path
            if root and os.path.normcase(os.path.normpath(root)) == wanted:
                return workspace.id
        return None


def _agent_fix(agent: str | None, exc: ClioAPIError) -> str:
    reason = str(exc.details.get("reason") or exc.error)
    return (
        f"{exc.info.message} ({reason}). Use an agent the "
        "server lists (GET /v1/agents, or the + menu in CLIO), install the marketplace "
        "that provides it (clio-agent plugin install <source>), or name it as "
        "<marketplace>/<id> to use one from an installed marketplace that is not trusted "
        "yet (an unattended run trusts it)."
    )


__all__ = ["FLOW_REFUSALS", "AgentNotAvailable", "RunAPI", "RunSession"]
