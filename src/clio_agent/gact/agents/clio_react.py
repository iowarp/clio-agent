"""``ClioReAct`` -- clio's own ReAct loop, a ``dspy.Module`` on public DSPy API.

clio owns the loop (it used to run a modified copy of ``dspy.ReActV2.forward``
behind hash pins on upstream code it never ran). DSPy still provides the
signature, the adapter, the LM layer and module composition (``BestOfN`` /
``Refine`` wrap this module like any other). The wire matches stock ``ReActV2``
(same react signature and instructions, the reserved ``submit`` tool, one
``dspy.History`` event per step), which the differential test pins.

Per step:

1. **boundary** -- cancellation is checked (a typed ``_TurnCancelled``), then the
   proactive compaction trigger runs;
2. **context** -- the task inputs (+ tools) are ONE static head event; the steps
   after it come from the ARC live plane folded by step
   (:func:`~clio_agent.gact.agents.clio_react_record.fold_steps`), or from the
   loop's own History when there is no ARC scope. A plane read failure is a typed
   turn failure; there is no fallback between the two;
3. **predict** once -- no current inputs, so every call is the previous call plus
   the new step beneath one byte-static closing instruction;
4. **tools** -- a step's calls run concurrently, one worker each in a copy of the
   step's context; results keep call order. A terminal MCP protocol refusal or a
   cancellation raised by a tool escalates after the step is recorded;
5. **end** -- no tool call is the answer (``direct_response``); ``submit`` returns
   its typed outputs; ``ask_user`` / ``plan_exit`` yield; ``max_iters`` (``<= 0`` =
   unlimited), ``parse_error`` and ``context_window_exceeded`` stop. Nothing calls
   the model after the loop (``react-loop-completion-2026-09.md``).
"""

from __future__ import annotations

import asyncio
import contextvars
import inspect
import itertools
import traceback
import uuid
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, get_args

import dspy
import pydantic
from dspy.adapters import ToolCallResults
from dspy.utils.exceptions import AdapterParseError, ContextWindowExceededError

from clio_agent.errors import ClioError, MCPProtocolError
from clio_agent.gact.agents import clio_react_record as record
from clio_agent.gact.agents.clio_react_submit import (
    active_react_scope_safe,
    record_submit_audit,
)

__all__ = ["ClioReAct"]

#: A submit output field's value flowed to the return contract (the final Prediction),
#: not to a visible text lane.
REACT_SUBMIT_FIELD_SUPPRESSED = "react_submit_field_suppressed"
#: The ``submit`` tool rejected a typed/missing final-output arg: the value did not flow.
REACT_SUBMIT_INVALID_OUTPUT = "react_submit_invalid_output"


class ClioReAct(dspy.Module):
    """clio's ReAct loop (see the module docstring)."""

    def __init__(self, signature: Any, tools: Iterable[Any], max_iters: int = 20) -> None:
        super().__init__()
        self.signature = dspy.ensure_signature(signature)
        self.max_iters = max_iters
        user_tools = [t if isinstance(t, dspy.Tool) else dspy.Tool(t) for t in tools]
        self.tools: dict[str, dspy.Tool] = {t.name: t for t in user_tools}
        if "submit" in self.tools:
            raise ValueError("`submit` is reserved as the final-output tool.")
        self.tools["submit"] = _submit_tool(self.signature)
        self.react = dspy.Predict(_react_signature(self.signature, self.tools))

    def forward(self, **input_args: Any) -> dspy.Prediction:
        """Run the loop for one expert turn (see the module docstring)."""
        from clio_agent.providers.stateful_common import stateful_scope  # noqa: PLC0415

        # A fresh stateful scope per forward: the claude_code transport shares one SDK
        # session across this forward's calls; the Codex SDK keys its thread on the
        # conversation and uses the token only to route ARC-op resets.
        with stateful_scope():
            return _Loop(self, input_args).run()


def _submit_tool(signature: Any) -> dspy.Tool:
    """The reserved ``submit`` tool: its args are the signature's outputs.

    An output field declared with a Pydantic default may be omitted (the default is
    filled -- the author declared it droppable); every other field is required.
    """
    output_fields = signature.output_fields
    names = list(output_fields)
    defaults = {
        name: default
        for name, field in output_fields.items()
        for has_default, default in [_declared_default(field)]
        if has_default
    }

    def submit(**kwargs: Any) -> dict[str, Any]:
        missing = [n for n in names if n not in kwargs and n not in defaults]
        if missing:
            raise ValueError(f"Missing required final output field(s): {', '.join(missing)}")
        return {n: kwargs[n] if n in kwargs else defaults[n] for n in names}

    return dspy.Tool(
        submit,
        name="submit",
        desc="Submit the final outputs for the task.",
        args={name: _json_schema(field.annotation) for name, field in output_fields.items()},
        arg_types={name: field.annotation for name, field in output_fields.items()},
    )


