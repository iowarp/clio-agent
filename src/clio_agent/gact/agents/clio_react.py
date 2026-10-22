"""``ClioReAct`` -- clio's agent loop on DSPy 3.4's direct LM interface.

Each step is ONE ``lm(dspy.lm15.Request)`` call -- no ``dspy.Predict``, no adapter, no
field format. The request carries the expert's system prompt (or the signature's
instructions), shared guidance for its declared tools, the task as one user message,
every earlier step of this turn
as typed messages, and the tools as native function tools (``submit`` among them for
structured outputs). The reply comes back typed: thinking (with the provider's
continuation state, sent back as-is on the next call), the visible text (shown as
written), and tool calls. Codex direct sends them as native function tools too; only
the Claude Code engine, whose transport has no native tools, carries them as text
inside the engine (``text_block``). The loop is the same for every provider.

Per step:

1. **boundary** -- cancellation (a typed ``_TurnCancelled``), then the proactive
   compaction trigger;
2. **context** -- the head plus the ARC live plane folded into typed messages
   (:func:`~clio_agent.gact.agents.clio_react_record.fold_steps`), or the loop's own
   step list when there is no ARC scope; a plane read failure is a typed turn failure;
3. **call** -- streamed on the one persistent LM loop (connections are reused across
   steps): text deltas to the transcript lane, thinking to the thinking lane, as they
   arrive;
4. **tools** -- a step's calls run concurrently, one worker each in a copy of the
   step's context; results keep call order. A terminal MCP protocol refusal or a
   cancellation raised by a tool escalates after the step is recorded;
5. **end** -- no tool call: the text is the answer (``direct_response``); ``submit``:
   its typed outputs; ``ask_user`` / ``plan_exit``: yield to the user;
   ``draft_alternatives``: the selected draft is the answer, or the turn yields the
   drafts to the user; ``max_iters`` (``<= 0`` unlimited) or ``context_window_exceeded``:
   stop. Nothing calls the model after the loop (``react-loop-completion-2026-09.md``).

Human-judged module variants collect their own common Core Question results.
Ordinary agent Questions remain inputs to the current conversation.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import dataclasses
import inspect
import itertools
import logging
import threading
import traceback
import uuid
from collections.abc import Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

import dspy
from dspy.lm15 import (
    CacheConfig,
    Config,
    ContextLengthError,
    DocumentPart,
    FunctionTool,
    ImagePart,
    Message,
    Request,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolResultPart,
)
from dspy.utils.exceptions import AdapterParseError, ContextWindowExceededError

from clio_agent.errors import ClioError, MCPProtocolError
from clio_agent.gact.agents import clio_react_extract as extract
from clio_agent.gact.agents import clio_react_record as record
from clio_agent.gact.agents import clio_react_stream as stream
from clio_agent.gact.agents.clio_react_recovery import recover_stream
from clio_agent.gact.agents.clio_react_stream_call import call_lm as _call_lm
from clio_agent.gact.agents.clio_react_submit import active_react_scope_safe, record_submit_audit
from clio_agent.gact.agents.clio_react_submit import submit_tool as _submit_tool
from clio_agent.gact.injection_parts import emit_injection
from clio_agent.lm.engines.text_tools import INVALID_TOOL_CALL
from clio_agent.lm.request_builder import sendable_lm_kwargs
from clio_agent.lm.request_config import config_from_lm_kwargs
from clio_agent.tools import injections

__all__ = ["ClioReAct"]

#: A submit output field's value flowed to the return contract (the final Prediction).
REACT_SUBMIT_FIELD_SUPPRESSED = "react_submit_field_suppressed"
#: The ``submit`` tool rejected a typed/missing final-output arg: the value did not flow.
REACT_SUBMIT_INVALID_OUTPUT = "react_submit_invalid_output"
_SYSTEM_INPUT = "system_prompt"
_MEDIA_INPUTS = ("images", "files")
_log = logging.getLogger(__name__)


#: Recorded once per agent context (an injection the user sees): a step's calls run at
#: the same time, and each extra step is a whole model round trip.
TOOL_USE_NOTE = (
    "You can make several tool calls in one step; the calls of a step run at the same "
    "time. When calls do not depend on each other's results -- loading several skills, "
    "reading several files, independent lookups -- make them together in one step."
)


#: The loop running in this context (a tool reads it: ``draft_alternatives`` runs the
#: same agent on the same task as tries).
_ACTIVE_LOOP: contextvars.ContextVar["_Loop | None"] = contextvars.ContextVar(
    "clio_react_active_loop", default=None
)
#: A forward that continues a forked line: its task is already on the scope, so it
#: records no new user message (a Refine try forked from the user's pick).
_CONTINUING: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "clio_react_continuing", default=False
)


def active_loop() -> "_Loop | None":
    """The :class:`ClioReAct` loop running in this context, if any."""
    return _ACTIVE_LOOP.get()


@contextlib.contextmanager
def continuing_fork() -> Iterator[None]:
    """Forwards inside continue their scope's line instead of opening a new task."""
    token = _CONTINUING.set(True)
    try:
        yield
    finally:
        _CONTINUING.reset(token)


