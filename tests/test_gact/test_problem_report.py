"""GET /v1/system/problem-report: the "Report a problem" page's report.

The report reuses /v1/health, carries the tail of the launcher's log files,
removes secrets on the server, and describes a picked conversation's failed
replies without its message text unless the person asks for it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.parts import Part
from clio_agent.gact.types import ErrorInfo, Message
from clio_agent.runtime import log_sources
from clio_agent.runtime.log_sources import LogCandidate

FAKE_KEY = "sk" + "-ant-api03-TestOnlyKeyZq8vT1xY2wK9mN4pR7sL0bC3"  # split for secret scanners


@pytest.fixture
def server_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    log = tmp_path / "clio-server.test.log"
    log.write_text(
        "".join(f"INFO request {i}\n" for i in range(1, 801))
        + f"ERROR provider rejected key api_key={FAKE_KEY}\n"
        + f"DEBUG Authorization: Bearer {FAKE_KEY}\n"
    )
    candidates = [
        LogCandidate("server", log, "CLIO_LOG"),
        LogCandidate("gact", tmp_path / "gact-stderr.log", "launcher"),
    ]
    monkeypatch.setattr(log_sources, "log_candidates", lambda: candidates)
    return log


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    return TestClient(build_app(sessions_path=tmp_path / "s.json", agent=None))


def _seed_failed_conversation(client: TestClient) -> str:
    app = client.app
    session = app.state.sessions.create(workspace_id="ws_default", title="Rainfall in Kansas")
    now = "2026-11-15T10:00:00Z"
    question = Message(
        id="q1",
        session_id=session.id,
        turn_id="q1",
        role="user",
        created_at=now,
        updated_at=now,
        parts=[Part(id="q1-text", type="text", text="My private question about wells")],
    )
    failed = Message(
        id="a1",
        session_id=session.id,
        turn_id="q1",
        role="assistant",
        created_at=now,
        updated_at=now,
        error_info=ErrorInfo(
            error="provider_model_not_found",
            message=f"model 'no-such-model' was rejected (key {FAKE_KEY})",
            recoverable=True,
        ),
    )
    app.state.messages[session.id] = [question, failed]
    return session.id


def test_report_has_version_health_and_a_redacted_log_tail(
    client: TestClient, server_log: Path
) -> None:
    body = client.get("/v1/system/problem-report", params={"client": "web"}).json()

    assert body["schema"] == "clio.problem_report.v1"
    assert body["about"]["version"]
    assert body["about"]["app_kind"] == "web"
    assert body["about"]["os"]
    assert body["health"]["overall_status"] in {"ready", "degraded", "unavailable"}
    assert body["health"]["rows"], "the health rows come from /v1/health itself"
    (source,) = body["logs"]["sources"]
    assert source["role"] == "server"
    assert len(source["lines"]) == 500
    assert source["earlier_lines"] is True
    assert source["lines"][-1] == "DEBUG Authorization: [redacted]"
    assert body["logs"]["searched"][0]["reason"] == "log_file_absent"
    assert FAKE_KEY not in body["text"]
    assert FAKE_KEY not in str(body)
    assert body["filename"].startswith("clio-problem-report-") and body["filename"].endswith(".txt")
    sections = [entry["section"] for entry in body["manifest"]]
    assert sections[:2] == ["about", "health"] and "log" in sections
    assert body["text"].startswith("CLIO problem report")
    assert "== Contents ==" in body["text"] and "== Secrets removed ==" in body["text"]


def test_log_tail_length_is_adjustable_and_bounded(client: TestClient, server_log: Path) -> None:
    short = client.get("/v1/system/problem-report", params={"lines": 20}).json()
    assert len(short["logs"]["sources"][0]["lines"]) == 20
    assert short["logs"]["lines_requested"] == 20
    full = client.get("/v1/system/problem-report", params={"lines": 5000}).json()
    assert len(full["logs"]["sources"][0]["lines"]) == 802
    assert full["logs"]["sources"][0]["earlier_lines"] is False
    assert client.get("/v1/system/problem-report", params={"lines": 5001}).status_code == 422
    assert client.get("/v1/system/problem-report", params={"lines": 0}).status_code == 422


def test_no_log_file_is_a_typed_reason_not_a_crash(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        log_sources,
        "log_candidates",
        lambda: [LogCandidate("server", tmp_path / "absent.log", "launcher")],
    )
    response = client.get("/v1/system/problem-report")
    assert response.status_code == 200
    logs = response.json()["logs"]
    assert logs["sources"] == []
    assert logs["degradation"]["reason"] == "no_log_files"
    assert logs["searched"] == [
        {"role": "server", "path": str(tmp_path / "absent.log"), "reason": "log_file_absent"}
    ]
    assert "no_log_files" in response.json()["text"]


def test_conversation_section_without_messages_has_outcomes_only(
    client: TestClient, server_log: Path
) -> None:
    sid = _seed_failed_conversation(client)
    body = client.get("/v1/system/problem-report", params={"session_id": sid}).json()

    conversation = body["conversation"]
    assert conversation["id"] == sid
    assert conversation["busy"] is False
    assert conversation["messages_included"] is False
    assert conversation["messages"] is None
    assert conversation["title"] is None
    (reply,) = conversation["recent_replies"]
    assert reply["outcome"] == "failed"
    assert reply["error_code"] == "provider_model_not_found"
    assert "no-such-model" in reply["error_message"]
    assert FAKE_KEY not in reply["error_message"]
    assert "private question" not in body["text"]
    assert "Rainfall in Kansas" not in body["text"]
    assert "provider_model_not_found" in body["text"]


def test_conversation_messages_only_when_asked(client: TestClient, server_log: Path) -> None:
    sid = _seed_failed_conversation(client)
    body = client.get(
        "/v1/system/problem-report", params={"session_id": sid, "include_messages": "true"}
    ).json()

    conversation = body["conversation"]
    assert conversation["messages_included"] is True
    assert conversation["title"] == "Rainfall in Kansas"
    assert conversation["messages"][0] == {
        "role": "user",
        "created_at": "2026-11-15T10:00:00Z",
        "text": "My private question about wells",
    }
    assert "My private question about wells" in body["text"]
    assert any(entry["section"] == "conversation" for entry in body["manifest"])


def test_unknown_conversation_is_a_typed_reason(client: TestClient, server_log: Path) -> None:
    body = client.get("/v1/system/problem-report", params={"session_id": "sess_nope"}).json()
    assert body["conversation"]["degradation"]["reason"] == "conversation_not_found"


def test_download_is_the_same_text_as_an_attachment(client: TestClient, server_log: Path) -> None:
    response = client.get("/v1/system/problem-report/download", params={"lines": 10})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "attachment" in response.headers["content-disposition"]
    assert "clio-problem-report-" in response.headers["content-disposition"]
    assert response.text.startswith("CLIO problem report")
    assert FAKE_KEY not in response.text
    assert "INFO request 800" in response.text


def test_planted_environment_key_is_redacted(
    client: TestClient, server_log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    planted = "planted-value-9f8e7d6c5b4a"
    monkeypatch.setenv("SOME_PROVIDER_API_KEY", planted)
    server_log.write_text(f"INFO started with {planted} in the environment\n")
    body = client.get("/v1/system/problem-report").json()
    assert planted not in body["text"]
    assert "INFO started with [redacted] in the environment" in body["text"]