def _react_signature(signature: Any, tools: dict[str, dspy.Tool]) -> Any:
    """Task inputs (optional: they ride the head event) + history + tools -> step."""
    fields: dict[str, Any] = {
        name: (
            _optional(field.annotation),
            dspy.InputField(desc=field.json_schema_extra.get("desc")),
        )
        for name, field in signature.input_fields.items()
    }
    fields["history"] = (dspy.History, dspy.InputField())
    fields["tools"] = (list[dspy.Tool] | None, dspy.InputField())
    fields["next_thought"] = (str, dspy.OutputField())
    fields["tool_calls"] = (dspy.ToolCalls, dspy.OutputField())
    inputs = ", ".join(f"`{name}`" for name in signature.input_fields)
    outputs = ", ".join(f"`{name}`" for name in signature.output_fields)
    instructions = "\n".join(
        [
            signature.instructions,
            f"You are an Agent. Use the supplied tools to produce {outputs} from {inputs}.",
            "Call tools when more information is needed.",
            f"When the final answer is ready, call `submit` with {outputs}.",
            f"The available tools are: {', '.join(f'`{name}`' for name in tools)}.",
        ]
    ).strip()
    return dspy.Signature(fields, instructions)


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
        prior = input_args.pop("history", None)
        self.inputs = {n: input_args[n] for n in agent.signature.input_fields if n in input_args}
        self.head = {**self.inputs, "tools": list(agent.tools.values())}
        self.events: list[dict[str, Any]] = list(getattr(prior, "messages", None) or [])
        self.arc, self.session, self.scope = record.arc_scope()
        self.recorder = record.StepRecorder(
            self.arc,
            self.session,
            self.scope,
            expert_id=str(getattr(agent, "_clio_expert_id", "") or ""),
        )
        self.step = -1
        self.span = ""

    def run(self) -> dspy.Prediction:
        from clio_agent.gact import context as _ctx  # noqa: PLC0415

        record.reset_working_set(self.arc, self.session, self.scope)
        expert_span = uuid.uuid4().hex[:16]
        self.recorder.started(expert_span, self.inputs)
        parent_token = _ctx.set_parent_span(expert_span)
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
            _ctx.reset(parent_token)

    def _one_step(self) -> dspy.Prediction | None:
        from clio_agent.gact import context as _ctx  # noqa: PLC0415
        from clio_agent.gact.compaction import maybe_autocompact  # noqa: PLC0415
        from clio_agent.gact.runtime.globals import _active_lm_last_reasoning  # noqa: PLC0415

        _raise_if_cancelled()
        maybe_autocompact()
        try:
            pred = self.agent.react(history=self._context())
            calls = _tool_calls(getattr(pred, "tool_calls", None), self.step)
        except (AdapterParseError, ValueError):
            return self._stop("parse_error")
        except ContextWindowExceededError:
            return self._stop("context_window_exceeded")
        thought = str(getattr(pred, "next_thought", "") or "")
        reasoning = _active_lm_last_reasoning()
        if not calls.tool_calls:
            self._record(thought, reasoning, calls, ToolCallResults(tool_call_results=[]))
            outputs = {"answer": thought}
            self.recorder.completed(outputs, self.step + 1)
            return self._prediction(outputs, "direct_response")
        thought_token = _ctx.set_step_thought(thought, reasoning)
        try:
            self.recorder.step_open(self.step, self.span, thought, calls)
            _raise_if_cancelled()
            outcomes = self._execute(calls)
        finally:
            _ctx.reset(thought_token)
        results = ToolCallResults.from_tool_calls_and_values(
            calls, [o.value for o in outcomes], [o.is_error for o in outcomes]
        )
        self._record(thought, reasoning, calls, results)
        for outcome in outcomes:
            if outcome.escalate is not None:
                raise outcome.escalate
        return self._finish_step(calls, outcomes)

    def _finish_step(
        self, calls: dspy.ToolCalls, outcomes: list[_CallOutcome]
    ) -> dspy.Prediction | None:
        final = self._submitted(calls, outcomes)
        if yield_name := record.pending_turn_yield(calls):
            return self._prediction({}, f"{yield_name}_yield")
        if final is not None:
            self.recorder.completed(final, self.step + 1)
            return self._prediction(final, "submit")
        _raise_if_cancelled()
        return None

    def _context(self) -> dspy.History:
        steps = (
            record.read_steps(self.arc, self.session, self.scope)
            if self.arc is not None
            else self.events
        )
        return dspy.History(messages=[self.head, *steps])

    def _execute(self, calls: dspy.ToolCalls) -> list[_CallOutcome]:
        """Run the step's calls concurrently; outcomes in call order."""
        items = list(calls.tool_calls)
        if len(items) == 1:
            return [self._call(items[0])]
        with ThreadPoolExecutor(max_workers=len(items), thread_name_prefix="clio-tool") as pool:
            futures = [pool.submit(contextvars.copy_context().run, self._call, c) for c in items]
            return [f.result() for f in futures]

    def _call(self, call: dspy.ToolCalls.ToolCall) -> _CallOutcome:
        from clio_agent.gact.runtime.globals import _TurnCancelled  # noqa: PLC0415
        from clio_agent.tools.mcp_errors import typed_mcp_protocol_error  # noqa: PLC0415

        tool = self.agent.tools.get(call.name)
        if tool is None:
            return _CallOutcome(f"Unknown tool: {call.name}", True)
        try:
            if inspect.iscoroutinefunction(getattr(tool, "func", None)):
                # The worker thread has no running loop; the task copies this context.
                return _CallOutcome(asyncio.run(tool.acall(**(call.args or {}))), False)
            return _CallOutcome(tool(**(call.args or {})), False)
        except Exception as err:  # noqa: BLE001 - a tool error is the model's observation
            refusal = err if isinstance(err, MCPProtocolError) else typed_mcp_protocol_error(err)
            escalate = (
                refusal
                if refusal is not None
                else (err if isinstance(err, _TurnCancelled) else None)
            )
            return _CallOutcome(f"Execution error in {call.name}: {_fmt_exc(err)}", True, escalate)

    def _submitted(
        self, calls: dspy.ToolCalls, outcomes: list[_CallOutcome]
    ) -> dict[str, Any] | None:
        """The typed final outputs of a successful ``submit`` in this step, audited."""
        final: dict[str, Any] | None = None
        agent_id = active_react_scope_safe()
        for call, outcome in zip(calls.tool_calls, outcomes, strict=True):
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
        self, thought: str, reasoning: str, calls: dspy.ToolCalls, results: ToolCallResults
    ) -> None:
        event: dict[str, Any] = {"next_thought": thought}
        if calls.tool_calls:
            event["tool_calls"] = (
                calls.model_copy(update={"tool_call_results": results})
                if results.tool_call_results
                else calls
            )
        self.events.append(event)
        self.recorder.step_done(
            self.step, self.span, thought=thought, reasoning=reasoning, calls=calls, results=results
        )

    def _stop(self, reason: str) -> dspy.Prediction:
        steps = self.max_iters if reason == "max_iters" and self.max_iters > 0 else self.step + 1
        self.recorder.completed({}, steps)
        return self._prediction({}, reason)

    def _prediction(self, outputs: dict[str, Any], reason: str) -> dspy.Prediction:
        history = dspy.History(messages=[self.inputs, *self.events])
        return dspy.Prediction(**outputs, history=history, termination_reason=reason)


