"""What :class:`~clio_agent.gact.agents.clio_react.ClioReAct` records and reads back.

One step of the loop is recorded three ways, all span-correlated:

* the ARC live plane -- a ``thought`` segment (the step's visible text and its thinking
  parts with their provider continuation state, byte-exact), then a ``tool_call`` +
  ``observation`` segment per call (call id, name, arguments; result text, error flag),
  after a pre-execution ``step_open`` breadcrumb;
* the semantic highway -- ``react.step.completed`` per step (every call of the step)
  and the expert lifecycle (``expert.lifecycle.started`` / ``expert.extract.completed``
  / ``expert.lifecycle.failed``);
* the step's context for the tool observer (step thought + parent span).

Each forward first records its user message (a ``user`` segment: the question and the
user's own attachments). Nothing is wiped between forwards: the scope's plane holds the
whole conversation, and only a recorded op (compaction, delete) removes content.

The loop's context is read back from the same plane as typed ``dspy.lm15`` messages
(:func:`fold_steps`): each user message as itself, per step one assistant message
(thinking, text, tool calls) and one tool message (the results), so a step with
concurrent calls renders as the one step it was, and a later turn sees the earlier
turns as the messages they were. A read failure is a typed :class:`ContextReadError`
-- there is no fallback.
"""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Mapping, Sequence
from typing import Any

from dspy.lm15 import (
    ContinuationState,
    DocumentPart,
    ImagePart,
    Message,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolResultPart,
)

from clio_agent.errors import ClioError
from clio_agent.gact.injection_parts import emit_injection

logger = logging.getLogger(__name__)

# Tools whose successful call is itself the terminal outcome of the current model
# turn. Their authoritative payload stays in session metadata until the post-forward
# pause seam mints the user-facing interaction.
TURN_YIELD_METADATA: dict[str, str] = {
    "ask_user": "pending_ask_user",
    "plan_exit": "pending_plan_exit",
}

_FOLDED_KINDS = frozenset({"user", "thought", "tool_call", "observation", "summary"})


class ContextReadError(ClioError):
    """The loop could not read its context from the ARC live plane (typed turn failure)."""

    reason = "arc_context_read_failed"

    def __init__(self, scope: str, cause: BaseException) -> None:
        super().__init__(
            f"could not read the agent context for scope {scope!r}: {cause}",
            error_type=self.reason,
            details={"scope": scope, "cause": type(cause).__name__},
        )


class ContextWriteError(ClioError):
    """The loop could not record a message on the ARC live plane (typed turn failure).

    The plane is the conversation: a lost write would leave the model a context that
    silently lacks what just happened, so it fails the turn instead.
    """

    reason = "arc_context_write_failed"

    def __init__(self, scope: str, kind: str, cause: BaseException) -> None:
        super().__init__(
            f"could not record the agent's {kind} for scope {scope!r}: {cause}",
            error_type=self.reason,
            details={"scope": scope, "kind": kind, "cause": type(cause).__name__},
        )


def arc_scope() -> tuple[Any, str, str]:
    """Resolve ``(ARCMemory, session_id, scope)`` for the live plane, or ``(None, '', '')``.

    ``arc`` is ``None`` whenever there is no app, no ARC, or no react scope (a bare
    unit call, the CLI) -- then the loop's own step list is its context. The in-process
    variant try index is folded into the ARC key only (#953).
    """
    from clio_agent.gact import context as _ctx  # noqa: PLC0415

    app = _ctx.active_app()
    scope = _ctx.run_keyed_scope(_ctx.active_react_scope())
    session = _ctx.active_react_session()
    arc = getattr(getattr(app, "state", None), "arc", None) if (app is not None and scope) else None
    if arc is None:
        return None, "", ""
    return arc, session, scope


def read_steps(arc: Any, session: str, scope: str) -> list[Message]:
    """Fold the scope's live plane into typed messages; typed failure on a read error."""
    try:
        segments = arc.render_segments(session, scope)
    except Exception as exc:  # noqa: BLE001 - re-raised typed, never swallowed
        raise ContextReadError(scope, exc) from exc
    return fold_steps(segments)


# --------------------------------------------------------------------------- #
# The step codec: typed parts <-> plane content (CLIO-owned, JSON)            #
# --------------------------------------------------------------------------- #
def thinking_to_record(part: ThinkingPart) -> dict[str, Any]:
    """A thinking part as plane content, provider continuation state kept byte-exact."""
    return {
        "text": part.text,
        "continuation": [
            {"provider": c.provider, "kind": c.kind, "data": c.data} for c in part.continuation
        ],
    }


