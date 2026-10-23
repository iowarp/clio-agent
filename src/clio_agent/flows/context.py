"""The runtime context a flow's ``forward(task, ctx)`` receives.

Every primitive is built on something the task layer already provides
(``docs/design/sdk/orchestration-semantics.md``):

=====================  ==================================================
``ctx.call``           ``Conversations.spawn`` for subagents, agents and flows;
                       the agent's own tool for a tool target
``ctx.start``          the same, returning the handle at acceptance
``ctx.gather``         concurrent calls, bounded, waited with ``TaskOperations.wait``
``ctx.as_completed``   each result as it settles, with no barrier
``ctx.ask``            a Question task
``ctx.step``           any other side effect, recorded in the run's checkpoint
``ctx.workspace``      the session's workspace folder
``ctx.depth``          how many flows this run is nested in
``ctx.react``          the agent's own ReAct program inside this assignment
=====================  ==================================================

Each primitive gets an invocation id derived from the run, the call site, a
digest of target and input, and an occurrence index, computed when the call is
made (in program order), so the task handle is deterministic and a replay after
a restart re-attaches instead of submitting twice.
"""

from __future__ import annotations

import asyncio
import contextvars
import inspect
from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from clio_agent.flows.call_metadata import child_model, child_session_metadata
from clio_agent.flows.journal import FlowJournal, digest
from clio_agent.flows.outcomes import (
    STATUS_OUTCOMES,
    CallCancelled,
    CallFailed,
    CallRefused,
    FlowCheckpointLimit,
    FlowLimitExceeded,
    FlowOutcome,
    FlowTargetUnknown,
    GatherFailed,
    TaskStateUncertain,
)
from clio_agent.flows.values import CallResult, FlowWorkspace, _as_prediction, _plain, render_input
from clio_agent.sdk.flow import Flow

_FLOWS_PACKAGE = str(Path(__file__).resolve().parent)


class FlowCall:
    """One ``ctx.call``: awaitable, runs at most once, identity fixed at creation."""

    def __init__(
        self,
        ctx: FlowContext,
        target: Any,
        kind: str,
        name: str,
        payload: Any,
        invocation: str,
        evidence: list[str] | None,
        model: str = "",
    ) -> None:
        self.ctx = ctx
        self.model = model  #: The model the child runs on; empty lets the child decide.
        self.target = target
        self.kind = kind
        self.name = name
        self.input = payload
        self.invocation = invocation
        self.evidence = list(evidence or [])
        self.index = -1
        self.together = ""  #: The first invocation of the gather this call ran in.
        #: Set by the workflow flow for an editor's call: the child gets the editor tools.
        self.editor: dict[str, Any] | None = None
        self._task: asyncio.Future[CallResult] | None = None

    def _future(self) -> asyncio.Future[CallResult]:
        if self._task is None:
            self._task = asyncio.ensure_future(self.ctx._execute(self))
        return self._task

    def __await__(self) -> Any:
        return self._future().__await__()

    def __repr__(self) -> str:
        return f"FlowCall({self.kind}:{self.name}, {self.invocation})"


