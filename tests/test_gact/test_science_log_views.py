"""Study views (Addendum B): ``view`` entries, the live study sources and the formula."""

from __future__ import annotations

import asyncio
import math
from pathlib import Path
from typing import Any

import pytest

from clio_agent.arc.memory import ARCMemory
from clio_agent.gact.a2ui import A2UISurfaceRecord
from clio_agent.gact.a2ui_producer._data_reference import validate_component_data_references
from clio_agent.gact.routes.science_log import science_log_view
from clio_agent.gact.routes.study_sources import StudyTableQueryRequest, study_table_query
from clio_agent.gact.science_log.entries import EntryRejected
from clio_agent.gact.science_log.formula import FormulaError, evaluate_formula, parse_formula
from clio_agent.gact.science_log.lineage import call_reference
from clio_agent.gact.science_log.sources import parse_study_uri, source_rows
from clio_agent.gact.science_log.store import ScienceLog
from clio_agent.gact.types import Message, Part
from tests.test_arc.test_conversation_owner_contracts import create_environment
from tests.test_gact.test_science_log import _record_call


class _Surfaces:
    """The two ``A2UIStore`` reads the views use, over surfaces a test places."""

    def __init__(self) -> None:
        self.records: dict[tuple[str, str], A2UISurfaceRecord] = {}

    def add(self, session_id: str, surface_id: str, state: str = "active") -> None:
        self.records[(session_id, surface_id)] = A2UISurfaceRecord(
            id=surface_id, session_id=session_id, catalog_id="clio-workspace", state=state
        )

    def get(self, session_id: str, surface_id: str) -> A2UISurfaceRecord | None:
        return self.records.get((session_id, surface_id))


async def _study(arc: ARCMemory, tmp_path: Path) -> tuple[Any, str, ScienceLog, _Surfaces]:
    app, _, root = await create_environment(arc, tmp_path)
    log = ScienceLog(app)
    app.state.science_log = log
    surfaces = _Surfaces()
    app.state.a2ui_store = surfaces
    return app, root.owner.session_id, log, surfaces


def _measured_study(app: Any, sid: str, log: ScienceLog, seconds: list[float]) -> dict[str, Any]:
    """A question, a law-shaped hypothesis and one Observation per reading."""

    question = log.write(sid, "question", {"statement": "How fast is it?"}, None, "c1")
    hypothesis = log.write(
        sid,
        "hypothesis",
        {"claim": "Linear in n", "refuting_prediction": "Not linear", "formula": "t = 2*n"},
        {"question": question["id"]},
        "c2",
    )
    experiment = log.write(
        sid, "experiment", {"method": "Vary n"}, {"hypothesis": hypothesis["id"]}, "c3"
    )
    for index, value in enumerate(seconds):
        call = f"call_m{index}"
        _record_call(app, sid, call, "measure", {"reading": {"seconds": value}})
        log.write(
            sid,
            "observation",
            {
                "from_calls": [call_reference(sid, call)],
                "measurements": [
                    {
                        "metric": "t",
                        "source": {"call": call, "field": "reading.seconds"},
                        "unit": "s",
                        "conditions": {"n": index + 1},
                    }
                ],
            },
            {"experiment": experiment["id"]},
            f"c_obs{index}",
        )
    return {"question": question, "hypothesis": hypothesis}


@pytest.mark.asyncio
async def test_a_view_attaches_a_surface_of_its_writer_and_is_listed_until_withdrawn(
    arc: ARCMemory, tmp_path: Path
) -> None:
    app, sid, log, surfaces = await _study(arc, tmp_path)

    def work() -> tuple[str, dict[str, Any], dict[str, Any]]:
        question = log.write(sid, "question", {"statement": "What sets the rate?"}, None, "c1")
        fields = {"surface_id": "overview", "title": "Rate", "purpose": "Watch the rate"}
        with pytest.raises(EntryRejected) as missing:
            log.write(sid, "view", fields, {"serves": question["id"]}, "c2")
        assert missing.value.reason == "view_surface_missing"
        surfaces.add(sid, "overview")
        view = log.write(sid, "view", fields, {"serves": question["id"]}, "c3")
        return question["id"], view, science_log_view(app, sid)

    question, view, shown = await asyncio.to_thread(work)
    assert view["status"] == "recorded"
    [row] = shown["views"]
    assert row["entry"] == view["id"]
    assert row["session_id"] == sid
    assert (row["title"], row["purpose"], row["serves"]) == ("Rate", "Watch the rate", [question])
    assert row["surface"]["id"] == "overview"

    def delete_then_withdraw() -> tuple[dict[str, Any], dict[str, Any]]:
        surfaces.add(sid, "overview", state="deleted")
        deleted = science_log_view(app, sid)
        log.set_status(sid, view["id"], "withdrawn", "the question moved on", "c4")
        return deleted, science_log_view(app, sid)

    deleted, withdrawn = await asyncio.to_thread(delete_then_withdraw)
    assert deleted["views"][0]["surface"] is None
    assert deleted["views"][0]["reason"] == "view_surface_deleted"
    assert withdrawn["views"] == []


