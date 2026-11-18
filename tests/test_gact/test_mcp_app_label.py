"""A view is named by its connector and its subject (FP-D2), not by its tool's title."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from clio_agent.gact import mcp_app_label
from clio_agent.gact.mcp_app_label import call_subject, view_label


def _tool(title: str = "Open a viewer", properties: tuple[str, ...] = ()) -> SimpleNamespace:
    return SimpleNamespace(
        title=title,
        annotations=None,
        inputSchema={"type": "object", "properties": {key: {} for key in properties}},
    )


@pytest.fixture
def names(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    labels: dict[str, str] = {}
    monkeypatch.setattr(
        "clio_agent.gact.mcp_user_configuration.user_mcp_labels", lambda: dict(labels)
    )
    return labels


def test_a_file_argument_is_the_subject_reduced_to_its_name(names: dict[str, str]) -> None:
    names["viewer"] = "Viewer Pro"
    tool = _tool(properties=("path", "scene", "new"))
    label = view_label("viewer", tool, {"path": "/data/run/pulse_series.pvd", "new": True})
    assert label == "Viewer Pro: pulse_series.pvd"


def test_a_file_wins_over_an_earlier_plain_string(names: dict[str, str]) -> None:
    tool = _tool(properties=("mode", "source"))
    args = {"mode": "surface", "source": "https://example.org/files/1abc.pdb?raw=1"}
    assert call_subject(tool, args) == "1abc.pdb"


def test_a_short_plain_string_is_the_subject_when_no_file_is_named() -> None:
    tool = _tool(properties=("pdb_id", "style"))
    assert call_subject(tool, {"pdb_id": "1CRN", "style": "cartoon"}) == "1CRN"


def test_code_long_text_and_non_strings_are_never_a_subject() -> None:
    tool = _tool(properties=("code", "count", "note"))
    args = {"code": "import x\nx.run()", "count": 3, "note": "n" * 200}
    assert call_subject(tool, args) == ""
    assert call_subject(tool, None) == ""


def test_undeclared_arguments_follow_declared_ones(names: dict[str, str]) -> None:
    tool = _tool(properties=("scene",))
    assert call_subject(tool, {"extra": "b.vti", "scene": "a-1"}) == "b.vti"


def test_without_a_saved_label_the_catalog_name_is_used(
    names: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = SimpleNamespace(name="Catalog Name")
    monkeypatch.setattr(
        "clio_agent.connectors.entries.load_catalog",
        lambda: SimpleNamespace(entries={"cat": entry}),
    )
    assert view_label("cat", _tool(properties=("p",)), {"p": "a.csv"}) == "Catalog Name: a.csv"


def test_an_unknown_server_falls_back_to_the_tool_title_then_the_namespace(
    names: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "clio_agent.connectors.entries.load_catalog", lambda: SimpleNamespace(entries={})
    )
    assert view_label("plain", _tool("Show a board"), {}) == "Show a board"
    assert view_label("plain", None, {"name": "Q3"}) == "plain: Q3"
    assert view_label(None, None, None) == ""


def test_an_unreadable_label_file_names_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from clio_agent.gact.mcp_user_configuration import McpUserConfigurationError

    def broken() -> dict[str, str]:
        raise McpUserConfigurationError("broken")

    monkeypatch.setattr("clio_agent.gact.mcp_user_configuration.user_mcp_labels", broken)
    monkeypatch.setattr(
        "clio_agent.connectors.entries.load_catalog", lambda: SimpleNamespace(entries={})
    )
    assert mcp_app_label.connector_name("x") == ""


def test_a_pattern_names_its_folder_or_its_own_stem() -> None:
    """FQ: "~/data/pulse_series/*" was labelled "ParaView: *"."""

    tool = _tool(properties=("path",))
    assert call_subject(tool, {"path": "~/data/pulse_series/*"}) == "pulse_series"
    assert call_subject(tool, {"path": "~/data/pulse*"}) == "pulse*"
    assert call_subject(tool, {"path": "/runs/*/*.vti"}) == "*.vti"
