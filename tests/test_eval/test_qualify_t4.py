"""T4 reads spawn calls as they are recorded: ``spawns`` (each with its ``agent``) and
``wait``. It read ``tasks``, a key no call has, so a survey through three explore
children could not pass."""

from __future__ import annotations

from typing import Any

from scripts.eval.tool_calling.qualify import check_t4


def _record(spawns: list[dict[str, Any]], wait: bool) -> dict[str, Any]:
    call = {"tool": "spawn_agents_parallel", "args": {"spawns": spawns, "wait": wait}}
    sessions = [{"depth": 0, "tool_calls": [call]}]
    sessions += [{"depth": 1, "tool_calls": []} for _ in spawns]
    return {"normalized": {"sess_1": {"sessions": sessions}}}


def test_three_explore_children_waited_on_pass() -> None:
    """SABOTAGE: read ``args["tasks"]`` again and this reads fail."""
    spawns = [{"agent": "explore", "task": f"survey {n}"} for n in range(3)]
    assert check_t4(_record(spawns, wait=True))[0] == "pass"


def test_a_child_of_another_agent_or_no_wait_fails() -> None:
    mixed = [{"agent": "explore", "task": "a"}, {"agent": "general", "task": "b"}]
    assert check_t4(_record(mixed, wait=True))[0] == "fail"
    only = [{"agent": "explore", "task": "a"}, {"agent": "explore", "task": "b"}]
    assert check_t4(_record(only, wait=False))[0] == "fail"
