"""The wire copies of a tool's structured content are previews; clio-core keeps it whole."""

from __future__ import annotations

import base64

from clio_agent.gact.structured_wire_bound import (
    BOUNDED_MARKER,
    STRUCTURED_CONTENT_OVER_WIRE_CAP_REASON,
    WHOLE_VALUE_EVENT,
    bounded_result_structured,
    bounded_structured_content,
    whole_structured,
    wire_structured,
)


def _array_payload(n_bytes: int) -> dict[str, object]:
    return {
        "shape": [n_bytes // 4],
        "dtype": "float32",
        "encoding": "base64-le",
        "data": base64.b64encode(b"\0" * n_bytes).decode(),
    }


def test_a_small_payload_passes_unchanged() -> None:
    payload = {"shape": [3], "values": [1, 2, 3]}
    assert bounded_structured_content(payload, limit=1024) is payload


def test_an_array_over_the_cap_becomes_an_envelope_with_its_shape() -> None:
    payload = _array_payload(10 * 2**20)
    bounded = bounded_structured_content(payload, limit=256 * 1024)
    assert bounded[BOUNDED_MARKER] is True
    assert bounded["reason"] == STRUCTURED_CONTENT_OVER_WIRE_CAP_REASON
    assert bounded["original_bytes"] > 10 * 2**20
    assert bounded["shape"] == payload["shape"] and bounded["dtype"] == "float32"
    assert "data" not in bounded
    assert bounded_structured_content(bounded, limit=10) is bounded  # idempotent


def test_the_result_copy_is_bounded_without_touching_the_original() -> None:
    payload = _array_payload(2 * 2**20)
    result = {"content": [{"type": "text", "text": "summary"}], "structuredContent": payload}
    bounded = bounded_result_structured(result, limit=1024)
    assert bounded is not result
    assert result["structuredContent"] is payload  # the caller's value is whole
    assert bounded["structuredContent"]["reason"] == STRUCTURED_CONTENT_OVER_WIRE_CAP_REASON
    assert bounded["content"] == result["content"]  # the model's text lane is unchanged


def test_wire_structured_prefers_the_declared_payload(monkeypatch) -> None:
    monkeypatch.setenv("CLIO_TOOL_STRUCTURED_BYTES", "1024")
    declared = {"calls": 3}
    result = {"structuredContent": _array_payload(4096)}
    structured, wired = wire_structured(declared, result)
    assert structured is declared
    assert wired["structuredContent"][BOUNDED_MARKER] is True
    structured, _ = wire_structured(None, result)
    assert structured[BOUNDED_MARKER] is True


def test_the_clio_core_event_keeps_every_previewed_value_whole(monkeypatch) -> None:
    """SABOTAGE: build the event from the wire result and the array is lost from clio-core."""
    monkeypatch.setenv("CLIO_TOOL_STRUCTURED_BYTES", "1024")
    array = _array_payload(4096)
    whole = {"content": [{"type": "text", "text": "summary"}], "structuredContent": array}
    _, wired = wire_structured(None, whole)
    assert wired["structuredContent"]["whole_value_in"] == WHOLE_VALUE_EVENT
    event = whole_structured({"call_id": "c1"}, None, whole, wired)
    assert event["result"]["structuredContent"] is array and event["call_id"] == "c1"
    declared = _array_payload(4096)
    event = whole_structured({}, declared, {"text": "ok"}, {"text": "ok"})
    assert event["structured_content"] is declared and event["result"] == {"text": "ok"}
    small = {"structuredContent": {"n": 1}}
    assert whole_structured({}, {"n": 1}, small, small) == {"result": small}
