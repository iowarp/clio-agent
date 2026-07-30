"""Regression coverage for the shared MCP wire-value coercer."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, Field

from clio_agent.tools.mcp_runtime import wire_value


class _State(Enum):
    READY = "ready"


class _WireModel(BaseModel):
    created_at: datetime = Field(alias="createdAt")
    payload: bytes
    state: _State
    optional: str | None = None


def test_wire_value_coercion_parity() -> None:
    """Every historical contract retains its captured wire shape."""

    created_at = datetime(2026, 7, 30, 12, 34, 56, tzinfo=timezone.utc)
    value = {
        "model": _WireModel(
            createdAt=created_at,
            payload=b"payload",
            state=_State.READY,
        ),
        "nested": [created_at, b"payload", _State.READY, None],
        "none": None,
        "numbers": {2, 1},
    }

    assert wire_value(value, mode="mcp_results", exclude_none=False) == {
        "model": {
            "createdAt": "2026-07-30T12:34:56Z",
            "payload": "payload",
            "state": "ready",
            "optional": None,
        },
        "nested": [
            "2026-07-30 12:34:56+00:00",
            "b'payload'",
            "_State.READY",
            None,
        ],
        "none": None,
        "numbers": "{1, 2}",
    }
    assert wire_value(value, mode="mcp_results", exclude_none=True) == {
        "model": {
            "createdAt": "2026-07-30T12:34:56Z",
            "payload": "payload",
            "state": "ready",
        },
        "nested": [
            "2026-07-30 12:34:56+00:00",
            "b'payload'",
            "_State.READY",
            None,
        ],
        "numbers": "{1, 2}",
    }
    assert wire_value(value, mode="mcp_apps") == {
        "model": {
            "createdAt": "2026-07-30 12:34:56+00:00",
            "payload": "b'payload'",
            "state": "_State.READY",
        },
        "nested": [
            "2026-07-30 12:34:56+00:00",
            "b'payload'",
            "_State.READY",
            None,
        ],
        "none": None,
        "numbers": "{1, 2}",
    }
    assert wire_value(value, mode="gact_runtime") == {
        "model": {
            "created_at": "2026-07-30 12:34:56+00:00",
            "payload": "b'payload'",
            "state": "_State.READY",
        },
        "nested": [
            "2026-07-30 12:34:56+00:00",
            "b'payload'",
            "_State.READY",
            None,
        ],
        "none": None,
        "numbers": [1, 2],
    }


def test_wire_value_has_single_source_definition() -> None:
    """Only the shared runtime module defines the MCP wire coercer."""

    src_root = Path(__file__).resolve().parents[2] / "src"
    definition = re.compile(r"^\s*def (?:_wire_value|wire_value)\s*\(", re.MULTILINE)
    matches = [
        f"{path.relative_to(src_root).as_posix()}:{match.group(0).strip()}"
        for path in src_root.rglob("*.py")
        for match in definition.finditer(path.read_text(encoding="utf-8"))
    ]

    assert matches == ["clio_agent/tools/mcp_runtime.py:def wire_value("]
