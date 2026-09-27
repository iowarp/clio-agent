"""Shell output spill, command-length guidance, and discoverable limits (#1487, #887).

The shell tool used to DROP everything past its output cap, and its default cap
(16 KiB) was larger than the downstream tool-result bounds (12,000 chars), so a
tabular stdout was re-wrapped by the transcript/model bounders into a truncated,
malformed JSON-in-JSON preview (#887). These tests pin the replacement:

* over-budget output spills IN FULL to ``<workspace>/.clio/tool-output/`` and the
  result carries a line-aligned head excerpt, a tail, the path, and totals;
* the result always fits both downstream bounds, so neither bounder rewraps it
  and ``stdout`` stays a real string (CSV keeps its shape, on Windows too);
* an over-long command is refused with an actionable "write a file, run it";
* the model-facing description is generated from the effective limits.
"""

from __future__ import annotations

import csv
import io
import json
import os
import sys
from pathlib import Path

import pytest
from fastmcp import Client

from clio_agent import conf
from clio_agent.tools.execution import tool_workspace_context
from clio_agent.tools.servers.shell_output import (
    SPILL_FAILED_REASON,
    SPILLED_REASON,
    StreamCapture,
    shell_result_char_budget,
)
from clio_agent.tools.servers.shell_server import (
    ShellEnvFacts,
    ShellLimits,
    build_shell_tool_description,
    resolve_shell_limits,
    shell_server,
)
from clio_agent.tools.servers.shell_spill_store import spill_directory
from tests._config_layer import set_config


def _parse(result: object) -> dict:
    data = getattr(result, "data", result)
    if isinstance(data, dict):
        return data
    return json.loads(data)


def _python(script: Path) -> str:
    if os.name == "nt":
        return f"& '{sys.executable}' '{script}'"
    return f"'{sys.executable}' '{script}'"


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "ws"
    root.mkdir()
    monkeypatch.chdir(root)
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(tmp_path))
    conf.reload()
    yield root
    conf.reload()


async def _run(root: Path, args: dict) -> dict:
    with tool_workspace_context(str(root)):
        async with Client(shell_server) as client:
            return _parse(await client.call_tool("bash", args))


# --------------------------------------------------------------------------- #
# Spill instead of dropping
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_big_output_spills_in_full_with_head_tail_and_totals(workspace: Path) -> None:
    """200k+ bytes of stdout: the file holds EVERY line; the result is an excerpt."""

    lines = 20_000
    script = workspace / "big.py"
    script.write_text(
        f"for i in range({lines}):\n    print(f'row-{{i:06d}},payload')\n", encoding="utf-8"
    )
    data = await _run(workspace, {"command": _python(script), "timeout_s": 60})

    assert data["exit_code"] == 0
    assert data["stdout_truncated"] is True
    spill = data["stdout_spill"]
    assert spill["status"] == "spilled"
    assert spill["reason"] == SPILLED_REASON
    path = Path(spill["path"])
    assert path.parent == spill_directory(workspace).resolve()
    assert path.name.endswith(".stdout.txt")
    assert spill["relative_path"] == path.relative_to(workspace.resolve()).as_posix()

    text = path.read_bytes().decode("utf-8").replace("\r\n", "\n")
    expected = [f"row-{i:06d},payload" for i in range(lines)]
    assert text.splitlines() == expected  # nothing discarded
    assert spill["total_bytes"] == path.stat().st_size
    assert spill["total_lines"] == lines

    # Head excerpt: starts at the beginning, cut on a line boundary.
    assert data["stdout"].startswith("row-000000,payload\n")
    assert data["stdout"].endswith("\n")
    assert data["stdout"].splitlines() == expected[: spill["head_lines"]]
    # Tail excerpt: whole trailing lines ending at the real end.
    assert spill["tail"].splitlines() == expected[lines - spill["tail_lines"] :]
    assert spill["tail_lines"] > 0

    # The result fits both downstream bounds, so nothing rewraps it.
    assert len(json.dumps(data)) <= shell_result_char_budget()
    assert "stderr_spill" not in data


