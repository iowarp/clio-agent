"""P2.10 run-handle render contract and permanent one-surface deletion locks."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clio_agent.gact.types import Part

FIXTURE = Path(__file__).parents[1] / "fixtures" / "run_handle_parts_1127.json"


@pytest.mark.parametrize("index", [0, 1])
def test_run_handle_part_matches_committed_fixture(index: int) -> None:
    """Task and tool parts retain the committed additive run-handle vocabulary."""

    rows = json.loads(FIXTURE.read_text(encoding="utf-8"))
    part = Part(**rows[index])
    assert part.to_wire() == rows[index]


def test_forbidden_remote_spawn_surface_names_never_appear() -> None:
    """Forever lock: placement stays a parameter on the one existing spawn surface.

    ``"spawn_" + "remote_agent"`` is banned with zero exceptions -- clio must
    never define a second, agent-facing spawn tool competing with
    ``spawn_agent_task``/``spawn_agents_parallel`` (#1127: "no separate remote
    tool, ever").

    ``"relay_" + "submit_agent"`` was banned on the same premise, written when
    that string was only a hypothetical name for such a competing tool. #1221
    (commit 8b0484d0) discovered it is instead the REAL relay door's own MCP
    tool name (confirmed live via ``tools/list`` against 127.0.0.1:18796/mcp;
    the prior code had the wrong name, ``"relay_submit_" + "remote_agent"``,
    and never reached the door at all). ``RelayInvokerRuntime`` must call that
    tool literally by name to talk to the door -- this is the ONE spawn
    surface's OWN relay-placement implementation detail, not a second
    agent-facing surface (no ``dspy.Tool``/native tool anywhere is named after
    the door's tool; only ``spawn_agent_task``/``spawn_agents_parallel`` are
    agent-callable). ``RELAY_DOOR_TOOL_REF_ALLOWED_FILES`` below is the
    explicit, reviewed grandfather list of files that legitimately reference
    the door's real tool name (the relay invoker implementation + its tests);
    anything new must be added here deliberately, so the guard still catches
    an accidental second surface appearing anywhere else. (This docstring
    itself never spells the guarded string out contiguously, same trick the
    ``forbidden`` tuple below uses, so it doesn't trip its own guard.)
    """

    repo = Path(__file__).parents[2]
    forbidden = ("relay_" + "submit_agent", "spawn_" + "remote_agent")
    relay_door_tool_ref = forbidden[0]
    # Grandfathered by #1221 (commit 8b0484d0) -- see docstring above.
    RELAY_DOOR_TOOL_REF_ALLOWED_FILES = {
        "src/clio_agent/gact/run_registry.py",
        "src/clio_agent/gact/agents/relay_expert_invoker.py",
        "src/clio_agent/gact/agents/relay_invoker_runtime.py",
        "src/clio_agent/gact/routes/async_processes.py",
        "tests/test_gact/test_agent_tasks_s2.py",
        "tests/test_gact/test_async_processes_1205.py",
        "tests/test_gact/test_invoker_s7.py",
        "tests/test_gact/test_relay_invoker_runtime_contract.py",
        "tests/test_tools/test_relay_l25_wrapper_contract.py",
        "tests/test_tools/test_relay_transport.py",
    }
    offenders: list[str] = []
    for root_name in ("src", "tests"):
        for path in (repo / root_name).rglob("*"):
            if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            rel = path.relative_to(repo).as_posix()
            text = path.read_text(encoding="utf-8", errors="ignore")
            for name in forbidden:
                if name not in text:
                    continue
                if name == relay_door_tool_ref and rel in RELAY_DOOR_TOOL_REF_ALLOWED_FILES:
                    continue
                offenders.append(f"{rel}: {name}")
    assert offenders == []
