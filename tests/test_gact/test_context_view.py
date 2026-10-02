"""The context route's view is the agent's context, and folding never loses content.

``context_messages`` is ``fold_steps`` -- the loop's own fold -- serialized. The fuzz
holds the invariant the old trajectory-dict projection was fuzzed for (consecutive
observations once overwrote each other there): whatever coherent live plane a scope
holds -- user messages, summaries and steps whose results land in any order -- each
segment's content reaches the model. An incoherent plane is a typed failure.
"""

from __future__ import annotations

import json
import random
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact.agents.clio_react_record import ContextFoldError
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


def _random_step(rng: random.Random, counter: list[int]) -> list[tuple[SimpleNamespace, str]]:
    """One coherent step: an optional thought, 0-3 calls, every call answered after it.

    Results interleave with later calls and land out of call order, as concurrent
    calls do; each answers a call already made in the same step, matched by call id.
    """

    def needle() -> str:
        counter[0] += 1
        return f"NEEDLE{counter[0]}"

    out: list[tuple[SimpleNamespace, str]] = []
    n_calls = rng.randrange(4)
    if n_calls == 0 or rng.random() < 0.8:  # a call may open a step with no thought
        text = needle()
        out.append((_seg("thought", text=text), text))
    to_call = [needle() for _ in range(n_calls)]
    pending: list[str] = []
    while to_call or pending:
        if to_call and (not pending or rng.random() < 0.5):
            tag = to_call.pop(0)
            call = _seg("tool_call", id=f"c{tag}", name=f"tool{tag}", args={"k": tag})
            out.append((call, tag))
            pending.append(tag)
        else:
            tag = pending.pop(rng.randrange(len(pending)))
            text = needle()
            obs = _seg("observation", call_id=f"c{tag}", text=text, is_error=rng.random() < 0.2)
            out.append((obs, text))
    return out


def _random_plane(rng: random.Random) -> list[tuple[SimpleNamespace, str]]:
    """A coherent live plane: user messages, summaries and steps in random order."""
    counter = [0]
    pairs: list[tuple[SimpleNamespace, str]] = []
    for _ in range(rng.randrange(1, 12)):
        kind = rng.choice(("user", "summary", "step", "step"))
        if kind == "step":
            pairs.extend(_random_step(rng, counter))
        else:
            counter[0] += 1
            text = f"NEEDLE{counter[0]}"
            pairs.append((_seg(kind, text=text), text))
    return pairs


@pytest.mark.parametrize("seed", range(40))
def test_folding_never_loses_a_segment(seed: int) -> None:
    """A coherent plane folds without losing any segment's content."""
    rng = random.Random(seed)
    pairs = _random_plane(rng)
    shown = json.dumps(context_messages([seg for seg, _ in pairs]))
    lost = [(seg.kind, needle) for seg, needle in pairs if needle not in shown]
    assert not lost, f"seed={seed} lost {lost}"


@pytest.mark.parametrize("seed", range(20))
def test_dropping_an_observation_fails_typed_instead_of_losing_it(seed: int) -> None:
    """The same plane with one observation removed is incoherent: its call is left
    unanswered and the fold raises rather than showing the call without a result."""
    rng = random.Random(seed)
    pairs = _random_plane(rng)
    observed = [i for i, (seg, _) in enumerate(pairs) if seg.kind == "observation"]
    if observed:
        del pairs[rng.choice(observed)]
    else:  # no step with a call was drawn: end the plane on one whose result is gone
        pairs.append((_seg("tool_call", id="cX", name="toolX", args={}), "X"))
    with pytest.raises(ContextFoldError) as err:
        context_messages([seg for seg, _ in pairs])
    assert err.value.details["problem"] == "unanswered_call"
