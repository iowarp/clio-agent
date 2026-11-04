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
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from clio_agent.gact.compaction_policy import KeepPolicy, post_compaction_context


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
    newest_two = sum(len(str(s.content)) for s in live if s.id in {"q2", "steer"})
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