def _raise_if_cancelled() -> None:
    """Typed cooperative cancellation at a loop boundary."""
    from clio_agent.agent import cancellation_requested  # noqa: PLC0415
    from clio_agent.gact import context as _ctx  # noqa: PLC0415
    from clio_agent.gact.runtime.globals import (  # noqa: PLC0415
        _cancelled_error_info,
        _TurnCancelled,
    )

    if cancellation_requested():
        raise _TurnCancelled(
            _cancelled_error_info(_ctx.active_session_id(), execution_cancellation="cooperative")
        )


def _tool_calls(value: Any, step: int) -> dspy.ToolCalls:
    """Coerce the step's tool calls and give every call a stable id."""
    calls = (
        dspy.ToolCalls.model_validate(value) if value is not None else dspy.ToolCalls(tool_calls=[])
    )
    return dspy.ToolCalls(
        tool_calls=[
            c if c.id is not None else c.model_copy(update={"id": f"call_{step}_{i}"})
            for i, c in enumerate(calls.tool_calls)
        ]
    )


def _declared_default(field: Any) -> tuple[bool, Any]:
    """``(True, default)`` for an output field declared with a Pydantic default."""
    from pydantic_core import PydanticUndefined  # noqa: PLC0415

    default = getattr(field, "default", PydanticUndefined)
    if default is not PydanticUndefined:
        return True, default
    factory: Callable[[], Any] | None = getattr(field, "default_factory", None)
    if factory is not None:
        return True, factory()
    return False, None


def _json_schema(annotation: Any) -> dict[str, Any]:
    try:
        return pydantic.TypeAdapter(annotation).json_schema()
    except (pydantic.PydanticSchemaGenerationError, TypeError, ValueError):
        return {"type": "string"}


def _optional(annotation: Any) -> Any:
    if type(None) in get_args(annotation):
        return annotation
    try:
        return annotation | None
    except TypeError:
        return annotation


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