def thinking_from_record(record: Mapping[str, Any]) -> ThinkingPart:
    """Rebuild a thinking part from :func:`thinking_to_record`'s content."""
    return ThinkingPart(
        text=str(record.get("text") or ""),
        continuation=tuple(
            ContinuationState(provider=c["provider"], kind=c["kind"], data=c.get("data") or {})
            for c in record.get("continuation") or []
            if isinstance(c, Mapping)
        ),
    )


def user_to_record(message: Message) -> dict[str, Any]:
    """A user message as plane content: its text and its media, byte-exact."""
    text = "".join(p.text for p in message.parts if isinstance(p, TextPart))
    media = [
        {
            "type": "image" if isinstance(p, ImagePart) else "document",
            "media_type": p.media_type,
            "data": p.data,
        }
        for p in message.parts
        if isinstance(p, ImagePart | DocumentPart)
    ]
    return {"text": text, "media": media} if media else {"text": text}


def user_from_record(record: Mapping[str, Any]) -> Message:
    """Rebuild a user message from :func:`user_to_record`'s content.

    A CLIO addition (``actor: algorithm``) is headed with its source, so the model
    can tell it from what the user wrote.
    """
    text = _text(record.get("text"))
    if record.get("actor") == "algorithm":  # CLIO's addition; a steer is the user's own
        text = f"[clio: {_text(record.get('source'))}]\n{text}"
    parts: list[Any] = [TextPart(text=text)]
    for item in record.get("media") or []:
        if not isinstance(item, Mapping):
            continue
        cls = ImagePart if item.get("type") == "image" else DocumentPart
        parts.append(cls(data=str(item.get("data") or ""), media_type=str(item["media_type"])))
    return Message(role="user", parts=tuple(parts))


def fold_steps(segments: Sequence[Any]) -> list[Message]:
    """Group ordered live segments into typed messages, one assistant + tool pair per step.

    A ``user`` segment is the user message it recorded. A ``thought`` opens a step; its
    ``tool_call`` / ``observation`` segments attach to it (results matched by call id, by
    order for a segment written without one); a call with no step open starts its own. A
    ``summary`` -- or an observation with no open step -- becomes a user message carrying
    the text, so compacted content still reaches the model.
    """
    messages: list[Message] = []
    step: _StepFold | None = None
    for seg in segments:
        kind = getattr(seg, "kind", "")
        if kind not in _FOLDED_KINDS:
            continue
        content = getattr(seg, "content", None) or {}
        if kind == "thought":
            if step is not None:
                messages.extend(step.messages())
            step = _StepFold(content)
        elif kind == "tool_call":
            if step is None:  # a call with no thought before it (an edit put it there)
                step = _StepFold({})
            step.add_call(content)
        elif kind == "observation" and step is not None and step.expects_result():
            step.add_result(content)
        elif kind == "user":
            if step is not None:
                messages.extend(step.messages())
                step = None
            messages.append(user_from_record(content))
        else:
            if step is not None:
                messages.extend(step.messages())
                step = None
            messages.append(Message.user(f"[earlier context]\n{_text(content.get('text'))}"))
    if step is not None:
        messages.extend(step.messages())
    return messages


class _StepFold:
    """Accumulates one step's thinking, text, calls and results while folding."""

    def __init__(self, content: Mapping[str, Any]) -> None:
        self.text = _text(content.get("text"))
        self.thinking = [
            thinking_from_record(t) for t in content.get("thinking") or [] if isinstance(t, Mapping)
        ]
        self.calls: list[ToolCallPart] = []
        self.results: dict[str, ToolResultPart] = {}

    def add_call(self, content: Mapping[str, Any]) -> None:
        args = content.get("args")
        self.calls.append(
            ToolCallPart(
                id=str(content.get("id") or f"call_{len(self.calls)}"),
                name=_text(content.get("name")) or "unknown",
                input=dict(args) if isinstance(args, Mapping) else {},
            )
        )

    def expects_result(self) -> bool:
        return len(self.results) < len(self.calls)

    def add_result(self, content: Mapping[str, Any]) -> None:
        call_id = str(content.get("call_id") or "")
        if call_id not in {c.id for c in self.calls} or call_id in self.results:
            call_id = next(c.id for c in self.calls if c.id not in self.results)
        name = next(c.name for c in self.calls if c.id == call_id)
        self.results[call_id] = result_part(
            call_id, name, content.get("text", ""), bool(content.get("is_error"))
        )

    def messages(self) -> list[Message]:
        parts: list[Any] = [*self.thinking]
        if self.text or not (self.thinking or self.calls):
            parts.append(TextPart(text=self.text))
        parts.extend(self.calls)
        out = [Message(role="assistant", parts=tuple(parts))]
        answered = [self.results[c.id] for c in self.calls if c.id in self.results]
        if answered:
            out.append(Message(role="tool", parts=tuple(answered)))
        return out