@pytest.mark.asyncio
async def test_spill_is_filed_under_the_active_session(workspace: Path) -> None:
    """Retention: a session's spills live in its own folder so deleting the
    session can delete them (no TTL)."""

    from clio_agent.gact import context as _ctx

    script = workspace / "big.py"
    script.write_text("for i in range(5000):\n    print(f'r{i:05d}')\n", encoding="utf-8")
    token = _ctx.set_tool_session_id("sess_owner1")
    try:
        data = await _run(workspace, {"command": _python(script), "timeout_s": 60})
    finally:
        _ctx.reset(token)

    path = Path(data["stdout_spill"]["path"])
    assert path.parent == spill_directory(workspace, session_id="sess_owner1").resolve()
    assert data["stdout_spill"]["relative_path"].startswith(".clio/tool-output/sess_owner1/")


@pytest.mark.asyncio
async def test_small_output_stays_inline_and_writes_no_file(workspace: Path) -> None:
    script = workspace / "small.py"
    script.write_text("print('hello')\n", encoding="utf-8")
    data = await _run(workspace, {"command": _python(script), "timeout_s": 30})

    assert data["stdout"] == "hello\n"
    assert data["stdout_truncated"] is False
    assert "stdout_spill" not in data
    assert not spill_directory(workspace).exists()


@pytest.mark.asyncio
async def test_output_under_cap_but_over_result_bound_still_spills(workspace: Path) -> None:
    """A stream under max_output_bytes whose RESULT would breach the downstream
    bound spills too — the bound, not the per-stream cap, decides fit."""

    set_config("limits", {"tool_result_chars": 2_000, "model_tool_result_chars": 4_000})
    script = workspace / "mid.py"
    script.write_text("for i in range(400):\n    print(f'line {i:04d}')\n", encoding="utf-8")
    data = await _run(workspace, {"command": _python(script), "timeout_s": 30})

    assert shell_result_char_budget() == 2_000
    assert data["stdout_spill"]["status"] == "spilled"
    assert data["stdout_spill"]["total_lines"] == 400
    assert len(json.dumps(data)) <= 2_000


@pytest.mark.asyncio
async def test_spill_write_failure_returns_typed_reason_with_excerpt(workspace: Path) -> None:
    """If the spill file cannot be written, the result says so (typed) and still
    carries the excerpt — never a silent drop."""

    blocker = spill_directory(workspace)
    blocker.parent.mkdir(parents=True)
    blocker.write_text("not a directory", encoding="utf-8")  # mkdir must fail
    script = workspace / "big.py"
    script.write_text("for i in range(5000):\n    print(f'r{i:05d}')\n", encoding="utf-8")
    data = await _run(workspace, {"command": _python(script), "timeout_s": 60})

    spill = data["stdout_spill"]
    assert spill["status"] == "spill_failed"
    assert spill["reason"] == SPILL_FAILED_REASON
    assert spill["error"]
    assert "path" not in spill
    assert spill["total_lines"] == 5000
    assert data["stdout"].startswith("r00000\n")
    assert spill["tail"].rstrip().endswith("r04999")


# --------------------------------------------------------------------------- #
# #887: CSV keeps its content type (reproduced on Windows with `type`)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_csv_stdout_survives_both_downstream_bounders_unwrapped(workspace: Path) -> None:
    """#887: a CSV larger than the old cap used to come back as a truncated,
    malformed JSON preview. Now the shell result is sized to fit both bounds, so
    neither bounder rewraps it and ``stdout`` is still parseable CSV."""

    from clio_agent.gact.evidence import _bounded_tool_call_result
    from clio_agent.tools.mcp_result_projection import bounded_model_tool_result

    header = 'station,lat,lon,"name",value'
    rows = [header] + [
        f'ST{i:04d},{34 + i / 1000:.4f},{-118 - i / 1000:.4f},"Station {i}",{i * 1.5}'
        for i in range(400)
    ]
    data_csv = workspace / "data.csv"
    data_csv.write_text("\n".join(rows) + "\n", encoding="utf-8")
    command = f'type "{data_csv}"' if os.name == "nt" else f"cat '{data_csv}'"
    data = await _run(workspace, {"command": command, "timeout_s": 30})

    # Transcript lane: unchanged (no {preview, truncated} envelope).
    assert _bounded_tool_call_result(data) is data
    # Model lane: unchanged text that still decodes to the same dict.
    encoded = json.dumps(data)
    assert bounded_model_tool_result(encoded) == encoded

    parsed = list(csv.reader(io.StringIO(data["stdout"])))
    assert parsed[0] == ["station", "lat", "lon", "name", "value"]
    assert len(parsed) > 1
    assert all(len(row) == 5 for row in parsed)  # line-aligned: no torn row
    spill_text = Path(data["stdout_spill"]["path"]).read_text(encoding="utf-8")
    assert spill_text.replace("\r\n", "\n").splitlines() == rows


