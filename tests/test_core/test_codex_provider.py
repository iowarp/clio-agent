"""Focused contract tests for the sole official Codex Python SDK transport."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.providers import codex_litellm, codex_stream
from clio_agent.providers.claude_code_cancel import (
    _reset_for_tests as reset_sdk_cancel_registry,
)
from clio_agent.providers.claude_code_cancel import (
    abort_session_streams,
    active_stream_sessions,
)
from clio_agent.providers.codex_litellm import (
    CodexLLM,
    CodexUnsupportedMultimodalError,
    _messages_to_codex_prompt,
)


def _event(method: str, **payload: Any) -> SimpleNamespace:
    return SimpleNamespace(method=method, payload=SimpleNamespace(**payload))


def test_messages_serialize_without_losing_roles() -> None:
    prompt = _messages_to_codex_prompt(
        [
            {"role": "system", "content": "be terse"},
            {"role": "user", "content": "ping"},
        ]
    )
    rows = [json.loads(line) for line in prompt.splitlines() if line.startswith("{")]
    assert rows == [
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "ping"},
    ]


def test_multimodal_input_fails_instead_of_being_dropped() -> None:
    with pytest.raises(CodexUnsupportedMultimodalError, match="image message parts"):
        _messages_to_codex_prompt(
            [
                {
                    "role": "user",
                    "content": [{"type": "image_url", "image_url": {"url": "x"}}],
                }
            ]
        )


def test_only_sdk_transport_is_accepted() -> None:
    assert CodexLLM._resolve_transport({}) == "sdk"
    assert CodexLLM._resolve_transport({"codex_transport": "sdk"}) == "sdk"
    with pytest.raises(codex_stream.CodexSDKError, match="official Python SDK"):
        CodexLLM._resolve_transport({"codex_transport": "app_server"})


def test_sdk_boundary_disables_personal_capabilities() -> None:
    assert "mcp_servers={}" in codex_stream.BARE_LM_CONFIG_OVERRIDES
    assert "plugins={}" in codex_stream.BARE_LM_CONFIG_OVERRIDES
    assert all(enabled is False for enabled in codex_stream.BARE_LM_FEATURES.values())
    assert codex_stream.BARE_LM_THREAD_CONFIG["mcp_servers"] == {}
    assert "Clio owns the agent loop" in codex_stream.BARE_LM_BASE_INSTRUCTIONS


def test_sdk_cleanup_failure_is_reported(caplog: pytest.LogCaptureFixture) -> None:
    async def fail_close() -> None:
        raise RuntimeError("close broke")

    with caplog.at_level(logging.WARNING):
        asyncio.run(codex_stream._cleanup_sdk_action("test_close", fail_close()))

    assert "reason=codex_sdk_cleanup_failed" in caplog.text
    assert "action=test_close" in caplog.text


def test_sdk_home_contains_auth_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    personal_home = tmp_path / "personal"
    personal_home.mkdir()
    (personal_home / "auth.json").write_text('{"token":"secret"}', encoding="utf-8")
    (personal_home / "config.toml").write_text("[mcp_servers.bad]", encoding="utf-8")
    (personal_home / "plugins").mkdir()
    monkeypatch.setenv("CODEX_HOME", str(personal_home))

    isolated = codex_stream.IsolatedCodexHome()
    environment = isolated.start()
    isolated_path = Path(environment["CODEX_HOME"])
    try:
        assert {path.name for path in isolated_path.iterdir()} == {
            ".clio-owner-pid",
            "auth.json",
            "sqlite",
        }
        assert (isolated_path / "auth.json").read_text(encoding="utf-8") == '{"token":"secret"}'
    finally:
        isolated.close()
    assert not isolated_path.exists()


def test_sdk_home_reconciles_uncontended_auth_rotation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    personal_home = tmp_path / "personal"
    personal_home.mkdir()
    source_auth = personal_home / "auth.json"
    source_auth.write_text('{"token":"old"}', encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(personal_home))

    isolated = codex_stream.IsolatedCodexHome()
    isolated_path = Path(isolated.start()["CODEX_HOME"])
    (isolated_path / "auth.json").write_text('{"token":"rotated"}', encoding="utf-8")
    isolated.close()

    assert source_auth.read_text(encoding="utf-8") == '{"token":"rotated"}'


def test_hidden_sdk_action_is_a_typed_error() -> None:
    item = SimpleNamespace(root=SimpleNamespace(type="commandExecution"))
    with pytest.raises(codex_stream.CodexSDKError, match="hidden internal action"):
        codex_stream._validate_bare_lm_event(_event("item/started", item=item))


def test_unknown_sdk_informational_item_is_typed_skip(caplog: pytest.LogCaptureFixture) -> None:
    item = SimpleNamespace(root=SimpleNamespace(type="sdkNotice"))
    with caplog.at_level(logging.INFO):
        codex_stream._validate_bare_lm_event(_event("item/started", item=item))
    assert "reason=codex_sdk_informational_item_skipped" in caplog.text


def test_sdk_home_reaps_orphan_and_caps_live_copies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    orphan = tmp_path / "clio-codex-sdk-orphan"
    orphan.mkdir()
    (orphan / ".clio-owner-pid").write_text("99999999", encoding="ascii")
    with caplog.at_level(logging.INFO):
        assert codex_stream._reap_orphaned_codex_homes(tmp_path) == [orphan]
    assert not orphan.exists()
    assert "reason=credential_homes_reaped" in caplog.text

    personal_home = tmp_path / "personal"
    personal_home.mkdir()
    (personal_home / "auth.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(personal_home))
    monkeypatch.setattr(codex_stream, "MAX_LIVE_CODEX_HOMES", 1)
    first = codex_stream.IsolatedCodexHome()
    first.start()
    try:
        with pytest.raises(codex_stream.CodexSDKError, match="credential_home_capacity"):
            codex_stream.IsolatedCodexHome().start()
    finally:
        first.close()


@pytest.mark.asyncio
async def test_sdk_startup_is_inside_progress_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = codex_stream.CodexSDKClient()

    async def stalled_client() -> Any:
        await asyncio.Event().wait()

    monkeypatch.setenv("CLIO_CODEX_SDK_PROGRESS_TIMEOUT_S", "0.02")
    monkeypatch.setattr(client, "_ensure_client", stalled_client)
    try:
        with pytest.raises(codex_stream.CodexSDKError, match="codex_sdk_progress_timeout"):
            async for _event_row in client.stream(
                prompt="prompt",
                model="gpt-5.6-luna",
                cwd=None,
                effort="medium",
                timeout=1.0,
            ):
                pass
    finally:
        client.close_blocking()


@pytest.mark.asyncio
async def test_sdk_stream_is_interrupted_by_scoped_session_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelling one GACT session interrupts its SDK turn without late output."""

    from clio_agent.gact import context as gact_context

    client = codex_stream.CodexSDKClient()
    interrupted = asyncio.Event()
    release = asyncio.Event()

    class FakeTurn:
        def stream(self):
            async def events():
                yield _event("item/agentMessage/delta", delta="started")
                await release.wait()

            return events()

        async def interrupt(self) -> None:
            interrupted.set()

    class FakeThread:
        async def turn(self, *_args: Any, **_kwargs: Any) -> FakeTurn:
            return FakeTurn()

    class FakeClient:
        async def thread_start(self, **_kwargs: Any) -> FakeThread:
            return FakeThread()

    async def fake_ensure_client() -> FakeClient:
        return FakeClient()

    reset_sdk_cancel_registry()
    monkeypatch.setattr(client, "_ensure_client", fake_ensure_client)
    token = gact_context.set_session_id("sess_codex_cancel")
    seen: list[str] = []
    try:

        async def consume() -> None:
            async for event in client.stream(
                prompt="prompt",
                model="gpt-5.6-luna",
                cwd=None,
                effort="medium",
                timeout=5.0,
            ):
                seen.append(str(getattr(event.payload, "delta", "")))

        task = asyncio.create_task(consume())
        for _ in range(100):
            if "sess_codex_cancel" in active_stream_sessions():
                break
            await asyncio.sleep(0.01)
        assert active_stream_sessions() == {"sess_codex_cancel"}
        assert abort_session_streams("sess_codex_cancel") == 1
        await asyncio.wait_for(interrupted.wait(), timeout=2.0)
        await asyncio.wait_for(task, timeout=2.0)
    finally:
        release.set()
        gact_context.reset(token)
        reset_sdk_cancel_registry()
        client.close_blocking()

    assert seen == ["started"]


