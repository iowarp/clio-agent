"""The snapshot (diffusion) side of one ``ClioReAct`` LM call -- the one owner.

A diffusion model asked to show its denoising (``diffusing: true``) streams every
step as the FULL current answer, not an appended delta. The append-only stream path
would concatenate those snapshots into nonsense, so a call whose request asked for
diffusion (:func:`clio_agent.lm.diffusion.diffusion_requested`) is read here instead:

* each step's text is shown through the replace lane
  (:func:`~clio_agent.runtime.lm_activity.note_lm_answer_canvas`) and only the latest
  canvas is kept -- never concatenated;
* provider reasoning keeps the append lane, exactly as in an ordinary stream;
* when the stream ends, the last canvas becomes the call's answer: it replaces the
  typed ``Response`` text (so tool parsing, ARC and the session store see only it)
  and reaches the transcript once through the ordinary answer lane;
* the stream's shape is checked, never assumed. A ``diffusing`` request answered with
  appended deltas, or with tool calls streamed across steps, records the typed
  ``diffusion_stream_shape_mismatch`` note and keeps what DSPy assembled.

The step count and step timings go to the stream audit trace.
"""

from __future__ import annotations

import dataclasses
import difflib
import logging
import time
from typing import Any

from dspy.lm15 import Response, TextPart

from clio_agent.gact.reasoning_extract import (
    PROVIDER_FIELD,
    THINKING,
    InlineThinkSplitter,
    Piece,
    chunk_delta,
    field_reasoning,
    field_text,
)

__all__ = ["SHAPE_MISMATCH_REASON", "SnapshotStream", "step_is_snapshot", "with_answer_text"]

logger = logging.getLogger(__name__)

#: The audited stream-fallback reason a mismatched diffusion stream records.
SHAPE_MISMATCH_REASON = "diffusion_stream_shape_mismatch"

#: Below this many characters on both sides a step is not judged (too little text).
_JUDGE_MIN_CHARS = 24
#: Characters compared per step (bounded cost on long answers).
_JUDGE_HEAD_CHARS = 512
#: Similarity at or above which two consecutive canvases are versions of one text.
_SNAPSHOT_SIMILARITY = 0.5
#: Step timings kept for the trace.
_MAX_TIMED_STEPS = 256


def step_is_snapshot(previous: str, current: str) -> bool | None:
    """Whether ``current`` reads as a new version of ``previous`` (``None``: not judged).

    A snapshot step rewrites positions of the same text, so the two canvases are
    mostly alike; appended deltas are unrelated fragments.
    """
    if max(len(previous), len(current)) < _JUDGE_MIN_CHARS:
        return None
    if current == previous or current.startswith(previous):
        return True
    ratio = difflib.SequenceMatcher(
        None, previous[:_JUDGE_HEAD_CHARS], current[:_JUDGE_HEAD_CHARS], autojunk=False
    ).ratio()
    return ratio >= _SNAPSHOT_SIMILARITY


@dataclasses.dataclass
class SnapshotStream:
    """One diffusing call's stream: the latest canvas, the shape check, the timings."""

    canvas: str = ""
    steps: int = 0
    concatenated: list[str] = dataclasses.field(default_factory=list)
    judged: int = 0
    append_like: int = 0
    tool_call_chunks: int = 0
    _started: float = dataclasses.field(default_factory=time.monotonic)
    _step_ms: list[int] = dataclasses.field(default_factory=list)
    _last: Any = None

    def feed(self, chunk: Any) -> list[Piece]:
        """Show one chunk's canvas; return the reasoning pieces for the append lane."""
        from clio_agent.runtime.lm_activity import note_lm_answer_canvas  # noqa: PLC0415

        if chunk is self._last:
            return []
        self._last = chunk
        delta = chunk_delta(chunk)
        if delta is None:
            return []
        if _has_tool_calls(delta):
            self.tool_call_chunks += 1
        pieces: list[Piece] = []
        if reasoning := field_reasoning(delta):
            pieces.append(Piece(THINKING, reasoning, PROVIDER_FIELD))
        text = field_text(delta)
        if not text:
            return pieces
        if self.canvas:
            verdict = step_is_snapshot(self.canvas, text)
            if verdict is not None:
                self.judged += 1
                self.append_like += not verdict
        self.steps += 1
        self.canvas = text
        self.concatenated.append(text)
        if len(self._step_ms) < _MAX_TIMED_STEPS:
            self._step_ms.append(int((time.monotonic() - self._started) * 1000))
        note_lm_answer_canvas(text, self.steps)
        return pieces

    @property
    def shaped_as_snapshots(self) -> bool:
        """The stream's text read as snapshots (a single step is both shapes at once)."""
        return not (self.judged >= 2 and self.append_like * 2 > self.judged)

    def finish(self, response: Response, *, app: Any, session_id: str) -> Response:
        """Settle the call: its answer text, the final transcript text, the trace."""
        from clio_agent.gact.agents.clio_react_stream import route_pieces  # noqa: PLC0415
        from clio_agent.runtime.stream_audit import stream_audit  # noqa: PLC0415

        snapshots = self.shaped_as_snapshots
        stream_audit(
            "diffusion.stream",
            steps=self.steps,
            step_ms=list(self._step_ms),
            judged_steps=self.judged,
            append_like_steps=self.append_like,
            tool_call_chunks=self.tool_call_chunks,
            shape="snapshot" if snapshots else "append",
            final_len=len(self.canvas),
        )
        mismatches: list[str] = []
        if not snapshots:
            mismatches.append(
                f"{self.append_like} of {self.judged} judged steps were appended deltas, "
                "not snapshots; the concatenated text is kept"
            )
        if self.tool_call_chunks > 1:
            mismatches.append(
                f"tool calls arrived across {self.tool_call_chunks} streamed steps; "
                "the calls DSPy assembled are kept"
            )
        if mismatches:
            _record_mismatch(app, session_id, "; ".join(mismatches))
        if not self.steps:
            return response
        final = self.canvas if snapshots else "".join(self.concatenated)
        splitter = InlineThinkSplitter()
        pieces = splitter.feed(final) + splitter.finish()
        route_pieces(pieces, unclosed=splitter.found and not splitter.complete)
        return with_answer_text(response, final) if snapshots else response


def with_answer_text(response: Response, text: str) -> Response:
    """``response`` whose answer text is exactly ``text`` (other parts kept in order)."""
    parts: list[Any] = []
    placed = False
    for part in response.message.parts:
        if not isinstance(part, TextPart):
            parts.append(part)
        elif not placed:
            parts.append(dataclasses.replace(part, text=text))
            placed = True
    if not placed:
        parts.append(TextPart(text=text))
    message = dataclasses.replace(response.message, parts=tuple(parts))
    return dataclasses.replace(response, message=message)


def _has_tool_calls(delta: Any) -> bool:
    calls = (
        delta.get("tool_calls") if isinstance(delta, dict) else getattr(delta, "tool_calls", None)
    )
    return bool(calls)


def _record_mismatch(app: Any, session_id: str, message: str) -> None:
    from clio_agent.gact.stream_fallbacks import record_stream_fallback_note  # noqa: PLC0415

    if app is None or not session_id:
        # Off-turn (CLI, optimizer): no turn ledger to hold the note -- say so here.
        logger.warning(
            "diffusion stream degraded off-turn reason=%s message=%s",
            SHAPE_MISMATCH_REASON,
            message,
        )
        return
    record_stream_fallback_note(app, session_id, SHAPE_MISMATCH_REASON, message)
