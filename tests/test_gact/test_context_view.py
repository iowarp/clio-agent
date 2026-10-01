"""The context route's view is the agent's context, and folding never loses content.

``context_messages`` is ``fold_steps`` -- the loop's own fold -- serialized. The fuzz
holds the invariant the old trajectory-dict projection was fuzzed for (consecutive
observations once overwrote each other there): whatever live segments a scope holds,
in any order, each one's content reaches the model.
"""

from __future__ import annotations

import json
import random
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact.context_view import context_messages


def _seg(kind: str, **content: Any) -> SimpleNamespace:
    return SimpleNamespace(kind=kind, content=content)


def test_every_part_type_is_shown_as_the_model_receives_it() -> None:
    thinking = {"text": "weigh it", "continuation": [{"provider": "p", "kind": "k", "data": {}}]}
    messages = context_messages(
        [
            _seg(
                "user",
                text="what is this?",
                media=[{"type": "image", "media_type": "image/png", "data": "eA=="}],
            ),
            _seg("user", text="plan mode is on", source="plan_mode", actor="algorithm"),
            _seg("thought", text="look", thinking=[thinking]),
            _seg("tool_call", id="c1", name="read", args={"path": "a.csv"}),
            _seg("observation", call_id="c1", text="rows", is_error=False),
            _seg("summary", text="earlier work"),
        ]
    )
    assert messages == [
        {
            "role": "user",
            "parts": [
                {"type": "text", "text": "what is this?"},
                {"type": "image", "media_type": "image/png"},  # never the bytes
            ],
        },
        {"role": "user", "parts": [{"type": "text", "text": "[clio: plan_mode]\nplan mode is on"}]},
        {
            "role": "assistant",
            "parts": [
                {"type": "thinking", "text": "weigh it"},
                {"type": "text", "text": "look"},
                {"type": "tool_call", "id": "c1", "name": "read", "input": {"path": "a.csv"}},
            ],
        },
        {
            "role": "tool",
            "parts": [
                {
                    "type": "tool_result",
                    "id": "c1",
                    "name": "read",
                    "is_error": False,
                    "content": [{"type": "text", "text": "rows"}],
                }
            ],
        },
        {"role": "user", "parts": [{"type": "text", "text": "[earlier context]\nearlier work"}]},
    ]


def test_kinds_outside_the_agent_context_are_not_shown() -> None:
    messages = context_messages(
        [_seg("answer", text="HIDDEN"), _seg("semantic_event", text="HIDDEN")]
    )
    assert messages == []


_KINDS = ("user", "thought", "tool_call", "observation", "summary")


def _random_segment(rng: random.Random, n: int) -> tuple[SimpleNamespace, str]:
    kind = rng.choice(_KINDS)
    needle = f"NEEDLE{n}"
    if kind == "tool_call":
        return _seg(kind, id=f"c{n}", name=f"tool{needle}", args={"k": needle}), needle
    if kind == "observation":
        call_id = f"c{rng.randrange(n + 1)}" if rng.random() < 0.7 else ""
        return _seg(kind, call_id=call_id, text=needle, is_error=rng.random() < 0.2), needle
    return _seg(kind, text=needle), needle


@pytest.mark.parametrize("seed", range(40))
def test_folding_never_loses_a_segment(seed: int) -> None:
    rng = random.Random(seed)
    pairs = [_random_segment(rng, n) for n in range(rng.randrange(1, 30))]
    shown = json.dumps(context_messages([seg for seg, _ in pairs]))
    lost = [
        (seg.kind, needle)
        for seg, needle in pairs
        if needle not in shown and not (seg.kind == "tool_call" and needle in shown)
    ]
    assert not lost, f"seed={seed} lost {lost}"
