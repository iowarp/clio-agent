"""The run-shape report: steps, width, shell composition, scripts and the time split."""

from __future__ import annotations

from typing import Any

from scripts.eval.tool_calling.behaviour import render, run_behaviour, shell_shape


def test_shell_shape_counts_piped_and_sequenced_commands() -> None:
    assert shell_shape("ls") == (1, 0, 0)
    assert shell_shape("git log --oneline | head -5") == (2, 1, 0)
    assert shell_shape("cd x && rg foo | wc -l; echo done") == (4, 1, 2)
    # Separators inside quotes are text, not composition.
    assert shell_shape("rg 'a|b' && echo \"x; y\"") == (2, 0, 1)
    assert shell_shape("a || b") == (2, 0, 1)


def _call(cid: str, tool: str, step: str, start: float, end: float, **args: Any) -> dict[str, Any]:
    return {
        "id": cid,
        "tool": tool,
        "step_id": step,
        "started_at": start,
        "ended_at": end,
        "args": args,
    }


def _run() -> dict[str, Any]:
    calls = [
        _call("a", "shell_bash", "s1", 100.0, 101.0, command="rg x | head"),
        _call("b", "fs_read_file", "s1", 100.0, 100.5),
        _call("c", "run_code", "s2", 103.0, 107.0, code="..."),
        _call("d", "records_get_record", "s2", 103.5, 104.0),
        _call("e", "records_get_record", "s2", 104.0, 106.0),
    ]
    lm = [
        {"step_id": "s1", "duration_ms": 1000.0},
        {"step_id": "s2", "duration_ms": 1500.0},
        {"step_id": "s3", "duration_ms": 500.0},
    ]
    return {
        "task": "P5",
        "config": "current",
        "wall_s": 10.0,
        "sessions": [{"tool_calls": calls, "lm_calls": lm}],
    }


def test_run_behaviour_separates_script_calls_and_splits_the_time() -> None:
    shape = run_behaviour(_run())
    assert (shape["steps"], shape["calls"], shape["width_max"]) == (3, 3, 2)
    assert (shape["scripts"], shape["nested"]) == (1, 2)
    assert (shape["shells"], shape["commands_per_shell"], shape["pipes"]) == (1, 2.0, 1)
    assert shape["model_s"] == 3.0 and shape["tool_s"] == 5.0
    assert shape["other_s"] == 2.0


def test_render_puts_configurations_side_by_side() -> None:
    table = render([_run(), {**_run(), "config": "base"}])
    assert "### P5" in table
    assert table.index("| base |") < table.index("| current |")
