"""SPOTTER review tools read only the armed watcher's parent, bounded and typed."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact import spotter_tools
from clio_agent.gact.attention.finding_evidence import (
    FindingAttentionRequest,
    bind_finding_evidence,
)
from clio_agent.gact.attention.store import AttentionStore
from clio_agent.gact.session_store import _replace_session_messages
from clio_agent.gact.spotter_tools import (
    MAX_ITEMS,
    build_attention_sources_tool,
    build_review_activity_tool,
    compact_attention,
    summarize_activity,
)
from clio_agent.gact.spotter_watcher import disarm_spotter_watcher, ensure_spotter_watcher
from clio_agent.gact.types import Message
from tests.test_gact.task_records import active_turn_fixture
from tests.test_gact.test_attention._support import (
    FixtureRenderer,
    fixture_flowcept,
    fixture_transcript,
)
from tests.test_gact.test_attention.test_service_real_run import (
    FIRST_SENTENCE,
    _call,
    _explain,
)

_AGENT = SimpleNamespace(id="spotter-ai")


def _msg(mid: str, role: str, at: str, parts: list[dict[str, Any]], turn: str = "") -> dict:
    return {
        "id": mid,
        "role": role,
        "created_at": at,
        "turn_id": turn or mid,
        "parts": parts,
    }


def _activity() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    messages = [
        _msg("u1", "user", "2026-10-26T10:00:00Z", [{"type": "text", "text": "Summarize a.csv"}]),
        _msg(
            "a1",
            "assistant",
            "2026-10-26T10:00:05Z",
            [
                {
                    "type": "tool_call",
                    "id": "c1",
                    "call_id": "c1",
                    "tool_name": "fs_write_file",
                    "thought": "I will write the summary.",
                    "input": {"path": "/w/out.md", "content": "x" * 2000},
                },
                {"type": "tool_result", "call_id": "c1", "text": "wrote 2000 bytes"},
                {"type": "text", "text": "Done: summary written."},
            ],
            turn="u1",
        ),
        _msg("u2", "user", "2026-10-26T11:00:00+00:00", [{"type": "text", "text": "Thanks"}]),
    ]
    events = [
        {
            "event_id": "lm1",
            "event_type": "lm.call",
            "turn_id": "u1",
            "occurred_at": "2026-10-26T10:00:04Z",
            "payload": {"response_id": "resp-1", "model": "granite"},
        },
        {
            "event_id": "art1",
            "event_type": "artifact.created",
            "occurred_at": "2026-10-26T10:00:06Z",
            "payload": {"path": "/w/report.pdf", "artifact_id": "art_1"},
        },
    ]
    return messages, events


def test_activity_projects_requests_answers_tools_writes_and_calls() -> None:
    messages, events = _activity()
    out = summarize_activity(messages, events)
    assert [r["text"] for r in out["user_requests"]] == ["Summarize a.csv", "Thanks"]
    [answer] = out["assistant_answers"]
    assert answer["message_id"] == "a1" and answer["has_thinking"] is True
    assert answer["text"] == "Done: summary written."
    [call] = out["tool_calls"]
    assert call["tool"] == "fs_write_file" and call["output"] == "wrote 2000 bytes"
    assert len(call["input"]) <= spotter_tools.MAX_FIELD_CHARS
    assert {w["path"] for w in out["writes"]} == {"/w/out.md", "/w/report.pdf"}
    [lm] = out["lm_calls"]
    assert lm["response_id"] == "resp-1" and lm["message_id"] == "a1"
    assert out["next_since"] == "2026-10-26T11:00:00+00:00"


def test_activity_since_returns_only_strictly_newer_items_across_timezone_spellings() -> None:
    messages, events = _activity()
    out = summarize_activity(messages, events, since="2026-10-26T12:00:05+02:00")
    # 10:00:05Z == 12:00:05+02:00 is not newer; only the 11:00 request is.
    assert [r["message_id"] for r in out["user_requests"]] == ["u2"]
    assert out["assistant_answers"] == [] and out["tool_calls"] == []
    assert [w["path"] for w in out["writes"]] == ["/w/report.pdf"]
    nothing = summarize_activity(messages, events, since=out["next_since"])
    assert nothing["user_requests"] == [] and nothing["next_since"] == out["next_since"]
    with pytest.raises(ValueError):
        summarize_activity(messages, events, since="yesterday")


def test_a_streamed_answer_updated_after_the_cursor_is_reviewed_again() -> None:
    messages, events = _activity()
    first = summarize_activity(messages, events)
    messages[1] = {**messages[1], "updated_at": "2026-10-26T11:30:00Z"}
    again = summarize_activity(messages, events, since=first["next_since"])
    assert [a["message_id"] for a in again["assistant_answers"]] == ["a1"]
    assert again["next_since"] == "2026-10-26T11:30:00Z"


def test_activity_is_bounded_to_the_most_recent_items() -> None:
    messages = [
        _msg(
            f"u{i}",
            "user",
            f"2026-10-26T10:{i // 60:02d}:{i % 60:02d}Z",
            [{"type": "text", "text": "y" * 5000}],
        )
        for i in range(MAX_ITEMS + 7)
    ]
    out = summarize_activity(messages, [])
    assert len(out["user_requests"]) == MAX_ITEMS
    assert out["truncated"] == {"user_requests": 7}
    assert out["user_requests"][-1]["message_id"] == f"u{MAX_ITEMS + 6}"
    assert all(len(r["text"]) <= spotter_tools.MAX_FIELD_CHARS for r in out["user_requests"])


@pytest.fixture
def capture_files(monkeypatch: pytest.MonkeyPatch) -> Any:
    from clio_agent.gact.attention import files
    from tests.test_gact.test_attention._support import FIXTURES

    monkeypatch.setenv("CLIO_PROVENANCE_ATTENTION_FILES_DIR", str(FIXTURES))
    files.clear_cache()
    yield
    files.clear_cache()


@pytest.mark.usefixtures("capture_files")
def test_compact_attention_keeps_labelled_sources_excerpts_and_bindable_evidence() -> None:
    payload = _explain()
    messages = fixture_transcript()
    out = compact_attention(payload, messages)
    shares = [s["share"] for s in out["sources"]]
    assert shares == sorted(shares, reverse=True) and out["sources"]
    assert 0 < len(out["sections"]) <= spotter_tools.TOP_SECTIONS
    assert all(s["label"] or s["domain"] for s in out["sections"])
    assert 0 < len(out["blocks"]) <= spotter_tools.TOP_BLOCKS
    assert any(block["excerpts"] for block in out["blocks"])
    for block in out["blocks"]:
        assert len(block["excerpts"]) <= spotter_tools.TOP_EXCERPTS
        assert all(len(e["text"]) <= spotter_tools.EXCERPT_CHARS for e in block["excerpts"])
    assert out["selection"]["text"] == FIRST_SENTENCE
    evidence = out["evidence"]
    request = FindingAttentionRequest.model_validate(
        {
            **{k: evidence[k] for k in ("response_id", "capture_sha256", "steps", "profile")},
            "profile_revision": evidence["profile_revision"],
            "uncertainty": "Attention alone is not proof.",
        }
    )
    inspection = bind_finding_evidence(
        session_id=_call().session_id,
        messages=messages,
        calls=[_call()],
        store=AttentionStore(fixture_flowcept()),
        renderer_for=lambda _: FixtureRenderer(),
        request=request,
    )
    assert inspection.lm_call_id == evidence["lm_call_id"]


def test_windows_merge_nearby_runs_and_sum_their_values() -> None:
    windows = spotter_tools._windows([[100, 104, 0.1], [0, 4, 0.2], [6, 9, 0.3]])
    assert windows == [(0, 9, pytest.approx(0.5)), (100, 104, pytest.approx(0.1))]


@pytest.fixture
def armed(watcher_world: tuple[Any, str, str]) -> tuple[Any, str, str]:
    app, parent, child = watcher_world
    ensure_spotter_watcher(app, app.state.sessions.get(parent))  # backfills the read grant
    return app, parent, child


def _install_transcript(app: Any, parent: str) -> list[Message]:
    messages = [m.model_copy(update={"session_id": parent}) for m in fixture_transcript()]
    _replace_session_messages(app, parent, messages)
    return messages


def _attention_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    from clio_agent.gact.attention import routes

    renderer = FixtureRenderer()
    monkeypatch.setattr(routes, "session_lm_calls", lambda app, sid: [_call(session_id=sid)])
    monkeypatch.setattr(routes, "attention_store", lambda app: AttentionStore(fixture_flowcept()))
    monkeypatch.setattr(routes, "_renderer", lambda identity: renderer)


@pytest.mark.usefixtures("capture_files")
def test_attention_sources_reads_the_watched_parent_by_quoted_text(
    armed: tuple[Any, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    app, parent, child = armed
    _install_transcript(app, parent)
    _attention_backend(monkeypatch)
    tool = build_attention_sources_tool(_AGENT)
    with active_turn_fixture(app, child):
        out = tool.func(text=FIRST_SENTENCE)
        missing = tool.func(text="This sentence was never generated.")
        blank = tool.func(text="  ")
    assert out["available"] is True and out["session_id"] == parent
    assert out["selection"]["message_id"] == "msg_asst_1"
    assert out["evidence"]["response_id"] == _call().response_id
    assert missing["error"] == "selection_not_located" and "exactly" in missing["hint"]
    assert blank["error"] == "invalid_argument"


@pytest.mark.usefixtures("capture_files")
def test_attention_sources_surfaces_typed_unavailability_with_a_next_step(
    armed: tuple[Any, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    app, parent, child = armed
    _install_transcript(app, parent)
    _attention_backend(monkeypatch)
    tool = build_attention_sources_tool(_AGENT)
    with active_turn_fixture(app, child):
        wrong = tool.func(text=FIRST_SENTENCE, message_id="msg_user_earlier")
        absent = tool.func(text=FIRST_SENTENCE, message_id="nope")
    # The quote is not in the user message: a typed miss with a next step.
    assert wrong["error"] == "selection_not_located" and wrong["hint"]
    assert absent["error"] == "message_not_found" and "review_activity" in absent["hint"]


def test_review_activity_reads_the_parent_only_while_armed(
    armed: tuple[Any, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from clio_agent.gact import session_export

    app, parent, child = armed
    _install_transcript(app, parent)
    _, events = _activity()
    monkeypatch.setattr(session_export, "_events", lambda app, sid: (events, "durable_trace"))
    tool = build_review_activity_tool(_AGENT)
    with active_turn_fixture(app, child):
        out = tool.func(since="")
        bad = tool.func(since="not-a-time")
    assert out["session_id"] == parent and out["recording"] == "durable_trace"
    assert [a["message_id"] for a in out["assistant_answers"]][-1] == "msg_asst_1"
    assert out["tool_calls"] and out["next_since"]
    assert bad["error"] == "invalid_argument"

    disarm_spotter_watcher(app, app.state.sessions.get(parent))
    with active_turn_fixture(app, child):
        denied = tool.func(since="")
    assert denied["error"] == "watched_session_unavailable"


def test_unarmed_session_cannot_review(watcher_world: tuple[Any, str, str]) -> None:
    app, parent, _child = watcher_world
    with active_turn_fixture(app, parent):
        out = build_review_activity_tool(_AGENT).func()
    assert out["error"] == "not_a_watcher"


def test_both_tools_are_claimable_registered_natives() -> None:
    from clio_agent.gact.agents.native_tool_registry import NATIVE_TOOL_NAMES, build_native_tools

    assert {"review_activity", "attention_sources"} <= NATIVE_TOOL_NAMES
    built, _gateway, withheld = build_native_tools(
        _AGENT, ["review_activity", "attention_sources"], {}
    )
    assert set(built) == {"review_activity", "attention_sources"} and not withheld
