"""CLI completion selection follows confirmed input identities, not event timing."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.client.events import MessageCompleted, StreamEvent
from clio_agent.client.types import UserQuestion
from clio_agent.ui.cli import ClioAgentCLI
from tests.test_ui.test_cli_pause import _FakeStream, _recording_console


def completed(turn: str, message: str, identity: int, reason: str = "end_turn") -> MessageCompleted:
    """Create a server-shaped segment completion for deterministic client tests."""
    return MessageCompleted(
        id=identity,
        type="message.completed",
        payload={"turn_id": turn, "message_id": message, "stop_reason": reason},
    )


@pytest.mark.parametrize("answer_turn", ["msg_1", "turn_wake"])
@pytest.mark.parametrize("child", [False, True])
@pytest.mark.parametrize("old_reason", ["end_turn", "error", "cancelled"])
def test_answered_question_uses_its_owner_input_and_completion(
    monkeypatch: pytest.MonkeyPatch, answer_turn: str, child: bool, old_reason: str
) -> None:
    """An answer during closure must not return the old segment or another pending Question."""
    owner = "child" if child else "root"
    target = "child_wake" if child else answer_turn
    question = UserQuestion(
        id="question_current",
        session_id=owner,
        owner_session_id=owner,
        attended_session_id="root",
        prompt="Choose the dataset",
        status="pending",
    )
    events = {
        "root": [
            _FakeStream(
                [
                    completed("unrelated_old", "unrelated", 1),
                    StreamEvent(id=2, type="user_question.created", payload=question.model_dump()),
                ],
                last_event_id=2,
            ),
            _FakeStream(
                [
                    completed(
                        "msg_1",
                        "original",
                        3,
                        old_reason if answer_turn == "turn_wake" else "end_turn",
                    ),
                    completed("turn_wake", "root_answer", 4),
                ],
                last_event_id=4,
            ),
        ],
        "child": [
            _FakeStream(
                [
                    completed("child_old", "child_before_answer", 1, old_reason),
                    completed("child_wake", "child_after_answer", 2),
                ],
                last_event_id=2,
            )
        ],
    }
    receipt_visible = False
    calls: list[tuple[str, Any]] = []

    def stream(sid: str, *, last_event_id: int | None = None) -> _FakeStream:
        calls.append(("events", (sid, last_event_id)))
        return events[sid].pop(0)

    def read(sid: str, handle: str) -> UserQuestion:
        nonlocal receipt_visible
        calls.append(("question", (sid, handle)))
        assert handle == question.id and sid in {"root", owner}
        if question.status == "answered":
            # On ask-and-yield/child paths the earlier completion is already
            # queued when the answer is posted. The receipt becomes visible
            # only at the next model input, before the later completion.
            if receipt_visible or target == "msg_1":
                question.answer_input = {"session_id": owner, "turn_id": target}
            receipt_visible = True
        return question

    def answer(sid: str, handle: str, **_kwargs: Any) -> UserQuestion:
        assert sid == "root" and handle == question.id
        question.status = "answered"
        return question

    def fetch(sid: str, identity: str) -> SimpleNamespace:
        calls.append(("message", (sid, identity)))
        return SimpleNamespace(id=identity)

    client = SimpleNamespace(
        sessions=SimpleNamespace(events=stream, question=read, answer_question=answer),
        messages=SimpleNamespace(get=fetch),
    )
    cli = ClioAgentCLI(client, console=_recording_console())
    monkeypatch.setattr(cli, "_prompt", lambda *_args, **_kwargs: "chosen")
    result, message, error = cli._consume_turn("root", "msg_1")
    assert error is None
    assert result is not None and message is not None
    expected = "original" if child or answer_turn == "msg_1" else "root_answer"
    assert message.id == expected
    if child:
        assert ("events", ("child", None)) in calls
        assert ("message", ("child", "child_after_answer")) in calls
        assert cli._event_cursor == 4
    assert ("message", ("root", "unrelated")) not in calls


def test_without_answered_questions_completion_matches_the_submitted_input() -> None:
    """A stale earlier completion cannot finish a later ordinary CLI request."""
    client = SimpleNamespace(
        sessions=SimpleNamespace(
            events=lambda *_args, **_kwargs: _FakeStream(
                [
                    completed("old", "wrong", 1),
                    completed("requested", "right", 2),
                ]
            )
        ),
        messages=SimpleNamespace(get=lambda _sid, identity: SimpleNamespace(id=identity)),
    )
    cli = ClioAgentCLI(client, console=_recording_console())
    event, message, error = cli._consume_turn("root", "requested")
    assert error is None and event is not None and message.id == "right"


def test_each_answered_question_needs_its_own_confirmed_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Collecting the later answer must not hide an earlier still-pending answer."""
    questions = {
        handle: UserQuestion(id=handle, session_id="root", owner_session_id="root", prompt=handle)
        for handle in ("first", "second")
    }
    feeds = [
        _FakeStream([StreamEvent(type="user_question.created", payload={"id": handle})])
        for handle in questions
    ]
    feeds.append(
        _FakeStream(
            [
                completed("second_input", "second_only", 3),
                completed("first_input", "both_consumed", 4),
            ]
        )
    )
    reads = 0

    def read(_sid: str, handle: str) -> UserQuestion:
        nonlocal reads
        question = questions[handle]
        if question.status == "answered":
            if handle == "first":
                reads += 1
            question.answer_input = (
                {"turn_id": handle + "_input"} if handle == "second" or reads > 1 else None
            )
        return question

    def answer(_sid: str, handle: str, **_kwargs: Any) -> UserQuestion:
        questions[handle].status = "answered"
        return questions[handle]

    client = SimpleNamespace(
        sessions=SimpleNamespace(
            events=lambda *_args, **_kwargs: feeds.pop(0),
            question=read,
            answer_question=answer,
        ),
        messages=SimpleNamespace(get=lambda _sid, identity: SimpleNamespace(id=identity)),
    )
    cli = ClioAgentCLI(client, console=_recording_console())
    monkeypatch.setattr(cli, "_prompt", lambda *_args, **_kwargs: "selected")
    event, message, error = cli._consume_turn("root", "submitted")
    assert error is None and event is not None and message.id == "both_consumed"
    assert all(question.status == "answered" for question in questions.values())
    assert reads == 2