def result_part(call_id: str, name: str, value: Any, is_error: bool) -> ToolResultPart:
    """A tool's result as the provider-native part (images/PDFs as media, else text).

    Media the history can no longer show (its snapshot is gone) becomes a note the
    agent reads -- one old image never fails the whole conversation.
    """
    try:
        media = _media(value)
    except ValueError as exc:  # ViewImageError / ViewPdfError / ViewedMediaUnavailable
        logger.warning("tool media unavailable reason=%s call=%s", type(exc).__name__, call_id)
        note = (
            f"[clio: media_unavailable] The media this call returned can no longer be shown: {exc}"
        )
        return ToolResultPart(
            id=call_id, content=(TextPart(text=note),), name=name, is_error=is_error
        )
    content: tuple[Any, ...] = (
        (media,) if media is not None else (TextPart(text=_observation_text(value)),)
    )
    return ToolResultPart(id=call_id, content=content, name=name, is_error=is_error)


def _media(value: Any) -> ImagePart | DocumentPart | None:
    """Hydrate a view_image / view_pdf result descriptor into a native media part."""
    from clio_agent.gact.view_image_tool import (
        _hydrate_descriptor as image_hydrate,  # noqa: PLC0415
    )
    from clio_agent.gact.view_image_tool import _is_descriptor as is_image  # noqa: PLC0415
    from clio_agent.gact.view_pdf_tool import _hydrate_descriptor as pdf_hydrate  # noqa: PLC0415
    from clio_agent.gact.view_pdf_tool import _is_descriptor as is_pdf  # noqa: PLC0415

    if not isinstance(value, Mapping):
        return None
    if is_image(value):
        media, _size = image_hydrate(value)
        media_type, data = _split_data_url(str(getattr(media, "url", "")))
        return ImagePart(data=data, media_type=media_type)
    if is_pdf(value):
        media, _size = pdf_hydrate(value)
        media_type, data = _split_data_url(
            str(getattr(media, "url", "") or getattr(media, "file_data", ""))
        )
        return DocumentPart(data=data, media_type=media_type or "application/pdf")
    return None


def _split_data_url(url: str) -> tuple[str, str]:
    header, _, data = url.partition(",")
    media_type = header.removeprefix("data:").split(";", 1)[0]
    if not data:
        raise ValueError("an attachment descriptor did not hydrate to inline data")
    base64.b64decode(data, validate=True)  # typed failure on a corrupt payload
    return media_type, data


def _observation_text(value: Any) -> str:
    if isinstance(value, str):
        return value or "(empty result)"
    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):
        return str(value)


