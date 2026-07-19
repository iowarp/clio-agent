"""The CLI's pause-aware turn loop (#799/#800 defect fixes).

Covers the three defects fixed in :mod:`clio_agent.ui.cli`:

* DEFECT 1 — the interactive CLI must NOT hang when a turn pauses to ask a
  clarifying question (``session.status_changed`` → ``waiting_user`` with no
  ``message.completed``) or to request a tool permission
  (``permission.requested``). The loop resolves the pause and resumes, and is
  hard-bounded so a server that never settles cannot spin it forever.
* DEFECT 2 — the ``--session`` title threads through to the created session.
* DEFECT 3 — a failed authoritative message fetch after a clean completion is
  surfaced as a structured error, never a silently empty answer.

The strongest DEFECT 1 tests drive a REAL ask_user pause / permission request
through the in-process gact app (via :class:`StreamingASGITransport`), exactly
like the server would; the bound test uses injected fakes so it stays fast and
deterministic.
"""

from __future__ import annotations

import io
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import pytest
from rich.console import Console

from clio_agent.gact.app import build_app
from clio_agent.sdk import (
    ClioClient,
    ClioConnectionError,
    SessionStatusChanged,
    UserQuestion,
)
from clio_agent.ui.cli import ClioAgentCLI, run_query
from tests.test_sdk.conftest import StreamingASGITransport, StubAgent, _fresh_arc

# #948 S4b: default sessions run the blueprint react ``main``; route it to each
# test's ``build_app(agent=...)`` host/stub fake.
pytestmark = pytest.mark.usefixtures("host_agent_executor")

# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _recording_console() -> Console:
    return Console(file=io.StringIO(), record=True, width=200, force_terminal=False)


def _make_cli(client: ClioClient, *, verbose: bool = False) -> tuple[ClioAgentCLI, Console]:
    console = _recording_console()
    return ClioAgentCLI(client, verbose=verbose, console=console), console


