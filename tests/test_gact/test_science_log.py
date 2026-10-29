"""The science log: typed, linked entries on the task layer's writer, with sources."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from clio_agent.arc.memory import ARCMemory
from clio_agent.gact.science_log.entries import EntryRejected
from clio_agent.gact.science_log.lineage import call_reference
from clio_agent.gact.science_log.measurements import write_export
from clio_agent.gact.science_log.store import ScienceLog
from clio_agent.gact.task_access import TaskCaller
from clio_agent.gact.types import Message, Part
from tests.test_arc.test_conversation_owner_contracts import create_environment


def _record_call(app: Any, session_id: str, call_id: str, tool: str, result: Any) -> None:
    """A finished call as the transcript records it."""

    text = json.dumps(result)
    app.state.messages.setdefault(session_id, []).append(
        Message(
            id="msg_" + call_id,
            session_id=session_id,
            role="assistant",
            created_at="2026-10-27T00:00:00+00:00",
            updated_at="2026-10-27T00:00:01+00:00",
            parts=[
                Part(
                    type="tool_call",
                    call_id=call_id,
                    tool_name=tool,
                    input={"setting": 2},
                    metadata={"started_at": "2026-10-27T00:00:00+00:00"},
                ),
                Part(
                    type="tool_result",
                    call_id=call_id,
                    tool_name=tool,
                    content=[Part(type="text", text=text)],
                    metadata={"completed_at": "2026-10-27T00:00:01+00:00"},
                ),
            ],
        )
    )


async def _study(arc: ARCMemory, tmp_path: Path) -> tuple[Any, Any, Any, ScienceLog]:
    app, owner, root = await create_environment(arc, tmp_path)
    return app, owner, root, ScienceLog(app)


def _run(
    log: ScienceLog, sid: str, kind: str, fields: dict[str, Any], links: Any, call: str
) -> dict[str, Any]:
    return log.write(sid, kind, fields, links, call)


@pytest.mark.asyncio
async def test_a_study_runs_from_question_to_a_cited_conclusion(
    arc: ARCMemory, tmp_path: Path
) -> None:
    app, owner, root, log = await _study(arc, tmp_path)
    sid = root.owner.session_id
    _record_call(app, sid, "call_measure1", "measure", {"reading": {"seconds": 12.5}})

    def work() -> dict[str, Any]:
        question = _run(log, sid, "question", {"statement": "What sets the rate?"}, None, "c1")
        hypothesis = _run(
            log,
            sid,
            "hypothesis",
            {"claim": "The input sets it", "refuting_prediction": "No change when varied"},
            {"question": question["id"]},
            "c2",
        )
        experiment = _run(
            log,
            sid,
            "experiment",
            {"method": "Vary the input", "sample_size": 3},
            {"hypothesis": hypothesis["id"]},
            "c3",
        )
        observation = _run(
            log,
            sid,
            "observation",
            {
                "from_calls": [call_reference(sid, "call_measure1")],
                "measurements": [
                    {
                        "metric": "duration",
                        "source": {"call": "call_measure1", "field": "reading.seconds"},
                        "unit": "s",
                        "conditions": {"setting": 2},
                    }
                ],
            },
            {"experiment": experiment["id"]},
            "c4",
        )
        evidence = _run(
            log,
            sid,
            "evidence",
            {"interpretation": "It changed", "strength": "weak"},
            {"observation": [observation["id"]], "supports": hypothesis["id"]},
            "c5",
        )
        conclusion = _run(
            log,
            sid,
            "conclusion",
            {
                "claims": [{"text": "The input sets it", "cites": [evidence["id"]]}],
                "confidence": "low",
                "relation": "answers",
            },
            {"question": question["id"]},
            "c6",
        )
        log.set_status(sid, hypothesis["id"], "supported", "evidence supports it", "c7")
        return {
            "question": question,
            "hypothesis": hypothesis,
            "observation": observation,
            "conclusion": conclusion,
        }

    made = await asyncio.to_thread(work)
    study = made["question"]["id"]
    assert all(entry["study"] == study for entry in made.values())
    observation = made["observation"]["fields"]
    # The log copied the value and the call's facts from the record.
    assert observation["measurements"][0]["value"] == 12.5
    [lineage] = observation["lineage"]
    assert lineage["tool"] == "measure" and lineage["args"] == {"setting": 2}
    assert lineage["started_at"] and lineage["completed_at"]
    assert (
        lineage["result_sha256"]
        == hashlib.sha256(json.dumps({"reading": {"seconds": 12.5}}).encode()).hexdigest()
    )
    effective = {row["id"]: row for row in await asyncio.to_thread(log.effective, study)}
    assert effective[made["hypothesis"]["id"]]["status"] == "supported"
    # A citation of a missing entry, and a call never recorded, are typed refusals.
    with pytest.raises(EntryRejected) as missing:
        await asyncio.to_thread(
            _run,
            log,
            sid,
            "conclusion",
            {
                "claims": [{"text": "x", "cites": ["e_missing"]}],
                "confidence": "low",
                "relation": "answers",
            },
            {"question": study},
            "c8",
        )
    assert missing.value.reason == "citation_unknown"
    for fields, reason in (
        ({"from_calls": []}, "lineage_missing"),
        ({"from_calls": ["call:nowhere/call_x"]}, "lineage_call_unknown"),
        (
            {
                "from_calls": ["call_measure1"],
                "measurements": [
                    {"metric": "m", "source": {"call": "call_measure1", "field": "nope"}}
                ],
            },
            "measurement_field_unknown",
        ),
    ):
        with pytest.raises(EntryRejected) as refused:
            await asyncio.to_thread(
                _run,
                log,
                sid,
                "observation",
                fields,
                {
                    "experiment": next(
                        e["id"] for e in effective.values() if e["type"] == "experiment"
                    )
                },
                "c9-" + reason,
            )
        assert refused.value.reason == reason
    await owner.recovery.drain()


@pytest.mark.asyncio
async def test_branches_read_the_whole_study_and_cite_only_their_own_calls(
    arc: ARCMemory, tmp_path: Path
) -> None:
    app, owner, root, log = await _study(arc, tmp_path)
    sid = root.owner.session_id
    caller = TaskCaller(sid, "human")
    first = await owner.spawn(caller, "Branch one", "worker", "b1", start=False)
    second = await owner.spawn(caller, "Branch two", "worker", "b2", start=False)
    one, two = first["session_id"], second["session_id"]
    _record_call(app, two, "call_other", "measure", {"v": 1})
    _record_call(app, one, "call_mine", "measure", {"v": 2})

    def work() -> None:
        question = _run(log, sid, "question", {"statement": "Q"}, None, "q")
        hypothesis = _run(
            log,
            one,
            "hypothesis",
            {"claim": "H", "refuting_prediction": "P"},
            {"question": question["id"]},
            "h",
        )
        experiment = _run(
            log, two, "experiment", {"method": "M"}, {"hypothesis": hypothesis["id"]}, "x"
        )
        # A sibling sees the other branch's entries and links to them.
        assert experiment["study"] == question["id"]
        with pytest.raises(EntryRejected) as outside:
            _run(
                log,
                one,
                "observation",
                {"from_calls": [call_reference(two, "call_other")]},
                {"experiment": experiment["id"]},
                "o1",
            )
        assert outside.value.reason == "lineage_call_outside_branch"
        mine = _run(
            log,
            one,
            "observation",
            {"from_calls": [call_reference(one, "call_mine")]},
            {"experiment": experiment["id"]},
            "o2",
        )
        assert mine["branch"] == one
        # The root delegated to both branches, so it may cite either one's calls.
        _run(
            log,
            sid,
            "observation",
            {"from_calls": [call_reference(two, "call_other")]},
            {"experiment": experiment["id"]},
            "o3",
        )

    await asyncio.to_thread(work)
    view = await asyncio.to_thread(log.effective, log.study_of(one))
    assert {row["branch"] for row in view} == {sid, one, two}
    await owner.recovery.drain()


@pytest.mark.asyncio
async def test_a_budget_is_charged_and_a_replay_writes_nothing_twice(
    arc: ARCMemory, tmp_path: Path
) -> None:
    app, owner, root, log = await _study(arc, tmp_path)
    sid = root.owner.session_id
    for index in range(3):
        _record_call(app, sid, f"call_{index}", "measure", {"v": index})

    def work() -> tuple[dict[str, Any], dict[str, Any]]:
        question = _run(
            log,
            sid,
            "question",
            {"statement": "Q", "budget": {"amount": 2, "unit": "runs"}},
            None,
            "q",
        )
        hypothesis = _run(
            log,
            sid,
            "hypothesis",
            {"claim": "H", "refuting_prediction": "P"},
            {"question": question["id"]},
            "h",
        )
        experiment = _run(
            log, sid, "experiment", {"method": "M"}, {"hypothesis": hypothesis["id"]}, "x"
        )
        links = {"experiment": experiment["id"]}
        with pytest.raises(EntryRejected) as unpriced:
            _run(log, sid, "observation", {"from_calls": ["call_0"]}, links, "o-free")
        assert unpriced.value.reason == "cost_missing"
        first = _run(log, sid, "observation", {"from_calls": ["call_0"], "cost": 1}, links, "o0")
        again = _run(log, sid, "observation", {"from_calls": ["call_0"], "cost": 1}, links, "o0")
        assert again == first
        _run(log, sid, "observation", {"from_calls": ["call_1"], "cost": 1}, links, "o1")
        with pytest.raises(EntryRejected) as spent:
            _run(log, sid, "observation", {"from_calls": ["call_2"], "cost": 1}, links, "o2")
        assert spent.value.reason == "budget_exhausted"
        return question, first

    question, first = await asyncio.to_thread(work)
    # A fresh reader rebuilds the study from the session's event log.
    reread = ScienceLog(app)
    entries = await asyncio.to_thread(reread.study_entries, question["id"])
    assert [e["id"] for e in entries].count(first["id"]) == 1
    assert sum(e["type"] == "observation" for e in entries) == 2
    await owner.recovery.drain()


def test_measurements_export_with_a_manifest_that_matches_the_file(tmp_path: Path) -> None:
    observations = [
        {
            "id": f"e_{index}",
            "branch": "sess_a",
            "type": "observation",
            "fields": {
                "measurements": [
                    {
                        "metric": "duration",
                        "value": value,
                        "unit": "s",
                        "conditions": {"setting": index},
                    }
                ]
            },
        }
        for index, value in enumerate([1, 2.5, 4])
    ]
    for file_format in ("parquet", "csv"):
        written = write_export(tmp_path, observations, {"type": "observation"}, file_format, "m")
        data = Path(written["output_path"])
        manifest = json.loads(Path(written["saved_path"]).read_text())
        assert manifest["sha256"] == hashlib.sha256(data.read_bytes()).hexdigest()
        assert manifest["observations"] == ["e_0", "e_1", "e_2"]
        assert manifest["condition_keys"] == ["setting"] and manifest["rows"] == 3
    import pyarrow.parquet as pq

    table = pq.read_table(tmp_path / "m.parquet").to_pydict()
    assert table["value"] == [1.0, 2.5, 4.0] and table["condition.setting"] == [0, 1, 2]


def test_every_science_log_tool_builds_with_a_declared_presentation() -> None:
    from clio_agent.gact.science_log.tools import BUILDERS, SCIENCE_LOG_TOOLS

    built = {name: BUILDERS[name]() for name in SCIENCE_LOG_TOOLS}
    assert set(built) == set(SCIENCE_LOG_TOOLS)
    assert all(getattr(tool, "name", name) == name for name, tool in built.items())


def test_a_call_is_read_from_its_structured_result_not_its_text_preview(
    tmp_path: Path,
) -> None:
    from types import SimpleNamespace

    from clio_agent.gact.science_log.lineage import _field, find_call

    structured = {"readings": [{"seconds": 14.86}]}
    message = Message(
        id="msg_live",
        session_id="sess_a",
        role="assistant",
        created_at="2026-10-27T00:00:00+00:00",
        updated_at="2026-10-27T00:00:01+00:00",
        parts=[
            Part(type="tool_call", call_id="call_m", tool_name="measure", input={}),
            Part(
                type="tool_result",
                call_id="call_m",
                tool_name="measure",
                structured_content=structured,
                # The live turn's text and the stored preview differ; neither is read.
                content=[Part(type="text", text="Tool measure completed.")],
            ),
        ],
    )
    app = SimpleNamespace(state=SimpleNamespace(messages={"sess_a": [message]}))
    import clio_agent.gact.tool_observer as observer

    original = observer._session_turn_transcript
    observer._session_turn_transcript = lambda *_: None
    try:
        call = find_call(app, "sess_a", "call_m")
    finally:
        observer._session_turn_transcript = original
    canonical = json.dumps(structured, sort_keys=True, separators=(",", ":"))
    assert call["result_sha256"] == hashlib.sha256(canonical.encode()).hexdigest()
    parsed = json.loads(call["result_text"])
    assert _field(parsed, "readings[0].seconds") == _field(parsed, "readings.0.seconds") == 14.86
