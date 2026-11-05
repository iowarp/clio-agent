"""Comments on browser pages: the picker inside the browser service, and what the agent reads.

Runs against a real Chromium through the shared ``browser`` fixture, with the host gate
attached as in production (comments plan, Phase 5; ledger FB17).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from clio_agent.gact.browser import picker
from clio_agent.gact.browser.comments import render_page_comments
from clio_agent.gact.documents.comment_attachments import AttachmentMark, CommentAttachment
from clio_agent.gact.documents.models import DocumentAnchor

pytestmark = pytest.mark.timeout(120)  # each test launches a real Chromium


def _open(browser: Any, site: dict[str, Any], path: str) -> Any:
    record = browser.wait(browser.open("sess_a", "Survey"))
    browser.run(record, "goto", lambda r: r.page.goto(site["a"] + path))
    return record


def _wait_for(items: list[Any], count: int) -> None:
    deadline = time.time() + 5
    while len(items) < count and time.time() < deadline:
        time.sleep(0.05)


def test_a_pick_is_reported_only_while_one_is_active(browser: Any, site: dict[str, Any]) -> None:
    record = _open(browser, site, "/form")
    received: list[dict[str, Any]] = []

    async def forge(r: Any) -> None:
        await r.page.evaluate(
            "() => window.__clioCommentPick({kind: 'element', selector: 'body', tag: 'body',"
            " text: '', html: '', rect: [0, 0, 1, 1], viewport: [1280, 800]})"
        )

    browser.run(record, "evaluate", forge)  # no pick active: the page's own call is ignored
    assert browser.wait(
        picker.handle(record, {"type": "pick.start", "mode": "element"}, received.append)
    )

    async def click(r: Any) -> None:
        box = await r.page.locator("button").first.bounding_box()
        await r.page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)

    browser.run(record, "click", click)
    _wait_for(received, 1)
    assert browser.wait(picker.handle(record, {"type": "pick.stop"}, received.append))
    assert len(received) == 1
    anchor = received[0]["anchor"]
    assert received[0]["type"] == "pick"
    assert anchor["profile"] == "dom" and anchor["tag"] == "button"
    assert anchor["text_excerpt"] == "Submit"
    assert anchor["source"]["page"]["id"] == record.id
    # The pick did not submit the form: the click was the picker's, not the page's.
    assert record.url().endswith("/form")


def test_a_page_comment_names_the_element_by_role_with_its_crop(
    browser: Any, site: dict[str, Any], tmp_path: Path
) -> None:
    record = _open(browser, site, "/form")
    page = {"id": record.id, "url": record.url(), "title": record.title}
    attachment = CommentAttachment(
        kind="browser-page",
        marks=[
            AttachmentMark(
                label="S1",
                anchor=DocumentAnchor(
                    profile="dom", selector="button", tag="button", source={"page": page}
                ),
                comment="Why is this disabled?",
            ),
            AttachmentMark(
                label="S2",
                anchor=DocumentAnchor(
                    profile="region", rect=[0.0, 0.0, 0.5, 0.3], source={"page": page}
                ),
                comment="Too crowded.",
            ),
        ],
    )
    rendered = render_page_comments(None, "sess_a", attachment, tmp_path, True)  # type: ignore[arg-type]
    assert rendered.text.startswith(f"Comment on page {record.id}, {record.url()}")
    assert "Image 1 is the page with S1 and S2 drawn on it." in rendered.text
    assert "S1 (element, attached as image 2)" in rendered.text
    assert '  Element:  <button>, role button "Submit"' in rendered.text
    assert '{"steps": [{"by": "role", "role": "button", "name": "Submit"}]}' in rendered.text
    assert "S2 (region, attached as image 3)" in rendered.text
    assert "Submit a dataset" in rendered.text  # the text under the region
    assert [caption for caption, _png in rendered.images] == [
        "the page with S1 and S2 drawn on it",
        "S1 at twice its size",
        "S2 at twice its size",
    ]


def test_a_comment_on_a_closed_page_says_so(browser: Any, tmp_path: Path) -> None:
    attachment = CommentAttachment(
        kind="browser-page",
        marks=[
            AttachmentMark(
                label="S1",
                anchor=DocumentAnchor(
                    profile="text-quote", exact="hello", source={"page": {"id": "p404"}}
                ),
            )
        ],
    )
    rendered = render_page_comments(None, "sess_a", attachment, tmp_path, True)  # type: ignore[arg-type]
    assert "page_unavailable: browser_page_not_found" in rendered.text
    assert '  Selected: "hello"' in rendered.text
    assert rendered.images == []


def test_stopping_a_pick_gives_the_page_its_clicks_back(browser: Any, site: dict[str, Any]) -> None:
    record = _open(browser, site, "/form")
    received: list[dict[str, Any]] = []
    assert browser.wait(
        picker.handle(record, {"type": "pick.start", "mode": "element"}, received.append)
    )
    browser.wait(picker.stop(record))  # what the live view does when its controller leaves

    async def click(r: Any) -> bool:
        await r.page.evaluate(
            "() => document.querySelector('button').addEventListener('click', (e) => {"
            " e.preventDefault(); window.__clicked = true; })"
        )
        await r.page.locator("button").first.click()
        return bool(await r.page.evaluate("() => window.__clicked === true"))

    assert browser.run(record, "click", click)  # the page's own handler saw the click
    assert received == []
