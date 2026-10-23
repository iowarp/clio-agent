"""The spending guard and the model_exhausted record (docs/design/sdk/model-fleet.md)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact.fleet import guard
from clio_agent.gact.fleet.exhaustion import exhausted_message, model_exhausted
from clio_agent.gact.fleet.guard import CONSENT_KEY, delegation_allowed, guard_spending


class _Sessions:
    def __init__(self, rows: dict[str, Any]) -> None:
        self.rows = rows

    def get(self, sid: str) -> Any:
        return self.rows.get(sid)

    def update(self, sid: str, *, metadata_patch: dict[str, Any]) -> None:
        self.rows[sid].metadata.update(metadata_patch)


def _app(decisions: list[str]) -> tuple[Any, list[tuple[str, dict[str, Any]]]]:
    asked: list[tuple[str, dict[str, Any]]] = []

    def gate(name: str, args: dict[str, Any], meta: dict[str, Any]) -> str:
        asked.append((name, args))
        return decisions.pop(0)

    sessions = _Sessions(
        {
            "root": SimpleNamespace(
                id="root",
                parent_session_id="",
                model={"provider_id": "claude_code", "model_id": "opus"},
                metadata={},
            ),
            "child": SimpleNamespace(
                id="child",
                parent_session_id="root",
                model={"provider_id": "claude_code", "model_id": "haiku"},
                metadata={},
            ),
        }
    )
    state = SimpleNamespace(sessions=sessions, make_permission_gate=lambda: gate)
    return SimpleNamespace(state=state), asked


def _choice(account: str, source: str = "explicit") -> dict[str, Any]:
    return {"source": source, "ran": {"provider_id": account, "model_id": "m"}}


@pytest.fixture(autouse=True)
def _no_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(guard, "_overrides", lambda: {})


def test_subscriptions_and_free_accounts_are_allowed_by_default() -> None:
    assert delegation_allowed("claude_code") is True
    assert delegation_allowed("vllm") is True
    assert delegation_allowed("openrouter") is False
    assert delegation_allowed("anthropic") is False


def test_an_override_turns_an_account_on_or_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(guard, "_overrides", lambda: {"openrouter": True, "claude_code": False})
    assert delegation_allowed("openrouter") is True
    assert delegation_allowed("claude_code") is False


def test_a_metered_account_asks_once_per_conversation() -> None:
    """M7: one Question; after approval a second spawn in the session does not ask."""
    app, asked = _app(["allow"])
    assert asyncio.run(guard_spending(app, "child", _choice("openrouter"))) is None
    assert asyncio.run(guard_spending(app, "root", _choice("openrouter"))) is None
    assert [name for name, _ in asked] == ["use_model_account"]
    assert app.state.sessions.get("root").metadata[CONSENT_KEY] == ["openrouter"]


def test_a_denied_account_is_a_typed_refusal() -> None:
    app, _asked = _app(["deny"])
    refused = asyncio.run(guard_spending(app, "root", _choice("openrouter")))
    assert refused is not None and refused["reason"] == "model_spending_not_allowed"
    assert CONSENT_KEY not in app.state.sessions.get("root").metadata


def test_allowed_accounts_inherited_models_and_the_callers_own_account_never_ask() -> None:
    app, asked = _app([])
    assert asyncio.run(guard_spending(app, "root", _choice("vllm"))) is None
    assert asyncio.run(guard_spending(app, "root", _choice("openrouter", "inherited"))) is None
    app.state.sessions.get("root").model = {"provider_id": "openrouter", "model_id": "x"}
    assert asyncio.run(guard_spending(app, "root", _choice("openrouter"))) is None
    assert asked == []


def test_model_exhausted_says_who_chose_and_what_else_can_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from clio_agent.gact.fleet import child_model
    from clio_agent.gact.fleet.entries import FleetEntry

    monkeypatch.setattr(
        child_model,
        "fleet_entries",
        lambda _app, needed: [
            FleetEntry("claude_code", "haiku", available=True),
            FleetEntry("codex", "gpt-5.5", available=True),
            FleetEntry("openrouter", "x", available=False),
        ],
    )
    app, _ = _app([])
    app.state.sessions.get("child").metadata["model_choice"] = _choice("claude_code")
    record = model_exhausted(
        app, "child", {"reason": "claude_code_plan_limit", "resets_at": 1_800_000_000}
    )
    assert record == {
        "model": "claude_code/haiku",
        "resets_at": "2027-01-15T08:00:00+00:00",
        "chooser": "clio",
        "alternatives": ["codex/gpt-5.5"],
    }
    assert exhausted_message(record, "Limit hit.").startswith(
        "model_exhausted: claude_code/haiku cannot run until 2027-01-15"
    )
    assert model_exhausted(app, "root", {"reason": "claude_code_plan_limit"})["chooser"] == "person"
    assert model_exhausted(app, "root", {"reason": "provider_auth_required"}) is None