def _run_with_timeout(fn: Callable[[], Any], timeout: float = 30.0) -> Any:
    """Run ``fn`` on a worker thread; fail (not hang) if it doesn't return.

    This makes the "no infinite hang" property an explicit, fast assertion:
    the buggy loop would spin forever consuming heartbeats, so a regression
    fails here in ``timeout`` seconds instead of wedging the whole suite.
    """

    box: dict[str, Any] = {}

    def _target() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised on the main thread
            box["error"] = exc

    worker = threading.Thread(target=_target, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise AssertionError(f"call did not complete within {timeout}s (hang regression?)")
    if "error" in box:
        raise box["error"]
    return box["value"]


@dataclass
class _AskUserPred:
    """A prediction that pauses the turn to ask the user a question."""

    ask_user: dict[str, Any]
    answer: str = ""
    selected_expert: str = ""
    routing_rationale: str = ""


@dataclass
class _AnswerPred:
    answer: str
    selected_expert: str = "data_expert"
    routing_rationale: str = "resumed"


@dataclass
class AskThenAnswerAgent:
    """Asks a clarifying question on the first turn, answers on the resume.

    Mirrors ``tests/test_gact/test_ask_user_retry.py::_AskUserAgent`` but as a
    small dataclass so the CLI test can drive a real pause/resume cycle.
    """

    question: str
    answer: str
    calls: list[tuple[str, str]] = field(default_factory=list)

    def forward(self, question: str, session_id: str) -> Any:
        self.calls.append((question, session_id))
        if len(self.calls) == 1:
            return _AskUserPred(ask_user={"action": "ask_user", "question": self.question})
        return _AnswerPred(answer=self.answer)


class _FakeStream:
    """Minimal stand-in for :class:`~clio_agent.sdk.EventStream`."""

    def __init__(self, events: list[Any], *, last_event_id: int = 1) -> None:
        self._events = events
        self.last_event_id = last_event_id

    def __enter__(self) -> _FakeStream:
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def __iter__(self) -> Iterator[Any]:
        return iter(self._events)


@pytest.fixture()
def make_client(tmp_path: Path) -> Iterator[Callable[[Any], ClioClient]]:
    """Factory: build an in-process gact app around a given agent and return a
    client wired over the streaming ASGI transport (cleaned up on teardown)."""

    transports: list[StreamingASGITransport] = []
    clients: list[ClioClient] = []

    def _make(agent: Any) -> ClioClient:
        idx = len(clients)
        app = build_app(
            sessions_path=tmp_path / f"sessions_{idx}.json",
            agent=agent,
            arc=_fresh_arc(tmp_path / f"inst_{idx}"),
        )
        transport = StreamingASGITransport(app)
        transports.append(transport)
        client = ClioClient("http://testserver", transport=transport)
        clients.append(client)
        return client

    yield _make

    for client in clients:
        client.close()
    for transport in transports:
        transport.close()


# --------------------------------------------------------------------------- #
# DEFECT 1 — ask_user pause is driven, not hung
# --------------------------------------------------------------------------- #


def test_ask_question_drives_ask_user_pause_and_resumes(
    make_client: Callable[[Any], ClioClient], monkeypatch: Any
) -> None:
    agent = AskThenAnswerAgent(question="Which dataset?", answer="Ratio was 3.2x on HDF5.")
    client = make_client(agent)
    cli, _ = _make_cli(client)
    monkeypatch.setattr(cli, "_prompt", lambda *a, **k: "the HDF5 dataset")

    result = _run_with_timeout(lambda: cli.ask_question("How well did it compress?"))

    # No hang, and the agent was invoked twice: the initial turn + the resume.
    assert len(agent.calls) == 2
    # The resumed answer surfaced cleanly.
    assert result["error_info"] is None
    assert "3.2x" in result["answer"]
    # The clarifying question was actually answered via the SDK.
    questions = client.sessions.questions(result["session_id"])
    assert questions, "the pause created a user question"
    assert all(q.status == "answered" for q in questions)
    assert questions[0].answer == "the HDF5 dataset"


def test_ask_user_pause_aborts_structured_in_non_interactive(
    make_client: Callable[[Any], ClioClient],
) -> None:
    """``--query`` (non-interactive) must NOT hang on an ask_user pause: it
    surfaces a structured 'input_required' error and stops."""

    agent = AskThenAnswerAgent(question="Which dataset?", answer="never reached")
    client = make_client(agent)
    cli, _ = _make_cli(client)

    result = _run_with_timeout(lambda: cli.ask_question("q", interactive=False))

    assert result["error_info"] is not None
    assert result["error_info"]["error"] == "input_required"
    assert "Which dataset?" in result["error_info"]["message"]
    # Only the initial forward ran — we aborted instead of answering + resuming.
    assert len(agent.calls) == 1


# --------------------------------------------------------------------------- #
# DEFECT 1 — permission pause is driven, not hung
# --------------------------------------------------------------------------- #


def test_ask_question_drives_permission_pause(
    make_client: Callable[[Any], ClioClient], monkeypatch: Any
) -> None:
    agent = StubAgent(
        answer="Command finished.",
        permissions_requested=[
            {"id": "perm_abc", "tool_call": {"tool_name": "shell.run"}, "summary": "run ls"}
        ],
    )
    client = make_client(agent)
    cli, _ = _make_cli(client)
    monkeypatch.setattr(cli, "_prompt", lambda *a, **k: "y")

    responded: list[tuple[str, str]] = []
    orig_respond = client.permissions.respond

    def _spy_respond(permission_id: str, action: str) -> None:
        responded.append((permission_id, action))
        return orig_respond(permission_id, action)  # type: ignore[arg-type]

    monkeypatch.setattr(client.permissions, "respond", _spy_respond)

    result = _run_with_timeout(lambda: cli.ask_question("do the thing"))

    # The permission was resolved (allow, since the prompt said "y"), and the
    # turn then completed — no hang.
    assert responded == [("perm_abc", "allow")]
    assert result["error_info"] is None
    assert "Command finished." in result["answer"]


def test_permission_pause_auto_denies_in_non_interactive(
    make_client: Callable[[Any], ClioClient], monkeypatch: Any
) -> None:
    agent = StubAgent(
        answer="Command finished.",
        permissions_requested=[
            {"id": "perm_xyz", "tool_call": {"tool_name": "shell.run"}, "summary": "run rm"}
        ],
    )
    client = make_client(agent)
    cli, _ = _make_cli(client)

    responded: list[tuple[str, str]] = []
    orig_respond = client.permissions.respond

    def _spy_respond(permission_id: str, action: str) -> None:
        responded.append((permission_id, action))
        return orig_respond(permission_id, action)  # type: ignore[arg-type]

    monkeypatch.setattr(client.permissions, "respond", _spy_respond)

    result = _run_with_timeout(lambda: cli.ask_question("do it", interactive=False))

    # Non-interactive auto-denies (announced, not silent) and the turn resumes.
    assert responded == [("perm_xyz", "deny")]
    assert result["error_info"] is None


# --------------------------------------------------------------------------- #
# DEFECT 1 — the pause loop is hard-bounded (injected fakes, fast + determinist)
# --------------------------------------------------------------------------- #


def test_consume_turn_bounds_pause_rounds(client: ClioClient, monkeypatch: Any) -> None:
    """A server that answers every question with another question can never
    spin the CLI forever: the loop aborts with a structured error after
    ``MAX_PAUSE_ROUNDS``."""

    cli, _ = _make_cli(client)
    monkeypatch.setattr(cli, "_prompt", lambda *a, **k: "answer")

    def _fake_events(session_id: str, *, last_event_id: int | None = None) -> _FakeStream:
        event = SessionStatusChanged(
            id=7, type="session.status_changed", payload={"status": "waiting_user"}
        )
        return _FakeStream([event])

    pending = UserQuestion(id="q1", session_id="sess_x", prompt="again?", status="pending")
    answered: list[str] = []

    def _fake_answer(session_id: str, question_id: str, **kwargs: Any) -> UserQuestion:
        answered.append(question_id)
        return pending

    monkeypatch.setattr(cli.client.sessions, "events", _fake_events)
    monkeypatch.setattr(cli.client.sessions, "questions", lambda sid, **k: [pending])
    monkeypatch.setattr(cli.client.sessions, "answer_question", _fake_answer)

    completed, message, error_info = cli._consume_turn("sess_x", "msg_1", interactive=True)

    assert completed is None
    assert message is None
    assert error_info is not None
    assert error_info["error"] == "too_many_pauses"
    # It answered exactly the bounded number of times, then gave up.
    assert len(answered) == cli.MAX_PAUSE_ROUNDS


# --------------------------------------------------------------------------- #
# DEFECT 2 — session title threads through
# --------------------------------------------------------------------------- #


def test_ask_question_uses_session_title(client: ClioClient) -> None:
    cli, _ = _make_cli(client)

    result = cli.ask_question("hello", session_title="My Special Title")

    sess = cli.client.sessions.get(result["session_id"])
    assert sess.title == "My Special Title"


def test_run_query_threads_session_title(client: ClioClient, monkeypatch: Any) -> None:
    import clio_agent.ui.cli as cli_mod

    monkeypatch.setattr(cli_mod, "boot_client", lambda *a, **k: client)
    # run_query closes the client in `finally`; keep the shared fixture alive.
    monkeypatch.setattr(client, "close", lambda: None)

    run_query("hello there", session_title="Threaded Title", port=0)

    titles = [s.title for s in client.sessions.list()]
    assert "Threaded Title" in titles


# --------------------------------------------------------------------------- #
# DEFECT 3 — a failed message fetch is surfaced, not swallowed
# --------------------------------------------------------------------------- #


def test_message_fetch_failure_surfaces_structured_error(
    client: ClioClient, monkeypatch: Any
) -> None:
    cli, console = _make_cli(client)

    def _boom(session_id: str, message_id: str) -> Any:
        raise ClioConnectionError("ledger unreachable")

    monkeypatch.setattr(cli.client.messages, "get", _boom)

    result = cli.ask_question("please answer")

    assert result["error_info"] is not None
    assert result["error_info"]["error"] == "message_fetch_failed"
    assert "could not be fetched" in result["error_info"]["message"]

    cli._render_answer(result)
    out = console.export_text()
    assert "CLIO Error" in out
    assert "(no answer)" not in out
