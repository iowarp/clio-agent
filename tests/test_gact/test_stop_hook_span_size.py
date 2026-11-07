"""The Stop hook's dispatch span names the assistant message instead of copying it.

Found in a P7 journal (2026-11-03): two ``hook.invocation.started`` events held 13.2 MB,
the whole final assistant message of a 2,000-call turn each time. The hooks still get
the message (``run_stop_hooks``); the span carries its part count.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact.hooks import stop_loop


def test_the_dispatch_span_does_not_embed_the_assistant_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SABOTAGE: put ``dict(assistant_payload)`` back in the span payload and the event
    grows with the message (about 2 MB here)."""
    emitted: list[tuple[str, dict[str, Any]]] = []
    hooks_saw: list[dict[str, Any]] = []

    def emit(_app: Any, _sid: str, event_type: str, **kwargs: Any) -> dict[str, Any]:
        emitted.append((event_type, kwargs.get("payload") or {}))
        return {}

    def run_stop_hooks(_app: Any, **kwargs: Any) -> Any:
        hooks_saw.append(kwargs["payload"])
        return SimpleNamespace(redrive=False, deferred=False, capped=False)

    monkeypatch.setattr("clio_agent.gact.runtime.globals._emit_semantic_event", emit)
    monkeypatch.setattr(stop_loop, "run_stop_hooks", run_stop_hooks)
    parts = [{"type": "tool_result", "text": "x" * 1000} for _ in range(2000)]
    stop_loop.dispatch_stop_at_finalize(
        SimpleNamespace(state=SimpleNamespace()),
        session_id="sess_stop",
        turn_id="t1",
        trace_id="tr",
        cwd="/tmp",
        assistant_msg_id="msg_1",
        assistant_payload={"parts": parts},
        agent_ref="main",
    )
    [(event_type, payload)] = [e for e in emitted if e[0] == "hook.invocation.started"]
    assert payload == {"assistant_parts": 2000}
    assert len(json.dumps(payload)) < 100
    assert hooks_saw and len(hooks_saw[0]["assistant"]["parts"]) == 2000  # hooks still see it
