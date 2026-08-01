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
    """Forever lock: placement stays a parameter on the one existing spawn surface."""

    repo = Path(__file__).parents[2]
    forbidden = ("relay_" + "submit_agent", "spawn_" + "remote_agent")
    offenders: list[str] = []
    for root_name in ("src", "tests"):
        for path in (repo / root_name).rglob("*"):
            if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for name in forbidden:
                if name in text:
                    offenders.append(f"{path.relative_to(repo)}: {name}")
    assert offenders == []