# --------------------------------------------------------------------------- #
# Recording                                                                   #
# --------------------------------------------------------------------------- #
class StepRecorder:
    """Writes one expert forward's steps to the ARC live plane and the highway."""

    def __init__(self, arc: Any, session: str, scope: str, *, expert_id: str) -> None:
        from clio_agent.gact.runtime.globals import _active_semantic_turn_id  # noqa: PLC0415

        self.arc = arc
        self.session = session
        self.scope = scope
        self.expert_id = expert_id
        self.turn_id = _active_semantic_turn_id()
        self.expert_span_id = ""

    def started(self, expert_span_id: str, inputs: Mapping[str, Any]) -> None:
        """Open the expert lifecycle on the highway."""
        from clio_agent.gact.runtime.globals import _emit_expert_lifecycle_event  # noqa: PLC0415
        from clio_agent.tools.mcp_runtime import wire_value  # noqa: PLC0415

        self.expert_span_id = expert_span_id
        _emit_expert_lifecycle_event(
            "expert.lifecycle.started",
            expert_id=self.expert_id,
            expert_span_id=expert_span_id,
            status="running",
            payload={"input": wire_value(dict(inputs), mode="gact_runtime")},
        )

    def user_message(self, message: Message) -> None:
        """Record the forward's user message; the projection starts every turn with it."""
        self._write("user", user_to_record(message), 0, "")

    def carry_over(self, ledger: Sequence[Any]) -> None:
        """Seed a scope new to the conversation with its earlier turns, once.

        An agent with nothing recorded yet (the user switched the session's agent,
        or its store changed) joins a conversation that already has turns: the
        transcript's model context -- user and assistant text, the latest
        compaction summary -- is recorded as the messages it was, and the agent is
        told its tool details are not included. From then on the scope is
        append-only like any other.
        """
        if self.arc is None or self.arc.list_segments(
            self.session, self.scope, include_tombstoned=True
        ):
            return
        from clio_agent.gact.conversation_projection import (  # noqa: PLC0415
            model_context_messages,
        )

        rows = list(model_context_messages(list(ledger)))
        while rows and _field(rows[-1], "role") == "user":
            rows.pop()  # this turn's own message is recorded by the loop itself
        carried = 0
        for row in rows:
            for kind, content in _carried(row):
                self._write(kind, content, 0, "")
                carried += 1
        if carried:
            note = (
                f"The {carried} earlier messages of this conversation were carried over "
                "from its transcript (their tool calls and results are not included)."
            )
            self.injections([("earlier_turns", note)])

    def injections(self, injections: Sequence[tuple[str, str]]) -> None:
        """Record CLIO's additions for this turn, each once.

        An addition whose text equals the latest recorded one from the same source is
        still in the model's context, so it is not repeated (prefix reuse); a changed
        one is recorded again, as a new message.
        """
        if self.arc is None or not injections:
            return
        latest: dict[str, str] = {}
        for seg in self.arc.render_working_set(self.session, self.scope):
            content = getattr(seg, "content", None) or {}
            if getattr(seg, "kind", "") == "user" and content.get("actor") == "algorithm":
                latest[str(content.get("source"))] = str(content.get("text"))
        for source, text in injections:
            if text and latest.get(source) != text:
                record = {"text": text, "source": source, "actor": "algorithm"}
                self._write("user", record, 0, "")
                emit_injection(source, text, agent_id=self.expert_id)
                latest[source] = text

    def arrivals(self, arrivals: Sequence[tuple[str, str]], step: int) -> list[Message]:
        """Record what arrived mid-turn, in order; returns the messages it adds.

        A ``steer`` is the user's own message; anything else (a finished child's
        result) is a CLIO addition, headed with its source.
        """
        messages: list[Message] = []
        for source, text in arrivals:
            actor = "user" if source == "steer" else "algorithm"
            record = {"text": text, "source": source, "actor": actor}
            self._write("user", record, step, "")
            if actor == "algorithm":
                emit_injection(source, text, agent_id=self.expert_id)
            messages.append(user_from_record(record))
        return messages

    def step_open(self, step: int, span: str, text: str, calls: Sequence[ToolCallPart]) -> None:
        """The pre-execution breadcrumb: a crash mid-step still leaves the step's opening."""
        from clio_agent.arc.working_set_fold import emit_step_open  # noqa: PLC0415

        emit_step_open(
            self.arc,
            self.session,
            self.scope,
            {"thought": text, "tools": [c.name for c in calls]},
            step=step,
            turn_id=self.turn_id,
            expert_span_id=self.expert_span_id,
            run_span_id=span,
        )

    def step_done(
        self,
        step: int,
        span: str,
        *,
        text: str,
        thinking: Sequence[ThinkingPart],
        calls: Sequence[ToolCallPart],
        results: Mapping[str, tuple[Any, bool]],
    ) -> None:
        """Write the step's segments and put ``react.step.completed`` on the highway."""
        from clio_agent.gact.runtime.context_tokens import _arc_obs_value  # noqa: PLC0415
        from clio_agent.gact.runtime.globals import _emit_react_step_event  # noqa: PLC0415

        self._write(
            "thought",
            {"text": text, "thinking": [thinking_to_record(t) for t in thinking]},
            step,
            span,
        )
        rows: list[dict[str, Any]] = []
        for call in calls:
            value, is_error = results.get(call.id, ("", False))
            self._write(
                "tool_call",
                {"id": call.id, "name": call.name, "args": dict(call.input)},
                step,
                span,
            )
            self._write(
                "observation",
                {"call_id": call.id, "text": _arc_obs_value(value), "is_error": is_error},
                step,
                span,
            )
            rows.append(
                {
                    "id": call.id,
                    "name": call.name,
                    "args": dict(call.input),
                    "observation": value,
                    "is_error": is_error,
                }
            )
        _emit_react_step_event(
            expert_id=self.expert_id,
            expert_span_id=self.expert_span_id,
            step_span_id=span,
            step_index=step,
            thought=text,
            reasoning="".join(t.text for t in thinking),
            tool_calls=rows,
            is_finish=any(call.name == "submit" for call in calls),
        )

    def completed(self, outputs: Mapping[str, Any] | None, step_count: int) -> None:
        """Close the expert lifecycle with the final answer and structured outputs."""
        from clio_agent.gact.runtime.globals import _emit_expert_lifecycle_event  # noqa: PLC0415

        outputs = outputs or {}
        _emit_expert_lifecycle_event(
            "expert.extract.completed",
            expert_id=self.expert_id,
            expert_span_id=self.expert_span_id,
            status="completed",
            payload={
                "output": str(outputs.get("answer", "") or ""),
                "structured": {
                    k: v for k, v in outputs.items() if k != "answer" and v not in (None, "")
                },
                "step_count": step_count,
            },
        )

    def failed(self, exc: ClioError, step: int, span: str) -> None:
        """Close the lifecycle ``failed`` and record why on the plane (never raises)."""
        from clio_agent.gact.runtime.globals import _emit_expert_lifecycle_event  # noqa: PLC0415

        reason = str(getattr(exc, "reason", "") or type(exc).__name__)
        try:
            _emit_expert_lifecycle_event(
                "expert.lifecycle.failed",
                expert_id=self.expert_id,
                expert_span_id=self.expert_span_id,
                status="failed",
                payload={"reason": reason, "error": str(exc)},
            )
            if step >= 0:
                self._write(
                    "observation", {"text": f"[turn escalated] {reason}: {exc}"}, step, span
                )
        except Exception:  # noqa: BLE001 - cleanup must never mask the real error
            logger.warning(
                "clio_react escalation cleanup failed expert_id=%s reason=%s",
                self.expert_id,
                reason,
                exc_info=True,
            )

    def _write(self, kind: str, content: dict[str, Any], step: int, span: str) -> None:
        if self.arc is None:
            return
        try:
            self.arc.append_segment(
                self.session,
                self.scope,
                kind,
                content,
                step=step,
                token_count=_token_estimate(content),
                turn_id=self.turn_id,
                expert_span_id=self.expert_span_id,
                run_span_id=span,
            )
        except Exception as exc:  # noqa: BLE001 - re-raised typed, never swallowed
            raise ContextWriteError(self.scope, kind, exc) from exc


