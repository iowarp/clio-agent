"""The agent asks for optional needs of a connector already on (KiCad plan KC5)."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any

import pytest

from clio_agent.connectors import state
from clio_agent.connectors.entries import entry_from_raw
from clio_agent.gact import connector_needs_offer as needs_offer
from clio_agent.gact import connector_offer as offer
from clio_agent.gact import connector_setup
from tests.test_connectors.test_offer import REFS, _app

KEYS = entry_from_raw(
    {
        "id": "fix-keys",
        "name": "Fixture parts",
        "description": "Parts search with optional keys",
        "server": {"command": "python3", "args": ["-c", "pass"], "env": {"K": "${need.api_key}"}},
        "needs": [
            {"key": "where", "kind": "value", "label": "Where", "required": False},
            {
                "key": "api_key",
                "kind": "secret",
                "label": "Catalog key",
                "required": False,
                "group": "Part catalogs",
                "help": "Adds a second catalog.",
            },
        ],
    },
    folder="",
)


@pytest.fixture
def here(fixture_catalog: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    app = _app()
    monkeypatch.setattr(state, "all_entries", lambda: {"fix-keys": KEYS})
    state.update("fix-keys", enabled=True)
    monkeypatch.setattr(offer, "session_agent", lambda _a, _s: (REFS, REFS[0], "Standard"))
    monkeypatch.setattr(offer, "conversation_connectors", lambda _a, _s: {"fix-keys": ["p"]})
    monkeypatch.setattr(
        "clio_agent.gact.permission_delivery.attended_session_id", lambda _a, sid: sid
    )
    monkeypatch.setattr(
        "clio_agent.gact.code_task_context.question_cancel_token", lambda _a, _s: None
    )
    monkeypatch.setattr("clio_agent.gact.context.active_app", lambda: app)
    monkeypatch.setattr(offer, "_session_id", lambda: "sess_a")
    return app


def _ask(keys: list[str]) -> tuple[threading.Thread, dict[str, Any]]:
    outcome: dict[str, Any] = {}

    def run() -> None:
        outcome["value"] = asyncio.run(
            needs_offer.offer_needs(KEYS, "jlcsearch is rate limited", keys)
        )

    worker = threading.Thread(target=run)
    worker.start()
    return worker, outcome


def _card(app: Any, worker: threading.Thread) -> Any:
    for _ in range(200):
        rows = getattr(app.state, "connector_setups", None) or {}
        if any(row["status"] == "pending" for row in rows.values()):
            break
        worker.join(0.05)
    return next(c for c in connector_setup.projected(app, {"sess_a"}) if c.status == "pending")


def test_the_listing_names_empty_optional_needs_with_group_and_help(here: Any) -> None:
    rows = needs_offer.optional_needs(here, "sess_a")
    assert [row["key"] for row in rows] == ["where", "api_key"]
    assert rows[1] == {
        "connector": "fix-keys",
        "key": "api_key",
        "label": "Catalog key",
        "kind": "secret",
        "group": "Part catalogs",
        "help": "Adds a second catalog.",
    }


def test_the_card_shows_only_the_asked_rows_and_not_now_is_not_asked_again(here: Any) -> None:
    worker, outcome = _ask(["api_key"])
    card = _card(here, worker)
    assert card.payload["intent"] == "values"
    assert [row["key"] for row in card.payload["missing"]] == ["api_key"]
    assert card.payload["missing"][0]["group"] == "Part catalogs"
    assert card.title == "Fixture parts: add Catalog key?"
    assert card.payload["reason"] == "jlcsearch is rate limited"
    asyncio.run(connector_setup.respond(here, {"sess_a"}, card.id, "decline", {}))
    worker.join(5)
    assert outcome["value"]["refusal"] == needs_offer.DECLINED
    again = asyncio.run(needs_offer.offer_needs(KEYS, "still limited", ["api_key"]))
    assert again["refusal"] == needs_offer.DECLINED
    assert "already chose not to" in again["message"]
    pending = [r for r in (here.state.connector_setups or {}).values() if r["status"] == "pending"]
    assert pending == []


def test_add_saves_the_secret_and_restarts_the_server(
    here: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clio_agent.connectors import secrets
    from clio_agent.gact import connector_follow
    from clio_agent.gact import connectors_service as service

    saved: list[tuple[str, dict[str, str]]] = []
    applied: list[str] = []
    monkeypatch.setattr(secrets, "save", lambda cid, values: saved.append((cid, dict(values))))

    async def apply(_app: Any, found: Any) -> dict[str, Any]:
        applied.append(found.id)
        return {}

    monkeypatch.setattr(service, "apply", apply)
    monkeypatch.setattr(connector_follow, "add_to_conversation", lambda _a, _s, _c: ["p"])
    worker, outcome = _ask(["api_key"])
    card = _card(here, worker)
    asyncio.run(connector_setup.respond(here, {"sess_a"}, card.id, "submit", {"api_key": "k-123"}))
    worker.join(5)
    assert saved == [("fix-keys", {"api_key": "k-123"})]
    assert applied == ["fix-keys"]
    assert outcome["value"]["needs_set"] == ["api_key"]
    assert "k-123" not in str(outcome["value"])


def test_an_unknown_key_lists_what_can_be_asked(here: Any) -> None:
    answer = asyncio.run(needs_offer.offer_needs(KEYS, "why", ["nexar_id"]))
    assert answer["error"] == "connector_needs_unknown"
    assert [row["key"] for row in answer["optional_needs"]] == ["where", "api_key"]
