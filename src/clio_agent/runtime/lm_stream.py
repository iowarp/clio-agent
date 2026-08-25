"""Streaming answer-field extractor for the unified LM token highway (#693).

DSPy's ``StreamListener`` only surfaces field-level deltas inside the
``dspy.streamify`` pump, which the blueprint/expert calls never enter (they run
in executor threads). So the single LM-stream tap (``config.IOLoggingLM._clio_
streamed_call``) needs its OWN field extraction to turn the raw ChatAdapter token
stream into clean *answer-field* deltas for the live UI, while reasoning and the
structured fields stay off the user-facing text.

ChatAdapter formats outputs as repeated sections::

    [[ ## reasoning ## ]]
    ...chain of thought...
    [[ ## answer ## ]]
    the user-facing answer
    [[ ## workflow_state ## ]]
    {...}
    [[ ## completed ## ]]

``AnswerFieldExtractor`` is fed raw content deltas (in order) and returns only
the NEW text that belongs to the target field, holding back a short tail so a
section marker arriving split across chunks is never emitted as answer text.
Pure + deterministic so it unit-tests without a provider.
"""

from __future__ import annotations

import re
from typing import Any

# ``[[ ## <field> ## ]]`` section markers (ChatAdapter). Each REAL marker stands ALONE
# on its own line, so anchor to line boundaries (MULTILINE ^…$, all inner spacing kept
# on one line via [ \t]). Without this, a marker the model QUOTES inline in its own prose
# — e.g. a next_thought explaining "respond starting with `[[ ## next_thought ## ]]`, then
# `[[ ## next_tool_name ## ]]`" — was matched as a real field boundary, and the field was
# extracted as the garbage BETWEEN the quoted markers (the "`, then `" truncation).
_SECTION = re.compile(r"(?m)^[ \t]*\[\[[ \t]*##[ \t]*([A-Za-z0-9_]+)[ \t]*##[ \t]*\]\][ \t]*$")
# Some SDK-backed models occasionally encode a section line's surrounding newlines
# as the two literal characters ``\\n``.  The resulting text is still an otherwise
# valid ChatAdapter contract, but neither DSPy's parser nor the live field extractor
# sees the next field boundary.  Match only a marker framed by TWO encoded newlines:
# this cannot reinterpret an inline marker quoted in prose as a structural boundary.
_ESCAPED_SECTION = re.compile(
    r"\\n[ \t]*(\[\[[ \t]*##[ \t]*([A-Za-z0-9_]+)[ \t]*##[ \t]*\]\])[ \t]*\\n"
)
# Longest tail to hold back so a partial marker mid-arrival isn't emitted.
_HOLDBACK = len("[[ ## workflow_state ## ]]") + 2


def normalize_escaped_section_boundaries(text: str) -> str:
    """Restore encoded ChatAdapter section separators without touching prose.

    Only ``\\n<declared-looking marker>\\n`` is normalized.  Inline/quoted markers,
    ordinary escaped newlines, JSON payloads, and field values remain byte-for-byte
    unchanged.  The repaired output still goes through DSPy's normal typed parser.
    """

    return _ESCAPED_SECTION.sub(lambda match: f"\n{match.group(1)}\n", text)


class AnswerFieldExtractor:
    """Incrementally extract one field's text from a streamed ChatAdapter output."""

    def __init__(self, field: str = "answer") -> None:
        self._field = field
        self._full = ""
        self._emitted = 0

    def feed(self, text: str) -> str:
        """Append a raw content delta; return the new answer-field text to emit."""
        if not text:
            return ""
        self._full += text
        answer = self._current_answer(safe=True)
        if len(answer) <= self._emitted:
            return ""
        delta = answer[self._emitted :]
        self._emitted = len(answer)
        return delta

    def is_structured(self) -> bool:
        """True when the answer field is a structured payload (JSON object/array)
        rather than user-facing prose. Intermediate blueprint experts (geospatial,
        data, …) put a region/evidence object in their ``answer`` field; only the
        terminal synthesis answer is prose. The live UI streams prose only; the
        structured answers still ride the highway/trace. Decided from the first
        non-whitespace char so it's stable once content starts."""
        answer = self._current_answer(safe=False).lstrip()
        if not answer:
            return False
        if answer[0] in "{[":
            return True
        return answer.startswith("```json") or answer.startswith("```JSON")

    def flush(self) -> str:
        """Final call after the stream ends; emit any held-back remainder."""
        answer = self._current_answer(safe=False)
        if len(answer) <= self._emitted:
            return ""
        delta = answer[self._emitted :]
        self._emitted = len(answer)
        return delta

    def _current_answer(self, *, safe: bool) -> str:
        normalized = normalize_escaped_section_boundaries(self._full)
        sections = list(_SECTION.finditer(normalized))
        for index, sec in enumerate(sections):
            if sec.group(1) != self._field:
                continue
            start = sec.end()
            if index + 1 < len(sections):
                end = sections[index + 1].start()
            else:
                end = len(normalized)
                if safe:
                    # The field is still streaming (no following marker yet): hold
                    # back a tail so a partial next-marker isn't leaked as answer.
                    end = max(start, end - _HOLDBACK)
            text = normalized[start:end]
            return text.lstrip("\n")
        return ""


def extract_delta(chunk: object) -> tuple[str, str]:
    """Best-effort ``(content, reasoning_content)`` text from a streamed chunk.

    Handles litellm ``ModelResponseStream`` objects and plain dicts; returns
    empty strings for any chunk that carries no delta text. Never raises."""

    def _get(obj: object, key: str) -> Any:
        value = getattr(obj, key, None)
        if value is None and isinstance(obj, dict):
            value = obj.get(key)
        return value

    try:
        choices = _get(chunk, "choices")
        if not choices:
            return "", ""
        delta = _get(choices[0], "delta")
        if delta is None:
            return "", ""
        content = _get(delta, "content") or ""
        reasoning = _get(delta, "reasoning_content") or ""
        return str(content), str(reasoning)
    except Exception:  # noqa: BLE001 - extraction is best-effort
        return "", ""
