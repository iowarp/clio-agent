"""Connector rows, refusals and the setup card (CN3, CN5, CN6)."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.connectors import entries, secrets, state
from clio_agent.gact import connector_setup
from clio_agent.gact import connectors_service as service
from clio_agent.gact.connector_tool import present


def _app() -> Any:
    return SimpleNamespace(state=SimpleNamespace(bus=None, sessions=None))


def test_agent_and_person_refusals(fixture_catalog: Path) -> None:
    found = entries.load_catalog().entries["fixture-secret"]
    with pytest.raises(service.ConnectorError) as secret:
        service.check_values(found, {"token": "x"}, by_agent=True)
    assert secret.value.code == "connector_secret_not_agent_settable"
    signin = entries.load_catalog().entries["fixture-signin"]
    with pytest.raises(service.ConnectorError) as sign_in:
        service.check_values(signin, {"account": "me"}, by_agent=False)
    assert sign_in.value.code == "connector_sign_in_not_a_value"
    with pytest.raises(service.ConnectorError) as unknown:
        service.check_values(found, {"nope": "x"}, by_agent=False)
    assert unknown.value.code == "connector_need_unknown"
    with pytest.raises(service.ConnectorError) as line:
        service.check_values(found, {"where": "a\nb"}, by_agent=True)
    assert line.value.code == "connector_value_invalid"
    plain, secret_values = service.check_values(
        found, {"where": " h ", "token": "t"}, by_agent=False
    )
    assert plain == {"where": "h"} and secret_values == {"token": "t"}


def test_rows_say_what_is_missing_and_why_unreachable(fixture_catalog: Path) -> None:
    app = _app()
    catalog = entries.load_catalog().entries
    row = service.row(app, catalog["fixture-secret"])
    assert row["state"] == {"status": "needs", "needs": ["token"], "reason": None}
    assert [n["has_value"] for n in row["needs"]] == [False, False]
    secrets.save("fixture-secret", {"token": "hidden-value"})
    row = service.row(app, catalog["fixture-secret"])
    assert row["state"]["status"] == "ready"
    token = next(n for n in row["needs"] if n["key"] == "token")
    assert token["value"] is None and token["has_value"] is True
    assert "hidden-value" not in repr(row)
    signin = catalog["fixture-signin"]
    state.update("fixture-signin", enabled=True, values={"address": "http://127.0.0.1:9/mcp"})
    assert service.row(app, signin)["state"]["needs"] == ["account"]
    key = service._probe_key(signin, state.choice("fixture-signin"))
    app.state.connector_probes = {
        "fixture-signin": {"status": "unreachable", "reason": "Nothing answered", "key": key}
    }
    assert service.row(app, signin)["state"] == {
        "status": "unreachable",
        "needs": [],
        "reason": "Nothing answered",
    }
    assert service.unreachable_reason(
        signin, {"url": "http://h:1/mcp?x=1"}, "httpx.ConnectError: All connection attempts failed"
    ) == ("Nothing answered at http://h:1/mcp: the connection was refused.")
    app.state.connector_probes["fixture-signin"]["key"] = "an older configuration"
    assert service.row(app, signin)["state"]["needs"] == ["account"]


def test_a_waiting_call_continues_on_submit_and_is_refused_on_decline(
    fixture_catalog: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app()
    monkeypatch.setattr(
        "clio_agent.gact.permission_delivery.attended_session_id", lambda _app, sid: sid
    )
    monkeypatch.setattr(
        "clio_agent.gact.code_task_context.question_cancel_token", lambda _app, _sid: None
    )
    state.update("fixture-secret", enabled=True)
    outcome: dict[str, Any] = {}

    def call() -> None:
        outcome["value"] = connector_setup.await_setup(
            app, "sess_a", "fixture-secret", "fixture-secret_fixture_secret_check"
        )

    async def set_values(_app: Any, connector_id: str, values: Any, **_: Any) -> dict:
        secrets.save(connector_id, values)
        return {}

    monkeypatch.setattr(service, "set_values", set_values)
    worker = threading.Thread(target=call)
    worker.start()
    for _ in range(100):
        if getattr(app.state, "connector_setups", None):
            break
        worker.join(0.05)
    (row_id,) = list(app.state.connector_setups)
    card = connector_setup.projected(app, {"sess_a"})[0]
    assert card.kind == "connector_setup" and card.payload["missing"][0]["key"] == "token"
    with pytest.raises(connector_setup.SetupResponseError) as incomplete:
        asyncio.run(connector_setup.respond(app, {"sess_a"}, card.id, "submit", {}))
    assert incomplete.value.code == "connector_setup_incomplete"
    answered = asyncio.run(
        connector_setup.respond(app, {"sess_a"}, card.id, "submit", {"token": "zz-typed-zz"})
    )
    worker.join(5)
    assert answered.status == "answered" and outcome["value"] is None
    assert "zz-typed-zz" not in repr(app.state.connector_setups[row_id])

    secrets.save("fixture-secret", {"token": None})
    worker = threading.Thread(target=call)
    worker.start()
    for _ in range(100):
        pending = [r for r in app.state.connector_setups.values() if r["status"] == "pending"]
        if pending:
            break
        worker.join(0.05)
    asyncio.run(
        connector_setup.respond(
            app, {"sess_a"}, connector_setup.PREFIX + pending[0]["id"], "decline", {}
        )
    )
    worker.join(5)
    assert outcome["value"] == "deny"
    assert outcome["value"].deny_message.startswith("connector_declined:")
    assert "Fixture Secret" in outcome["value"].deny_message


def test_a_ready_or_disabled_connector_never_waits(fixture_catalog: Path) -> None:
    app = _app()
    assert connector_setup.await_setup(app, "s", "fixture-secret", "t") is None
    state.update("fixture-plain", enabled=True)
    assert connector_setup.await_setup(app, "s", "fixture-plain", "t") is None
    assert connector_setup.await_setup(app, "s", "not-a-connector", "t") is None


def test_the_tool_row_shows_the_value_or_the_refusal() -> None:
    done = present(
        {"connector": "fixture-secret", "key": "where", "value": "node08"},
        None,
        {
            "connector_name": "Fixture Secret",
            "label": "Where it runs",
            "value": "node08",
            "state": {"status": "ready"},
            "tool_count": 1,
        },
    )
    assert done["subject"] == "Where it runs of Fixture Secret"
    assert [b["text"] for b in done["blocks"]] == ["node08", "Ready (1 tools)"]
    refused = present(
        {"connector": "fixture-secret", "key": "token"},
        None,
        {
            "error": "connector_secret_not_agent_settable",
            "message": "Access token is a secret",
            "label": "Access token",
            "connector_name": "Fixture Secret",
        },
    )
    assert refused["status"] == "failed"
    assert refused["blocks"][0]["detail"] == "connector_secret_not_agent_settable"
