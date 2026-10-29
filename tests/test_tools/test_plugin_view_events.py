"""A plugin's view event rides in its result ``_meta`` to the UI, never to the model;
every product MCP call names its calling session in ``_meta``."""

from __future__ import annotations

from types import SimpleNamespace

from mcp.types import TextContent

from clio_agent.gact.tool_observer_parts import completed_tool_metadata
from clio_agent.tools.caller_scope import CALLER_META_KEY, caller_meta, tool_caller_context
from clio_agent.tools.mcp_results import (
    MAX_UI_EVENT_BYTES,
    UI_EVENT_META_KEY,
    call_tool_result_to_observer,
)


def _result(meta: dict) -> SimpleNamespace:
    return SimpleNamespace(
        content=[TextContent(type="text", text='{"turns_left": 5}')],
        structured_content={"turns_left": 5},
        meta=meta,
        is_error=False,
    )


def test_the_view_event_is_kept_for_the_part_and_other_meta_is_dropped() -> None:
    event = {"view": "demo/wall", "key": "g1", "group": "sess_a", "rows": [{"x": 1}]}
    observed = call_tool_result_to_observer(_result({UI_EVENT_META_KEY: event, "other": 1}))
    assert observed["uiEvent"] == event and "other" not in str(observed)
    assert observed["structuredContent"] == {"turns_left": 5}
    metadata = completed_tool_metadata(observed, completed_at="t", cancellation={}, provenance={})
    assert metadata["ui_event"] == event
    # An oversized event is not recorded rather than truncated.
    big = {"view": "v", "rows": ["x" * MAX_UI_EVENT_BYTES]}
    assert "uiEvent" not in call_tool_result_to_observer(_result({UI_EVENT_META_KEY: big}))


def test_the_caller_is_named_only_while_a_session_call_runs() -> None:
    assert caller_meta() is None
    with tool_caller_context("sess_branch", "sess_root"):
        assert caller_meta() == {CALLER_META_KEY: {"session": "sess_branch", "root": "sess_root"}}
    with tool_caller_context("sess_root", ""):
        assert caller_meta() == {CALLER_META_KEY: {"session": "sess_root", "root": "sess_root"}}
    assert caller_meta() is None
