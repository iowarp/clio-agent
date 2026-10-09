"""Codex argument diagnostics preserve shape without recording values."""

from __future__ import annotations

from typing import Any

import pytest

from clio_agent.providers.codex import audit


def test_tool_snapshot_omits_argument_values(monkeypatch: pytest.MonkeyPatch) -> None:
    """A completed call can be compared with its stream without copying its content."""
    rows: list[dict[str, Any]] = []
    monkeypatch.setattr(audit, "stream_audit_enabled", lambda: True)
    monkeypatch.setattr(audit, "active_gact_ids", lambda: ("session", "turn", "trace"))
    monkeypatch.setattr(audit, "stream_audit", lambda stage, **row: rows.append(row))
    audit.emit_tool_snapshot(
        call_index=2,
        output_index=1,
        item={
            "type": "function_call",
            "name": "load_skill",
            "call_id": "call-1",
            "arguments": '{"skill_id":"private-value","file":"private-path"}',
        },
        streamed_argument_chars=0,
    )
    assert rows[0]["argument_keys"] == ["file", "skill_id"]
    assert rows[0]["argument_chars"] > 0
    assert rows[0]["streamed_argument_chars"] == 0
    assert rows[0]["arguments_object"] is True
    assert "private-value" not in str(rows)
    assert "private-path" not in str(rows)


@pytest.mark.parametrize("arguments", ["{invalid", "[]", None])
def test_invalid_argument_snapshot_is_diagnostic(
    monkeypatch: pytest.MonkeyPatch, arguments: Any
) -> None:
    """Malformed argument shapes remain observable without breaking stream handling."""
    rows: list[dict[str, Any]] = []
    monkeypatch.setattr(audit, "stream_audit_enabled", lambda: True)
    monkeypatch.setattr(audit, "active_gact_ids", lambda: ("session", "turn", "trace"))
    monkeypatch.setattr(audit, "stream_audit", lambda stage, **row: rows.append(row))
    audit.emit_tool_snapshot(
        call_index=1,
        output_index=0,
        item={"type": "function_call", "arguments": arguments},
        streamed_argument_chars=0,
    )
    assert rows[0]["argument_keys"] == []
    assert rows[0]["arguments_object"] is False