@pytest.mark.asyncio
async def test_sdk_progress_deadline_resets_on_every_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = codex_stream.CodexSDKClient()

    class FakeTurn:
        def stream(self):
            async def events():
                for value in ("one", "two", "three", "four"):
                    await asyncio.sleep(0.04)
                    yield _event("item/agentMessage/delta", delta=value)

            return events()

    class FakeThread:
        async def turn(self, *_args: Any, **_kwargs: Any) -> FakeTurn:
            return FakeTurn()

    class FakeClient:
        async def thread_start(self, **_kwargs: Any) -> FakeThread:
            return FakeThread()

    async def fake_ensure_client() -> FakeClient:
        return FakeClient()

    monkeypatch.setenv("CLIO_CODEX_SDK_PROGRESS_TIMEOUT_S", "0.1")
    monkeypatch.setattr(client, "_ensure_client", fake_ensure_client)
    seen = [
        event.payload.delta
        async for event in client.stream(
            prompt="prompt",
            model="gpt-5.6-luna",
            cwd=None,
            effort="medium",
            timeout=1.0,
        )
    ]
    client.close_blocking()
    assert seen == ["one", "two", "three", "four"]


@pytest.mark.asyncio
async def test_sdk_stream_keeps_raw_reasoning_and_summary_distinct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    usage_last = SimpleNamespace(
        input_tokens=100,
        cached_input_tokens=20,
        cache_write_input_tokens=0,
        output_tokens=30,
        reasoning_output_tokens=12,
        total_tokens=130,
    )
    events = [
        _event("item/reasoning/textDelta", delta="raw thought"),
        _event("item/reasoning/summaryPartAdded"),
        _event("item/reasoning/summaryTextDelta", delta="summary one"),
        _event("item/reasoning/summaryPartAdded"),
        _event("item/reasoning/summaryTextDelta", delta="summary two"),
        _event("item/agentMessage/delta", delta="answer"),
        _event(
            "thread/tokenUsage/updated",
            token_usage=SimpleNamespace(last=usage_last),
        ),
    ]

    async def fake_stream(**_kwargs: Any):
        for event in events:
            yield event

    observed: list[tuple[str, str]] = []
    monkeypatch.setattr(codex_stream._SDK_CLIENT, "stream", fake_stream)
    monkeypatch.setattr(
        "clio_agent.runtime.lm_activity.note_lm_provider_thinking_delta",
        lambda text, *, provider="": observed.append((provider, text)),
    )

    chunks = [
        chunk
        async for chunk in codex_stream.astream_sdk(
            prompt="prompt",
            model="gpt-5.6-luna",
            cwd=None,
            effort="medium",
            timeout=30.0,
            call_index=1,
        )
    ]

    assert "".join(str(chunk["text"]) for chunk in chunks) == "answer"
    assert observed == [
        ("codex_sdk_reasoning", "raw thought"),
        ("codex_sdk_summary", "summary one"),
        ("codex_sdk_summary", "\n\n"),
        ("codex_sdk_summary", "summary two"),
    ]
    assert chunks[-1]["usage"] == {
        "prompt_tokens": 100,
        "completion_tokens": 30,
        "reasoning_output_tokens": 12,
        "total_tokens": 130,
    }


