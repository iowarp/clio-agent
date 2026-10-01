"""The ``lm.call`` trace callback (:mod:`clio_agent.lm.call_trace`)."""

from __future__ import annotations

from typing import Any

import dspy
import pytest
from dspy.lm15 import ContextLengthError, ImagePart, Message, Request, TextPart

from clio_agent.lm import call_trace
from clio_agent.lm.call_trace import LMCallTrace, call_record
from tests._scripted_engine import Reply, calls, scripted_lm


@pytest.fixture
def emitted(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    monkeypatch.setattr(call_trace, "_emit", rows.append)
    return rows


def _request(lm: dspy.LM, *messages: Message) -> Request:
    return Request(model=lm.model, system="sys", messages=messages)


def test_a_request_call_records_messages_reply_thinking_and_usage(
    emitted: list[dict[str, Any]],
) -> None:
    lm, _ = scripted_lm(
        [calls(("search", {"q": "x"}), text="Looking.", thinking="plan")],
        callbacks=[LMCallTrace()],
    )
    image = ImagePart(data="aGk=", media_type="image/png")
    lm(_request(lm, Message(role="user", parts=(TextPart(text="what?"), image))))

    [record] = emitted
    assert record["model"] == "scripted/model"
    assert record["messages"] == [
        {"role": "system", "content": "sys"},
        {
            "role": "user",
            "parts": [
                {"type": "text", "text": "what?"},
                {"type": "image", "media_type": "image/png"},
            ],
        },
    ]
    assert (record["content"], record["reasoning_content"]) == ("Looking.", "plan")
    assert record["finish_reason"] == "tool_call"
    assert record["usage"] == {"input_tokens": 10, "output_tokens": 5}
    assert "error" not in record


def test_a_failed_call_is_recorded_with_its_error(emitted: list[dict[str, Any]]) -> None:
    lm, _ = scripted_lm([Reply(raises=ContextLengthError("too long"))], callbacks=[LMCallTrace()])
    with pytest.raises(Exception, match="too long"):
        lm(_request(lm, Message.user("q")))
    [record] = emitted
    assert "too long" in record["error"]


def test_adapter_outputs_are_recorded_too() -> None:
    record = call_record(
        "m",
        {"messages": [{"role": "user", "content": "q"}]},
        [{"text": "a", "reasoning_content": "r"}],
        None,
    )
    assert (record["messages"], record["content"], record["reasoning_content"]) == (
        [{"role": "user", "content": "q"}],
        "a",
        "r",
    )


def test_an_emit_failure_never_fails_the_call(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(record: dict[str, Any]) -> None:
        raise RuntimeError("store down")

    monkeypatch.setattr(call_trace, "_emit", boom)
    lm, _ = scripted_lm([Reply(text="ok")], callbacks=[LMCallTrace()])
    assert lm(_request(lm, Message.user("q"))).message.parts == (TextPart(text="ok"),)


def test_an_async_call_is_recorded_exactly_once(emitted: list[dict[str, Any]]) -> None:
    import asyncio

    lm, _ = scripted_lm([Reply(text="ok")], callbacks=[LMCallTrace()])
    asyncio.run(lm.acall(_request(lm, Message.user("q"))))
    assert [r["content"] for r in emitted] == ["ok"]


def test_factory_lms_carry_the_trace() -> None:
    from clio_agent.config import LMProviderConfig
    from clio_agent.lm.factory import create_lm

    for config in (
        LMProviderConfig(provider="lm_studio", model="qwen", api_key="lm-studio"),
        LMProviderConfig(provider="codex", model="gpt-5.5", api_base="codex://direct"),
    ):
        assert call_trace.LM_CALL_TRACE in create_lm(config).callbacks
