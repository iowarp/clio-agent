"""Browser tool declarations, targets, presentation and provenance (no browser needed)."""

from __future__ import annotations

from typing import Any

import pytest

from clio_agent.gact.agents.tool_instrumentation import READ_ONLY_ATTR
from clio_agent.gact.artifacts.ingest_edges import declare_web_domains
from clio_agent.gact.browser.presentation import presenter_for, starting_for
from clio_agent.gact.browser.provenance import page_source_edges
from clio_agent.gact.browser.settings import BrowserError
from clio_agent.gact.browser.targets import describe, locator_for
from clio_agent.gact.browser.tools import BROWSER_TOOLS, BUILDERS
from clio_agent.gact.code_mode.dispatch import CONCURRENCY_GROUP_ATTR
from clio_agent.tools.tool_settings import CALLERS_ATTR


class _Locator:
    """Records the Playwright locator calls a spec turns into."""

    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def __getattr__(self, name: str) -> Any:
        if name in {"first", "last", "content_frame"}:
            self.calls.append(name)
            return self

        def call(*args: Any, **kwargs: Any) -> "_Locator":
            shown = [repr(a) for a in args] + [f"{k}={v!r}" for k, v in sorted(kwargs.items())]
            self.calls.append(f"{name}({', '.join(shown)})")
            return self

        return call


def test_every_browser_tool_is_code_only_in_one_group_with_a_presenter() -> None:
    assert set(BUILDERS) == set(BROWSER_TOOLS) and len(BROWSER_TOOLS) == 10
    for name, build in BUILDERS.items():
        tool = build()
        assert tool.name == name
        func = tool.func
        assert getattr(func, CALLERS_ATTR) == "code"
        assert getattr(func, CONCURRENCY_GROUP_ATTR) == "browser"
    reads = {n for n, b in BUILDERS.items() if getattr(b().func, READ_ONLY_ATTR)}
    assert reads == {
        "browser_pages",
        "browser_snapshot",
        "browser_read",
        "browser_wait",
        "browser_screenshot",
    }


def test_a_target_spec_becomes_playwright_locator_calls() -> None:
    calls: list[str] = []
    page = _Locator(calls)
    locator_for(
        page,
        {
            "frame": [{"by": "css", "css": "iframe#results"}],
            "steps": [
                {"by": "role", "role": "row", "name": "HDF5"},
                {"by": "filter", "has_text": "2026"},
                {"by": "nth", "index": 0},
            ],
        },
    )
    assert calls == [
        "locator('iframe#results')",
        "content_frame",
        "get_by_role('row', exact=False, name='HDF5')",
        "filter(has_text='2026')",
        "nth(0)",
    ]
    calls.clear()
    locator_for(page, {"steps": [{"by": "ref", "ref": "f9e14"}]})
    assert calls == ["get_by_ref('f9e14')"]


@pytest.mark.parametrize(
    "spec",
    [
        None,
        {"steps": []},
        {"steps": [{"by": "xpath", "xpath": "//a"}]},
        {"steps": [{"by": "nth", "index": 1}]},
        {"steps": [{"by": "css", "css": "a"}, {"by": "ref", "ref": "e1"}]},
        {"steps": [{"by": "role"}]},
    ],
)
def test_a_malformed_target_is_a_typed_refusal(spec: Any) -> None:
    with pytest.raises(BrowserError) as refused:
        locator_for(_Locator([]), spec)
    assert refused.value.reason == "browser_target_invalid"


def test_describe_is_a_short_human_form() -> None:
    assert describe({"steps": [{"by": "role", "role": "button", "name": "Save changes"}]}) == (
        'button "Save changes"'
    )
    assert describe({"steps": [{"by": "ref", "ref": "f9e14"}]}) == "ref f9e14"


def test_a_browser_file_declares_its_page_as_a_web_source() -> None:
    page = {"url": "https://arxiv.org/list/cs.DC/new", "host": "arxiv.org"}
    edges = page_source_edges("browser_screenshot", {"page": page, "screenshot": {"path": "x"}})
    [edge] = declare_web_domains(edges)
    assert edge.authority == page["url"]
    assert edge.net_domain == "arxiv.org" and edge.net_mechanism == "tool-declared"
    downloaded = {"page": page, "downloads": [{"path": "/s/report.csv", "name": "report.csv"}]}
    assert len(page_source_edges("browser_act", downloaded)) == 1
    assert page_source_edges("browser_read", {"page": page}) == []  # no file, no edge
    assert page_source_edges("browser_screenshot", {"page": {"url": "about:blank"}}) == []


