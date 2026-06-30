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

# ``[[ ## <field> ## ]]`` section markers (ChatAdapter). Tolerant of extra spaces.
_SECTION = re.compile(r"\[\[\s*##\s*([A-Za-z0-9_]+)\s*##\s*\]\]")
# Longest tail to hold back so a partial marker mid-arrival isn't emitted.
_HOLDBACK = len("[[ ## workflow_state ## ]]") + 2


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
        sections = list(_SECTION.finditer(self._full))
        for index, sec in enumerate(sections):
            if sec.group(1) != self._field:
                continue
            start = sec.end()
            if index + 1 < len(sections):
                end = sections[index + 1].start()
            else:
                end = len(self._full)
                if safe:
                    # The field is still streaming (no following marker yet): hold
                    # back a tail so a partial next-marker isn't leaked as answer.
                    end = max(start, end - _HOLDBACK)
            text = self._full[start:end]
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