def test_sdk_usage_preserves_reasoning_as_output_subset() -> None:
    payload = SimpleNamespace(
        token_usage=SimpleNamespace(
            last=SimpleNamespace(
                input_tokens=10,
                cached_input_tokens=2,
                cache_write_input_tokens=0,
                output_tokens=7,
                reasoning_output_tokens=5,
                total_tokens=17,
            )
        )
    )
    usage = codex_stream._normalize_usage(payload)
    assert usage["reasoning_output_tokens"] == 5
    assert usage["output_tokens"] == 7
    assert usage["total_tokens"] == 17


def test_completion_calls_sdk_not_app_server(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_run_sdk(**kwargs: Any) -> tuple[str, dict[str, int]]:
        captured.update(kwargs)
        return "ok", {"input_tokens": 4, "output_tokens": 1, "total_tokens": 5}

    monkeypatch.setattr(codex_litellm, "run_sdk", fake_run_sdk)
    response = CodexLLM().completion(
        model="gpt-5.6-luna",
        messages=[{"role": "user", "content": "hi"}],
        api_base="",
        custom_prompt_dict={},
        model_response=None,
        print_verbose=None,
        encoding=None,
        api_key=None,
        logging_obj=None,
        optional_params={"codex_transport": "sdk"},
    )
    assert response.choices[0].message.content == "ok"
    assert captured["model"] == "gpt-5.6-luna"


def test_model_response_preserves_reasoning_token_count() -> None:
    response = codex_litellm._build_model_response(
        text="answer",
        model="gpt-5.6-luna",
        usage_payload={
            "input_tokens": 100,
            "output_tokens": 30,
            "reasoning_output_tokens": 21,
            "total_tokens": 130,
        },
    )
    assert response.usage.completion_tokens_details.reasoning_tokens == 21
