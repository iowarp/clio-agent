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


def test_the_card_names_the_call_it_holds(
    fixture_catalog: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The card carries the waiting call's id, so the conversation shows its line there."""

    from clio_agent.tools.tool_observation import call_identity_scope, peek_call_identity

    app = _app()
    monkeypatch.setattr(
        "clio_agent.gact.permission_delivery.attended_session_id", lambda _app, sid: sid
    )
    monkeypatch.setattr(
        "clio_agent.gact.code_task_context.question_cancel_token", lambda _app, _sid: None
    )
    state.update("fixture-secret", enabled=True)
    seen: dict[str, str] = {}

    def call() -> None:
        with call_identity_scope():
            seen["id"] = peek_call_identity()
            connector_setup.await_setup(
                app, "sess_a", "fixture-secret", "fixture-secret_fixture_secret_check"
            )

    worker = threading.Thread(target=call)
    worker.start()
    for _ in range(100):
        if getattr(app.state, "connector_setups", None):
            break
        worker.join(0.05)
    card = connector_setup.projected(app, {"sess_a"})[0]
    assert card.source.invocation_id and card.source.invocation_id == seen["id"]
    asyncio.run(connector_setup.respond(app, {"sess_a"}, card.id, "decline", {}))
    worker.join(5)


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


def test_a_refused_sign_in_waits_for_the_card_then_runs_again(
    fixture_catalog: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import contextvars

    from clio_agent.tools.mcp_sign_in import MCPSignInNeeded

    app = _app()
    spec = SimpleNamespace(connector="fixture-signin")
    seen: list[str] = []

    def answered(_app: Any, sid: str, connector: str, tool: str) -> Any:
        seen.append(connector)
        return None

    monkeypatch.setattr(connector_setup, "await_setup", answered)
    monkeypatch.setattr(
        "clio_agent.gact.runtime.globals._resolve_tool_session", lambda _app: ("sess_a", None)
    )
    failed = RuntimeError("Client failed to connect")
    failed.__cause__ = MCPSignInNeeded("fixture-signin")
    context = contextvars.copy_context()
    assert asyncio.run(connector_setup.retry_after_setup(app, context, spec, "t", failed))
    assert seen == ["fixture-signin"]
    seen.clear()
    # A plain protocol error (the proxy dropped the type) still waits while CLIO's own
    # record says the sign-in is missing, and does not for a connector with nothing missing.
    assert asyncio.run(
        connector_setup.retry_after_setup(app, context, spec, "t", RuntimeError("other"))
    )
    state.update("fixture-plain", enabled=True)
    ready = SimpleNamespace(connector="fixture-plain")
    assert not asyncio.run(
        connector_setup.retry_after_setup(app, context, ready, "t", RuntimeError("other"))
    )
    plain = SimpleNamespace(connector="")
    assert not asyncio.run(connector_setup.retry_after_setup(app, context, plain, "t", failed))

    monkeypatch.setattr(
        connector_setup,
        "await_setup",
        lambda *_: SimpleNamespace(deny_message="connector_declined: x"),
    )
    with pytest.raises(PermissionError, match="connector_declined"):
        asyncio.run(connector_setup.retry_after_setup(app, context, spec, "t", failed))


def test_the_stdio_preload_offers_only_the_entrys_tools(
    fixture_catalog: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcp.types import Tool

    from clio_agent.connectors import listing
    from clio_agent.tools import listing_cache

    tools = [Tool(name=n, inputSchema={"type": "object"}) for n in ("fixture_ping", "other")]
    monkeypatch.setattr(listing_cache, "load_listing", lambda *_a, **_k: tools)
    entry = entries.load_catalog().entries["fixture-plain"]
    monkeypatch.setattr(
        state,
        "all_entries",
        lambda: {
            "fixture-plain": entry.__class__(**{**entry.__dict__, "tools": ("fixture_ping",)})
        },
    )
    spec = SimpleNamespace(command="py", args=(), env={}, connector="fixture-plain")
    assert [t.name for t in listing.cached_offered("fixture-plain", spec)] == ["fixture_ping"]
    unbound = SimpleNamespace(command="py", args=(), env={}, connector="")
    assert len(listing.cached_offered("fixture-plain", unbound)) == 2


def test_several_values_are_set_with_one_apply(
    fixture_catalog: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Each apply restarts the connector's server: a host, a jump host and a prepare
    # line given together cost one restart, not three.
    from clio_agent.gact import connector_tool, context

    catalog = tmp_path / "catalog"
    catalog.mkdir()
    (catalog / "remote.yaml").write_text(
        """- id: fixture-remote
  name: Fixture Remote
  description: A test connector reached through a jump host
  server:
    command: python3
    args: [-c, "pass"]
    env: {WHERE: "${need.address}", VIA: "${need.via}"}
  needs:
    - {key: address, kind: address, label: Where it runs, required: false, agent_settable: true}
    - {key: via, kind: value, label: Jump host, required: false, agent_settable: true}
    - {key: token, kind: secret, label: Token, required: false}
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("CLIO_CONNECTOR_CATALOG_DIRS", f"{fixture_catalog}:{catalog}")
    state.update("fixture-remote", enabled=True)
    applied: list[dict[str, Any]] = []

    async def set_values(_app: Any, sid: str, connector_id: str, values: Any, **kw: Any) -> dict:
        applied.append({"session": sid, "connector": connector_id, "values": dict(values), **kw})
        return {"state": {"status": "ready"}, "tool_count": 2}

    # CV-D4: the agent's value is this conversation's, applied to its own fleet.
    monkeypatch.setattr("clio_agent.gact.connector_values.set_session_values", set_values)
    monkeypatch.setattr(context, "active_app", lambda: _app())
    monkeypatch.setattr(context, "active_tool_session_id", lambda: "sess_a")
    result = asyncio.run(
        connector_tool.set_connector_value(
            "Fixture Remote", values={"address": "node08", "Jump host": "login"}
        )
    )
    assert applied == [
        {
            "session": "sess_a",
            "connector": "fixture-remote",
            "values": {"address": "node08", "via": "login"},
            "by_agent": True,
            "move_turn": True,
        }
    ]
    assert result["values"] == {"Where it runs": "node08", "Jump host": "login"}
    row = connector_tool.present(
        {"connector": "Fixture Remote", "values": {"address": "node08", "via": "login"}},
        None,
        result,
    )
    assert row["subject"] == "Where it runs, Jump host of Fixture Remote"
    assert [b["text"] for b in row["blocks"]] == [
        "node08",
        "login",
        "This conversation",
        "Ready (2 tools)",
    ]

    one = asyncio.run(connector_tool.set_connector_value("fixture-remote", "via", "ares"))
    assert applied[-1]["values"] == {"via": "ares"} and one["value"] == "ares"
    unknown = asyncio.run(
        connector_tool.set_connector_value("fixture-remote", values={"nope": "x"})
    )
    assert unknown["error"] == "connector_need_unknown" and len(applied) == 2
    empty = asyncio.run(connector_tool.set_connector_value("fixture-remote"))
    assert empty["error"] == "connector_value_missing"
