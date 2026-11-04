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
