"""The default keep rule: every typed message (within a budget) and the newest step.

Every harness reviewed keeps the user's own messages or a recent tail verbatim across a
compaction (``docs/design/compaction-review-2026-11.md``); CLIO kept only the open
turn's request, so earlier requests, limits and steers were known to the agent only
through the summary, and the output it was in the middle of using was folded.

SABOTAGE (each turns the named test red):

* ``post_compaction_context``: drop the typed-message loop ->
  ``test_every_typed_message_and_the_newest_step_stay_verbatim``.
* ``post_compaction_context``: keep typed messages oldest first ->
  ``test_the_budget_keeps_the_newest_typed_messages``.
* ``_units``: leave steers out of ``typed`` ->
  ``test_every_typed_message_and_the_newest_step_stay_verbatim``.
* ``post_compaction_context``: ``break`` at the first message over the budget ->
  ``test_a_long_typed_message_is_summarized_and_older_ones_that_fit_stay``.
* ``_message_chars``: measure the stored reference ->
  ``test_a_confirmed_input_is_measured_by_its_text_not_its_reference``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.storage import make_arc_store
from clio_agent.gact.agents.context_errors import ContextReadError
from clio_agent.gact.compaction_policy import KeepPolicy, post_compaction_context
from clio_agent.gact.task_history import TaskHistory
from tests._confirmed_input import confirm_input


def _segment(seg_id: str, kind: str, turn: str, content: dict[str, Any]) -> Any:
    return SimpleNamespace(id=seg_id, kind=kind, turn_id=turn, content=content)


def _live() -> list[Any]:
    return [
        _segment("q1", "user", "t1", {"text": "use mm/day; skip campaign B"}),
        _segment("th1", "thought", "t1", {"text": "reading"}),
        _segment("note", "user", "t1", {"text": "a reminder", "source": "x", "actor": "algorithm"}),
        _segment("q2", "user", "t2", {"text": "now add the dark interval"}),
        _segment("th2", "thought", "t2", {"text": "step a"}),
        _segment("steer", "user", "t2", {"text": "actually use cm/day", "actor": "user"}),
        _segment("th3", "thought", "t2", {"text": "step b"}),
        _segment("call3", "tool_call", "t2", {"id": "c3", "name": "read", "args": {}}),
    ]


def test_every_typed_message_and_the_newest_step_stay_verbatim() -> None:
    decision = post_compaction_context(_live(), open_turn_id="t2", policy=KeepPolicy())
    assert set(decision.keep) == {"q1", "q2", "steer", "th3", "call3"}
    assert set(decision.summarize) == {"th1", "note", "th2"}


def test_the_budget_keeps_the_newest_typed_messages() -> None:
    live = _live()
    newest_two = sum(len(s.content["text"]) for s in live if s.id in {"q2", "steer"})
    policy = KeepPolicy(keep_last_steps=0, keep_user_chars=newest_two)
    decision = post_compaction_context(live, open_turn_id="t2", policy=policy)
    assert {"q2", "steer"} <= set(decision.keep)
    assert "q1" in decision.summarize


def test_the_working_state_is_kept_unless_the_measurement_switch_is_off() -> None:
    """The latest plan and todos stay verbatim through a compaction; with
    ``keep_working_state`` off (the Step 3 ablation) they are summarized like the rest.
    SABOTAGE: ignore the switch and the off case still keeps them."""
    live = [
        _segment(
            "plan1", "user", "t1", {"text": "old plan", "source": "plan", "actor": "algorithm"}
        ),
        _segment(
            "plan2", "user", "t1", {"text": "new plan", "source": "plan", "actor": "algorithm"}
        ),
        _segment("todo", "user", "t1", {"text": "[ ] a", "source": "todos", "actor": "algorithm"}),
        *_live(),
    ]
    kept = post_compaction_context(live, open_turn_id="t2", policy=KeepPolicy()).keep
    assert {"plan2", "todo"} <= set(kept) and "plan1" not in kept
    off = KeepPolicy(keep_working_state=False)
    kept = post_compaction_context(live, open_turn_id="t2", policy=off).keep
    assert not {"plan1", "plan2", "todo"} & set(kept)


def test_a_long_typed_message_is_summarized_and_older_ones_that_fit_stay() -> None:
    """The budget skips a message too long for what is left and keeps going: an older,
    short constraint stays verbatim.

    SABOTAGE: ``break`` at the first message over the budget -> ``q1`` is summarized."""
    live = _live()
    live[3] = _segment("q2", "user", "t2", {"text": "x" * 5_000})
    small = sum(len(s.content["text"]) for s in live if s.id in {"q1", "steer"})
    policy = KeepPolicy(keep_last_steps=0, keep_user_chars=small)
    decision = post_compaction_context(live, open_turn_id="", policy=policy)
    assert {"q1", "steer"} <= set(decision.keep)
    assert "q2" in decision.summarize


@pytest.fixture
def arc() -> Iterator[ARCMemory]:
    memory = ARCMemory(store=make_arc_store(backend="cte", namespace=f"kp-{uuid.uuid4().hex[:8]}"))
    yield memory
    memory._store.clear()


def test_a_confirmed_input_is_measured_by_its_text_not_its_reference(arc: ARCMemory) -> None:
    """A message delivered through confirmed input stores Core references; the budget
    measures the text they resolve to, so a huge paste is not kept as if it were a few
    hundred characters of reference.

    SABOTAGE: measure ``len(str(rendered_content))`` -> the 20 KB paste is kept."""
    history = TaskHistory(arc)
    confirm_input(history, "paste", "p" * 20_000)
    confirm_input(history, "ask", "use mm/day", turn_id="t2")
    live = arc.render_working_set("owner", "main")
    paste, ask = (s.id for s in live if s.kind == "recipient_input")
    assert len(str(next(s for s in live if s.id == paste).content["rendered_content"])) < 1_000
    policy = KeepPolicy(keep_head=False, keep_last_steps=0, keep_user_chars=1_000)
    decision = post_compaction_context(
        live, open_turn_id="", policy=policy, read_content=history.read_content
    )
    assert ask in decision.keep and paste in decision.summarize
    with pytest.raises(ContextReadError):  # a reference with no reader is never guessed
        post_compaction_context(live, open_turn_id="", policy=policy)
