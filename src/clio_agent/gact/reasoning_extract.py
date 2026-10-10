"""Provider-neutral reasoning extraction: one rule for the live stream and the record.

A model's thinking reaches CLIO one of two ways:

* **provider field** -- the server parses it out and sends it beside the answer as
  ``reasoning_content`` (LiteLLM, DeepSeek, vLLM with a reasoning parser) or
  ``reasoning`` (newer vLLM, some OpenAI-compatible servers), on a streamed ``delta``
  or a final ``message``, dict- or object-shaped;
* **inline tag** -- no parser ran, so the answer text itself starts with a
  ``<think>...</think>`` block (Qwen3 / R1-style models on a server started without
  ``--reasoning-parser``).

The live path (:class:`StreamRouter`, fed every streamed chunk of one call), the
loop's typed step (:func:`split_leading_think` on the reply's text) and the persisted
reasoning log (:func:`entry_reasoning_text` / :func:`entry_response_text` over
``lm.history``) all apply the SAME rule, so a reloaded transcript equals the live one:

1. A provider field wins: when the call carries one, the answer text is never parsed
   (a literal ``<think>`` in it stays in the answer).
2. Otherwise only a LEADING block counts (whitespace before ``<think>`` allowed); a tag
   later in the answer is answer text. Whitespace right after ``</think>`` is dropped.
3. A block never closed by the end of the call is all thinking, marked incomplete
   (the answer is empty): an unterminated reasoning trace is not an answer.

Origins are labelled :data:`PROVIDER_FIELD` / :data:`INLINE_TAG`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, NamedTuple

#: The thinking came from the provider's own reasoning field.
PROVIDER_FIELD = "provider_field"
#: The thinking was a leading ``<think>...</think>`` block cut out of the answer text.
INLINE_TAG = "inline_tag"
#: The reasoning-field spellings, in preference order.
REASONING_FIELDS = ("reasoning_content", "reasoning")
THINKING, ANSWER = "thinking", "answer"
_OPEN, _CLOSE = "<think>", "</think>"


class Piece(NamedTuple):
    """One routed piece of a stream: ``kind`` is :data:`THINKING` or :data:`ANSWER`."""

    kind: str
    text: str
    source: str = ""


class Split(NamedTuple):
    """A whole reply split into thinking and answer (``source`` ``""``: no thinking)."""

    thinking: str
    answer: str
    source: str
    complete: bool = True


def _get(obj: Any, key: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, Mapping):
        return obj.get(key)
    return getattr(obj, key, None)


def field_reasoning(obj: Any) -> str:
    """The provider reasoning text on a delta or message (either spelling), or ``""``."""
    for key in REASONING_FIELDS:
        value = _get(obj, key)
        if isinstance(value, str) and value:
            return value
    return ""


def field_text(obj: Any) -> str:
    """The answer text on a delta or message (``content``), or ``""``."""
    value = _get(obj, "content")
    return value if isinstance(value, str) else ""


def chunk_delta(chunk: Any) -> Any:
    """``choices[0].delta`` of a streamed chunk (dict or object), or ``None``."""
    try:
        choices = _get(chunk, "choices")
        return _get(choices[0], "delta") if choices else None
    except (IndexError, KeyError, TypeError):
        return None


def _held_close(buf: str) -> int:
    """Length of the longest suffix of ``buf`` that may still grow into ``</think>``."""
    for size in range(min(len(buf), len(_CLOSE) - 1), 0, -1):
        if buf.endswith(_CLOSE[:size]):
            return size
    return 0


class InlineThinkSplitter:
    """Streaming split of a LEADING ``<think>`` block out of answer text.

    Safe across any chunking: a tag split over chunks (``"<th"`` + ``"ink>"``) is held
    until it is decided, so the concatenated output never depends on where the chunks
    were cut, and no text is ever emitted twice.
    """

    def __init__(self) -> None:
        self._state = "probe"  # probe -> think -> after -> answer, or probe -> answer
        self._buf = ""
        self.found = False
        self.complete = True

    def feed(self, text: str) -> list[Piece]:
        """Route ``text`` (the next answer-channel delta); returns what is now decided."""
        self._buf += text
        out: list[Piece] = []
        while True:
            if self._state == "probe":
                head = self._buf.lstrip()
                if not head or (len(head) < len(_OPEN) and _OPEN.startswith(head)):
                    return out  # still undecided: whitespace or a partial opening tag
                if not head.startswith(_OPEN):
                    self._state = "answer"
                    continue
                self.found, self.complete = True, False
                self._state, self._buf = "think", head[len(_OPEN) :]
            elif self._state == "think":
                end = self._buf.find(_CLOSE)
                if end < 0:
                    cut = len(self._buf) - _held_close(self._buf)
                    emit, self._buf = self._buf[:cut], self._buf[cut:]
                    if emit:
                        out.append(Piece(THINKING, emit, INLINE_TAG))
                    return out
                if end:
                    out.append(Piece(THINKING, self._buf[:end], INLINE_TAG))
                self.complete = True
                self._state, self._buf = "after", self._buf[end + len(_CLOSE) :]
            elif self._state == "after":
                head = self._buf.lstrip()
                self._buf = head
                if not head:
                    return out
                self._state = "answer"
            else:
                if self._buf:
                    out.append(Piece(ANSWER, self._buf))
                    self._buf = ""
                return out

    def finish(self) -> list[Piece]:
        """The end of the stream: flush what is still held (idempotent)."""
        buf, self._buf = self._buf, ""
        state, self._state = self._state, "answer"
        if not buf:
            return []
        if state == "think":
            return [Piece(THINKING, buf, INLINE_TAG)]  # never closed: stays incomplete
        return [Piece(ANSWER, buf)]


def split_leading_think(text: str) -> Split:
    """Split a whole reply's text by the streaming rule (identical result by design)."""
    splitter = InlineThinkSplitter()
    pieces = splitter.feed(text or "") + splitter.finish()
    thinking = "".join(p.text for p in pieces if p.kind == THINKING)
    answer = "".join(p.text for p in pieces if p.kind == ANSWER)
    return Split(thinking, answer, INLINE_TAG if splitter.found else "", splitter.complete)