class NoContextStoreError(ClioError):
    """The loop ran with no clio-core context plane (no app, ARC or react scope)."""

    reason = "no_context_store"

    def __init__(self) -> None:
        super().__init__(
            "ClioReAct needs clio-core as its context store: no app/ARC/react scope is bound",
            error_type=self.reason,
        )


class NoLanguageModelError(ClioError):
    """The loop ran with no LM bound (``dspy.context(lm=...)``)."""

    reason = "no_language_model"

    def __init__(self) -> None:
        super().__init__(
            "ClioReAct needs an LM bound with dspy.context(lm=...)", error_type=self.reason
        )


class ClioReAct(dspy.Module):
    """clio's agent loop (see the module docstring)."""

    def __init__(self, signature: Any, tools: Iterable[Any], max_iters: int = 20) -> None:
        super().__init__()
        self.signature: Any = dspy.ensure_signature(signature)
        if self.signature is None:
            raise ValueError("ClioReAct requires an output signature")
        self.max_iters = max_iters
        # The module's own LM, as on a DSPy predictor: ``dspy.BestOfN`` / ``dspy.Refine``
        # read it and set a per-rollout copy; unset, the context's LM is used.
        self.lm: Any = None
        self._clio_expert_id: str = ""
        # Tools that run but stay out of the request's tool list (a plugin change
        # held as a note: :mod:`clio_agent.gact.agents.prompt_basis`).
        self._clio_unlisted: frozenset[str] = frozenset()
        user_tools = [t if isinstance(t, dspy.Tool) else dspy.Tool(t) for t in tools]
        self.tools: dict[str, dspy.Tool] = {}
        for tool in user_tools:
            if not isinstance(tool.name, str) or not tool.name.strip():
                raise ValueError("Agent tools require an explicit nonempty name")
            self.tools[tool.name] = tool
        if "submit" in self.tools:
            raise ValueError("`submit` is reserved as the final-output tool.")
        self.tools["submit"] = _submit_tool(self.signature)

    def get_lm(self) -> Any:
        """The module's own LM (``None``: the context's LM is used)."""
        return self.lm

    def set_lm(self, lm: Any) -> None:
        """Bind this module to ``lm`` (a DSPy variant's per-rollout copy)."""
        self.lm = lm

    def forward(self, **input_args: Any) -> dspy.Prediction:
        """Run the loop for one expert turn (see the module docstring)."""
        from clio_agent.providers.stateful_common import stateful_scope  # noqa: PLC0415

        # A fresh stateful scope per forward: the stateful engines key their kept
        # conversations on it (Codex direct its WebSocket, Claude Code its session), and
        # the scope routes ARC-op resets to the conversations this forward drove.
        with stateful_scope():
            return _Loop(self, input_args).run()


# --------------------------------------------------------------------------- #
# The request pieces                                                          #
# --------------------------------------------------------------------------- #


def _function_tool(tool: dspy.Tool) -> FunctionTool:
    spec = tool.format_as_litellm_function_call()["function"]
    return FunctionTool(
        name=spec["name"],
        description=spec.get("description") or None,
        parameters=spec["parameters"],
    )


def _system(signature: Any, inputs: dict[str, Any], tools: Iterable[str] = ()) -> str:
    """The agent's composed prompt (capability guidance comes from its plugins' fragments)."""
    del tools
    expert = str(inputs.get(_SYSTEM_INPUT) or "").strip()
    return expert or signature.instructions.strip()