@pytest.mark.parametrize("target", ["existing_segment", "later_wake"])
def test_ordinary_busy_input_uses_confirmed_receipt(target: str) -> None:
    """The ack's message ID is not necessarily the segment that consumes it."""
    reads = 0

    def fetch(_sid: str, identity: str) -> SimpleNamespace:
        nonlocal reads
        if identity == "new-feedback":
            reads += 1
            return SimpleNamespace(
                id=identity,
                metadata={"recipient_input": {"turn_id": target} if reads > 1 else None},
            )
        return SimpleNamespace(id=identity)

    client = SimpleNamespace(
        sessions=SimpleNamespace(
            events=lambda *_args, **_kwargs: _FakeStream(
                [
                    completed("unrelated", "wrong", 1),
                    completed(target, "uses-feedback", 2),
                ]
            )
        ),
        messages=SimpleNamespace(get=fetch),
    )
    cli = ClioAgentCLI(client, console=_recording_console())
    event, message, error = cli._consume_turn("root", "new-feedback")
    assert error is None and event is not None and message.id == "uses-feedback"
    assert reads == 2


def test_cli_collects_bundle_fields_and_submits_one_atomic_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    question = UserQuestion(
        id="bundle",
        session_id="root",
        prompt="Analysis choices",
        status="pending",
        metadata={
            "question_bundle": {
                "questions": [
                    {
                        "id": "view",
                        "question": "Which view?",
                        "options": [{"value": "table", "label": "Station table"}],
                        "allow_freeform": False,
                    },
                    {
                        "id": "notes",
                        "question": "What matters?",
                        "options": [],
                        "allow_freeform": True,
                    },
                ]
            }
        },
    )
    submitted = []

    def answer(sid: str, handle: str, **kwargs: Any) -> UserQuestion:
        assert sid == "root" and handle == "bundle"
        submitted.append(kwargs)
        return question.model_copy(update={"status": "answered"})

    client = SimpleNamespace(
        sessions=SimpleNamespace(
            question=lambda *_args: question,
            answer_question=answer,
        )
    )
    cli = ClioAgentCLI(client, console=_recording_console())
    answers = iter(["invalid", "table", "Uncertainty"])
    monkeypatch.setattr(cli, "_prompt", lambda *_args, **_kwargs: next(answers))
    error, answered = cli._resolve_waiting_user("root", interactive=True, question_id="bundle")
    assert error is None and answered.status == "answered"
    assert submitted == [
        {
            "metadata": {
                "answers": {
                    "view": {"answer": "", "selected_options": ["table"]},
                    "notes": {"answer": "Uncertainty", "selected_options": []},
                }
            }
        }
    ]


def test_cli_preserves_single_question_choice_restriction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SDK decoding retains the server's freeform restriction and offered option identity."""
    question = UserQuestion.model_validate(
        {
            "id": "choice",
            "session_id": "root",
            "prompt": "Which view?",
            "kind": "choice",
            "allow_freeform": False,
            "options": [{"value": "table", "label": "Station table"}],
        }
    )
    submitted: list[dict[str, Any]] = []

    def answer(_sid: str, _handle: str, **kwargs: Any) -> UserQuestion:
        submitted.append(kwargs)
        return question.model_copy(update={"status": "answered"})

    client = SimpleNamespace(
        sessions=SimpleNamespace(
            question=lambda *_args: question,
            answer_question=answer,
        )
    )
    cli = ClioAgentCLI(client, console=_recording_console())
    replies = iter(["unlisted", "table"])
    monkeypatch.setattr(cli, "_prompt", lambda *_args, **_kwargs: next(replies))
    error, answered = cli._resolve_waiting_user("root", interactive=True, question_id="choice")
    assert error is None and answered.status == "answered"
    assert submitted == [{"answer": "", "selected_options": ["table"]}]
