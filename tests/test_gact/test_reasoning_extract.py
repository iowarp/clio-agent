"""The provider-neutral reasoning rule (gact/reasoning_extract.py), live and persisted.

One rule for every path: a provider reasoning field (``reasoning_content`` or
``reasoning``, dict- or object-shaped) wins and the answer text is kept as written;
otherwise a LEADING ``<think>...</think>`` block of the answer text is the thinking --
whatever the chunking -- and a block never closed is all thinking, marked incomplete.
The persisted reasoning log (``lm.history`` entries) splits the same way as the live
stream, so a reload equals what streamed.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact.protocol.v3.message import part_to_v3_block
from clio_agent.gact.reasoning_extract import (
    ANSWER,
    INLINE_TAG,
    PROVIDER_FIELD,
    THINKING,
    InlineThinkSplitter,
    Piece,
    StreamRouter,
    entry_reasoning_text,
    entry_response_text,
    split_leading_think,
    split_message,
    thinking_part_metadata,
)


def _chunk(delta: Any) -> dict[str, Any]:
    return {"choices": [{"delta": delta}]}


def _obj_chunk(**delta: Any) -> SimpleNamespace:
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(**delta))])


def _route(chunks: list[Any]) -> tuple[str, str, set[str], StreamRouter]:
    router = StreamRouter()
    pieces: list[Piece] = []
    for chunk in chunks:
        pieces.extend(router.feed(chunk))
    pieces.extend(router.finish())
    thinking = "".join(p.text for p in pieces if p.kind == THINKING)
    answer = "".join(p.text for p in pieces if p.kind == ANSWER)
    return thinking, answer, {p.source for p in pieces if p.kind == THINKING}, router


def _stream_text(text: str, cuts: list[int]) -> list[dict[str, Any]]:
    bounds = [0, *cuts, len(text)]
    return [_chunk({"content": text[a:b]}) for a, b in zip(bounds, bounds[1:], strict=False)]


# --------------------------------------------------------------------------- #
# the inline <think> splitter                                                 #
# --------------------------------------------------------------------------- #
QWEN = "<think>\nweigh A vs B\n</think>\n\nB is better."


def test_a_leading_block_is_thinking_and_the_rest_the_answer() -> None:
    split = split_leading_think(QWEN)
    assert split.thinking == "\nweigh A vs B\n"
    assert split.answer == "B is better."
    assert split.source == INLINE_TAG
    assert split.complete is True


@pytest.mark.parametrize(
    "text",
    [
        QWEN,
        "  \n<think>x</think>y",
        "<think>a</th</think>b",
        "<think>open only",
        "<think>abc</thi",
        "plain answer",
        "Answer first <think>x</think>",
        "<th",
        "   ",
        "<thinking>not a tag</thinking>",
    ],
)
def test_every_chunk_boundary_gives_the_same_split(text: str) -> None:
    whole = split_leading_think(text)
    for cut in range(len(text) + 1):
        thinking, answer, _, _ = _route(_stream_text(text, [cut]))
        assert (thinking, answer) == (whole.thinking, whole.answer), cut
    # One character per chunk: every tag is split across chunks.
    thinking, answer, _, _ = _route(_stream_text(text, list(range(1, len(text)))))
    assert (thinking, answer) == (whole.thinking, whole.answer)


def test_a_tag_split_across_chunks_is_held_until_decided() -> None:
    splitter = InlineThinkSplitter()
    assert splitter.feed("<th") == []
    assert splitter.feed("ink>pla") == [Piece(THINKING, "pla", INLINE_TAG)]
    assert splitter.feed("n</thi") == [Piece(THINKING, "n", INLINE_TAG)]
    assert splitter.feed("nk>\n\nDone") == [Piece(ANSWER, "Done")]
    assert splitter.finish() == []


def test_whitespace_before_the_opening_tag_is_allowed() -> None:
    split = split_leading_think("\n\n  <think>x</think>  y")
    assert (split.thinking, split.answer, split.source) == ("x", "y", INLINE_TAG)


def test_only_a_leading_block_counts() -> None:
    text = "The tag <think>x</think> is literal here."
    split = split_leading_think(text)
    assert (split.thinking, split.answer, split.source) == ("", text, "")
    assert split_leading_think("<thinking>no</thinking>").source == ""


def test_an_unclosed_block_is_all_thinking_marked_incomplete() -> None:
    split = split_leading_think("<think>still going</thi")
    assert split.thinking == "still going</thi"
    assert split.answer == ""
    assert (split.source, split.complete) == (INLINE_TAG, False)
    _, _, _, router = _route(_stream_text("<think>still going", [3, 9]))
    assert router.incomplete is True


def test_an_empty_block_leaves_whitespace_thinking_and_a_clean_answer() -> None:
    # Qwen3 with thinking off still emits an empty block; the transcript drops a
    # whitespace-only thinking part, and the step records none (see test_clio_react).
    split = split_leading_think("<think>\n\n</think>\n\nHi")
    assert split.thinking.strip() == ""
    assert split.answer == "Hi"


# --------------------------------------------------------------------------- #
# provider fields                                                             #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("key", ["reasoning_content", "reasoning"])
def test_both_reasoning_spellings_on_dict_and_object_deltas(key: str) -> None:
    for chunk in (_chunk({key: "plan", "content": None}), _obj_chunk(**{key: "plan"})):
        thinking, answer, sources, _ = _route([chunk, _chunk({"content": "ok"})])
        assert (thinking, answer, sources) == ("plan", "ok", {PROVIDER_FIELD})


def test_a_provider_field_wins_and_inline_tags_stay_in_the_answer() -> None:
    chunks = [
        _chunk({"reasoning_content": "real"}),
        _chunk({"content": "<think>lit"}),
        _chunk({"content": "eral</think>ans"}),
    ]
    thinking, answer, sources, router = _route(chunks)
    assert (thinking, answer, sources) == ("real", "<think>literal</think>ans", {PROVIDER_FIELD})
    assert router.incomplete is False
    split = split_message({"text": "<think>literal</think>ans", "reasoning": "real"})
    assert split.thinking == "real"
    assert (split.answer, split.source) == ("<think>literal</think>ans", PROVIDER_FIELD)


def test_a_held_prefix_is_flushed_as_answer_when_a_provider_field_arrives() -> None:
    thinking, answer, _, _ = _route(
        [_chunk({"content": "\n"}), _chunk({"reasoning_content": "r"}), _chunk({"content": "a"})]
    )
    assert (thinking, answer) == ("r", "\na")


def test_a_repeated_chunk_object_is_routed_once() -> None:
    first = _chunk({"content": "<think>ab"})
    second = _chunk({"content": "</think>c"})
    thinking, answer, _, _ = _route([first, first, second, second])
    assert (thinking, answer) == ("ab", "c")
    # Equal text in a NEW chunk is a real repeated token, not a duplicate.
    thinking, answer, _, _ = _route([_chunk({"content": "ha"}), _chunk({"content": "ha"})])
    assert answer == "haha"


def test_chunks_without_a_delta_are_ignored() -> None:
    router = StreamRouter()
    assert router.feed({"choices": []}) == []
    assert router.feed(SimpleNamespace(choices=None)) == []
    assert router.feed("not a chunk") == []


# --------------------------------------------------------------------------- #
# persisted reasoning log == live stream                                      #
# --------------------------------------------------------------------------- #
def test_history_entries_split_inline_thinking_out_of_the_response() -> None:
    entry = {"outputs": [{"text": QWEN}]}
    assert entry_reasoning_text(entry) == "weigh A vs B"
    assert entry_response_text(entry) == "B is better."
    assert entry_response_text({"outputs": [QWEN]}) == "B is better."


@pytest.mark.parametrize("key", ["reasoning_content", "reasoning"])
def test_history_entries_read_either_field_and_keep_the_text(key: str) -> None:
    entry = {"outputs": [{"text": "<think>t</think>a", key: "r"}]}
    assert entry_reasoning_text(entry) == "r"
    assert entry_response_text(entry) == "<think>t</think>a"


def test_history_entries_fall_back_to_the_raw_response_message() -> None:
    message = SimpleNamespace(content="<think>raw</think>x", reasoning=None)
    entry = {"outputs": [], "response": SimpleNamespace(choices=[SimpleNamespace(message=message)])}
    assert entry_reasoning_text(entry) == "raw"
    message = SimpleNamespace(content="x", reasoning="field")
    entry["response"] = SimpleNamespace(choices=[SimpleNamespace(message=message)])
    assert entry_reasoning_text(entry) == "field"
    assert entry_reasoning_text({"outputs": [{"text": "no thinking"}]}) == ""


@pytest.mark.parametrize(
    "chunks",
    [
        [_chunk({"content": c}) for c in ("<th", "ink>\nplan\n</thi", "nk>\n\n", "Answer.")],
        [_chunk({"reasoning_content": "plan"}), _chunk({"content": "Answer."})],
        [_chunk({"reasoning": "plan"}), _chunk({"content": "<think>x</think>Answer."})],
        [_chunk({"content": c}) for c in ("<think>never", " closed")],
        [_chunk({"content": "plain"})],
    ],
)
def test_the_reloaded_reasoning_log_equals_the_live_stream(chunks: list[Any]) -> None:
    live_thinking, live_answer, _, _ = _route(chunks)
    deltas = [c["choices"][0]["delta"] for c in chunks]
    output: dict[str, Any] = {"text": "".join(d.get("content") or "" for d in deltas)}
    reasoning = "".join(d.get("reasoning_content") or d.get("reasoning") or "" for d in deltas)
    if reasoning:
        output["reasoning_content"] = reasoning
    entry = {"outputs": [output]}
    assert entry_reasoning_text(entry) == live_thinking.strip()
    assert entry_response_text(entry) == live_answer.strip()


# --------------------------------------------------------------------------- #
# the origin label on the transcript part and the wire                        #
# --------------------------------------------------------------------------- #
def test_thinking_part_metadata_labels_a_named_origin_only() -> None:
    assert thinking_part_metadata("provider_thinking:anthropic") == {
        "thinking_source": "provider",
        "provider_source": "anthropic",
        "default_collapsed": True,
    }
    inline = thinking_part_metadata("provider_thinking:model:inline_tag")
    assert (inline["provider_source"], inline["reasoning_source"]) == ("model", INLINE_TAG)
    field = thinking_part_metadata("provider_thinking:model:provider_field")
    assert field["reasoning_source"] == PROVIDER_FIELD
    # An unknown suffix is part of the provider name, as before.
    other = thinking_part_metadata("provider_thinking:codex:reasoning")
    assert other["provider_source"] == "codex:reasoning"
    assert "reasoning_source" not in other


def test_the_v3_reasoning_block_carries_the_origin_when_known() -> None:
    metadata = thinking_part_metadata("provider_thinking:model:inline_tag")
    block = part_to_v3_block({"id": "p1", "type": "thinking", "text": "t", "metadata": metadata})
    assert block["type"] == "reasoning"
    assert block["source"] == "provider"  # unchanged wire value
    assert block["reasoning_source"] == INLINE_TAG
    plain = thinking_part_metadata("provider_thinking:anthropic")
    block = part_to_v3_block({"id": "p2", "type": "thinking", "text": "t", "metadata": plain})
    assert "reasoning_source" not in block