def _head(signature: Any, inputs: dict[str, Any]) -> Message:
    """The task as one user message: the inputs as text, images/files as native parts."""
    parts: list[Any] = []
    lines: list[str] = []
    for name in signature.input_fields:
        if name in (_SYSTEM_INPUT, *_MEDIA_INPUTS) or name not in inputs:
            continue
        value = inputs[name]
        lines.append(str(value) if name == "question" else f"{name}: {value}")
    if lines:
        parts.append(TextPart(text="\n\n".join(lines)))
    for name in _MEDIA_INPUTS:
        for item in inputs.get(name) or []:
            parts.append(_media_part(item))
    return Message(role="user", parts=tuple(parts or [TextPart(text="")]))


def _media_part(item: Any) -> ImagePart | DocumentPart:
    url = str(getattr(item, "url", "") or getattr(item, "file_data", "") or "")
    header, _, data = url.partition(",")
    media_type = header.removeprefix("data:").split(";", 1)[0]
    if not data:
        raise ValueError(f"attachment {type(item).__name__} carries no inline data")
    if media_type.startswith("image/"):
        return ImagePart(data=data, media_type=media_type)
    return DocumentPart(data=data, media_type=media_type or "application/pdf")


def _with_cache_key(config: Config, lm: Any) -> Config:
    """Route this conversation's calls to one prompt cache, where the LM takes a key.

    ``prompt_cache_key`` (OpenAI Responses, Codex direct) is a routing hint: calls
    with the same key and prefix land on the same cache. The key is the
    conversation (GACT session + agent scope), so parallel agents never share one.
    Only an LM that declares ``_clio_prompt_cache_key`` gets it -- lm15 raises for a
    provider without the field.
    """
    from clio_agent.gact import context as _ctx  # noqa: PLC0415

    session = _ctx.active_session_id()
    scope = _ctx.run_keyed_scope(_ctx.active_react_scope())
    if not getattr(lm, "_clio_prompt_cache_key", False) or not session or not scope:
        return config
    cache = config.cache or CacheConfig()
    return dataclasses.replace(
        config, cache=dataclasses.replace(cache, key=f"clio:{session}:{scope}")
    )


def _place_tool_media(messages: list[Message], placement: str) -> list[Message]:
    """Put tool-result images/documents where the LM's API takes them.

    ``native``: inside the tool result. ``user_message`` (chat-completions servers take
    text-only tool rows): the tool result keeps a text marker and the media follows in
    one user message right after the tool message -- the same content, in the one
    place that API carries it. Deterministic, so the context stays prefix-stable.
    """
    if placement == "native":
        return messages
    out: list[Message] = []
    for message in messages:
        media: list[Any] = []
        if message.role == "tool":
            parts = []
            for part in message.parts:
                if not isinstance(part, ToolResultPart):
                    parts.append(part)
                    continue
                kept = [p for p in part.content if not isinstance(p, ImagePart | DocumentPart)]
                moved = [p for p in part.content if isinstance(p, ImagePart | DocumentPart)]
                if moved:
                    note = f"({len(moved)} attachment(s) of this result follow in the next message)"
                    kept.append(TextPart(text=note))
                    media.extend(moved)
                    part = dataclasses.replace(part, content=tuple(kept))
                parts.append(part)
            message = dataclasses.replace(message, parts=tuple(parts))
        out.append(message)
        if media:
            label = TextPart(text="[attachments from the tool results above]")
            out.append(Message(role="user", parts=(label, *media)))
    return out


@dataclass
class _CallOutcome:
    value: Any
    is_error: bool
    escalate: BaseException | None = None


