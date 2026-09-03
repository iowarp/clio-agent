"""Unit coverage for the harness's spawn-routing extraction (#948 spawn architecture).

Failing-first regression for the stale-vocabulary bug: ``ClioAgent._to_run``
used to derive ``run.steps`` ONLY from ``message.metadata.expert_handoffs`` — a
legacy delegation surface the live runtime no longer emits (verified live:
metadata carries NO ``expert_handoffs`` key). The current routing surface is the
assistant CALLING ``spawn_agent_task`` / ``spawn_agents_parallel``: each spawn
call IS a routing decision. These tests feed a synthetic messages list (no live
gact server, no provider, no network) through ``ClioAgent._to_run`` /
``ClioAgent._extract_messages`` and assert the derived ``steps`` directly, so
they run in the default offline subset on every platform.

Plain unit test: no ``live`` / ``real_case`` marker, so it always runs (no
``CLIO_RUN_LIVE`` gate, no live gact server).
"""

from __future__ import annotations

from tests.test_real_cases.clio_sut import ClioAgent


def _assistant_message(tools_called: list[dict], *, message_id: str = "m1") -> dict:
    return {
        "role": "assistant",
        "id": message_id,
        "cost_usd": 0.0,
        "metadata": {"tools_called": tools_called},
    }


def _run_from(messages: list[dict]):
    agent = ClioAgent()
    return agent._to_run(
        assistant=messages[-1] if messages else {},
        messages=messages,
        children=[],
        active={},
        session_id="s1",
        blueprint_id="bp1",
    )


def test_spawn_agent_task_becomes_one_step():
    """A single spawn_agent_task call -> one single-agent step."""
    messages = [
        _assistant_message(
            [
                {
                    "name": "spawn_agent_task",
                    "args": {"agent": "data", "task": "acquire evidence"},
                    "result": '{"task_id": "t1", "status": "queued"}',
                }
            ]
        )
    ]
    run = _run_from(messages)
    assert run.steps == [["data"]]
    assert run.routed_to("data")
    assert run.tool_calls[0].name == "spawn_agent_task"


def test_spawn_agents_parallel_becomes_one_batched_step():
    """One spawn_agents_parallel call fanning out to N agents -> ONE step
    listing every agent (they ran together, not sequentially)."""
    messages = [
        _assistant_message(
            [
                {
                    "name": "spawn_agents_parallel",
                    "args": {
                        "spawns": [
                            {"agent": "geospatial", "task": "resolve place"},
                            {"agent": "data", "task": "discover stations"},
                        ]
                    },
                    "result": '{"spawned": []}',
                }
            ]
        )
    ]
    run = _run_from(messages)
    assert run.steps == [["geospatial", "data"]]
    assert run.routed_to("geospatial")
    assert run.routed_to("data")


def test_spawn_steps_across_messages_preserve_order():
    """Two spawn calls in two separate (oldest-first) turns -> two ordered steps."""
    messages = [
        _assistant_message(
            [{"name": "spawn_agent_task", "args": {"agent": "data", "task": "x"}}],
            message_id="m2",
        ),
        _assistant_message(
            [{"name": "spawn_agent_task", "args": {"agent": "analysis", "task": "y"}}],
            message_id="m1",
        ),
    ]
    # invoke passes messages newest-first (as the gact /messages endpoint returns
    # them); _to_run/_extract_messages walk reversed(messages) to process oldest
    # first, so m1 (spawned "analysis") must come first in steps even though it's
    # listed second here.
    run = _run_from(messages)
    assert run.steps == [["analysis"], ["data"]]


def test_spawn_steps_skip_empty_or_non_str_agent():
    """A malformed spawn call (missing/blank/non-string agent) is skipped, not
    turned into a bogus step."""
    messages = [
        _assistant_message(
            [
                {"name": "spawn_agent_task", "args": {"agent": "", "task": "x"}},
                {"name": "spawn_agent_task", "args": {"agent": None, "task": "x"}},
                {"name": "spawn_agent_task", "args": {"task": "x"}},
            ]
        )
    ]
    run = _run_from(messages)
    assert run.steps == []


def test_wait_check_observe_tool_calls_are_kept_as_harmless_tool_calls():
    """Collector tools (wait/check/observe) are NOT routing decisions, but they
    are still recorded as ordinary tool_calls (harmless, useful evidence)."""
    messages = [
        _assistant_message(
            [
                {"name": "spawn_agent_task", "args": {"agent": "data", "task": "x"}},
                {"name": "wait_agent_tasks", "args": {"task_ids": ["t1"], "timeout_s": 30}},
                {"name": "check_agent_tasks", "args": {}},
            ]
        )
    ]
    run = _run_from(messages)
    assert run.steps == [["data"]]
    assert run.tool_names == ["spawn_agent_task", "wait_agent_tasks", "check_agent_tasks"]


def test_legacy_expert_handoffs_still_recognized():
    """Back-compat: an OLDER runtime/replay trace that still carries
    expert_handoffs metadata is still turned into a step (not a hard cutover) --
    the live server no longer emits this surface, but a stale/replayed trace
    might."""
    messages = [
        {
            "role": "assistant",
            "id": "m1",
            "cost_usd": 0.0,
            "metadata": {"expert_handoffs": [{"agent_id": "analysis"}]},
        }
    ]
    run = _run_from(messages)
    assert run.steps == [["analysis"]]


def test_constructor_repr_result_coerced_to_dict():
    """A structured tool result recorded as a Python constructor-repr (the shape
    matchers hit live: "Root(ok=True, radius_km=50.0, ...)") is coerced to a
    nested dict via the owner adapter, so dict-output matchers can read it. A
    plain non-repr string stays a string (raise==not-a-repr contract)."""
    messages = [
        _assistant_message(
            [
                {
                    "name": "geo_filter_points_by_radius",
                    "args": {},
                    "result": (
                        "Root(ok=True, count=1, radius_km=50.0, "
                        "center=Root(lat=32.7, lon=-117.1), "
                        "points=[{'id': 'P475', 'distance_km': 6.2}])"
                    ),
                },
                {"name": "shell_run", "args": {}, "result": "plain text output"},
            ]
        )
    ]
    run = _run_from(messages)
    filt = run.tool_calls[0].output
    assert isinstance(filt, dict), filt
    assert filt["radius_km"] == 50.0
    assert filt["center"] == {"lat": 32.7, "lon": -117.1}
    assert filt["points"][0]["id"] == "P475"
    assert run.tool_calls[1].output == "plain text output"
