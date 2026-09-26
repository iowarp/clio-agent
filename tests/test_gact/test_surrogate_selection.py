"""Surrogates are listed, but never bound as the chat model (typed refusal).

A surrogate (embedding, rerank, classification, generation, ...) is a
first-class catalog row. ``PUT /v1/providers/lm`` is the chat-model SELECTION,
so choosing one there answers the typed ``surrogate_model_not_chat`` error --
before any provider swap starts. A general model is not refused.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.model_selection import SURROGATE_NOT_CHAT


def _catalog() -> dict[str, Any]:
    def row(model_id: str, task: str) -> dict[str, Any]:
        return {
            "model_id": model_id,
            "availability": "available",
            "modalities": ["text"],
            "task": task,
            "evidence": {"evidenced": True, "source": "live", "generated_at": ""},
        }

    return {
        "providers": [
            {
                "id": "openrouter",
                "health": "ready",
                "models": [
                    row("~typesafe/jev-latest", "text-classification"),
                    row("openrouter/free", "text-generation"),
                ],
            }
        ]
    }


def test_binding_a_surrogate_as_the_chat_model_is_refused_with_a_typed_reason(
    tmp_path: Path, monkeypatch: Any
) -> None:
    def _no_swap(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("a refused selection must not start a provider swap")

    monkeypatch.setattr("clio_agent.config.create_lm", _no_swap)
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        app.state.provider_catalog = _catalog()
        resp = c.put(
            "/v1/providers/lm",
            json={
                "provider": "openai",
                "provider_id": "openrouter",
                "api_base": "https://openrouter.ai/api/v1",
                "model": "~typesafe/jev-latest",
            },
        )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    error = body["error"] if "error" in body else body["detail"]["error"]
    assert error["error"] == SURROGATE_NOT_CHAT
    assert error["details"]["task"] == "text-classification"
    assert error["details"]["role"] == "surrogate"
