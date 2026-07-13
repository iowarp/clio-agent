"""Tests for the #891 Claude Code stream-audit instrumentation.

Covers the audit module (:mod:`clio_agent.providers.claude_code_audit`) in
isolation AND end-to-end through the real streaming bridge
(:func:`clio_agent.providers.claude_code_litellm._astream_sdk`): the new
``provider.call_started`` / ``provider.call_usage`` rows must be written when the
existing ``CLIO_STREAM_AUDIT_LOG`` gate is on and must NOT be written when it is
off (zero-overhead contract).

Sabotage check: deleting the ``emit_call_usage`` call in ``_astream_sdk`` (or its
body) makes :func:`test_astream_sdk_emits_call_rows_when_gate_on` fail on the
``provider.call_usage`` assertions — the emission is genuinely exercised, not
mocked away.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, AsyncIterator

import pytest

from clio_agent.providers import claude_code_litellm
from clio_agent.providers.claude_code_audit import (
    emit_call_started,
    emit_call_usage,
    prompt_prefix_fingerprint,
)
from clio_agent.providers.claude_code_sessions import _reset_sessions_for_tests


@pytest.fixture(autouse=True)
def _clean_stream_pool() -> Any:
    """Each test gets a fresh streaming client pool — the pooled default (#891)
    would otherwise carry one test's fake SDK client into the next test's
    differently-faked module (isinstance mismatch -> empty stream)."""
    _reset_sessions_for_tests()
    yield
    _reset_sessions_for_tests()


def _read_rows(path: Path) -> list[dict[str, Any]]:
    """Read a stream-audit JSONL file into a list of row dicts."""
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _install_fake_sdk(monkeypatch: pytest.MonkeyPatch, *, usage: dict[str, Any]) -> None:
    """Install a minimal fake ``claude_agent_sdk`` that streams two tokens."""

    class FakeTextBlock:
        def __init__(self, text: str) -> None:
            self.text = text

    class FakeStreamEvent:
        def __init__(self, event: dict[str, Any]) -> None:
            self.event = event

    class FakeAssistantMessage:
        def __init__(self) -> None:
            self.content = [FakeTextBlock("Hello")]
            self.usage = usage
            self.stop_reason = "end_turn"

    class FakeResultMessage:
        def __init__(self) -> None:
            self.usage = usage
            self.stop_reason = "end_turn"
            self.result = "Hello"
            self.is_error = False

    class FakeClaudeAgentOptions:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

    class FakeClaudeSDKClient:
        def __init__(self, options: FakeClaudeAgentOptions) -> None:
            self.options = options

        async def connect(self) -> None:
            return None

        async def disconnect(self) -> None:
            return None

        async def query(self, prompt: str, session_id: str = "default") -> None:
            return None

        async def receive_response(self) -> AsyncIterator[Any]:
            yield FakeStreamEvent(
                {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hel"}}
            )
            yield FakeStreamEvent(
                {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "lo"}}
            )
            yield FakeAssistantMessage()
            yield FakeResultMessage()

    fake_sdk = ModuleType("claude_agent_sdk")
    fake_sdk.AssistantMessage = FakeAssistantMessage
    fake_sdk.ClaudeAgentOptions = FakeClaudeAgentOptions
    fake_sdk.ClaudeSDKClient = FakeClaudeSDKClient
    fake_sdk.ResultMessage = FakeResultMessage
    fake_sdk.StreamEvent = FakeStreamEvent
    fake_sdk.TextBlock = FakeTextBlock
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake_sdk)


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


def _install_timed_fake_sdk(
    monkeypatch: pytest.MonkeyPatch, *, connect_delay: float, events: list[tuple[str, float]]
) -> None:
    """Install a fake SDK whose connect/disconnect record entry times, so a test
    can prove the per-call spawn/teardown falls inside the audit call window.
    """
    import time as _time

    class FakeTextBlock:
        def __init__(self, text: str) -> None:
            self.text = text

    class FakeStreamEvent:
        def __init__(self, event: dict[str, Any]) -> None:
            self.event = event

    class FakeAssistantMessage:
        def __init__(self) -> None:
            self.content = [FakeTextBlock("Hi")]
            self.usage = {"input_tokens": 1, "output_tokens": 1}
            self.stop_reason = "end_turn"

    class FakeResultMessage:
        def __init__(self) -> None:
            self.usage = {"input_tokens": 1, "output_tokens": 1}
            self.stop_reason = "end_turn"
            self.result = "Hi"
            self.is_error = False

    class FakeClaudeAgentOptions:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

    class FakeClaudeSDKClient:
        def __init__(self, options: FakeClaudeAgentOptions) -> None:
            self.options = options

        async def connect(self) -> None:
            events.append(("connect_enter", _time.time()))
            import asyncio as _asyncio

            await _asyncio.sleep(connect_delay)

        async def disconnect(self) -> None:
            events.append(("disconnect_enter", _time.time()))

        async def query(self, prompt: str, session_id: str = "default") -> None:
            return None

        async def receive_response(self) -> AsyncIterator[Any]:
            yield FakeStreamEvent(
                {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hi"}}
            )
            yield FakeAssistantMessage()
            yield FakeResultMessage()

    fake_sdk = ModuleType("claude_agent_sdk")
    fake_sdk.AssistantMessage = FakeAssistantMessage
    fake_sdk.ClaudeAgentOptions = FakeClaudeAgentOptions
    fake_sdk.ClaudeSDKClient = FakeClaudeSDKClient
    fake_sdk.ResultMessage = FakeResultMessage
    fake_sdk.StreamEvent = FakeStreamEvent
    fake_sdk.TextBlock = FakeTextBlock
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake_sdk)


async def test_call_started_brackets_connect_and_usage_precedes_disconnect(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # #891 finding, pinned on the PER-CALL transport (kill-switch off — the pooled
    # default never disconnects per call): the SDK connect (cold-start) must be
    # INSIDE the [call_started -> call_usage] window (else it is misfiled as
    # inter_call_gap), and call_usage must be recorded BEFORE disconnect.
    from clio_agent import conf  # noqa: PLC0415

    audit = tmp_path / "audit.jsonl"
    monkeypatch.setenv("CLIO_STREAM_AUDIT_LOG", str(audit))
    monkeypatch.setenv("CLIO_CLAUDE_CODE_SESSION_REUSE", "false")
    conf.reload()
    events: list[tuple[str, float]] = []
    delay = 0.5
    _install_timed_fake_sdk(monkeypatch, connect_delay=delay, events=events)

    try:
        async for _ in claude_code_litellm._astream_sdk(
            prompt="hello", model="haiku", timeout=5.0, cwd="/tmp/clio", call_index=3
        ):
            pass
    finally:
        conf.reload()

    rows = _read_rows(audit)
    started = next(r for r in rows if r["stage"] == "provider.call_started")
    usage = next(r for r in rows if r["stage"] == "provider.call_usage")
    connect_enter = next(t for name, t in events if name == "connect_enter")
    disconnect_enter = next(t for name, t in events if name == "disconnect_enter")

    # Marker opens before connect begins; connect's 0.5 s lands inside the window.
    # Tolerance 50ms, not 1ms: both stamps are sequential time.time() calls, and
    # wall clock is NOT monotonic — an NTP slew inverted them by 1.5ms in a full
    # suite run. A real misorder would be off by ~delay (500ms), so 50ms keeps
    # 10x discrimination while being immune to clock adjustment.
    assert started["ts"] <= connect_enter + 0.05
    assert usage["ts"] - started["ts"] >= delay * 0.8
    # Usage is recorded before teardown, so it survives a disconnect failure.
    assert usage["ts"] <= disconnect_enter + 0.05


async def test_astream_sdk_emits_call_rows_when_gate_on(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    audit = tmp_path / "audit.jsonl"
    monkeypatch.setenv("CLIO_STREAM_AUDIT_LOG", str(audit))
    _install_fake_sdk(monkeypatch, usage={"input_tokens": 2, "output_tokens": 3})

    chunks = [
        chunk
        async for chunk in claude_code_litellm._astream_sdk(
            prompt="hello", model="haiku", timeout=5.0, cwd="/tmp/clio", call_index=7
        )
    ]
    assert [c["text"] for c in chunks] == ["Hel", "lo", ""]  # bridge unaffected

    rows = _read_rows(audit)
    started = [r for r in rows if r["stage"] == "provider.call_started"]
    usage = [r for r in rows if r["stage"] == "provider.call_usage"]
    assert len(started) == 1
    assert len(usage) == 1

    s, u = started[0], usage[0]
    assert s["call_index"] == 7
    assert s["transport"] == "sdk"
    assert s["provider"] == "claude_code_sdk"
    assert s["prompt_chars"] == len("hello")
    assert s["prefix_2k_sha256"] and s["prefix_16k_sha256"]
    # call_id correlates the started row with its usage row.
    assert s["call_id"] == u["call_id"]
    assert u["call_index"] == 7
    assert u["usage_input_tokens"] == 2
    assert u["usage_output_tokens"] == 3
    assert u["output_chars"] == len("Hello")


async def test_astream_sdk_writes_no_call_rows_when_gate_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    audit = tmp_path / "audit.jsonl"
    monkeypatch.delenv("CLIO_STREAM_AUDIT_LOG", raising=False)
    _install_fake_sdk(monkeypatch, usage={"input_tokens": 2, "output_tokens": 3})

    chunks = [
        chunk
        async for chunk in claude_code_litellm._astream_sdk(
            prompt="hello", model="haiku", timeout=5.0, cwd="/tmp/clio", call_index=7
        )
    ]
    assert [c["text"] for c in chunks] == ["Hel", "lo", ""]  # still produces output

    rows = _read_rows(audit)
    assert [r for r in rows if r["stage"] in ("provider.call_started", "provider.call_usage")] == []
