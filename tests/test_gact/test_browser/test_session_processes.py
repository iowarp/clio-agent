"""Deleting a session stops its kernels and closes its pages (browser plan Phase 1 item 2)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.code_mode import kernels

pytestmark = pytest.mark.timeout(120)  # a real kernel and a real Chromium


def test_deleting_a_session_leaves_neither_its_kernel_nor_its_page(
    browser: Any, site: dict[str, Any], tmp_path: Path
) -> None:
    from clio_agent.gact.app import build_app

    client = TestClient(build_app(sessions_path=tmp_path / "sessions.json", arc=None))
    sid = client.post("/v1/sessions", json={"title": "browsing"}).json()["id"]
    kernel = kernels.kernel_for(sid, "main", tmp_path)
    page = browser.wait(browser.open(sid, "browsing"))
    other = browser.wait(browser.open("sess_other", ""))
    browser.run(page, "goto", lambda r: r.page.goto(site["a"] + "/"))
    assert kernel.alive() and not page.closed

    assert client.delete(f"/v1/sessions/{sid}").status_code == 204

    assert not kernel.alive() and (sid, "main") not in kernels._KERNELS
    assert page.closed and page.id not in browser.pages
    assert not other.closed  # another conversation's page is untouched


def test_a_settled_child_closes_its_pages(browser: Any) -> None:
    from clio_agent.gact.session_processes import close_child_pages

    child = browser.wait(browser.open("sess_child", "explore: survey"))
    parent = browser.wait(browser.open("sess_parent", ""))
    close_child_pages("sess_child")
    assert child.closed and not parent.closed