@pytest.mark.asyncio
async def test_measurements_carry_the_current_law_and_follow_its_status(
    arc: ARCMemory, tmp_path: Path
) -> None:
    app, sid, log, _ = await _study(arc, tmp_path)

    def work() -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
        made = _measured_study(app, sid, log, [2.1, 3.9])
        study = made["question"]["id"]
        before = source_rows(app, study, "measurements")
        log.set_status(sid, made["hypothesis"]["id"], "refuted", "it is not linear", "c9")
        return study, before, source_rows(app, study, "measurements")

    study, before, after = await asyncio.to_thread(work)
    assert [row["value"] for row in before] == [2.1, 3.9]
    assert [row["law_value"] for row in before] == [2.0, 4.0]
    assert "t = 2*n" in before[0]["law"]
    assert before[0]["observation_status"]
    assert all("law" not in row and "law_value" not in row for row in after)
    assert study


@pytest.mark.asyncio
async def test_entries_and_records_sources_read_the_study_now(
    arc: ARCMemory, tmp_path: Path
) -> None:
    app, sid, log, _ = await _study(arc, tmp_path)

    def record(call: str, key: str, rows: list[dict[str, Any]], at: str) -> None:
        app.state.messages.setdefault(sid, []).append(
            Message(
                id="msg_" + call,
                session_id=sid,
                role="assistant",
                created_at=at,
                updated_at=at,
                parts=[
                    Part(
                        type="tool_result",
                        call_id=call,
                        tool_name="guess",
                        content=[Part(type="text", text="{}")],
                        metadata={
                            "completed_at": at,
                            "ui_event": {"view": "board", "key": key, "rows": rows},
                        },
                    )
                ],
            )
        )

    def work() -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
        question = log.write(sid, "question", {"statement": "Solve the board"}, None, "c1")
        record("call_a", "game1", [{"guess": "crane", "score": 1}], "2026-10-27T00:00:01+00:00")
        record("call_b", "game1", [{"guess": "slate", "score": 3}], "2026-10-27T00:00:02+00:00")
        record("call_c", "game2", [{"guess": "pious", "score": 0}], "2026-10-27T00:00:03+00:00")
        study = question["id"]
        return (
            study,
            source_rows(app, study, "entries"),
            source_rows(app, study, "records", "board"),
        )

    study, entries, records = await asyncio.to_thread(work)
    assert [(row["type"], row["summary"]) for row in entries] == [("question", "Solve the board")]
    by_key = {row["key"]: row for row in records}
    assert by_key["game1"]["guess"] == "slate"  # the newest event of a (group, key) wins
    assert by_key["game2"]["score"] == 0
    with pytest.raises(EntryRejected) as unknown:
        await asyncio.to_thread(source_rows, app, "e_nope", "entries")
    assert unknown.value.reason == "study_not_found"
    assert study


@pytest.mark.asyncio
async def test_a_query_of_a_study_with_no_rows_yet_is_empty_not_an_error(
    arc: ARCMemory, tmp_path: Path
) -> None:
    app, sid, log, _ = await _study(arc, tmp_path)

    def work() -> dict[str, Any]:
        study = log.write(sid, "question", {"statement": "Nothing yet"}, None, "c0")["id"]
        return study_table_query(app, study, StudyTableQueryRequest(source="measurements"), 100)

    empty = await asyncio.to_thread(work)
    assert empty["empty"] == "study_source_empty"
    assert empty["totalRows"] == 0


@pytest.mark.asyncio
async def test_a_query_filters_and_sorts_study_measurements(arc: ARCMemory, tmp_path: Path) -> None:
    app, sid, log, _ = await _study(arc, tmp_path)

    def work() -> dict[str, Any]:
        study = _measured_study(app, sid, log, [2.1, 3.9, 6.2])["question"]["id"]
        body = StudyTableQueryRequest.model_validate(
            {
                "source": "measurements",
                "columns": ["condition.n", "value", "law_value"],
                "filter": [{"column": "value", "op": "range", "value": [3, None]}],
                "sort": [{"column": "value", "desc": True}],
            }
        )
        return study_table_query(app, study, body, 100)

    result = await asyncio.to_thread(work)
    assert result["columns"]["value"] == [6.2, 3.9]
    assert result["columns"]["law_value"] == [6.0, 4.0]
    assert result["cached"] is False


