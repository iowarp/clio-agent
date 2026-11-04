"""A ``run_code`` script drives the fixture site through the real tools and the facade."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.agents.tool_settings import apply_tool_settings
from clio_agent.gact.browser.tools import BUILDERS
from clio_agent.gact.code_mode import kernels
from clio_agent.gact.code_mode import tool as tool_module
from clio_agent.tools.execution import tool_workspace_context

pytestmark = pytest.mark.timeout(150)  # a real kernel and a real Chromium

FACADE = (
    Path(__file__).resolve().parents[3]
    / "external/clio-agent-marketplace/plugins/browser/ai.iowarp.clio/kernel/browser.py"
)


@pytest.fixture()
def session(browser: Any, tmp_path: Path) -> Iterator[tuple[Any, str]]:
    from clio_agent.gact import context
    from clio_agent.gact.app import build_app

    app = build_app(sessions_path=tmp_path / "sessions.json", arc=None)
    sid = TestClient(app).post("/v1/sessions", json={"title": "Table survey"}).json()["id"]
    tokens = [context.set_app(app), context.set_session_id(sid)]
    workspace = tmp_path / "ws"
    workspace.mkdir()
    with tool_workspace_context(str(workspace)):
        yield app, sid
    for token in reversed(tokens):
        context.reset(token)
    kernels.close_session(sid)


def _agent() -> Any:
    tools = {name: build() for name, build in BUILDERS.items()}
    tools["run_code"] = SimpleNamespace(name="run_code")  # present: bindings need run_code
    agent = SimpleNamespace(
        tools=tools, _clio_tool_servers=frozenset(), _clio_deferred_catalog=None
    )
    apply_tool_settings(agent, SimpleNamespace(tools=[], mcp_servers={}))
    del tools["run_code"]
    text = FACADE.read_text()
    agent._clio_kernel_bindings = {
        "browser": {"source": text, "digest": hashlib.sha256(text.encode()).hexdigest()}
    }
    return agent


def test_a_script_pages_through_the_fixture_table(
    session: tuple[Any, str], site: dict[str, Any], browser: Any
) -> None:
    _app, sid = session
    code = f"""
page = await browser.open({site["a"] + "/table"!r})
names = []
while True:
    rows = page.get_by_role("row").filter(has_text="Dataset")
    for i in range(await rows.count()):
        names.append((await rows.nth(i).inner_text()).split()[1])
    more = page.get_by_role("link", name="Next")
    if not await more.count():
        break
    await more.click()
print(len(names), names[0], names[-1], page.url.endswith("page=3"))
"""
    outcome = tool_module.run_script(_agent(), code, session_id=sid, scope="main")
    assert outcome["status"] == "completed", outcome
    assert outcome["output"].strip() == "23 01 23 True"
    assert outcome["failed_calls"] == 0 and outcome["calls"] > 20
    [page] = browser.session_pages(sid)
    later = tool_module.run_script(
        _agent(),
        f"p = await browser.page({page.id!r})\nprint(p.id, (await browser.pages())[0].host)",
        session_id=sid,
        scope="main",
    )
    assert later["output"].strip() == f"{page.id} a.fixture.localhost"


def test_a_tool_error_carries_its_typed_reason_into_the_script(
    session: tuple[Any, str], site: dict[str, Any]
) -> None:
    _app, sid = session
    code = f"""
page = await browser.open({site["a"] + "/signin"!r})
try:
    await page.get_by_label("Password").fill("hunter2")
except ToolError as error:
    print("refused:", str(error).split(":")[1].strip())
try:
    await page.get_by_role("textbox").click()
except ToolError as error:
    print("ambiguous:", "resolved to 2 elements" in str(error))
"""
    outcome = tool_module.run_script(_agent(), code, session_id=sid, scope="main")
    assert outcome["status"] == "completed", outcome
    assert outcome["output"].split("\n")[:2] == [
        "refused: browser_password_field",
        "ambiguous: True",
    ]