def split_message(message: Any) -> Split:
    """Split one final message / output (dict, object or plain string)."""
    if isinstance(message, str):
        return split_leading_think(message)
    reasoning = field_reasoning(message)
    text = field_text(message) or _get(message, "text")
    text = text if isinstance(text, str) else ""
    if reasoning:
        return Split(reasoning, text, PROVIDER_FIELD)
    return split_leading_think(text)


class StreamRouter:
    """Routes ONE LM call's streamed chunks to thinking and answer pieces.

    A chunk object delivered twice is routed once. The first chunk carrying a provider
    reasoning field switches the call to provider mode for the rest of the stream (rule
    1): later answer text is passed through unparsed.
    """

    def __init__(self) -> None:
        self._splitter = InlineThinkSplitter()
        self._provider = False
        self._last: Any = None

    @property
    def incomplete(self) -> bool:
        """The stream ended inside an unclosed inline ``<think>`` block."""
        return self._splitter.found and not self._splitter.complete

    def feed(self, chunk: Any) -> list[Piece]:
        """The pieces one chunk decides."""
        if chunk is self._last:
            return []
        self._last = chunk
        delta = chunk_delta(chunk)
        if delta is None:
            return []
        out: list[Piece] = []
        reasoning = field_reasoning(delta)
        if reasoning:
            if not self._provider:
                self._provider = True
                out.extend(self._splitter.finish())
            out.append(Piece(THINKING, reasoning, PROVIDER_FIELD))
        text = field_text(delta)
        if text:
            out.extend([Piece(ANSWER, text)] if self._provider else self._splitter.feed(text))
        return out

    def finish(self) -> list[Piece]:
        """The pieces still held when the stream ends."""
        return [] if self._provider else self._splitter.finish()


def entry_reasoning_text(entry: Mapping[str, Any]) -> str:
    """The thinking of one dspy ``lm.history`` entry.

    From ``entry["outputs"]`` (a ``reasoning_content``/``reasoning`` field, else a leading
    ``<think>`` block of the output's text), falling back to the raw ``entry["response"]``
    (``choices[i].message``) under the same rule.
    """
    parts = [split_message(out).thinking for out in _outputs(entry)]
    if not any(parts):
        choices = _get(entry.get("response"), "choices")
        if isinstance(choices, list):
            parts = [split_message(_get(choice, "message")).thinking for choice in choices]
    return "\n".join(p for p in parts if p).strip()


def entry_response_text(entry: Mapping[str, Any]) -> str:
    """The answer of one dspy ``lm.history`` entry, its inline thinking cut out."""
    return "\n".join(a for a in (split_message(o).answer for o in _outputs(entry)) if a).strip()


def _outputs(entry: Mapping[str, Any]) -> list[Any]:
    outputs = entry.get("outputs")
    if not isinstance(outputs, list):
        return []
    return [o for o in outputs if isinstance(o, (str, Mapping))]


def thinking_part_metadata(field: str) -> dict[str, Any]:
    """Transcript metadata for a ``provider_thinking:<provider>[:<origin>]`` live part.

    ``reasoning_source`` (``provider_field`` / ``inline_tag``) is present only when the
    producer named the origin, so other producers' parts keep their exact shape.
    """
    _, _, rest = field.partition(":")
    provider, _, origin = rest.partition(":")
    if origin not in (PROVIDER_FIELD, INLINE_TAG):
        provider, origin = rest, ""
    return {
        "thinking_source": "provider",
        "provider_source": provider,
        "default_collapsed": True,
        **({"reasoning_source": origin} if origin else {}),
    }
