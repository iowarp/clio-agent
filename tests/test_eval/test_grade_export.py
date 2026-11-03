"""H4/H5 grading: each seeded finding detected, the wrong run or the decoy rejected."""

from __future__ import annotations

from typing import Any

import pytest

from scripts.eval.tool_calling.fixtures.export_h4 import write_export
from scripts.eval.tool_calling.grade import grade
from scripts.eval.tool_calling.grade_export import names_run, passages


@pytest.fixture(scope="module")
def key(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    return write_export(tmp_path_factory.mktemp("h4-key") / "h4-export", 7, scale=360)


def _a(key: dict[str, Any], kind: str) -> dict[str, Any]:
    return next(a for a in key["anomalies"] if a["id"] == kind)


def _long_date(iso_day: str) -> str:
    year, month, day = iso_day.split("-")
    names = ["January", "February", "March", "April", "May", "June", "July"]
    return f"{int(day)} {names[int(month) - 1]} {year}"


def _findings(key: dict[str, Any]) -> dict[str, str]:
    """One correct passage per finding, each naming its run a different way."""
    cal, unit, dup, clock = (_a(key, k) for k in ("calibration", "unit", "duplicate", "clock"))
    rate = key["effect"]["metrics"][0]
    return {
        "calibration": f"- **{cal['runs'][0]}**: the gas analyser failed its zero check, so "
        "A_net is biased upward and reads positive at night.",
        "unit": f"- {unit['chamber']}, {_long_date(unit['date'])}: plant heights are stored "
        "in cm, though the metadata says mm.",
        "duplicate": f"- `{dup['copy']}` is a byte-identical duplicate of {dup['runs'][0]}.",
        "clock": f"- {clock['chamber']} in campaign {clock['campaign']}: the environment logger "
        "used the panel clock, so env.csv timestamps are one hour late.",
        "effect": "\n".join(
            [
                "## Far-red effect on elongation",
                "",
                "| Condition | Stem elongation (mm/day) |",
                "|---|---|",
                f"| A (control) | {rate['A']:.1f} |",
                f"| B (far-red) | {rate['B']:.1f} |",
            ]
        ),
    }


def _answer(parts: dict[str, str], *extra: str) -> str:
    anomalies = [v for k, v in parts.items() if k != "effect"]
    return "\n".join(
        [
            "# PFR-26 export",
            "",
            "Tomato seedlings were grown under white LED (A) and far-red enriched light (B).",
            "",
            "## Data problems",
            "",
            *anomalies,
            *extra,
            "",
            parts.get("effect", ""),
        ]
    )


def _grade(key: dict[str, Any], text: str, task: str = "H4") -> dict[str, Any]:
    return grade(task, key, {"outcome": {"final_text": text}})


def test_a_correct_answer_finds_everything(key) -> None:
    decoy = key["decoy"]
    explained = (
        f"- The three dark hours in {decoy['runs'][0]} are the planned dark adaptation "
        "(protocol 5.2), not a fault."
    )
    result = _grade(key, _answer(_findings(key), explained), task="H5")
    assert result["task"] == "H5" and result["graded"]
    assert result["findings"] == dict.fromkeys(
        ("calibration", "unit", "duplicate", "clock", "effect"), True
    )
    assert result["correct"] and result["finding_recall"] == 1.0
    assert not result["decoy_flagged"] and result["decoy_explained"]


@pytest.mark.parametrize("missing", ["calibration", "unit", "duplicate", "clock", "effect"])
def test_each_finding_is_graded_on_its_own(key, missing: str) -> None:
    parts = _findings(key)
    del parts[missing]
    result = _grade(key, _answer(parts))
    assert not result["correct"]
    assert [k for k, v in result["findings"].items() if not v] == [missing]


def test_the_right_problem_on_the_wrong_run_does_not_count(key) -> None:
    parts = _findings(key)
    cal, unit = _a(key, "calibration"), _a(key, "unit")
    parts["calibration"] = parts["calibration"].replace(cal["runs"][0], unit["runs"][0])
    parts["unit"] = f"- {cal['runs'][0]}: plant heights are stored in cm."
    result = _grade(key, _answer(parts))
    assert not result["findings"]["calibration"] and not result["findings"]["unit"]
    table = (
        "| Run | Problem |\n|---|---|\n"
        f"| {unit['runs'][0]} | analyser zero check failed |\n| {cal['runs'][0]} | heights in cm |"
    )
    del parts["calibration"], parts["unit"]
    result = _grade(key, _answer(parts, "", table))
    assert not result["findings"]["calibration"] and not result["findings"]["unit"]


def test_the_right_run_with_the_wrong_problem_does_not_count(key) -> None:
    parts = _findings(key)
    clock = _a(key, "clock")
    parts["clock"] = f"- {clock['runs'][0]}: two plants were removed after imaging."
    assert not _grade(key, _answer(parts))["findings"]["clock"]


def test_reporting_the_decoy_as_a_fault_fails(key) -> None:
    decoy = key["decoy"]
    day = decoy["dark_utc"][0][:10]
    flagged = (
        f"- {decoy['chamber']} lost its lights on {_long_date(day)} from 10:00 to 13:00: "
        "a lighting fault; exclude that interval."
    )
    result = _grade(key, _answer(_findings(key), flagged))
    assert result["decoy_flagged"] and not result["correct"]
    assert all(result["findings"].values())


def test_a_decoy_explained_beside_a_fault_word_is_not_flagged(key) -> None:
    """The interval explained in one passage, mentioned with a fault word in another.

    Found grading head1 H4 run 3 (2026-11-03): its section "Checked and fine: the
    10:00-13:00 dark interval is documented ... This is not a fault" explained the decoy,
    but the progress notes merged above the answer named the chamber and "issues", and
    that one passage flagged the decoy.

    SABOTAGE: return ``faulted`` (not ``faulted and not explained``) from ``_decoy`` and
    the explained decoy is flagged.
    """
    decoy = key["decoy"]
    run = decoy["runs"][0]
    day = decoy["dark_utc"][0][:10]
    checked = "\n".join(
        [
            f"Working notes: {run} shows dropped frames in the 10:00-13:00 interval; an issue?",
            "",
            "## Checked and fine",
            f"- **{run}:** the 10:00-13:00 dark interval on {day} is documented in the README.",
            "  - This is the planned dark adaptation, not a fault.",
        ]
    )
    result = _grade(key, _answer(_findings(key), checked))
    assert not result["decoy_flagged"] and result["decoy_explained"]
    assert result["correct"]


def test_effect_needs_its_size(key) -> None:
    rate = key["effect"]["metrics"][0]
    parts = _findings(key)
    for text, found in (
        (f"Far-red (B) stems elongated {rate['B']:.2f} mm/day against {rate['A']:.2f} in A.", True),
        (
            f"Far-red enrichment made stems elongate about {round(100 * rate['relative'])}% faster.",
            True,
        ),
        (
            f"Height grew {rate['A']:.1f} mm/day in A and {rate['A'] + 0.3:.1f} under far-red.",
            False,
        ),
        ("Under far-red light the stems grew no differently (height within 2%).", False),
        (f"Elongation: A {rate['A']:.1f}, B {rate['B']:.1f} mm/day.", True),
        (f"Leaf area: A {rate['A']:.1f}, B {rate['B']:.1f} cm2 per day.", False),
        # A gain this small cannot be told from other percentages (far-red share, RH).
        ("Under far-red the plants ended 13% taller.", False),
    ):
        parts["effect"] = text
        assert _grade(key, _answer(parts))["findings"]["effect"] is found, text


def test_passages_keep_their_heading_and_parent() -> None:
    text = (
        "### run_0112\n\n- analyser\n  - zero check failed\n\n"
        "| Run | Problem |\n|---|---|\n| run_0107 | cm |\n| run_0102 | clock |"
    )
    chunks = passages(text)
    assert "### run_0112\n- analyser\n  - zero check failed" in chunks
    assert "### run_0112\n| Run | Problem |\n| run_0107 | cm |" in chunks
    assert any(c.endswith("| run_0102 | clock |") and "run_0107" not in c for c in chunks)
    joined = passages(text, join_tables=True)
    assert any("run_0107" in c and "run_0102" in c for c in joined)


@pytest.mark.parametrize(
    ("text", "named"),
    [
        ("run_0112", True),
        ("run 112", True),
        ("| 0112 |", True),
        ("run_0113", False),
        ("A_net 0.0112", False),
        ("GC4 on 2026-03-17", True),
        ("chamber 4, Mar 16", True),
        ("GC4, 18th March", True),
        ("GC4 in campaign 3", True),
        ("GC4 on 2026-03-19", False),
        ("GC3 on 2026-03-16", False),
        ("2026-03-16 only", False),
    ],
)
def test_names_run(text: str, named: bool) -> None:
    where = {"runs": ["run_0112"], "chamber": "GC4", "campaign": 3,
             "dates": ["2026-03-16", "2026-03-17", "2026-03-18"]}  # fmt: skip
    assert names_run(where, text) is named


def test_an_empty_answer_finds_nothing(key) -> None:
    assert _grade(key, "")["findings"] == dict.fromkeys(
        ("calibration", "unit", "duplicate", "clock", "effect"), False
    )


def test_h6_needs_the_findings_the_constraints_and_the_change() -> None:
    """SABOTAGE: accept heights in mm in ``grade_h6`` and the mm answer passes."""
    from scripts.eval.tool_calling.grade_export import grade_h6

    answer = {
        "anomalies": [
            {"id": "calibration", "runs": ["run_0112"], "campaign": 3, "terms": ["offset"]},
            {"id": "clock", "runs": ["run_0102"], "campaign": 1, "terms": ["clock"]},
        ],
        "decoy": {"runs": ["run_0123"], "terms": [], "fault": [], "planned": []},
        "effect": {"metrics": []},
    }
    good = (
        "- run_0112 has an uncorrected offset.\n- run_0102 has a clock shift.\n"
        "- Plant heights reached 14.2 cm under B.\n\n"
        "| run | problem | evidence file |\n|---|---|---|\n"
        "| run_0112 | offset | a.log |\n| run_0102 | clock | b.log |\n"
    )
    result = grade_h6(answer, good)
    assert result["constraints"] == {
        "heights_in_cm": True,
        "change_honoured": True,
        "problem_table": True,
    }
    in_mm = grade_h6(answer, good.replace("14.2 cm", "14.2 cm (142 mm)"))
    assert in_mm["constraints"]["heights_in_cm"] is False and not in_mm["correct"]
    no_c3 = grade_h6(
        answer,
        good.replace("- run_0112 has an uncorrected offset.\n", "").replace(
            "| run_0112 | offset | a.log |\n", ""
        ),
    )
    assert no_c3["constraints"]["change_honoured"] is False
