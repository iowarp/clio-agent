"""A conversation is named after the person's first message, never over a chosen name."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.session_auto_title import derive_title, is_placeholder_title

pytestmark = pytest.mark.usefixtures("host_agent_executor")


@dataclass
class _Pred:
    answer: str = "ok"
    selected_expert: str = "data_expert"
    routing_rationale: str = ""


class _Agent:
    def forward(self, question: str, session_id: str) -> _Pred:
        return _Pred()


def _client(tmp_path: Path) -> TestClient:
    return TestClient(build_app(sessions_path=tmp_path / "sessions.json", agent=_Agent()))


def _title(client: TestClient, sid: str) -> str:
    return str(client.get(f"/v1/sessions/{sid}").json()["title"])


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Convert report.docx to PDF", "Convert report.docx to PDF"),
        ("\n\n  ## Summarize   the README  \nmore detail", "Summarize the README"),
        ("- 1. list item", "list item"),
        ("   \n\t", ""),
    ],
)
def test_title_is_the_first_meaningful_line(text: str, expected: str) -> None:
    assert derive_title(text) == expected


def test_long_titles_end_at_a_word_with_an_ellipsis() -> None:
    title = derive_title("Please read every file in this workspace and explain " * 3)
    assert len(title) <= 60
    assert title.endswith("…")
    assert not title.endswith(" …")
    assert "explain" in title


@pytest.mark.parametrize("title", ["", "New conversation", "new  conversation", "session abc123"])
def test_placeholders_are_recognized(title: str) -> None:
    assert is_placeholder_title(title, "ses_xyzabc123")


@pytest.mark.parametrize("title", ["GitHub", "My analysis", "session 999999"])
def test_chosen_names_are_not_placeholders(title: str) -> None:
    assert not is_placeholder_title(title, "ses_xyzabc123")


def test_first_message_names_a_new_conversation_and_persists(tmp_path: Path) -> None:
    from .conftest import complete_turn

    with _client(tmp_path) as client:
        sid = client.post("/v1/sessions", json={"title": "New conversation"}).json()["id"]
        complete_turn(client, sid, "Convert the attached report to a PDF\nThanks")
        assert _title(client, sid) == "Convert the attached report to a PDF"
        complete_turn(client, sid, "Now make it landscape")
        assert _title(client, sid) == "Convert the attached report to a PDF"
        listed = client.get("/v1/sessions").json()
        rows = listed["sessions"] if isinstance(listed, dict) else listed
        assert {row["id"]: row["title"] for row in rows}[sid] == (
            "Convert the attached report to a PDF"
        )
    with _client(tmp_path) as reloaded:
        assert _title(reloaded, sid) == "Convert the attached report to a PDF"


def test_a_chosen_title_is_never_replaced(tmp_path: Path) -> None:
    from .conftest import complete_turn

    with _client(tmp_path) as client:
        sid = client.post("/v1/sessions", json={"title": "Budget review"}).json()["id"]
        complete_turn(client, sid, "hello there")
        assert _title(client, sid) == "Budget review"


def test_an_idempotent_replay_does_not_rename(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        sid = client.post("/v1/sessions", json={"title": "New conversation"}).json()["id"]
        body = {"parts": [{"type": "text", "text": "first ask"}], "client_message_id": "m-1"}
        assert client.post(f"/v1/sessions/{sid}/messages", json=body).status_code == 202
        client.patch(f"/v1/sessions/{sid}", json={"title": "Renamed by me"})
        replay = client.post(f"/v1/sessions/{sid}/messages", json=body)
        assert replay.status_code == 200, replay.text
        assert _title(client, sid) == "Renamed by me"


def test_a_naming_failure_leaves_the_message_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    with _client(tmp_path) as client:
        sid = client.post("/v1/sessions", json={"title": "New conversation"}).json()["id"]
        store = client.app.state.sessions
        real_update = store.update

        def failing_update(session_id: str, **fields: object) -> object:
            if "title" in fields:
                raise OSError("disk full")
            return real_update(session_id, **fields)

        body = {"parts": [{"type": "text", "text": "first ask"}]}
        with (
            monkeypatch.context() as patched,
            caplog.at_level("WARNING", logger="clio_agent.gact.session_auto_title"),
        ):
            patched.setattr(store, "update", failing_update)
            accepted = client.post(f"/v1/sessions/{sid}/messages", json=body)
        assert accepted.status_code == 202, accepted.text
        assert "session_auto_title_failed" in caplog.text
        assert _title(client, sid) == "New conversation"
