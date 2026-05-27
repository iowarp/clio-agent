from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    return TestClient(build_app(sessions_path=tmp_path / "sessions.json"))


def test_capabilities_advertise_prompt_registry(client: TestClient) -> None:
    caps = client.get("/v1/capabilities").json()["capabilities"]

    assert caps["x_clio_prompt_registry"] is True


def test_builtin_prompts_are_listed_and_resolvable(client: TestClient) -> None:
    listed = client.get("/v1/prompts")

    assert listed.status_code == 200
    prompts = {row["id"]: row for row in listed.json()["prompts"]}
    assert "clio.chat" in prompts
    assert "default" in prompts["clio.chat"]["profiles"]

    resolved = client.get("/v1/prompts/clio.chat").json()["prompt"]
    assert resolved["id"] == "clio.chat"
    assert resolved["profile"] == "default"
    assert "CLIO" in resolved["text"]
    assert resolved["scope"] == "builtin"
    assert resolved["checksum"]


def test_put_prompt_saves_external_profile_and_resolution_uses_it(client: TestClient) -> None:
    resp = client.put(
        "/v1/prompts/clio.chat",
        json={
            "profile": "heavy",
            "title": "Heavy chat",
            "description": "More explicit behavior",
            "text": "Use detailed but grounded CLIO behavior.",
            "provider": "openai",
            "model": "gpt-5.1",
            "metadata": {"edited_by": "test"},
        },
    )

    assert resp.status_code == 200, resp.text
    saved = resp.json()["prompt"]
    assert saved["profiles"]["heavy"]["text"] == "Use detailed but grounded CLIO behavior."

    resolved = client.get("/v1/prompts/clio.chat?profile=heavy").json()["prompt"]
    assert resolved["text"] == "Use detailed but grounded CLIO behavior."
    assert resolved["title"] == "Heavy chat"
    assert resolved["scope"] == "global"
    assert resolved["provider"] == "openai"
    assert resolved["model"] == "gpt-5.1"
    assert resolved["metadata"]["edited_by"] == "test"


def test_put_prompt_rejects_invalid_profile(client: TestClient) -> None:
    resp = client.put(
        "/v1/prompts/clio.chat",
        json={"profile": "../bad", "text": "bad"},
    )

    assert resp.status_code == 422
    assert resp.json()["error"]["error"] == "bad_request"


def test_user_agent_runtime_uses_resolved_prompt_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from .conftest import complete_turn

    seen: dict[str, Any] = {}

    async def fake_stream_unavailable(
        app: Any,
        enriched_text: str,
        sid: str,
        emit_chunk: Any,
        **kwargs: Any,
    ) -> None:
        del enriched_text, emit_chunk, kwargs
        from clio_agent.gact.app import _record_stream_fallback

        _record_stream_fallback(app, sid, "dynamic_prompt_stream_unavailable")
        return None

    def fake_prompt_agent(
        base_agent: Any,
        agent_def: Any,
        question: str,
        session_id: str,
    ) -> Any:
        del base_agent
        seen.update(
            {
                "system_prompt": agent_def.system_prompt,
                "provider": agent_def.default_provider,
                "model": agent_def.default_model,
                "metadata": agent_def.metadata,
                "question": question,
                "session_id": session_id,
            }
        )
        return type(
            "Pred",
            (),
            {
                "answer": "PROMPT_PROFILE_OK",
                "selected_expert": agent_def.id,
                "routing_rationale": "selected registered prompt-backed agent",
            },
        )()

    monkeypatch.setattr("clio_agent.gact.app._try_streamed_forward", fake_stream_unavailable)
    monkeypatch.setattr("clio_agent.gact.app._run_prompt_user_agent", fake_prompt_agent)

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=object())
    with TestClient(app) as c:
        saved = c.put(
            "/v1/prompts/clio.reviewer",
            json={
                "profile": "light",
                "title": "Reviewer light",
                "text": "Use the external reviewer prompt.",
                "provider": "openai",
                "model": "gpt-5-mini",
            },
        )
        assert saved.status_code == 200, saved.text
        created = c.post(
            "/v1/agents",
            json={
                "id": "reviewer",
                "title": "Reviewer",
                "system_prompt": "This inline prompt should be overridden.",
                "metadata": {
                    "prompt_id": "clio.reviewer",
                    "prompt_profile": "light",
                },
            },
        )
        assert created.status_code == 201, created.text
        sid = c.post(
            "/v1/sessions",
            json={"title": "prompt-backed agent", "agent": {"id": "reviewer"}},
        ).json()["id"]
        assistant = complete_turn(c, sid, "review this")

    assert seen["question"] == "review this"
    assert seen["system_prompt"] == "Use the external reviewer prompt."
    assert seen["provider"] == "openai"
    assert seen["model"] == "gpt-5-mini"
    resolution = seen["metadata"]["prompt_resolution"]
    assert resolution["id"] == "clio.reviewer"
    assert resolution["profile"] == "light"
    assert resolution["scope"] == "global"
    assert resolution["status"] == "resolved"
    assert assistant["metadata"]["prompt_resolution"]["id"] == "clio.reviewer"
    assert assistant["metadata"]["prompt_resolution"]["profile"] == "light"
    assert assistant["parts"][1]["text"] == "PROMPT_PROFILE_OK"