def test_presenters_handle_failed_and_running_calls() -> None:
    args = {"page": "p2", "action": "click", "target": {"steps": [{"by": "text", "text": "Next"}]}}
    failed = presenter_for("browser_act")(args, None, None)
    assert failed == {"subject": 'click text "Next"', "summary": "", "blocks": []}
    running = starting_for("browser_evaluate")({"page": "p2", "expression": "() => 1"})
    assert running["blocks"][0]["language"] == "javascript"
    blocked = presenter_for("browser_wait")(
        {"page": "p2", "until": "load"},
        None,
        {
            "page": {"id": "p2", "host": "a.example"},
            "navigation_blocked": {"host": "b.example", "reason": "browser_host_not_granted"},
            "person_acted": False,
        },
    )
    assert blocked["status"] == "degraded"
    assert blocked["blocks"][0]["severity"] == "warning"
    pages = presenter_for("browser_pages")({}, None, {"pages": []})
    assert pages["summary"] == "0 pages" and pages["blocks"][0]["text"] == "No pages are open."


def test_a_parent_conversation_sees_its_subagents_pages_only() -> None:
    from types import SimpleNamespace

    from clio_agent.gact.browser.live import _in_family

    sessions = {
        "child": SimpleNamespace(parent_session_id="parent"),
        "grandchild": SimpleNamespace(parent_session_id="child"),
        "parent": SimpleNamespace(parent_session_id=""),
        "stranger": SimpleNamespace(parent_session_id=""),
    }
    app = SimpleNamespace(state=SimpleNamespace(sessions=sessions))
    assert _in_family(app, "parent", "parent")
    assert _in_family(app, "grandchild", "parent")
    assert not _in_family(app, "parent", "child")
    assert not _in_family(app, "stranger", "parent")


def test_the_browser_fragment_reaches_an_agent_only_with_run_code() -> None:
    from pathlib import Path

    from clio_agent.plugins.compose_final import fragment_reaches
    from clio_agent.plugins.loader import load_plugin

    root = Path(__file__).resolve().parents[3] / "external/clio-agent-marketplace/plugins/browser"
    record = load_plugin(root, marketplace="clio", trusted=True)
    [fragment] = [f for f in record.fragments if f.id == "browser.use"]
    assert fragment.when_tools == ("run_code", "browser_open")
    row = {"when_tools": list(fragment.when_tools)}
    tools = {"run_code", "browser_open", "browser_act"}
    assert fragment_reaches(row, tools, delegated=False)
    assert not fragment_reaches(row, tools - {"run_code"}, delegated=False)
    assert [b.name for b in record.kernel_bindings] == ["browser"]


def test_a_screenshot_artifact_is_generated_by_its_call_from_the_page(tmp_path: Any) -> None:
    """W15: the screenshot's lineage reaches the page it was taken of (``web:<host>``)."""
    from fastapi.testclient import TestClient

    from clio_agent.gact.app import build_app
    from clio_agent.gact.artifacts.transforms import observe_tool_transform
    from clio_agent.gact.browser import files
    from clio_agent.gact.tool_observer import _OBSERVER_CALL_IDS

    client = TestClient(build_app(sessions_path=tmp_path / "s.json"))
    workspace = tmp_path / "w"
    workspace.mkdir()
    wid = client.post("/v1/workspaces", json={"name": "w", "root_path": str(workspace)}).json()
    sid = client.post("/v1/sessions", json={"workspace_id": wid["id"]}).json()["id"]
    shot = tmp_path / "scratch" / "p1-1.png"
    shot.parent.mkdir()
    shot.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    page = {"id": "p1", "url": "https://arxiv.org/list/cs.DC/new", "host": "arxiv.org"}
    token = _OBSERVER_CALL_IDS.set("call_shot")
    try:
        minted = files.mint(client.app, sid, shot, image=True, page_url=page["url"])
    finally:
        _OBSERVER_CALL_IDS.reset(token)
    result = {"page": page, "screenshot": {"path": str(shot), **minted}}
    observe_tool_transform(client.app, sid, "browser_screenshot", {}, "call_shot", True, result)
    lineage = client.get(f"/v1/artifacts/{minted['artifact_id']}/lineage").json()
    [root] = [n for n in lineage["nodes"] if n["id"] == minted["artifact_id"]]
    assert root["producer_call_id"] == "call_shot"
    assert "arxiv.org" in str(lineage["nodes"]) and lineage["edges"]
