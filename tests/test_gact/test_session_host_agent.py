"""A session's own model selection starts its first turn with no global provider.

On a fresh install no provider is bound globally (the committed lm_studio default
is not a selection), so the server boots without a runtime host agent. Picking a
model in the composer is a complete choice: the first message builds the host
for that session's provider/model lazily -- no "apply in Settings" step.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact import session_host_agent
from clio_agent.gact.app import build_app

pytestmark = pytest.mark.usefixtures("host_agent_executor")

_CODEX = {"provider_id": "codex", "model_id": "gpt-5.5", "variant": "direct"}
_CODEX_REMOVED_SDK = {"provider_id": "codex", "model_id": "gpt-5.5", "variant": "sdk"}


@dataclass
class _Prediction:
    answer: str
    selected_expert: str = ""
    routing_rationale: str = ""
    route_source: str = ""
    route_reason: str = ""
    error_info: dict[str, Any] | None = None


class _HostAgent:
    """The runtime host the lazy build produces (records its turns)."""

    def __init__(self, provider_config: Any) -> None:
        self._provider_config = provider_config
        self.arc = None
        self.calls: list[str] = []

    def forward(self, question: str, session_id: str) -> Any:
        self.calls.append(question)
        return _Prediction(answer="hello from the session's model")


@pytest.fixture
def built(monkeypatch: pytest.MonkeyPatch) -> list[_HostAgent]:
    """Capture every lazily built host instead of constructing a real ClioAgent."""

    hosts: list[_HostAgent] = []

    async def _construct(app: Any, *, arc: Any, provider_config: Any) -> _HostAgent:
        host = _HostAgent(provider_config)
        hosts.append(host)
        return host

    async def _arc(app: Any) -> None:
        return None

    monkeypatch.setattr(session_host_agent, "construct_agent_with_relay", _construct)
    monkeypatch.setattr(session_host_agent, "process_arc_off_loop", _arc)
    return hosts


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    app = build_app(sessions_path=tmp_path / "sessions.json", agent=None)
    app.state.provider_catalog = {
        "providers": [
            {
                "id": "codex",
                "health": "ready",
                "models": [
                    {
                        "model_id": "gpt-5.5",
                        "transport": "direct",
                        "availability": "available",
                        "modalities": ["text"],
                        "evidence": {"live": True, "generated_at": "2026-09-26T00:00:00+00:00"},
                    }
                ],
            }
        ]
    }
    with TestClient(app) as test_client:
        yield test_client


def _session(client: TestClient, model: dict[str, str] | None = None) -> str:
    body: dict[str, Any] = {"title": "fresh"}
    if model is not None:
        body["model"] = model
    return client.post("/v1/sessions", json=body).json()["id"]


def test_first_message_with_a_model_ref_builds_the_host_and_runs(
    client: TestClient, built: list[_HostAgent]
) -> None:
    from .conftest import complete_turn

    sid = _session(client)
    assistant = complete_turn(client, sid, "hi", json_override={"model": _CODEX})

    assert len(built) == 1
    cfg = built[0]._provider_config
    assert (cfg.provider_id, cfg.model) == ("codex", "gpt-5.5")
    assert built[0].calls == ["hi"]
    assert client.app.state.agent is built[0]
    assert assistant.get("error_info") is None
    assert "hello from the session's model" in str(assistant["parts"])


def test_a_session_default_model_is_enough(client: TestClient, built: list[_HostAgent]) -> None:
    from .conftest import complete_turn

    sid = _session(client, model=_CODEX)
    complete_turn(client, sid, "hi")

    assert [host._provider_config.model for host in built] == ["gpt-5.5"]


def test_later_turns_reuse_the_host(client: TestClient, built: list[_HostAgent]) -> None:
    from .conftest import complete_turn

    sid = _session(client)
    complete_turn(client, sid, "one", json_override={"model": _CODEX})
    complete_turn(client, sid, "two", json_override={"model": _CODEX})

    assert len(built) == 1
    assert len(built[0].calls) == 2
    assert built[0].calls[1].endswith("two")


def test_no_model_anywhere_still_reports_no_agent(
    client: TestClient, built: list[_HostAgent]
) -> None:
    sid = _session(client)
    resp = client.post(f"/v1/sessions/{sid}/messages", json={"text": "hi"})

    assert resp.status_code == 503
    assert resp.json()["error"]["error"] == "agent_not_available"
    assert built == []


def test_a_failed_host_build_is_a_typed_refusal(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _boom(app: Any, *, arc: Any, provider_config: Any) -> Any:
        raise RuntimeError("provider exploded")

    async def _arc(app: Any) -> None:
        return None

    monkeypatch.setattr(session_host_agent, "construct_agent_with_relay", _boom)
    monkeypatch.setattr(session_host_agent, "process_arc_off_loop", _arc)
    sid = _session(client)
    resp = client.post(
        f"/v1/sessions/{sid}/messages",
        json={"parts": [{"type": "text", "text": "hi"}], "model": _CODEX},
    )

    assert resp.status_code == 503
    inner = resp.json()["error"]
    assert inner["error"] == "agent_not_available"
    assert "provider exploded" in inner["details"]["agent_init_error"]
    assert client.get(f"/v1/sessions/{sid}/messages").json()["messages"] == []


def test_a_ref_naming_the_removed_sdk_transport_builds_nothing_and_says_why(
    client: TestClient, built: list[_HostAgent]
) -> None:
    sid = _session(client)
    resp = client.post(
        f"/v1/sessions/{sid}/messages",
        json={"parts": [{"type": "text", "text": "hi"}], "model": _CODEX_REMOVED_SDK},
    )

    assert resp.status_code == 503
    inner = resp.json()["error"]
    assert inner["error"] == "agent_not_available"
    assert "The Codex SDK path was removed" in inner["details"]["agent_init_error"]
    assert built == []


def test_with_a_host_a_ref_naming_the_removed_sdk_transport_is_a_typed_400(
    client: TestClient, built: list[_HostAgent]
) -> None:
    from .conftest import complete_turn

    sid = _session(client)
    complete_turn(client, sid, "hi", json_override={"model": _CODEX})
    resp = client.post(
        f"/v1/sessions/{sid}/messages",
        json={"parts": [{"type": "text", "text": "again"}], "model": _CODEX_REMOVED_SDK},
    )

    assert resp.status_code == 400
    inner = resp.json()["error"]
    assert inner["error"] == "model_transport_removed"
    assert inner["message"].startswith("The Codex SDK path was removed")
    assert built[0].calls == ["hi"]