class _Loop:
    """One forward of :class:`ClioReAct`."""

    def __init__(self, agent: ClioReAct, input_args: dict[str, Any]) -> None:
        self.agent = agent
        self.max_iters = int(input_args.pop("max_iters", agent.max_iters))
        input_args.pop("history", None)
        self.inputs = {n: input_args[n] for n in agent.signature.input_fields if n in input_args}
        self.lm = agent.lm or dspy.settings.lm
        if self.lm is None:
            raise NoLanguageModelError()
        self.system = _system(agent.signature, self.inputs, agent.tools)
        self.head = _head(agent.signature, self.inputs)
        unlisted: frozenset[str] = getattr(agent, "_clio_unlisted", frozenset())
        self.tools = tuple(
            _function_tool(t) for t in agent.tools.values() if t.name not in unlisted
        )
        self.config = _with_cache_key(config_from_lm_kwargs(sendable_lm_kwargs(self.lm)), self.lm)
        self.tool_media = str(getattr(self.lm, "_clio_tool_result_media", "native"))
        self.arc, self.session, self.scope = record.arc_scope()
        if self.arc is None:
            raise NoContextStoreError()
        self.recorder = record.StepRecorder(
            self.arc,
            self.session,
            self.scope,
            expert_id=str(getattr(agent, "_clio_expert_id", "") or ""),
        )
        self.step = -1
        self.span = ""
        # A ``draft_alternatives`` call of this turn: settled once its step is recorded.
        self.variant_outcome: Any = None
        self.variant_claim = threading.Lock()
        from clio_agent.gact.compaction import AutoCompactionGuard  # noqa: PLC0415

        # Auto compaction stops for this forward once one leaves it over the threshold.
        self.autocompact = AutoCompactionGuard()

    def run(self) -> dspy.Prediction:
        from clio_agent.gact import context as _ctx  # noqa: PLC0415

        expert_span = uuid.uuid4().hex[:16]
        self.recorder.started(expert_span, self.inputs)
        state = getattr(_ctx.active_app(), "state", None)
        ledger = getattr(state, "messages", None)
        if ledger is not None and _ctx.active_react_run() < 0:
            sessions = getattr(state, "sessions", None)
            session = sessions.get(self.session) if sessions is not None else None
            primary = _ctx.current_turn().primary_agent_id or (
                getattr(session, "agent", {}).get("id", "main") if session is not None else ""
            )
            self.recorder.carry_over(
                ledger.get(self.session, []) or [],
                allow_transcript=self.scope == _ctx.run_keyed_scope(primary),
            )
        self.recorder.injections([*self._tool_use_note(), *_ctx.turn_injections()])
        # The continuing flag is this forward's alone: anything it starts opens its own task.
        continue_token = _CONTINUING.set(False) if _CONTINUING.get() else None
        if continue_token is None:
            self.recorder.user_message(self.head)
        parent_token = _ctx.set_parent_span(expert_span)
        loop_token = _ACTIVE_LOOP.set(self)
        try:
            steps = range(self.max_iters) if self.max_iters > 0 else itertools.count()
            for step in steps:
                self.step = step
                self.span = uuid.uuid4().hex[:16]
                step_token = _ctx.set_parent_span(self.span)
                try:
                    done = self._one_step()
                finally:
                    _ctx.reset(step_token)
                if done is not None:
                    return done
            return self._stop("max_iters")
        except ClioError as exc:
            self.recorder.failed(exc, self.step, self.span)
            raise
        finally:
            _ACTIVE_LOOP.reset(loop_token)
            _ctx.reset(parent_token)
            if continue_token is not None:
                _CONTINUING.reset(continue_token)

    def _tool_use_note(self) -> list[tuple[str, str]]:
        """Tell an agent with tools, once, that one step may call several at once."""
        return [("tool_use", TOOL_USE_NOTE)] if set(self.agent.tools) - {"submit"} else []

    def _arrivals(self, *, human_only: bool = False) -> bool:
        """Inspect Core-pending arrivals; only the next input commit delivers them."""
        from clio_agent.gact import context

        owner = getattr(getattr(context.active_app(), "state", None), "conversations", None)
        if owner is None or context.current().react_run >= 0:
            return False
        return any(
            not human_only or item.kind in {"human", "composer"}
            for item in owner.mail.stage(owner.caller(self.session)).items
        )

    def _one_step(self) -> dspy.Prediction | None:
        from clio_agent.gact import context as _ctx  # noqa: PLC0415
        from clio_agent.gact.compaction import maybe_autocompact  # noqa: PLC0415

        _raise_if_cancelled()
        self._arrivals()
        maybe_autocompact(self.autocompact)
        attempt = 0

        def request() -> Request:
            nonlocal attempt
            self.recorder.commit_input(self.step, attempt=attempt)
            attempt += 1
            return Request(
                model=self.lm.model,
                system=self.system,
                messages=tuple(_place_tool_media(self._context(), self.tool_media)),
                tools=self.tools,
                config=self.config,
            )

        try:
            response = recover_stream(self.lm, request, _call_lm, _raise_if_cancelled)
        except (ContextWindowExceededError, ContextLengthError):
            return self._stop("context_window_exceeded")
        if response.finish_reason == "length":
            # A cut-off reply is not an answer or a complete call: a typed turn failure.
            from clio_agent.lm.policy import LMOutputTruncatedError  # noqa: PLC0415

            raise LMOutputTruncatedError(self.lm.model)
        thinking, text, origin = stream.reply_thinking_and_text(response)
        calls = [p for p in response.message.parts if isinstance(p, ToolCallPart)]
        if not calls:
            self._record(text, thinking, calls, {}, origin)
            if (self.max_iters <= 0 or self.step + 1 < self.max_iters) and self._arrivals():
                # Feedback accepted during this model call must be considered
                # before closing the turn, even if that call chose no tools.
                return None
            return self._end({"answer": text}, "direct_response", self.step + 1)
        if self._arrivals(human_only=True):
            # Already streamed prose precedes the newly delivered human message.
            # These calls were proposals, not executions: replan before opening
            # tool spans or recording calls that never ran.
            if text or thinking:
                self._record(text, thinking, [], {}, origin)
            _log.info("Tool proposal superseded by steer at step=%s", self.step)
            return None
        thought_token = _ctx.set_step_thought(text, "".join(t.text for t in thinking))
        try:
            self.recorder.step_open(self.step, self.span, text, calls)
            _raise_if_cancelled()
            outcomes = self._execute(calls)
        finally:
            _ctx.reset(thought_token)
        results = {c.id: (o.value, o.is_error) for c, o in zip(calls, outcomes, strict=True)}
        self._record(text, thinking, calls, results, origin)
        for outcome in outcomes:
            if outcome.escalate is not None:
                raise outcome.escalate
        return self._finish_step(calls, outcomes)

    def _finish_step(
        self, calls: list[ToolCallPart], outcomes: list[_CallOutcome]
    ) -> dspy.Prediction | None:
        final = self._submitted(calls, outcomes)
        if self.variant_outcome is not None:
            answer = self.variant_outcome.settle()
            if answer is None:  # the drafts went to the user: the turn yields
                return self._prediction({}, "draft_alternatives_yield")
            self.recorder.completed({"answer": answer}, self.step + 1)
            return self._prediction({"answer": answer}, "variant_selected")
        if final is not None:
            if (self.max_iters <= 0 or self.step + 1 < self.max_iters) and self._arrivals():
                return None
            self.recorder.completed(final, self.step + 1)
            return self._prediction(final, "submit")
        _raise_if_cancelled()
        return None

    def _context(self) -> list[Message]:
        return self.recorder.read_steps()

    def _execute(self, calls: list[ToolCallPart]) -> list[_CallOutcome]:
        """Run the step's calls concurrently; outcomes in call order."""
        if len(calls) == 1:
            return [self._call(calls[0])]
        with ThreadPoolExecutor(max_workers=len(calls), thread_name_prefix="clio-tool") as pool:
            futures = [pool.submit(contextvars.copy_context().run, self._call, c) for c in calls]
            return [f.result() for f in futures]

    def _call(self, call: ToolCallPart) -> _CallOutcome:
        if call.name == INVALID_TOOL_CALL:
            return _CallOutcome(
                "Your tool_calls block could not be read: "
                f"{call.input.get('error')}. Send the calls again as one valid block.",
                True,
            )
        tool = self.agent.tools.get(call.name)
        if tool is None:
            return _CallOutcome(f"Unknown tool: {call.name}", True)
        with injections.collect() as told:
            outcome = self._run_tool(call, tool)
        # What the harness told the agent about this call is shown to the user too.
        for source, text in told:
            emit_injection(source, text, call_id=call.id, agent_id=self.recorder.expert_id)
        return outcome

    def _run_tool(self, call: ToolCallPart, tool: dspy.Tool) -> _CallOutcome:
        from clio_agent.gact.runtime.globals import _TurnCancelled  # noqa: PLC0415
        from clio_agent.tools.file_policy import FilePolicyError  # noqa: PLC0415
        from clio_agent.tools.mcp_errors import typed_mcp_protocol_error  # noqa: PLC0415

        try:
            if inspect.iscoroutinefunction(getattr(tool, "func", None)):
                # The worker thread has no running loop; the task copies this context.
                return _CallOutcome(asyncio.run(tool.acall(**call.input)), False)
            return _CallOutcome(tool(**call.input), False)
        except Exception as err:  # noqa: BLE001 - a tool error is the model's observation
            refusal = err if isinstance(err, MCPProtocolError) else typed_mcp_protocol_error(err)
            escalate = (
                refusal
                if refusal is not None
                else (err if isinstance(err, _TurnCancelled) else None)
            )
            if isinstance(err, FilePolicyError):
                # An expected, typed refusal: the model needs the reason, not our stack.
                detail = f"{type(err).__name__}: {err}"
            else:
                detail = _fmt_exc(err)
            return _CallOutcome(f"Execution error in {call.name}: {detail}", True, escalate)

    def _submitted(
        self, calls: list[ToolCallPart], outcomes: list[_CallOutcome]
    ) -> dict[str, Any] | None:
        """The typed final outputs of a successful ``submit`` in this step, audited."""
        final: dict[str, Any] | None = None
        agent_id = active_react_scope_safe()
        for call, outcome in zip(calls, outcomes, strict=True):
            if call.name != "submit":
                continue
            if outcome.is_error:
                record_submit_audit(
                    REACT_SUBMIT_INVALID_OUTPUT,
                    agent_id=agent_id,
                    field="submit",
                    text=str(outcome.value),
                    suppressed=False,
                )
            elif isinstance(outcome.value, dict):
                final = outcome.value
        for field, value in (final or {}).items():
            record_submit_audit(
                REACT_SUBMIT_FIELD_SUPPRESSED,
                agent_id=agent_id,
                field=field,
                text=value if isinstance(value, str) else _json_text(value),
                suppressed=True,
            )
        return final

    def _record(
        self,
        text: str,
        thinking: list[ThinkingPart],
        calls: list[ToolCallPart],
        results: dict[str, tuple[Any, bool]],
        origin: tuple[str, bool] = ("", True),
    ) -> None:
        self.recorder.step_done(
            self.step,
            self.span,
            text=text,
            thinking=thinking,
            calls=calls,
            results=results,
            thinking_source=origin[0],
            thinking_complete=origin[1],
        )

    def _stop(self, reason: str) -> dspy.Prediction:
        steps = self.max_iters if reason == "max_iters" and self.max_iters > 0 else self.step + 1
        return self._end({}, reason, steps)

    def _end(self, outputs: dict[str, Any], reason: str, steps: int) -> dspy.Prediction:
        """Close the loop; after a long one, DSPy's extract fills the missing outputs."""
        missing = extract.missing_outputs(self.agent.signature, outputs, reason, steps)
        if missing:
            _raise_if_cancelled()
            try:
                outputs = {
                    **outputs,
                    **extract.extract(
                        self.agent.signature, self.inputs, self._context(), missing, self.lm
                    ),
                }
            except AdapterParseError:
                # The answer has already streamed into the conversation. A
                # malformed optional extract must not turn that answer and its
                # rendered surfaces into a failed turn. With no answer (for
                # example max_iters), the extract remains required.
                if reason != "direct_response" or not outputs.get("answer"):
                    raise
                _log.warning(
                    "Optional agent output extraction failed; retaining the direct answer",
                    exc_info=True,
                )
                missing = []
        self.recorder.completed(outputs, steps, extracted=missing)
        return self._prediction(outputs, reason)

    def _prediction(self, outputs: dict[str, Any], reason: str) -> dspy.Prediction:
        # The agent's messages are what clio-core holds for its scope: one source.
        prediction = dspy.Prediction(
            **outputs,
            messages=self._context(),
            termination_reason=reason,
            assignment_disposition=getattr(outputs, "assignment_disposition", None),
        )
        from clio_agent.arc.task_types import ContextBoundary

        view = self.recorder.arc.context_view(self.recorder.session, self.recorder.scope)
        prediction.clio_context_boundary = ContextBoundary(
            session_id=self.recorder.session,
            scope=self.recorder.scope,
            logical_time=max((row.logical_time for row in view.segments), default=0),
        )
        return prediction


# --------------------------------------------------------------------------- #
# helpers                                                                     #
# --------------------------------------------------------------------------- #
def _raise_if_cancelled() -> None:
    """Typed cooperative cancellation at a loop boundary."""
    from clio_agent.agent import cancellation_requested  # noqa: PLC0415

    if cancellation_requested():
        raise _turn_cancelled()


def _turn_cancelled() -> BaseException:
    """The typed cooperative cancellation of the active session's turn."""
    from clio_agent.gact import context as _ctx  # noqa: PLC0415
    from clio_agent.gact.runtime.globals import (  # noqa: PLC0415
        _cancelled_error_info,
        _TurnCancelled,
    )

    return _TurnCancelled(
        _cancelled_error_info(_ctx.active_session_id(), execution_cancellation="cooperative")
    )


def _fmt_exc(err: BaseException, *, limit: int = 5) -> str:
    return (
        "\n"
        + "".join(
            traceback.format_exception(type(err), err, err.__traceback__, limit=limit)
        ).strip()
    )


def _json_text(value: Any) -> str:
    import json  # noqa: PLC0415

    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):
        return str(value)