def _chart(study: str, y: str) -> dict[str, Any]:
    return {
        "id": "law",
        "component": "clio.chart.v1",
        "preset": "scatter",
        "xField": "condition.n",
        "yField": y,
        "entityField": "branch",
        "dataUri": f"study://{study}/measurements",
    }


@pytest.mark.asyncio
async def test_an_empty_study_source_is_accepted_and_reported(
    arc: ARCMemory, tmp_path: Path
) -> None:
    app, sid, log, _ = await _study(arc, tmp_path)

    def work() -> tuple[str, Any, list[dict[str, str]], Any]:
        empty = log.write(sid, "question", {"statement": "Nothing yet"}, None, "c0")["id"]
        notes: list[dict[str, str]] = []
        unchecked = validate_component_data_references(app, [_chart(empty, "anything")], notes)
        unknown = validate_component_data_references(app, [_chart("e_nope", "value")])
        return empty, unchecked, notes, unknown

    empty, unchecked, notes, unknown = await asyncio.to_thread(work)
    assert unchecked is None
    assert notes == [
        {
            "component": "law",
            "dataUri": f"study://{empty}/measurements",
            "reason": "study_source_empty",
        }
    ]
    assert unknown["reason"] == "a2ui_data_reference_not_found"


@pytest.mark.asyncio
async def test_a_component_over_a_study_source_is_checked_against_its_columns(
    arc: ARCMemory, tmp_path: Path
) -> None:
    app, sid, log, _ = await _study(arc, tmp_path)

    def work() -> tuple[Any, Any, Any]:
        study = _measured_study(app, sid, log, [2.0])["question"]["id"]
        mesh = {
            "id": "m",
            "component": "clio.mesh-viewport.v1",
            "meshUri": f"study://{study}/entries",
        }
        return (
            validate_component_data_references(app, [_chart(study, "value")]),
            validate_component_data_references(app, [_chart(study, "seconds")]),
            validate_component_data_references(app, [mesh]),
        )

    good, bad, mesh = await asyncio.to_thread(work)
    assert good is None
    assert bad["reason"] == "a2ui_field_not_in_dataset"
    assert mesh["reason"] == "a2ui_data_reference_unsupported_format"


def test_study_uris_parse_and_refuse() -> None:
    assert parse_study_uri("study://e_1/measurements") == ("e_1", "measurements", "")
    assert parse_study_uri("study://e_1/records/board") == ("e_1", "records", "board")
    with pytest.raises(EntryRejected) as bad:
        parse_study_uri("study://e_1/notes")
    assert bad.value.reason == "study_uri_invalid"


@pytest.mark.parametrize(
    ("text", "values", "expected"),
    [
        ("t = 2*n + 1", {"n": 3}, 7.0),
        ("t = -n^2", {"n": 3}, -9.0),
        ("t = 2^3^2", {}, 512.0),
        ("t = a ** 2 / b", {"a": 4, "b": 2}, 8.0),
        ("t = max(n, 2, 5) + sqrt(16) + log(exp(1)) + abs(-1)", {"n": 1}, 11.0),
        ("t = pi", {}, math.pi),
        ("t = 1 / n", {"n": 0}, None),
        ("t = log(n)", {"n": -1}, None),
        ("t = n", {}, None),
    ],
)
def test_a_formula_evaluates_without_eval(
    text: str, values: dict[str, float], expected: float | None
) -> None:
    value = evaluate_formula(parse_formula(text), values)
    assert value == (pytest.approx(expected) if expected is not None else None)


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("", "empty"),
        ("x" * 2001, "too_long"),
        ("t = " + "(" * 70 + "1" + ")" * 70, "too_deep"),
        ("t = n; import os", "unexpected_character"),
        ("t = 1.2.3", "invalid_number"),
        ("t = n n", "unexpected_token"),
        ("t = (n", "unexpected_end"),
        ("t = open(n)", "unknown_function"),
        ("t = sqrt(1, 2)", "wrong_arity"),
        ("t = sqrt", "reserved_name"),
        ("2 = n", "not_an_equation"),
    ],
)
def test_a_formula_refuses_with_a_typed_code(text: str, code: str) -> None:
    with pytest.raises(FormulaError) as refused:
        parse_formula(text)
    assert refused.value.code == code


def test_a_formula_lists_its_names_and_target() -> None:
    formula = parse_formula("t = a * n + a")
    assert (formula.target, formula.names) == ("t", ("a", "n"))
