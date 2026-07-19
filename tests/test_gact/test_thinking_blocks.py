"""iowarp/clio-agent#17: DSPy reasoning lands as a thinking Part."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app

# #948 S4b: default sessions run the blueprint react ``main``; route it to each
# test's ``build_app(agent=...)`` host fake.
pytestmark = pytest.mark.usefixtures("host_agent_executor")


@dataclass
class _Pred:
    answer: str = "ok"
    selected_expert: str = ""
    routing_rationale: str = ""
    reasoning: str = ""
    trajectory: object = None


class _Agent:
    def __init__(self, pred):
        self._pred = pred

    def forward(self, *args, **kwargs):
        return self._pred


def _client(tmp_path: Path, pred) -> TestClient:
    return TestClient(build_app(sessions_path=tmp_path / "s.json", agent=_Agent(pred)))


def test_chain_of_thought_reasoning_becomes_thinking_part(
    tmp_path: Path,
) -> None:
    from .conftest import complete_turn

    pred = _Pred(reasoning="the user wants HDF5 metadata; call hdf5_list_datasets")
    c = _client(tmp_path, pred)
    sid = c.post("/v1/sessions", json={"title": "t"}).json()["id"]
    a = complete_turn(c, sid, "what's in this file")
    types = [p["type"] for p in a["parts"]]
    assert "thinking" in types
    thinking = next(p for p in a["parts"] if p["type"] == "thinking")
    assert "hdf5_list_datasets" in thinking["text"]


def test_react_trajectory_dict_becomes_thinking_part(tmp_path: Path) -> None:
    from .conftest import complete_turn

    pred = _Pred(
        trajectory={
            "step_0_thought": "first probe the schema",
            "step_0_tool_name": "hdf5_list_datasets",
            "step_1_thought": "now read /sim/temperature",
            "step_1_tool_name": "hdf5_analyze_dataset",
        }
    )
    c = _client(tmp_path, pred)
    sid = c.post("/v1/sessions", json={"title": "t"}).json()["id"]
    a = complete_turn(c, sid, "analyze")
    thinking = next(p for p in a["parts"] if p["type"] == "thinking")
    assert "first probe the schema" in thinking["text"]
    assert "hdf5_analyze_dataset" in thinking["text"]


def test_no_reasoning_skips_thinking_part(tmp_path: Path) -> None:
    from .conftest import complete_turn

    pred = _Pred(reasoning="", trajectory=None)
    c = _client(tmp_path, pred)
    sid = c.post("/v1/sessions", json={"title": "t"}).json()["id"]
    a = complete_turn(c, sid, "x")
    types = [p["type"] for p in a["parts"]]
    assert "thinking" not in types


def test_capability_advertised(tmp_path: Path) -> None:
    c = _client(tmp_path, _Pred())
    body = c.get("/v1/capabilities").json()
    assert body["capabilities"]["thinking_blocks"] is True
