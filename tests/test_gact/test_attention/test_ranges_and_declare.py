"""Range declaration: sections of a real CLIO prompt, and the vLLM request wiring."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from clio_agent.gact.attention import declare as declare_mod
from clio_agent.gact.attention import tokenizer_source
from clio_agent.gact.attention.chat_render import ChatRenderer, Encoded
from clio_agent.gact.attention.declare import build_declaration, current_declaration
from clio_agent.gact.attention.ranges import declare_ranges
from clio_agent.gact.attention.textmap import locate
from tests.test_gact.test_attention._support import (
    TEMPLATE,
    FixtureRenderer,
    fixture,
    tiny_tokenizer_json,
)


def _real_declaration() -> tuple[Any, Encoded]:
    data = fixture()
    encoded = FixtureRenderer().render_encoded(data["lm_call"]["messages"])
    return declare_ranges(data["lm_call"]["messages"], encoded), encoded


@pytest.mark.parametrize("unexpected", [False, True])
def test_renderer_failures_are_typed_or_propagated(
    monkeypatch: pytest.MonkeyPatch, unexpected: bool
) -> None:
    def resolve(*args: Any) -> Any:
        if unexpected:
            raise AssertionError("programming defect")
        raise ValueError("private prompt content")

    monkeypatch.setattr(tokenizer_source, "resolve_renderer", resolve)
    arguments = {"model": "hosted_vllm/test", "messages": [], "lm_kwargs": {}, "call_kwargs": {}}
    if unexpected:
        with pytest.raises(AssertionError, match="programming defect"):
            build_declaration(**arguments)
    else:
        _, record = build_declaration(**arguments)
        assert record["reason"] == "attention_tokenizer_unavailable"
        assert record["detail"] == "render failed: ValueError"


def test_real_prompt_sections_are_ordered_disjoint_and_labelled() -> None:
    declaration, encoded = _real_declaration()
    ranges = declaration.ranges
    assert declaration.unlocated_messages == []
    assert declaration.prompt_token_count == 15514
    assert all(a.hi <= b.lo for a, b in zip(ranges, ranges[1:], strict=False))
    by_label = {(r.domain, r.label) for r in ranges}
    assert {
        ("system", "adapter_instructions"),
        ("system", "system_prompt"),
        ("user", "question"),
        ("tool_definitions", "tools"),
        ("thinking", "next_thought"),
        ("tool_call", "tool_calls"),
        ("tool_result", "load_skill"),
        ("tool_result", "create_artifact"),
    } <= by_label


def test_section_token_spans_cover_their_text_exactly() -> None:
    declaration, encoded = _real_declaration()
    question = next(r for r in declaration.ranges if r.label == "question")
    text = encoded.text[question.char_lo : question.char_hi]
    assert "I want recent ground-motion data around Los Angeles" in text
    lo, hi = encoded.char_span(question.lo, question.hi)
    assert encoded.text[lo:hi].strip() == text.strip()
    # The last section is the adapter's output-format instructions (DSPy's own text).
    assert declaration.ranges[-1].label == "adapter_instructions"
    assert encoded.text[declaration.ranges[-1].char_lo :].startswith("Respond with")


def test_unlocatable_message_is_reported_not_dropped() -> None:
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hello"},
    ]
    encoded = Encoded(
        text="<s>system sys user HELLO",
        ids=[1, 2, 3, 4],
        offsets=[(0, 3), (3, 10), (10, 14), (14, 24)],
    )
    declaration = declare_ranges(messages, encoded)
    assert declaration.unlocated_messages == [1]


def test_token_span_follows_the_connector_e2e_rule() -> None:
    encoded = Encoded(text="aa bb cc", ids=[1, 2, 3], offsets=[(0, 2), (2, 5), (5, 8)])
    assert encoded.token_span(3, 5) == (1, 2)  # chars inside token 1
    assert encoded.token_span(0, 8) == (0, 3)
    assert encoded.token_span(2, 6) == (1, 3)


def test_json_escaped_text_is_located_with_an_exact_char_map() -> None:
    haystack = '{"value": "line one\\nsaid \\"hi\\""}'
    needle = 'line one\nsaid "hi"'
    hit = locate(haystack, needle)
    assert hit is not None and hit.encoding == "json"
    lo, hi = hit.to_haystack(9, 13)  # 'said'
    assert haystack[lo:hi] == "said"
    assert hit.from_haystack(lo, hi) == (9, 13)


@pytest.fixture
def tiny_tokenizer_dir(tmp_path: Path) -> Path:
    corpus = [TEMPLATE, "hello world question tools answer " * 40]
    (tmp_path / "tokenizer.json").write_text(tiny_tokenizer_json(corpus), encoding="utf-8")
    (tmp_path / "chat_template.jinja").write_text(TEMPLATE, encoding="utf-8")
    (tmp_path / "tokenizer_config.json").write_text(
        '{"bos_token": "<s>", "eos_token": "<|im_end|>"}', encoding="utf-8"
    )
    tokenizer_source.clear_cache()
    yield tmp_path
    tokenizer_source.clear_cache()


MESSAGES = [
    {"role": "system", "content": "Your input fields are: question"},
    {"role": "user", "content": "[[ ## question ## ]]\nhello world"},
]


def test_non_vllm_provider_is_never_declared() -> None:
    kwargs, record = build_declaration(
        model="claude_code/sonnet", messages=MESSAGES, lm_kwargs={}, call_kwargs={"x": 1}
    )
    assert kwargs == {"x": 1}
    assert record["status"] == "not_declared" and record["reason"] == "provider_not_vllm"


def test_vllm_request_carries_ranges_and_keeps_existing_extra_body(
    tiny_tokenizer_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLIO_PROVENANCE_ATTENTION_TOKENIZER", str(tiny_tokenizer_dir))
    kwargs, record = build_declaration(
        model="hosted_vllm/granite-4.2-30b",
        messages=MESSAGES,
        lm_kwargs={"api_base": "http://127.0.0.1:1/v1", "extra_body": {"top_k": 20}},
        call_kwargs={"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}},
    )
    body = kwargs["extra_body"]
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["top_k"] == 20
    ranges = body["kv_transfer_params"]["ranges"]
    assert record["status"] == "declared"
    assert [[r["lo"], r["hi"]] for r in record["ranges"]] == ranges
    assert {r["label"] for r in record["ranges"]} == {"adapter_instructions", "question"}
    assert record["template_kwargs"] == {"enable_thinking": False}
    renderer = ChatRenderer.from_dir(tiny_tokenizer_dir)
    encoded = renderer.render_encoded(MESSAGES, {"enable_thinking": False})
    assert record["prompt_token_count"] == len(encoded.ids)
    question = next(r for r in record["ranges"] if r["label"] == "question")
    lo, hi = encoded.char_span(question["lo"], question["hi"])
    assert encoded.text[lo:hi].strip() == "hello world"


def test_unresolvable_tokenizer_sends_undeclared_with_a_typed_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tokenizer_source.clear_cache()
    monkeypatch.setenv("CLIO_PROVENANCE_ATTENTION_TOKENIZER", str(tmp_path / "missing"))
    monkeypatch.setattr(
        tokenizer_source, "_download", lambda repo: (_ for _ in ()).throw(OSError("offline"))
    )
    kwargs, record = build_declaration(
        model="hosted_vllm/granite", messages=MESSAGES, lm_kwargs={}, call_kwargs={"a": 1}
    )
    assert kwargs == {"a": 1}
    assert record["reason"] == "attention_tokenizer_unavailable"
    assert "offline" in record["detail"]


def test_declared_request_is_a_noop_when_attention_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIO_PROVENANCE_ATTENTION", "0")
    with declare_mod.declared_request(
        model="hosted_vllm/x", messages=MESSAGES, lm_kwargs={}, call_kwargs={"k": 1}
    ) as kwargs:
        assert kwargs == {"k": 1}
        assert current_declaration() is None


def test_declared_request_requires_flowcept(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIO_PROVENANCE_ATTENTION", "1")
    monkeypatch.setenv("CLIO_PROVENANCE_PROVIDERS", "jsonl")
    with declare_mod.declared_request(
        model="hosted_vllm/x", messages=MESSAGES, lm_kwargs={}, call_kwargs={"k": 1}
    ) as kwargs:
        assert kwargs == {"k": 1}
        assert current_declaration() is None


def test_declared_request_exposes_the_record_for_the_lm_call(
    tiny_tokenizer_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLIO_PROVENANCE_ATTENTION", "1")
    monkeypatch.setenv("CLIO_PROVENANCE_PROVIDERS", "jsonl,flowcept")
    monkeypatch.setenv("CLIO_PROVENANCE_ATTENTION_TOKENIZER", str(tiny_tokenizer_dir))
    with declare_mod.declared_request(
        model="hosted_vllm/x", messages=MESSAGES, lm_kwargs={}, call_kwargs={}
    ) as kwargs:
        record = current_declaration()
        assert record is not None and record["status"] == "declared"
        assert kwargs["extra_body"]["kv_transfer_params"]["ranges"]
    assert current_declaration() is None


def test_attention_lm_sends_the_declared_kwargs(
    tiny_tokenizer_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import dspy

    from clio_agent.lm.attention_lm import attention_lm_class

    monkeypatch.setenv("CLIO_PROVENANCE_ATTENTION", "1")
    monkeypatch.setenv("CLIO_PROVENANCE_PROVIDERS", "jsonl,flowcept")
    monkeypatch.setenv("CLIO_PROVENANCE_ATTENTION_TOKENIZER", str(tiny_tokenizer_dir))
    cls = attention_lm_class()
    lm = cls("hosted_vllm/granite-4.2-30b", api_base="http://127.0.0.1:1/v1", cache=False)
    seen: dict[str, Any] = {}

    def fake_call(self: Any, prompt: Any = None, messages: Any = None, **kwargs: Any) -> list[str]:
        seen.update(kwargs)
        seen["record"] = current_declaration()
        return ["ok"]

    monkeypatch.setattr(dspy.LM, "__call__", fake_call)
    assert lm(messages=MESSAGES) == ["ok"]
    assert seen["extra_body"]["kv_transfer_params"]["ranges"]
    assert seen["record"]["status"] == "declared"


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("keyword_request", [False, True])
def test_typed_attention_request_preserves_wire_and_trace(
    tiny_tokenizer_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    asynchronous: bool,
    keyword_request: bool,
) -> None:
    import asyncio

    from dspy.lm15 import Config, Message, Request

    from clio_agent.lm import call_trace
    from clio_agent.lm.attention_lm import attention_lm_class
    from tests._scripted_engine import AsyncScriptedEngine, Reply, ScriptedEngine

    monkeypatch.setenv("CLIO_PROVENANCE_ATTENTION", "1")
    monkeypatch.setenv("CLIO_PROVENANCE_PROVIDERS", "jsonl,flowcept")
    monkeypatch.setenv("CLIO_PROVENANCE_ATTENTION_TOKENIZER", str(tiny_tokenizer_dir))
    records: list[dict[str, Any]] = []
    monkeypatch.setattr(call_trace, "_emit", records.append)
    engine = ScriptedEngine([Reply(text="answer")])
    lm = attention_lm_class()(
        "hosted_vllm/granite",
        engine=engine,
        async_engine=AsyncScriptedEngine(engine),
        cache=False,
        callbacks=[call_trace.LMCallTrace()],
    )
    extensions = {"chat_template_kwargs": {"enable_thinking": False}, "top_k": 20}
    request = Request(
        model=lm.model,
        system="sys",
        messages=(Message.user("hello world"),),
        config=Config(temperature=0.25, extensions=extensions),
    )
    args = () if keyword_request else (request,)
    kwargs = {"request": request} if keyword_request else {}
    response = asyncio.run(lm.acall(*args, **kwargs)) if asynchronous else lm(*args, **kwargs)
    assert response.id == "resp_0"
    [sent] = engine.requests
    assert sent.config.temperature == 0.25
    assert sent.config.extensions["top_k"] == 20
    assert sent.config.extensions["chat_template_kwargs"] == {"enable_thinking": False}
    assert sent.config.extensions["kv_transfer_params"]["ranges"]
    assert request.config.extensions == extensions
    assert "kv_transfer_params" not in extensions
    [record] = records
    assert record["response_id"] == "resp_0"
    assert record["attention"]["status"] == "declared"
    assert record["attention_messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hello world"},
    ]
    assert current_declaration() is None
    assert declare_mod.current_wire_messages() is None
