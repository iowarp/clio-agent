"""One-line MCP commands keep Windows paths intact (#1220)."""

from __future__ import annotations

import sys

import pytest

from clio_agent.tools.mcp_command_line import split_command
from clio_agent.tools.mcp_config import spec_from_declaration

_LOCALAPPDATA = r"C:\Users\jaime\AppData\Local"


@pytest.mark.parametrize(
    ("text", "argv"),
    [
        (r"C:\tools\uv.exe run server", [r"C:\tools\uv.exe", "run", "server"]),
        (
            r'"C:\Program Files\uv\uv.exe" --directory "D:\a b"',
            [r"C:\Program Files\uv\uv.exe", "--directory", r"D:\a b"],
        ),
        (r'tool "D:\dir\\" next', ["tool", "D:\\dir\\", "next"]),
        (r"tool a\"b \\\"c", ["tool", 'a"b', '\\"c']),
        ('tool "" --name="a b"\t x', ["tool", "", "--name=a b", "x"]),
        ("  ", []),
    ],
)
def test_windows_rules_keep_backslashes_and_drop_grouping_quotes(text, argv):
    assert split_command(text, windows=True) == argv


def test_an_unclosed_quote_is_refused_on_either_platform():
    with pytest.raises(ValueError, match="No closing quotation"):
        split_command('tool "C:\\x', windows=True)
    with pytest.raises(ValueError, match="No closing quotation"):
        split_command('tool "x', windows=False)


def test_posix_rules_are_unchanged_off_windows():
    assert split_command(r"tool a\ b 'c d'", windows=False) == ["tool", "a b", "c d"]


def test_a_string_declaration_keeps_an_expanded_windows_path(monkeypatch):
    """The live failure: ``${LOCALAPPDATA}`` expanded, then split as POSIX."""
    monkeypatch.setattr(sys, "platform", "win32")
    spec = spec_from_declaration(
        "spotter",
        "uv --directory ${LOCALAPPDATA}/clio-agent/packs/spotter run spotter-mcp",
        env={"LOCALAPPDATA": _LOCALAPPDATA},
    )
    assert spec.usable
    assert spec.command == "uv"
    assert spec.args == (
        "--directory",
        _LOCALAPPDATA + "/clio-agent/packs/spotter",
        "run",
        "spotter-mcp",
    )
    # The declared text is split the same way, so redaction lines up with argv.
    assert spec.declared["args"][1] == "${LOCALAPPDATA}/clio-agent/packs/spotter"


def test_a_mapping_declaration_with_string_args_keeps_a_windows_path(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    spec = spec_from_declaration(
        "spotter",
        {"command": r"C:\tools\uv.exe", "args": r"--directory C:\packs\spotter run x"},
        env={},
    )
    assert spec.command == r"C:\tools\uv.exe"
    assert spec.args == ("--directory", r"C:\packs\spotter", "run", "x")
