"""The shell card tells the human where the full output went (#1487).

When the shell tool spills a stream, its presentation carries one ``link``
block per spilled stream right after the terminal block: a plain label ("Full
output saved (N lines, X KB)"), the tail excerpt, a caption, and an "Open full
output" action pointing at the saved file. The blocks must validate against the
server's closed presentation contract.
"""

from __future__ import annotations

from clio_agent.gact.tool_result_presentation import PAGE_CHARS, PresentationBlock
from clio_agent.tools.shell_spill_presentation import format_size, saved_output_blocks
from clio_agent.tools.tool_presentation import present_mcp_result

_PATH = "D:/ws/.clio/tool-output/sess_1/sh_1.stdout.txt"


def _spilled_row(**spill_overrides: object) -> dict:
    spill = {
        "status": "spilled",
        "reason": "shell_output_spilled",
        "path": _PATH,
        "relative_path": ".clio/tool-output/sess_1/sh_1.stdout.txt",
        "total_bytes": 400_000,
        "total_lines": 20_000,
        "head_lines": 300,
        "tail": "row-19998\nrow-19999\n",
        "tail_lines": 2,
    }
    spill.update(spill_overrides)
    return {
        "command": "python big.py",
        "stdout": "row-00000\nrow-00001\n",
        "stderr": "",
        "exit_code": 0,
        "stdout_truncated": True,
        "stdout_spill": spill,
    }


def test_spilled_stdout_adds_a_saved_output_block_after_the_terminal() -> None:
    view = present_mcp_result(
        "shell_bash", {"command": "python big.py"}, {"structuredContent": _spilled_row()}
    )
    types = [block["type"] for block in view["blocks"]]
    assert types == ["terminal", "link"]
    saved = view["blocks"][1]
    assert saved["label"] == "Full output saved (20,000 lines, 391 KB)"
    assert saved["target"] == "file"
    assert saved["uri"] == _PATH
    assert saved["action_label"] == "Open full output"
    assert saved["text"] == "row-19998\nrow-19999\n"
    assert saved["detail"] == "Last 2 lines"
    for block in view["blocks"]:
        PresentationBlock.model_validate(block)


def test_stderr_spill_is_labelled_as_error_output() -> None:
    row = _spilled_row()
    row["stderr_spill"] = row.pop("stdout_spill")
    blocks = saved_output_blocks(row)
    assert blocks[0]["label"].startswith("Full error output saved (")
    assert blocks[0]["id"] == "saved-output-stderr"


def test_failed_spill_says_so_without_an_open_action() -> None:
    row = _spilled_row(status="spill_failed", reason="shell_output_spill_failed", error="EACCES")
    row["stdout_spill"].pop("path")
    blocks = saved_output_blocks(row)
    assert blocks[0]["label"] == "Full output could not be saved (20,000 lines, 391 KB)"
    assert blocks[0]["uri"] == ""
    assert blocks[0]["action_label"] == ""
    PresentationBlock.model_validate(blocks[0])


def test_tail_is_trimmed_to_one_page_on_a_line_boundary() -> None:
    tail = "".join(f"line-{i:05d}\n" for i in range(1_000))
    blocks = saved_output_blocks(_spilled_row(tail=tail, tail_lines=1_000))
    text = blocks[0]["text"]
    assert len(text) <= PAGE_CHARS
    assert text.endswith("line-00999\n")
    assert text.startswith("line-")  # whole lines only
    assert blocks[0]["detail"] == f"Last {len(text.splitlines())} lines"


def test_no_spill_means_no_extra_block() -> None:
    row = {"command": "echo hi", "stdout": "hi\n", "stderr": "", "exit_code": 0}
    assert saved_output_blocks(row) == []


def test_format_size() -> None:
    assert format_size(10) == "1 KB"
    assert format_size(400_000) == "391 KB"
    assert format_size(5 * 1024 * 1024 + 1) == "5.0 MB"
