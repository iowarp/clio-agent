"""Mechanical grading: location parsing and each task family's answer key."""

from __future__ import annotations

from typing import Any

import pytest

from scripts.eval.tool_calling.grade import grade, locations, same_file


def _run(text: str, tool_calls: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "outcome": {"final_text": text},
        "sessions": [{"tool_calls": tool_calls or []}],
    }


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("see `src/a/b.py:12`", [("src/a/b.py", 12, 12)]),
        ("b.py:12-15 and c.py line 40", [("b.py", 12, 15), ("c.py", 40, 40)]),
        ("in tools/x.py (lines 3–9)", [("tools/x.py", 3, 9)]),
        ("x.py#L7", [("x.py", 7, 7)]),
        ("| `pkg/mod.py` | 88 | off by one |", [("pkg/mod.py", 88, 88)]),
        ("| pkg/mod.py | 3 calls |", []),
        ("x.py lists things", []),
    ],
)
def test_locations(text: str, expected: list[tuple[str, int, int]]) -> None:
    assert locations(text) == expected


def test_same_file_matches_suffixes() -> None:
    assert same_file("tools/mcp_config.py", "tools/mcp_config.py")
    assert same_file("mcp_config.py", "tools/mcp_config.py")
    assert same_file("./p3/tools/mcp_config.py", "tools/mcp_config.py")
    assert not same_file("config.py", "tools/mcp_config.py")


P1 = {
    "function": "f_long_name",
    "definition": {"file": "src/pkg/a.py", "line": 10},
    "callers": [{"file": "src/pkg/b.py", "line": 5}, {"file": "src/pkg/c.py", "line": 9}],
}


def test_p1_all_callers() -> None:
    text = "Defined at src/pkg/a.py:10. Called from src/pkg/b.py:5 and c.py:9."
    result = grade("P1", P1, _run(text))
    assert result["correct"] and result["caller_recall"] == 1.0
    assert result["caller_precision"] == 1.0


def test_p1_missing_and_wrong_caller() -> None:
    result = grade("P1", P1, _run("a.py:10; callers: b.py:5, d.py:40"))
    assert not result["correct"] and result["definition_ok"]
    assert result["caller_recall"] == 0.5 and result["caller_precision"] == 0.5


P2 = {"file": "station_registry.py", "line": 4314, "value": 48, "name": "X"}


def test_p2() -> None:
    assert grade("P2", P2, _run("It is 48 ppm, set at station_registry.py:4314."))["correct"]
    assert grade("P2", P2, _run("48 ppm on line 4314"))["correct"]
    wrong = grade("P2", P2, _run("It is 55 ppm (line 2600)."))
    assert not wrong["correct"] and not wrong["line_ok"] and not wrong["value_ok"]


P3 = {
    "issues": [
        {"id": "B1", "file": "tools/a.py", "line_start": 10, "line_end": 10},
        {"id": "B2", "file": "tools/b.py", "line_start": 20, "line_end": 24},
        {"id": "B3", "file": "tools/c.py", "line_start": 5, "line_end": 5},
    ]
}


def test_p3_precision_and_recall() -> None:
    text = "1. tools/a.py:11 off by one\n2. b.py lines 22-23 swallowed\n3. z.py:1 style"
    result = grade("P3", P3, _run(text))
    assert result["matched"] == ["B1", "B2"]
    assert result["recall"] == pytest.approx(2 / 3, abs=1e-3)
    assert result["precision"] == pytest.approx(2 / 3, abs=1e-3)
    assert not result["correct"]


def test_p4_fact_recall() -> None:
    answer = {
        "subsystems": {
            "arc": {"facts": [{"match_any": ["memory"]}, {"match_any": ["ARCMemory"]}]},
            "plugins": {"facts": [{"match_any": ["loader"]}]},
        }
    }
    result = grade("P4", answer, _run("arc: memory.py holds ARCMemory. plugins: store."))
    assert result["fact_recall"] == pytest.approx(2 / 3, abs=1e-3)
    assert result["graded"] is False


RECORDS = {"count": 2, "ids": ["REC-00002", "REC-00015"]}


def test_records_exact_ids_and_count() -> None:
    assert grade("P5", RECORDS, _run("2 records: REC-00002 and REC-00015."))["correct"]
    partial = grade("P6", RECORDS, _run("3 records: REC-00002, REC-00015, REC-00016"))
    assert not partial["correct"] and partial["extra"] == ["REC-00016"]
    assert not partial["count_ok"]


def test_records_an_id_mentioned_in_an_aside_is_not_part_of_the_answer() -> None:
    text = (
        "**2 of the 20 records have a negative reading:**\n\n"
        "| Record | Reading |\n|---|---|\n| REC-00002 | -86.75 |\n| REC-00015 | -84.27 |\n\n"
        "REC-00001 is in kPa, and the same fields are available on the others."
    )
    result = grade("P5", RECORDS, _run(text))
    assert result["correct"] and result["extra"] == []
    assert result["mentioned_only"] == ["REC-00001"]
    ranged = "**2 of 20 are negative**: REC-00002, REC-00015. I read REC-00001 to REC-00020."
    result = grade("P5", RECORDS, _run(ranged))
    assert result["correct"] and result["mentioned_only"] == ["REC-00001", "REC-00020"]
    wrong = grade("P5", RECORDS, _run("2 negative:\n- REC-00002 (-3.1)\n- REC-00016 (-2.0)"))
    assert not wrong["correct"] and wrong["extra"] == ["REC-00016"]


P8 = {
    "tool": "climate_set_temperature",
    "args": {"entity_id": "climate.guest_bedroom", "temperature": 19.5},
}


def test_tool_call_grading() -> None:
    good = {
        "tool": "synthetic-home_climate_set_temperature",
        "args": {"entity_id": "climate.guest_bedroom", "temperature": 19.5},
    }
    wrong = {
        "tool": "synthetic-home_climate_set_temperature",
        "args": {"entity_id": "climate.office", "temperature": 19.5},
    }
    assert grade("P8", P8, _run("", [wrong, good]))["correct"]
    result = grade("P8", P8, _run("", [wrong]))
    assert result["tool_called"] and not result["args_ok"] and not result["correct"]
    assert not grade("P8", P8, _run("", []))["tool_called"]
    short_id = {**good, "args": {"entity_id": "guest_bedroom", "temperature": "19.5"}}
    assert grade("P8", P8, _run("", [short_id]))["correct"]  # fmt: skip


def test_p10_counts_deferred_calls() -> None:
    calls = [{"tool": "synthetic-home_light_list"}, {"tool": "fs_read_file"}]
    result = grade("P10", {"deferred_servers": ["synthetic-home"]}, _run("ok", calls))
    assert result == {"task": "P10", "graded": False, "correct": None, "deferred_tool_calls": 1}


H1 = {
    "root_cause_files": ["ledgerlite/parser.py"],
    "root_cause_functions": ["parse_amount"],
    "introducing_commit": "2cf5cf44cdac793cb75498b8322893cb19f0b72d",
}


def test_root_cause() -> None:
    text = "Commit 2cf5cf4 changed `parse_amount` in ledgerlite/parser.py so 1,250 reads 1.25."
    result = grade("H1", H1, _run(text))
    assert result == {
        "task": "H1",
        "graded": True,
        "correct": True,
        "file_ok": True,
        "function_ok": True,
        "commit_named": True,
    }
    decoy = grade("H3", H1, _run("The column alignment change in report.py broke format_report."))
    assert not decoy["correct"]
