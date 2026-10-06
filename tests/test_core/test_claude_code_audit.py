"""Tests for the #891 Claude Code stream-audit instrumentation.

Covers the audit module (:mod:`clio_agent.providers.claude_code_audit`) in isolation
AND end-to-end through the Claude Code engine over the real pooled transport
(:class:`clio_agent.providers.claude_code_engine.AsyncClaudeCodeEngine`): the
``provider.call_started`` / ``provider.call_usage`` rows must be written when the
``CLIO_STREAM_AUDIT_LOG`` gate is on and must NOT be written when it is off
(zero-overhead contract).

Sabotage check: deleting the engine's ``emit_call_usage`` call (or its body) makes
:func:`test_engine_emits_call_rows_when_gate_on` fail on the ``provider.call_usage``
assertions -- the emission is genuinely exercised, not mocked away.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from clio_agent.providers import claude_code_engine
from clio_agent.providers.claude_code_audit import (
    emit_call_started,
    emit_call_usage,
    prompt_prefix_fingerprint,
)
from clio_agent.providers.claude_code_sessions import _reset_sessions_for_tests
from tests import _fake_claude_sdk as fake


@pytest.fixture(autouse=True)
def _clean_stream_pool() -> Any:
    """Each test gets a fresh client pool and conversation registry."""
    _reset_sessions_for_tests()
    claude_code_engine._CONVERSATIONS.clear_for_tests()
    yield
    _reset_sessions_for_tests()
    claude_code_engine._CONVERSATIONS.clear_for_tests()


def _read_rows(path: Path) -> list[dict[str, Any]]:
    """Read a stream-audit JSONL file into a list of row dicts."""
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_prompt_prefix_fingerprint_is_stable_and_prefix_sensitive() -> None:
    shared = "x" * 20000  # shared head longer than the 16 KB window
    small_a, large_a = prompt_prefix_fingerprint(shared + "-tail-A")
    small_b, large_b = prompt_prefix_fingerprint(shared + "-tail-B")
    # The divergence is beyond both windows, so both fingerprints match.
    assert small_a == small_b
    assert large_a == large_b

    # A divergence within the first 2 KB flips BOTH windows.
    small_c, large_c = prompt_prefix_fingerprint("different" + "x" * 20000)
    assert small_c != small_a
    assert large_c != large_a

    # A divergence between the 2 KB and 16 KB windows flips only the large one.
    head_2k = "y" * 3000  # shared through the 2 KB window
    small_d, large_d = prompt_prefix_fingerprint(head_2k + "A" + "z" * 20000)
    small_e, large_e = prompt_prefix_fingerprint(head_2k + "B" + "z" * 20000)
    assert small_d == small_e  # first 2 KB identical
    assert large_d != large_e  # differ within the 16 KB window


def test_emit_helpers_are_noops_when_gate_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    audit = tmp_path / "audit.jsonl"
    monkeypatch.delenv("CLIO_STREAM_AUDIT_LOG", raising=False)
    emit_call_started(call_id="c", call_index=1, model="haiku", transport="sdk", prompt="hi")
    emit_call_usage(
        call_id="c",
        call_index=1,
        model="haiku",
        transport="sdk",
        usage={"output_tokens": 3},
        output_chars=5,
    )
    assert not audit.exists()  # nothing was written anywhere


def test_emit_call_usage_flattens_usage_dict(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    audit = tmp_path / "audit.jsonl"
    monkeypatch.setenv("CLIO_STREAM_AUDIT_LOG", str(audit))
    emit_call_usage(
        call_id="c9",
        call_index=9,
        model="haiku",
        transport="sdk",
        usage={
            "input_tokens": 200,
            "output_tokens": 10,
            "cache_read_input_tokens": 1000,
            "cache_creation_input_tokens": 0,
        },
        output_chars=42,
    )
    rows = _read_rows(audit)
    assert len(rows) == 1
    row = rows[0]
    assert row["stage"] == "provider.call_usage"
    assert row["provider"] == "claude_code_sdk"
    assert row["call_id"] == "c9"
    assert row["usage_input_tokens"] == 200
    assert row["usage_cache_read_input_tokens"] == 1000
    assert row["usage_raw"] == {
        "input_tokens": 200,
        "output_tokens": 10,
        "cache_read_input_tokens": 1000,
        "cache_creation_input_tokens": 0,
    }
    assert row["usage_keys"] == sorted(row["usage_raw"].keys())


def test_emit_call_usage_survives_colliding_usage_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    audit = tmp_path / "audit.jsonl"
    monkeypatch.setenv("CLIO_STREAM_AUDIT_LOG", str(audit))
    # A usage payload whose keys flatten to ``usage_keys`` / ``usage_raw`` collides
    # with the explicit fields. The audit must degrade (explicit wins), never raise
    # a TypeError that — from _astream_sdk's finally — would mask the call outcome.
    emit_call_usage(
        call_id="c",
        call_index=1,
        model="haiku",
        transport="sdk",
        usage={"keys": 1, "raw": 2, "input_tokens": 5},
        output_chars=3,
    )
    rows = _read_rows(audit)
    assert len(rows) == 1
    row = rows[0]
    assert row["usage_keys"] == ["input_tokens", "keys", "raw"]  # explicit list wins
    assert row["usage_raw"] == {"keys": 1, "raw": 2, "input_tokens": 5}  # explicit dict wins
    assert row["usage_input_tokens"] == 5  # non-colliding key still flattened


async def test_call_started_brackets_connect_and_usage_precedes_release(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """#891, re-pinned for S2 (B1): the pooled per-GACT-session client stays warm
    after a call, so this proves the SDK connect (cold-start, paid once per session)
    falls INSIDE the [call_started -> call_usage] window (else it is misfiled as
    inter_call_gap), and that call_usage is recorded before the session's eventual
    release/disconnect -- so usage survives even if that later teardown fails."""
    from clio_agent import conf  # noqa: PLC0415
    from clio_agent.providers.claude_code_sessions import _STREAM_CLIENT_POOL  # noqa: PLC0415

    audit = tmp_path / "audit.jsonl"
    monkeypatch.setenv("CLIO_STREAM_AUDIT_LOG", str(audit))
    conf.reload()
    delay = 0.5
    sdk = fake.install(monkeypatch, connect_delay=delay)

    from clio_agent.gact import context as gact_context  # noqa: PLC0415

    token = gact_context.set_session_id("sess-audit")
    try:
        await fake.drive(fake.request())
        # B1: the connection stays warm after the call -- release it explicitly
        # (mirrors a real session-end) to observe the disconnect ordering.
        _STREAM_CLIENT_POOL.release("sess-audit")
    finally:
        gact_context.reset(token)
        conf.reload()

    rows = _read_rows(audit)
    started = next(r for r in rows if r["stage"] == "provider.call_started")
    usage = next(r for r in rows if r["stage"] == "provider.call_usage")
    connect_enter = next(t for name, t in sdk.events if name == "connect_enter")
    disconnect_enter = next(t for name, t in sdk.events if name == "disconnect_enter")

    # Marker opens before connect begins; connect's 0.5 s lands inside the window.
    # Tolerance 50ms: both stamps are sequential time.time() calls and wall clock is
    # not monotonic; a real misorder would be off by ~delay (500ms).
    assert started["ts"] <= connect_enter + 0.05
    assert usage["ts"] - started["ts"] >= delay * 0.8
    # Usage is recorded before the session's eventual teardown.
    assert usage["ts"] <= disconnect_enter + 0.05


async def test_engine_emits_call_rows_when_gate_on(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    audit = tmp_path / "audit.jsonl"
    monkeypatch.setenv("CLIO_STREAM_AUDIT_LOG", str(audit))
    fake.install(
        monkeypatch,
        reply=lambda: fake.answer("Hello", usage={"input_tokens": 2, "output_tokens": 3}),
    )

    response = await fake.drive(fake.request())
    assert response.message.parts[0].text == "Hello"  # the call itself unaffected

    rows = _read_rows(audit)
    started = [r for r in rows if r["stage"] == "provider.call_started"]
    usage = [r for r in rows if r["stage"] == "provider.call_usage"]
    assert len(started) == 1
    assert len(usage) == 1

    s, u = started[0], usage[0]
    assert s["transport"] == "sdk"
    assert s["provider"] == "claude_code_sdk"
    assert s["prompt_chars"] == len("[user]\nhello")
    assert s["prefix_2k_sha256"] and s["prefix_16k_sha256"]
    # call_id and call_index correlate the started row with its usage row.
    assert s["call_id"] == u["call_id"]
    assert s["call_index"] == u["call_index"]
    assert u["usage_input_tokens"] == 2
    assert u["usage_output_tokens"] == 3
    assert u["output_chars"] == len("Hello")


async def test_engine_writes_no_call_rows_when_gate_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    audit = tmp_path / "audit.jsonl"
    monkeypatch.delenv("CLIO_STREAM_AUDIT_LOG", raising=False)
    fake.install(monkeypatch)

    response = await fake.drive(fake.request())
    assert response.message.parts[0].text == "Answer"  # still produces output

    rows = _read_rows(audit)
    assert [r for r in rows if r["stage"] in ("provider.call_started", "provider.call_usage")] == []