class FlowContext:
    """``ctx`` for one flow run inside an agent's assignment."""

    def __init__(
        self,
        *,
        app: Any,
        session_id: str,
        module: Any,
        flow: Flow,
        root_handle: str,
        run_invocation: str,
        task: Any,
        depth: int = 0,
        kwargs: dict[str, Any] | None = None,
    ) -> None:
        self.app = app
        self.session_id = session_id
        self._caller: Any = None
        self.module = module
        self.flow = flow
        self.task = task
        self.kwargs = dict(kwargs or {})
        self.root_handle = root_handle
        self.run_invocation = run_invocation
        self._depth = depth
        #: The flow's own default model for its calls (``Flow.model``); empty: none.
        self.default_model = str(getattr(flow, "model", "") or "")
        self.limits = flow.limits()
        resolution = getattr(module, "resolution", None)
        self.resolution = resolution
        self.journal = FlowJournal(
            app,
            session_id=session_id,
            root_handle=root_handle,
            run_invocation=run_invocation,
            flow_id=flow.reference()["id"] or "react",
            declaration=self._declaration(),
            request=_plain(task),
            depth=depth,
        )
        self._opened = False
        self._open_lock = asyncio.Lock()
        self._occurrences: dict[str, int] = {}
        self._model_calls = 0
        self._spent_tokens = 0
        concurrency = self.limits["max_concurrency"]
        self._slots = asyncio.Semaphore(concurrency if concurrency > 0 else 1_000_000)
        self.workflow_state: dict[str, Any] = {}
        self._state_sources: dict[str, str] = {}
        self.conflicts: list[dict[str, Any]] = []
        self.editor_tools: dict[str, Any] = {}

    # ------------------------------------------------------------ identity
    @property
    def caller(self) -> Any:
        """This assignment's task caller, made on first use (a plain ``react`` turn needs none)."""

        if self._caller is None and self.app is not None:
            self._caller = self.app.state.conversations.caller(self.session_id)
        return self._caller

    def _declaration(self) -> str:
        reference = self.flow.reference()
        resolution = self.resolution
        digest_of_code = getattr(resolution, "digest", "") if resolution is not None else ""
        return digest({"flow": reference, "resolution": digest_of_code})[:32]

    @property
    def depth(self) -> int:
        """How many flows this run is nested in (0 at the top)."""

        return self._depth

    @property
    def run_id(self) -> str:
        return self.journal.run_id

    @property
    def workspace(self) -> FlowWorkspace:
        root = None
        if self.app is not None:
            from clio_agent.gact.agents.resolution import (  # noqa: PLC0415
                _runtime_workspace_catalog_cwd,
            )

            root = _runtime_workspace_catalog_cwd(self.app, session_id=self.session_id)
        return FlowWorkspace(root)

    def _site(self) -> str:
        frame = inspect.currentframe()
        try:
            while frame is not None:
                filename = frame.f_code.co_filename
                if not filename.startswith(_FLOWS_PACKAGE):
                    return f"{Path(filename).name}:{frame.f_lineno}"
                frame = frame.f_back
        finally:
            del frame
        return "unknown"

    def _invocation(self, kind: str, name: str, payload: Any, *, site: str = "") -> str:
        call_site = site or self._site()
        content = digest({"kind": kind, "target": name, "input": _plain(payload)})[:16]
        key = f"{call_site}|{content}"
        occurrence = self._occurrences.get(key, 0)
        self._occurrences[key] = occurrence + 1
        return f"{self.journal.run_id}:{kind}:{call_site}:{content}:{occurrence}"

    async def _ensure_open(self) -> None:
        if self._opened:
            return
        async with self._open_lock:
            if not self._opened:
                await self.journal.open()
                self._opened = True

    # ------------------------------------------------------------- targets
    def _resolve_target(self, target: Any) -> tuple[str, str, Any]:
        """``(kind, name, spec)`` for a call target; raises :class:`FlowTargetUnknown`."""

        if isinstance(target, Flow) or (isinstance(target, type) and issubclass(target, Flow)):
            flow = target if isinstance(target, Flow) else target()
            reference = flow.reference()
            if not reference["id"]:
                raise FlowTargetUnknown("a flow target needs an id")
            return "flow", reference["id"], reference
        name = str(target or "").strip()
        if not name:
            raise FlowTargetUnknown("ctx.call needs a target")
        if name == "self":
            return "agent", "self", None
        resolution = self.resolution
        if resolution is not None:
            if any(row["id"] == name for row in resolution.subagents):
                return "subagent", name, None
            if any(row["id"] == name for row in resolution.flows):
                return "flow", name, {"id": name, "options": {}}
        tool = self._tool(name)
        if tool is not None:
            return "tool", tool.name, tool
        if "/" in name and self.app is not None:
            from clio_agent.gact.agents.resolution import plugin_service  # noqa: PLC0415
            from clio_agent.plugins.service import AgentNotFound  # noqa: PLC0415

            try:
                plugin_service(self.app).lookup(name)
            except AgentNotFound as exc:
                raise FlowTargetUnknown(f"agent {name!r} is not installed", target=name) from exc
            return "agent", name, None
        if self.app is not None:
            from clio_agent.gact.plugin_authoring import refuse_untrusted_flow  # noqa: PLC0415

            refuse_untrusted_flow(self.app, name)  # an installed, untrusted flow: typed refusal
        raise FlowTargetUnknown(
            f"{name!r} is not a subagent, flow, tool or installed agent this agent can reach",
            target=name,
        )

    def _tool(self, name: str) -> Any:
        tools = getattr(self.module, "tools", None) or []
        if isinstance(tools, Mapping):
            tools = list(tools.values())
        wanted = {name, name.replace(".", "_")}
        if name in self.editor_tools:
            return self.editor_tools[name]
        return next((tool for tool in tools if getattr(tool, "name", "") in wanted), None)

    # ------------------------------------------------------------ primitives
    def call(
        self,
        target: Any,
        input: Any = None,  # noqa: A002 - the design's public parameter name
        *,
        evidence: list[str] | None = None,
        key: str = "",
        model: str = "",
    ) -> FlowCall:
        """Run a subagent, agent, flow or tool and return its result (awaitable).

        ``evidence`` pins earlier task handles as the child's input evidence
        (``input_task_refs``); nothing else from this flow is passed implicitly.
        ``model`` names the model the child runs on (``account/model`` or a model
        id); empty uses the flow's own ``model`` default, then the child's
        preference, then this run's model. A model that cannot run the child is
        refused with ``model_requirement_unmet`` and the alternatives.
        ``key`` replaces the call site in the invocation id when the caller
        starts calls in an order replay may not repeat (the workflow flow keys each
        node's call by node id); keys must be unique within the run.
        """

        kind, name, spec = self._resolve_target(target)
        payload = self.task if input is None and kind != "tool" else input
        if kind in {"agent", "subagent", "flow"}:
            self._admit_model_call(kind, name)
        invocation = self._invocation(kind, name, payload, site=f"key={key}" if key else "")
        call = FlowCall(
            self,
            spec if kind == "tool" else target,
            kind,
            name,
            payload,
            invocation,
            evidence,
            model or self.default_model,
        )
        if kind == "flow":
            call.target = spec
        return call

    async def start(
        self,
        target: Any,
        input: Any = None,  # noqa: A002 - the design's public parameter name
        *,
        evidence: list[str] | None = None,
        key: str = "",
        model: str = "",
    ) -> str:
        """Like :meth:`call`, but return the task handle once it is accepted."""

        call = self.call(target, input, evidence=evidence, key=key, model=model)
        if call.kind == "tool":
            raise CallRefused(
                "ctx.start needs a subagent, agent or flow, not a tool", target=call.name
            )
        await self._ensure_open()
        return await self._accept(call)

    async def gather(
        self,
        calls: Iterable[FlowCall | Any],
        *,
        limit: int | None = None,
        return_when: str = "all",
        return_exceptions: bool = False,
    ) -> list[Any]:
        """Run calls concurrently (at most ``limit`` at once) and return their results.

        ``return_when="all"`` waits for every call; ``"any"`` returns as soon as
        one settles, with ``None`` for the rest (which keep running). A failed
        member never cancels the others: with ``return_exceptions`` the typed
        outcome takes its place, otherwise :class:`GatherFailed` carries every
        member's outcome once all have settled. Successful results' typed
        ``workflow_state`` merges into ``ctx.workflow_state`` in call order, and a
        key two members set differently is recorded in ``ctx.conflicts``.
        """

        if return_when not in {"all", "any"}:
            raise ValueError("return_when must be all or any")
        items = [self._as_call(item) for item in calls]
        for index, item in enumerate(items):
            item.index, item.together = index, items[0].invocation
        gate = asyncio.Semaphore(limit if limit and limit > 0 else max(1, len(items)))

        async def bounded(item: FlowCall) -> Any:
            async with gate:
                try:
                    return await item
                except FlowCheckpointLimit:
                    raise  # The run cannot record more work: it ends (D3).
                except FlowOutcome as outcome:
                    return outcome

        tasks = [asyncio.ensure_future(bounded(item)) for item in items]
        if return_when == "any" and tasks:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            return [task.result() if task in done else None for task in tasks]
        results = list(await asyncio.gather(*tasks))
        await self.merge_results(results)
        if not return_exceptions and any(isinstance(r, FlowOutcome) for r in results):
            raise GatherFailed(
                f"{sum(isinstance(r, FlowOutcome) for r in results)} of {len(results)} gathered "
                "calls did not succeed",
                results,
            )
        return results

    async def as_completed(
        self, calls: Iterable[FlowCall | Any], *, limit: int | None = None
    ) -> AsyncIterator[Any]:
        """Yield each call's result (or its typed outcome) as it settles, with no barrier.

        Each yielded value has ``index`` set to the call's position.
        """

        items = [self._as_call(item) for item in calls]
        for item in items:
            item.together = items[0].invocation
        gate = asyncio.Semaphore(limit if limit and limit > 0 else max(1, len(items)))

        async def bounded(position: int, item: FlowCall) -> Any:
            async with gate:
                try:
                    value = await item
                except FlowCheckpointLimit:
                    raise
                except FlowOutcome as outcome:
                    outcome.details["index"] = position
                    return outcome
                value.index = position
                return value

        pending = [asyncio.ensure_future(bounded(i, item)) for i, item in enumerate(items)]
        for finished in asyncio.as_completed(pending):
            yield await finished

    async def ask(
        self,
        question: str,
        schema: Any = str,
        *,
        options: list[str] | None = None,
        reason: str = "",
        key: str = "",
    ) -> Any:
        """Ask the human through a Question task and resume on the answer.

        ``schema=bool`` asks a yes/no choice and returns a bool; ``options``
        asks a choice and returns the chosen option; otherwise the free-text
        answer is returned.
        """

        await self._ensure_open()
        choices = ["Yes", "No"] if schema is bool else list(options or [])
        invocation = self._invocation(
            "ask",
            question,
            {"choices": choices, "schema": str(schema)},
            site=f"key={key}" if key else "",
        )
        row = self.journal.call(invocation)
        if row is None:
            handle = await self.app.state.task_history.io.run(
                self._post_question, invocation, question, choices, reason
            )
            await self.journal.record_call(
                invocation,
                {"kind": "ask", "target": question, "handle": handle, "status": "accepted"},
            )
        else:
            handle = row["handle"]
        value = await self._wait(handle, invocation, kind="ask", name=question)
        reply = value.outputs
        selected = list(reply.get("selected_options") or [])
        answer = str(reply.get("answer") or (selected[0] if selected else ""))
        if schema is bool:
            return answer.strip().lower() in {"yes", "true", "approve", "approved", "y"}
        return answer

    def _post_question(
        self, invocation: str, question: str, choices: list[str], reason: str
    ) -> str:
        import hashlib  # noqa: PLC0415

        from clio_agent.arc.task_types import QuestionDefinition, TaskOwner  # noqa: PLC0415

        history = self.app.state.task_history
        caller = self.caller
        handle = (
            "task_"
            + hashlib.sha256((caller.session_id + "\0question\0" + invocation).encode()).hexdigest()
        )
        with history.serialized():
            if history.task_snapshot(handle) is not None:
                return handle
            owner = history.task_snapshot(self.root_handle)
            if owner is None:
                raise CallRefused("the flow's owning assignment is unavailable")
            self.app.state.conversations.require_admission(owner)
            definition = QuestionDefinition.model_validate(
                {
                    "prompt": question,
                    "kind": "choice" if choices else "freeform",
                    "options": [{"label": c, "value": c.lower()} for c in choices],
                    "allow_freeform": not choices,
                    "source": "flow",
                    "metadata": {
                        "flow_run": self.journal.run_id,
                        **({"reason": reason} if reason else {}),
                    },
                }
            )
            current = self.app.state.questions.create(
                TaskOwner(
                    human_id=caller.human_id,
                    session_id=caller.session_id,
                    assignment_id=owner.handle,
                    parent_handle=owner.handle,
                ),
                definition,
                invocation_id=invocation,
                handle=handle,
            )
            return current.handle

    async def step(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Run a side effect once and record its result; a replay returns the record."""

        await self._ensure_open()
        name = str(getattr(fn, "__qualname__", None) or getattr(fn, "__name__", repr(fn)))
        invocation = self._invocation(
            "step", name, {"args": _plain(args), "kwargs": _plain(kwargs)}
        )
        row = self.journal.step(invocation)
        if row is not None:
            return row.get("result")
        if inspect.iscoroutinefunction(fn):
            result = await fn(*args, **kwargs)
        else:
            context = contextvars.copy_context()
            result = await asyncio.to_thread(context.run, fn, *args, **kwargs)
        await self.journal.record_step(invocation, {"name": name, "result": _plain(result)})
        return result

    async def react(self, task: Any) -> Any:
        """Run the agent's own ReAct program on ``task`` inside this assignment."""

        self._admit_model_call("react", "react")
        kwargs = dict(self.kwargs)
        kwargs["question"] = render_input(task)
        program = self.module.program
        context = contextvars.copy_context()
        return await asyncio.to_thread(context.run, lambda: program(**kwargs))

    async def publish(self, view: dict[str, Any]) -> None:
        """Replace the run's live view, which the flow run view draws (the workflow's steps).

        The view lives in the run's checkpoint and is overwritten on each publish,
        so it never counts toward the checkpoint limit and replays converge on it.
        """

        await self._ensure_open()
        await self.journal.publish(_plain(view))

    async def record_event(self, event: dict[str, Any]) -> None:
        """Record a run event (a workflow edit, a stall) in the checkpoint, once.

        Events are keyed like calls, so a replay that re-runs ``forward`` does
        not record them twice.
        """

        await self._ensure_open()
        key = self._invocation("event", str(event.get("event", "")), event, site="events")
        await self.journal.record_event(key, _plain(event))

    def _as_call(self, item: Any) -> FlowCall:
        if isinstance(item, FlowCall):
            return item
        if inspect.isawaitable(item):
            raise TypeError("ctx.gather takes ctx.call(...) objects, not other awaitables")
        raise TypeError(f"ctx.gather cannot run {item!r}")

    def _admit_model_call(self, kind: str, name: str) -> None:
        limit = self.limits["max_model_calls"]
        self._model_calls += 1
        if limit and self._model_calls > limit:
            raise FlowLimitExceeded(
                f"flow {self.journal.flow_id} reached its limit of {limit} model calls",
                limit="max_model_calls",
                target=name,
            )
        budget = self.limits["budget_tokens"]
        if budget and self._spent_tokens >= budget:
            raise FlowLimitExceeded(
                f"flow {self.journal.flow_id} spent its {budget}-token budget",
                limit="budget_tokens",
                spent=self._spent_tokens,
                target=name,
            )
        if kind == "flow":
            max_depth = self.limits["max_depth"]
            if max_depth and self._depth + 1 > max_depth:
                raise FlowLimitExceeded(
                    f"flow {name} would run at depth {self._depth + 1}, above {max_depth}",
                    limit="max_depth",
                    target=name,
                )

    async def _execute(self, call: FlowCall) -> CallResult:
        await self._ensure_open()
        async with self._slots:
            if call.kind == "tool":
                return await self._run_tool(call)
            handle = await self._accept(call)
            return await self._wait(handle, call.invocation, kind=call.kind, name=call.name)

    async def _accept(self, call: FlowCall) -> str:
        from clio_agent.gact.task_access import AdmissionClosed  # noqa: PLC0415

        row = self.journal.call(call.invocation)
        if row is not None and row.get("handle"):
            return str(row["handle"])
        app = self.app
        agent_id = "self" if call.kind == "flow" else call.name
        session_metadata = child_session_metadata(call, self._depth + 1, self.journal.run_id)
        launch_context: dict[str, Any] | None = None
        if call.evidence:
            from clio_agent.gact.assignment_evidence import (  # noqa: PLC0415
                select_assignment_evidence,
            )

            pins = await app.state.task_history.io.run(
                select_assignment_evidence, app.state.task_history, self.caller, call.evidence
            )
            launch_context = {"input_task_refs": [pin.model_dump(mode="json") for pin in pins]}
        try:
            accepted = await app.state.conversations.spawn(
                self.caller,
                render_input(call.input),
                agent_id,
                call.invocation,
                launch_context=launch_context,
                session_metadata=session_metadata,
                parent_handle=self.root_handle,
                model=call.model,
            )
        except AdmissionClosed as exc:
            if exc.stopping:
                # The run is being cancelled: the call is cancelled with it.
                raise CallCancelled(str(exc), target=call.name, status="cancelled") from exc
            raise CallRefused(str(exc), target=call.name) from exc
        except ValueError as exc:
            code = str(getattr(exc, "code", "") or "")
            raise CallRefused(str(exc), target=call.name, reason_code=code) from exc
        if not accepted.get("accepted", False):
            raise CallRefused(
                str(accepted.get("error") or accepted.get("reason") or "call was not accepted"),
                target=call.name,
                reason_code=accepted.get("reason", ""),
            )
        handle = str(accepted["handle"])
        await self.journal.record_call(
            call.invocation,
            {
                "kind": call.kind,
                "target": call.name,
                "handle": handle,
                "session_id": accepted.get("session_id", ""),
                "status": "accepted",
                **({"together": call.together} if call.together else {}),
            },
        )
        return handle

    def _collect(self, handle: str) -> None:
        """Retire the settled task's notice: the flow consumed its result.

        The result reaches the model only through the flow's own return, so the
        notice must not also wake this session (``session.mail.withdrawn`` records
        it, as for an unsubscribed result). A notice already committed to a model
        iteration stays as it is.
        """

        history = self.app.state.task_history
        with history.serialized():
            for item in history.pending_mail(self.session_id):
                if item.handle == handle and item.kind in {"task_result", "question_answer"}:
                    try:
                        history.withdraw_mail(self.session_id, item.id)
                    except (KeyError, ValueError):
                        continue

    async def _wait(self, handle: str, invocation: str, *, kind: str, name: str) -> CallResult:
        waited = await self.app.state.task_operations.wait(self.caller, handle, "all")
        if not waited.get("pending"):
            await self.app.state.task_history.io.run(self._collect, handle)
        outcome = (waited.get("outcomes") or [{}])[0]
        if waited.get("pending"):
            await self._settle(invocation, "task_state_uncertain")
            raise TaskStateUncertain(
                f"the state of {kind} {name!r} cannot be confirmed", handle=handle
            )
        status = str(outcome.get("status") or "")
        if outcome.get("error"):
            await self._settle(invocation, "task_unavailable")
            raise CallFailed(
                f"{kind} {name!r}: {outcome['error']}", handle=handle, error=outcome["error"]
            )
        await self._account(outcome)
        if status != "completed":
            await self._settle(invocation, status or "unknown")
            exception = STATUS_OUTCOMES.get(status, CallFailed)
            raise exception(
                f"{kind} {name!r} ended {status or 'without a status'}"
                + (f": {outcome.get('reason')}" if outcome.get("reason") else ""),
                handle=handle,
                status=status,
                result=outcome.get("result"),
            )
        await self._settle(invocation, "completed")
        value = outcome.get("result")
        if outcome.get("next_cursor"):
            value = await self.app.state.task_history.io.run(self._full_result, handle)
        model = await self.app.state.task_history.io.run(child_model, self.app, handle)
        return CallResult(value, handle=handle, target=name, kind=kind, model=model)

    def _full_result(self, handle: str) -> Any:
        """A paged result's whole content (the wait already authorized reading it)."""

        history = self.app.state.task_history
        with history.serialized():
            task = history.task_snapshot(handle)
            return history.read_content(task.result) if task is not None and task.result else None

    async def _settle(self, invocation: str, status: str) -> None:
        row = self.journal.call(invocation)
        if row is not None and row.get("status") != status:
            await self.journal.record_call(invocation, {**row, "status": status})

    async def _account(self, outcome: Mapping[str, Any]) -> None:
        """Add the child's model tokens to the run's spend (the budget limit)."""

        if not self.limits["budget_tokens"]:
            return
        row = next(
            (
                r
                for r in self.journal.content["calls"].values()
                if r.get("handle") == outcome.get("handle")
            ),
            None,
        )
        session = self.app.state.sessions.get(str((row or {}).get("session_id") or ""))
        if session is not None:
            self._spent_tokens += int(session.tokens_input or 0) + int(session.tokens_output or 0)

    async def _run_tool(self, call: FlowCall) -> CallResult:
        recorded = self.journal.step(call.invocation)
        if recorded is not None:
            return CallResult(recorded.get("result"), target=call.name, kind="tool")
        tool = call.target
        arguments = call.input if isinstance(call.input, Mapping) else {}
        if call.input is not None and not isinstance(call.input, Mapping):
            raise CallRefused(f"tool {call.name!r} takes a mapping of arguments", target=call.name)
        from clio_agent.gact.tool_observer import _OBSERVER_CALL_IDS  # noqa: PLC0415

        token = _OBSERVER_CALL_IDS.set(call.invocation)
        try:
            func = getattr(tool, "func", None)
            if inspect.iscoroutinefunction(func):
                result = await tool.acall(**dict(arguments))
            else:
                context = contextvars.copy_context()
                result = await asyncio.to_thread(context.run, lambda: tool(**dict(arguments)))
        except FlowOutcome:
            raise
        except Exception as exc:  # noqa: BLE001 - a tool error is the flow's typed outcome
            raise CallFailed(f"tool {call.name!r} failed: {exc}", error=type(exc).__name__) from exc
        finally:
            _OBSERVER_CALL_IDS.reset(token)
        if isinstance(result, Mapping) and result.get("accepted") and result.get("handle"):
            return await self._wait(
                str(result["handle"]), call.invocation, kind="tool", name=call.name
            )
        await self.journal.record_step(
            call.invocation, {"name": call.name, "result": _plain(result)}
        )
        return CallResult(result, target=call.name, kind="tool")

    async def merge_results(self, results: list[Any]) -> None:
        """Merge successful members' ``workflow_state`` in call order; record conflicts."""

        for position, value in enumerate(results):
            if not isinstance(value, CallResult):
                continue
            for key, section in value.workflow_state.items():
                if key in self.workflow_state and self.workflow_state[key] != section:
                    conflict = {
                        "key": key,
                        "earlier": self._state_sources.get(key, ""),
                        "later": value.handle,
                        "index": position,
                    }
                    self.conflicts.append(conflict)
                    await self.record_event({"event": "workflow_state_conflict", **conflict})
                self.workflow_state[key] = section
                self._state_sources[key] = value.handle

    # ------------------------------------------------------------------ run
    async def run(self) -> Any:
        """Run the flow's ``forward`` and record how the run ended."""

        try:
            value = await self.flow.forward(self.task, self)
        except FlowOutcome as outcome:
            if self._opened:
                # A cancellation is how the run ended, not a fault in it.
                status = "cancelled" if isinstance(outcome, CallCancelled) else outcome.reason
                await self.journal.finish(status, outcome.to_dict())
            raise
        except Exception as error:
            # Any other error ends the run too: its record must not stay "running".
            if self._opened:
                await self.journal.finish(
                    "flow_error", {"error": type(error).__name__, "message": str(error)[:2000]}
                )
            raise
        if self._opened:
            await self.journal.finish("completed", _plain(value))
        return _as_prediction(value)


__all__ = [
    "CallResult",
    "FlowCall",
    "FlowContext",
    "FlowWorkspace",
    "render_input",
]