# --------------------------------------------------------------------------- #
# Command length
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_over_long_command_gets_actionable_write_a_file_message(workspace: Path) -> None:
    limits = sys.modules["clio_agent.tools.servers.shell_server"]._SHELL_LIMITS
    command = "echo " + "x" * limits.max_command_chars
    data = await _run(workspace, {"command": command})

    error = data["error"]
    assert error["code"] == "command_too_long"
    assert str(limits.max_command_chars) in error["message"]
    assert "write" in error["message"].lower() and "file" in error["message"].lower()
    assert error["details"]["max_chars"] == limits.max_command_chars
    assert error["details"]["received_chars"] == len(command)
    assert error["details"]["next_action"]


def test_default_command_cap_fits_a_heredoc_analysis_script() -> None:
    """A ~150-line profiling heredoc is accepted by default (old cap was 4,000)."""

    script_line = "print(df.describe(include='all').T.to_string())  # profile\n"
    heredoc = "uv run python - <<'EOF'\n" + script_line * 150 + "EOF"
    assert len(heredoc) > 4_000
    assert len(heredoc) <= resolve_shell_limits().max_command_chars


# --------------------------------------------------------------------------- #
# Discoverable limits
# --------------------------------------------------------------------------- #


def test_limits_resolve_from_config_keys() -> None:
    set_config(
        "limits",
        {
            "shell_max_command_chars": 12_345,
            "shell_default_output_bytes": 2_222,
            "shell_max_output_bytes": 33_333,
            "tool_result_chars": 7_000,
            "model_tool_result_chars": 9_000,
        },
    )
    limits = resolve_shell_limits()
    assert limits.max_command_chars == 12_345
    assert limits.default_output_bytes == 2_222
    assert limits.max_output_bytes == 33_333
    assert limits.result_chars == 7_000


@pytest.mark.parametrize("is_windows", [True, False])
def test_description_states_effective_limits_and_spill(is_windows: bool) -> None:
    facts = ShellEnvFacts(
        is_windows=is_windows,
        system_label="Windows" if is_windows else "Linux",
        shell_label="PowerShell" if is_windows else "bash",
        posix_text_tools=not is_windows,
    )
    limits = ShellLimits(
        max_command_chars=12_345,
        default_output_bytes=2_222,
        max_output_bytes=33_333,
        result_chars=7_000,
    )
    text = build_shell_tool_description(facts, limits)
    for number in ("12345", "2222", "33333", "7000"):
        assert number in text
    assert ".clio/tool-output/" in text
    assert "stdout_spill" in text
    assert "write" in text.lower() and "file" in text.lower()


@pytest.mark.asyncio
async def test_listed_tool_description_matches_the_effective_config() -> None:
    from clio_agent.tools.servers import shell_server as _mod  # noqa: F401

    module = sys.modules["clio_agent.tools.servers.shell_server"]
    limits = module._SHELL_LIMITS
    async with Client(shell_server) as client:
        tools = {t.name: t for t in await client.list_tools()}
    description = tools["bash"].description or ""
    assert str(limits.max_command_chars) in description
    assert str(limits.default_output_bytes) in description
    assert ".clio/tool-output/" in description


# --------------------------------------------------------------------------- #
# StreamCapture unit behaviour (bounded memory, totals)
# --------------------------------------------------------------------------- #


def test_stream_capture_memory_is_bounded_and_totals_exact(tmp_path: Path) -> None:
    capture = StreamCapture("stdout", inline_limit=64, spill_path=tmp_path / "o" / "x.txt")
    total = 0
    for i in range(10_000):
        chunk = f"{i}\n".encode()
        capture.feed(chunk)
        total += len(chunk)
    capture.finish()
    assert capture.total_bytes == total
    assert capture.total_lines == 10_000
    assert capture.buffered_bytes <= 2 * 64
    assert capture.spill_status == "spilled"
    assert (tmp_path / "o" / "x.txt").stat().st_size == total


def test_stream_capture_counts_an_unterminated_last_line(tmp_path: Path) -> None:
    capture = StreamCapture("stdout", inline_limit=1_000, spill_path=tmp_path / "x.txt")
    capture.feed(b"a\nb\nc")
    capture.finish()
    assert capture.total_lines == 3
    assert capture.spill_status is None
    assert capture.full_text() == "a\nb\nc"
