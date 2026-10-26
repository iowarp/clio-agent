"""The fleet read model, usage readers and spend (docs/design/sdk/model-fleet.md)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from clio_agent.gact.fleet.entries import project_fleet
from clio_agent.gact.fleet.read import Fleet
from clio_agent.gact.fleet.spend import Spend, SpendReport, spend_by_account
from clio_agent.providers.usage import openrouter
from clio_agent.providers.usage.capture import record_rate_limit_usage
from clio_agent.providers.usage.readers import (
    claude_snapshot,
    codex_snapshot_from_headers,
    openrouter_snapshot,
)
from clio_agent.providers.usage.snapshot import USAGE_BOARD, UsageBoard


@pytest.fixture(autouse=True)
def _clean_board() -> Any:
    USAGE_BOARD.clear()
    yield
    USAGE_BOARD.clear()


def _model(model_id: str, **overrides: Any) -> dict[str, Any]:
    row = {
        "model_id": model_id,
        "aliases": [],
        "chat_selectable": True,
        "modalities": ["image", "text"],
        "native_tool_calling": True,
        "context_window": 200_000,
        "output_limit": 64_000,
        "availability": "available",
        "reasoning": {"levels": ["low", "high"]},
        "capability_tags": {"capabilities": []},
        "model_facts": {
            "pricing": {
                "value": {
                    "input": {"kind": "usd", "per_1m": 1.0},
                    "output": {"kind": "usd", "per_1m": 5.0},
                },
                "evidence": [{"source": "litellm"}],
            }
        },
    }
    row.update(overrides)
    return row


def _catalog() -> dict[str, Any]:
    return {
        "providers": [
            {
                "id": "claude_code",
                "name": "Claude Code",
                "kind": "claude_code",
                "auth": "ok",
                "health": "ready",
                "models": [
                    _model("claude-haiku-5-5", aliases=["haiku"]),
                    _model("text-only", modalities=["text"], native_tool_calling=False),
                    _model("embedder", chat_selectable=False),
                ],
            },
            # A local runtime nobody started is not an account.
            {"id": "lm_studio", "auth": "not_required", "health": "unavailable", "models": []},
            # A cloud provider with no key is not an account.
            {"id": "openai", "auth": "missing", "health": "unavailable", "models": []},
            {
                "id": "openrouter",
                "name": "OpenRouter",
                "kind": "openai",
                "auth": "ok",
                "health": "unavailable",
                "failure": "timeout",
                "models": [_model("some/model")],
            },
        ]
    }


def test_the_fleet_lists_connected_accounts_and_their_chat_models() -> None:
    accounts, entries = project_fleet(_catalog())

    assert [(a.id, a.billing) for a in accounts] == [
        ("claude_code", "subscription"),
        ("openrouter", "credit"),
    ]
    assert [e.ref for e in entries] == [
        "claude_code/claude-haiku-5-5",
        "claude_code/text-only",
        "openrouter/some/model",
    ]
    haiku, text_only, routed = entries
    assert (haiku.tool_calling, haiku.vision, haiku.context_tokens, haiku.output_tokens) == (
        True,
        True,
        200_000,
        64_000,
    )
    assert haiku.aliases == ("haiku",) and haiku.effort_levels == ("low", "high")
    assert haiku.price["input"] == {"kind": "usd", "per_1m": 1.0}
    assert haiku.price_source == "litellm"
    assert (text_only.tool_calling, text_only.vision) == (False, False)
    # An unhealthy account's models are listed but unavailable, with the reason.
    assert routed.available is False and routed.unavailable_reason == "account_unavailable"


def test_recorded_spend_does_not_promise_account_limits_from_unsupported_connections() -> None:
    accounts, entries = project_fleet(
        {
            "providers": [
                *_catalog()["providers"],
                {
                    "id": "anthropic",
                    "name": "Anthropic API",
                    "kind": "anthropic",
                    "auth": "ok",
                    "health": "ready",
                    "models": [_model("claude-sonnet")],
                },
            ]
        }
    )
    fleet = Fleet(
        accounts=accounts,
        entries=entries,
        usage={},
        spend=SpendReport({"anthropic/claude-sonnet": Spend(tokens_input=40_000, cost_usd=0.005)}),
        observed_at="",
    )
    rows = {row["id"]: row for row in fleet.to_wire()["accounts"]}
    assert rows["anthropic"]["usage"] is None
    assert rows["anthropic"]["usage_missing_reason"] == "not_supported"
    assert rows["anthropic"]["spend"]["tokens"] == 40_000
    assert rows["anthropic"]["spend"]["cost_usd"] == 0.005
    assert rows["claude_code"]["usage_missing_reason"] == "no_reading_yet"
    assert rows["openrouter"]["usage_missing_reason"] == "no_reading_yet"


def test_claude_windows_come_from_the_unified_windows_of_the_rate_limit_event() -> None:
    raw = {
        "status": "allowed",
        "rateLimitType": "five_hour",
        "unifiedWindows": {
            "five_hour": {"utilization": 0.15, "resetsAt": 1792716600},
            "seven_day": {"utilization": 0.34, "resetsAt": 1793062800},
        },
    }
    record_rate_limit_usage(SimpleNamespace(rate_limit_info=SimpleNamespace(raw=raw)))

    snapshot = USAGE_BOARD.latest("claude_code")
    assert snapshot is not None and snapshot.status == "allowed"
    assert [(w.name, w.used_percent, w.window_minutes) for w in snapshot.windows] == [
        ("5-hour", 15.0, 300),
        ("Weekly", 34.0, 10080),
    ]
    assert snapshot.windows[0].resets_at.startswith("2026-10-")


def test_an_older_claude_event_with_one_window_still_reads() -> None:
    snapshot = claude_snapshot(
        "claude_code",
        {"status": "allowed_warning", "rateLimitType": "seven_day", "utilization": 0.9},
    )
    assert snapshot is not None
    assert [(w.name, w.used_percent) for w in snapshot.windows] == [("Weekly", 90.0)]
    assert claude_snapshot("claude_code", {"status": "allowed"}) is None


def test_codex_headers_carry_both_windows_and_credits() -> None:
    snapshot = codex_snapshot_from_headers(
        "codex",
        [
            ("X-Codex-Primary-Used-Percent", "61.5"),
            ("x-codex-primary-window-minutes", "300"),
            ("x-codex-primary-reset-at", "1800000000"),
            ("x-codex-secondary-used-percent", "12"),
            ("x-codex-secondary-window-minutes", "10080"),
            ("x-codex-credits-has-credits", "true"),
            ("x-codex-credits-unlimited", "false"),
            ("x-codex-credits-balance", "4.5"),
        ],
    )
    assert snapshot is not None
    assert [(w.name, w.used_percent) for w in snapshot.windows] == [
        ("5-hour", 61.5),
        ("Weekly", 12.0),
    ]
    assert snapshot.balance is not None and snapshot.balance.remaining == 4.5
    assert codex_snapshot_from_headers("codex", {"content-type": "text/event-stream"}) is None


def test_openrouter_key_response_is_a_balance_and_a_key_window() -> None:
    snapshot = openrouter_snapshot(
        "openrouter",
        {"data": {"limit": 20.0, "limit_remaining": 15.0, "usage": 5.0, "limit_reset": "monthly"}},
    )
    assert snapshot.balance is not None
    assert (snapshot.balance.remaining, snapshot.balance.limit) == (15.0, 20.0)
    assert [(w.name, w.used_percent) for w in snapshot.windows] == [("Key limit (monthly)", 25.0)]
    with pytest.raises(ValueError, match="without_data"):
        openrouter_snapshot("openrouter", {"error": "nope"})


def test_a_failed_read_keeps_the_last_good_snapshot_and_says_why() -> None:
    board = UsageBoard()
    board.record(openrouter_snapshot("openrouter", {"data": {"limit_remaining": 3.0}}))
    board.record_failure("openrouter", "openrouter_key_unreachable: ConnectError")

    snapshot = board.latest("openrouter")
    assert snapshot is not None and snapshot.balance is not None
    assert snapshot.balance.remaining == 3.0
    assert snapshot.read_failure == "openrouter_key_unreachable: ConnectError"
    assert snapshot.read_failed_at

    board.record_failure("never-read", "openrouter_key_missing")
    assert board.latest("never-read").read_failure == "openrouter_key_missing"


def test_openrouter_is_read_with_its_own_key_and_a_failure_keeps_the_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    responses = iter(
        [
            httpx.Response(200, json={"data": {"limit_remaining": 7.0, "limit": 10.0}}),
            httpx.Response(503, json={}),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.headers["Authorization"])
        return next(responses)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        openrouter.httpx,
        "AsyncClient",
        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw),
    )
    monkeypatch.setattr(openrouter, "resolve_cloud_api_key", lambda _pid: "sk-or-test")

    asyncio.run(openrouter.refresh_openrouter_usage())
    asyncio.run(openrouter.refresh_openrouter_usage())  # fresh: not read again
    asyncio.run(openrouter.refresh_openrouter_usage(force=True))

    assert calls == ["Bearer sk-or-test", "Bearer sk-or-test"]
    snapshot = USAGE_BOARD.latest("openrouter")
    assert snapshot is not None and snapshot.balance.remaining == 7.0
    assert snapshot.read_failure == "openrouter_key_http_503"


def test_spend_is_summed_per_model_from_the_session_rollups() -> None:
    from types import SimpleNamespace

    from clio_agent.gact.fleet.spend import sum_sessions

    rolled = SimpleNamespace(
        id="a",
        usage_by_model={
            "claude_code/haiku": {
                "tokens_input": 11,
                "tokens_cached": 100,
                "tokens_output": 6,
                "turns": 2,
                "turns_without_cost": 2,
            },
            "openrouter/x": {"tokens_input": 3, "tokens_output": 2, "cost_usd": 0.25, "turns": 1},
        },
    )
    older = SimpleNamespace(
        id="b",
        usage_by_model={},
        tokens_input=40,
        tokens_output=2,
        cost_usd=0.0,
        cost_known=False,
        model={"provider_id": "claude_code", "model_id": "haiku"},
    )
    idle = SimpleNamespace(id="c", usage_by_model={}, tokens_input=0, tokens_output=0, model={})
    report = sum_sessions([rolled, older, idle])

    haiku = report.by_model["claude_code/haiku"]
    assert (haiku.tokens_input, haiku.tokens_cached, haiku.tokens_output) == (51, 100, 8)
    assert haiku.turns == 3 and haiku.turns_without_cost == 3
    assert report.estimated_sessions == ["b"]
    by_account = spend_by_account(report.by_model)
    assert by_account["openrouter"].cost_usd == 0.25
    assert by_account["claude_code"].tokens == 159


def test_a_finished_turn_adds_to_the_session_rollup_under_its_model() -> None:
    from types import SimpleNamespace

    from clio_agent.gact.turn_usage import model_usage_after_turn

    state = SimpleNamespace(
        turn_tokens={"input": 5, "output": 7, "cache_read": 100, "cache_write": 0},
        turn_cost=0.5,
        turn_cost_known=True,
        agent_runtime={"model": {"provider_id": "openrouter", "model_id": "x/y"}},
    )
    session = SimpleNamespace(usage_by_model={"openrouter/x/y": {"tokens_input": 1, "turns": 1}})

    rows = model_usage_after_turn(state, session)

    assert rows["openrouter/x/y"] == {
        "tokens_input": 6,
        "tokens_cached": 100,
        "tokens_output": 7,
        "cost_usd": 0.5,
        "turns": 2,
        "turns_without_cost": 0,
    }
    assert session.usage_by_model == {"openrouter/x/y": {"tokens_input": 1, "turns": 1}}
    idle = SimpleNamespace(**{**vars(state), "turn_tokens": {"input": 0, "output": 0}})
    assert model_usage_after_turn(idle, session) == session.usage_by_model


def test_a_runs_model_shares_count_the_tokens_read_and_written_not_cache_reads() -> None:
    from types import SimpleNamespace

    from clio_agent.gact.fleet.run_usage import run_spend

    def session(sid: str, parent: str, rows: dict) -> SimpleNamespace:
        return SimpleNamespace(id=sid, parent_session_id=parent, usage_by_model=rows)

    sessions = [
        session(
            "run", "", {"a/big": {"tokens_input": 10, "tokens_output": 90, "tokens_cached": 9000}}
        ),
        session("step", "run", {"a/small": {"tokens_input": 50, "tokens_output": 250}}),
        session("other", "", {"a/elsewhere": {"tokens_input": 999}}),
    ]
    app = SimpleNamespace(state=SimpleNamespace(sessions=SimpleNamespace(list=lambda: sessions)))
    USAGE_BOARD.record(
        claude_snapshot("a", {"unifiedWindows": {"five_hour": {"utilization": 0.4}}})
    )

    usage = run_spend(app, "run")

    assert [(row["model"], row["share"]) for row in usage["models"]] == [
        ("a/small", 0.75),
        ("a/big", 0.25),
    ]
    assert usage["windows"] == [
        {"account": "a", "label": "a", "window": "5-hour", "used_percent": 40.0}
    ]


def test_a_childs_task_row_and_live_event_carry_its_model_choice(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from clio_agent.gact import context as _ctx
    from clio_agent.gact.app import build_app
    from clio_agent.gact.events import Event
    from clio_agent.gact.protocol.v3.event import event_to_v3
    from clio_agent.gact.routes.tasks import agent_task_view
    from tests.test_gact.task_records import record_assignment_fixture

    choice = {
        "asked": "claude_code/claude-haiku-5-5",
        "ran": {"provider_id": "claude_code", "model_id": "claude-haiku-5-5"},
        "source": "explicit",
        "reasons": [],
        "effort": "",
    }
    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app):
        parent = app.state.sessions.create(workspace_id="ws_default", title="parent")
        child = app.state.sessions.create(
            workspace_id="ws_default", title="child", metadata={"model_choice": choice}
        )
        task = record_assignment_fixture(app, child.id, handle="task_child", parent_sid=parent.id)
        view = agent_task_view(app, task.model_dump(mode="json"))
        assert view["model_choice"] == choice
        token = _ctx.set_app(app)
        try:
            projected = event_to_v3(
                Event(type="agent.task.started", session_id=parent.id, payload=view),
                session=app.state.sessions.get(parent.id),
            )
        finally:
            _ctx.reset(token)
        assert projected["payload"]["model_choice"] == choice