_MEDIA_TOKENS = 1_500  # a rough per-attachment share of the context window


def _token_estimate(content: Mapping[str, Any]) -> int:
    """A segment's rough token count: its text by length, each attachment a flat share."""
    media = content.get("media") or []
    text = {k: v for k, v in content.items() if k != "media"}
    return max(1, len(json.dumps(text, default=str)) // 4 + _MEDIA_TOKENS * len(media))


def _field(row: Any, name: str) -> Any:
    return row.get(name) if isinstance(row, Mapping) else getattr(row, name, None)


def _carried(row: Any) -> list[tuple[str, dict[str, Any]]]:
    """A transcript row as plane segments (text only; a checkpoint as its summary)."""
    role = _field(row, "role")
    texts: list[str] = []
    for part in _field(row, "parts") or []:
        kind = _field(part, "type")
        if kind == "compaction" and _field(part, "summary"):
            return [("summary", {"text": str(_field(part, "summary"))})]
        if kind == "text" and str(_field(part, "text") or "").strip():
            texts.append(str(_field(part, "text")))
    if not texts:
        return []
    text = "\n\n".join(texts)
    if role == "user":
        return [("user", {"text": text})]
    if role == "assistant":
        return [("thought", {"text": text, "thinking": []})]
    return []


def pending_turn_yield(calls: Sequence[ToolCallPart]) -> str:
    """The successful turn-ending tool in this step (``ask_user`` / ``plan_exit``), if any.

    The tool name alone is not enough: a rejected ``plan_exit`` or malformed
    ``ask_user`` stays an ordinary tool error. The un-surfaced session-metadata record
    the tool wrote is the proof it completed and the turn should yield to the user.
    """
    from clio_agent.gact import context as _ctx  # noqa: PLC0415

    names = {call.name for call in calls}
    if not names.intersection(TURN_YIELD_METADATA):
        return ""
    app = _ctx.active_app()
    session_id = _ctx.active_session_id()
    sessions = getattr(getattr(app, "state", None), "sessions", None)
    session = sessions.get(session_id) if sessions is not None and session_id else None
    metadata = getattr(session, "metadata", None)
    if not isinstance(metadata, Mapping):
        return ""
    for tool_name, key in TURN_YIELD_METADATA.items():
        pending = metadata.get(key)
        if (
            tool_name in names
            and isinstance(pending, Mapping)
            and pending
            and not pending.get("surfaced")
        ):
            return tool_name
    return ""


def _text(value: Any) -> str:
    return value if isinstance(value, str) else str(value if value is not None else "")
