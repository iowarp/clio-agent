"""The kernel's binding registry and the ``browser`` facade, in a real kernel."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import dspy
import pytest

from clio_agent.gact.agents.tool_settings import apply_tool_settings
from clio_agent.gact.code_mode import kernels
from clio_agent.gact.code_mode import tool as tool_module
from clio_agent.plugins.kernel_bindings import attach
from clio_agent.tools.execution import tool_workspace_context

pytestmark = pytest.mark.timeout(120)  # real kernel processes

FACADE = (
    Path(__file__).resolve().parents[2]
    / "external"
    / "clio-agent-marketplace"
    / "plugins"
    / "browser"
    / "ai.iowarp.clio"
    / "kernel"
    / "browser.py"
)


def _binding(source: str) -> dict[str, str]:
    return {"source": source, "digest": hashlib.sha256(source.encode()).hexdigest()}


class _Recorder:
    """Stand-in browser tools: record each call and answer like the real ones."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def tool(self, name: str) -> Any:
        def call(**kwargs: Any) -> str:
            self.calls.append((name, kwargs))
            page = {
                "id": "p1",
                "url": "https://example.org/list",
                "title": "List",
                "host": "example.org",
            }
            result: dict[str, Any] = {"page": page, "person_acted": name == "browser_read"}
            if name == "browser_read":
                result["value"] = 3 if kwargs.get("what") == "count" else "Dataset 01"
            if name == "browser_snapshot":
                result["snapshot"] = '- link "Next" [ref=f1e2]'
            if name == "browser_screenshot":
                result["screenshot"] = {"path": "/scratch/p1-1.png"}
            if name == "browser_pages":
                return json.dumps({"pages": [page]})
            return json.dumps(result)

        call.__name__ = name
        return dspy.Tool(call, name=name, desc=f"stand-in {name}")


def _agent(recorder: _Recorder, bindings: dict[str, dict[str, str]]) -> Any:
    names = ["browser_open", "browser_pages", "browser_act", "browser_read", "browser_snapshot"]
    names += ["browser_screenshot", "browser_navigate", "browser_wait", "browser_close"]
    tools = {n: recorder.tool(n) for n in names}
    agent = SimpleNamespace(
        tools=tools, _clio_tool_servers=frozenset(), _clio_deferred_catalog=None
    )
    apply_tool_settings(agent, SimpleNamespace(tools=[], mcp_servers={}))
    agent._clio_kernel_bindings = bindings
    return agent


@pytest.fixture
def workspace(tmp_path: Path) -> Iterator[Path]:
    with tool_workspace_context(str(tmp_path)):
        yield tmp_path
    kernels.close_session("kb")


def _run(agent: Any, code: str) -> dict[str, Any]:
    return tool_module.run_script(agent, code, session_id="kb", scope="main")


def test_a_binding_is_bound_once_and_sees_the_current_tools(workspace: Path) -> None:
    recorder = _Recorder()
    source = "class _Greeter:\n    async def go(self):\n        return await tools.call('browser_pages')\n\nexport = _Greeter()\n"
    agent = _agent(recorder, {"greet": _binding(source)})
    outcome = _run(agent, "r = await greet.go()\nprint(r['pages'][0]['id'])")
    assert outcome["status"] == "completed", outcome
    assert outcome["output"].strip() == "p1"
    kernel = kernels._KERNELS[("kb", "main")]
    assert set(kernel.bound_bindings) == {"greet"}
    sent: list[dict[str, Any]] = []
    original = kernel.send
    kernel.send = lambda message: (sent.append(message), original(message))[1]  # type: ignore[method-assign]
    _run(agent, "print(type(greet).__name__)")
    assert [m["type"] for m in sent] == ["exec"]  # the unchanged binding is not re-sent
    agent._clio_kernel_bindings = {}
    outcome = _run(agent, "print('greet' in globals())")
    assert outcome["output"].strip() == "False"  # a removed binding leaves the kernel


def test_a_binding_that_fails_is_reported_and_left_out(workspace: Path) -> None:
    agent = _agent(_Recorder(), {"broken": _binding("raise RuntimeError('nope')\n")})
    outcome = _run(agent, "print('broken' in globals(), 'tools' in globals())")
    assert outcome["output"].strip() == "False True"
    assert "broken" not in kernels._KERNELS[("kb", "main")].bound_bindings


def test_the_browser_facade_builds_targets_and_calls_the_tools(workspace: Path) -> None:
    recorder = _Recorder()
    agent = _agent(recorder, {"browser": _binding(FACADE.read_text())})
    code = (
        "page = await browser.open('https://example.org/list')\n"
        "rows = page.get_by_role('row').filter(has_text='HDF5')\n"
        "n = await rows.count()\n"
        "first = await rows.nth(0).inner_text()\n"
        "await page.get_by_label('Search term').fill('parallel I/O')\n"
        "await page.keyboard.press('Enter')\n"
        "await page.frame_locator('iframe#results').get_by_role('button').click()\n"
        "shot = await page.screenshot(full_page=True)\n"
        "again = await browser.page('p1')\n"
        "print(n, first, page.person_acted, shot, again.id)\n"
        "print('differs from Playwright' in browser.__doc__)\n"
    )
    outcome = _run(agent, code)
    assert outcome["status"] == "completed", outcome
    lines = [line for line in outcome["output"].split("\n") if not line.startswith("[browser]")]
    assert lines[0] == "3 Dataset 01 False /scratch/p1-1.png p1"
    assert lines[1] == "True"
    # The stand-in reports the person acting on every read: the script's output says so.
    assert "[browser] the person used page p1 since your last call on it" in outcome["output"]
    calls = recorder.calls
    assert calls[0] == ("browser_open", {"url": "https://example.org/list", "label": ""})
    rows = {
        "steps": [
            {"by": "role", "role": "row", "exact": False},
            {"by": "filter", "has_text": "HDF5"},
        ]
    }
    assert calls[1] == ("browser_read", {"page": "p1", "target": rows, "what": "count"})
    nth = {"steps": [*rows["steps"], {"by": "nth", "index": 0}]}
    assert calls[2] == ("browser_read", {"page": "p1", "target": nth, "what": "text"})
    assert calls[3][1]["action"] == "fill" and calls[3][1]["value"] == "parallel I/O"
    assert calls[4] == ("browser_act", {"page": "p1", "action": "key", "value": "Enter"})
    framed = calls[5][1]["target"]
    assert framed["frame"] == [{"by": "css", "css": "iframe#results"}]
    assert calls[6] == ("browser_screenshot", {"page": "p1", "full_page": True})
    assert outcome["calls"] == len(calls)


def test_a_binding_needs_run_code_and_every_tool_it_requires() -> None:
    resolution = SimpleNamespace(
        kernel_bindings=[
            {
                "name": "browser",
                "plugin": "browser",
                "requires": ["browser_open", "browser_act"],
                "text": "export = 1",
                "digest": "d1",
            },
            {
                "name": "other",
                "plugin": "x",
                "requires": ["missing_tool"],
                "text": "export = 2",
                "digest": "d2",
            },
        ]
    )
    program = SimpleNamespace(tools={"run_code": 1, "browser_open": 1, "browser_act": 1})
    attach(program, resolution)
    assert set(program._clio_kernel_bindings) == {"browser"}
    denied = SimpleNamespace(
        tools={"run_code": 1, "browser_open": 1, "browser_act": 1}, _clio_blocked={"browser_act"}
    )
    attach(denied, resolution)
    assert denied._clio_kernel_bindings == {}
    no_code = SimpleNamespace(tools={"browser_open": 1, "browser_act": 1})
    attach(no_code, resolution)
    assert no_code._clio_kernel_bindings == {}


def _plugin(root: Path, source: str) -> Path:
    folder = root / "probe"
    (folder / "ai.iowarp.clio" / "kernel").mkdir(parents=True)
    (folder / "ai.iowarp.clio" / "kernel" / "b.py").write_text(source)
    (folder / "plugin.json").write_text(
        json.dumps(
            {
                "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
                "name": "probe",
                "version": "0.1.0",
                "description": "A probe.",
                "extensions": {
                    "ai.iowarp.clio": {
                        "kernel_bindings": {"probe": {"source": "ai.iowarp.clio/kernel/b.py"}}
                    }
                },
            }
        )
    )
    return folder


def test_plugin_check_rejects_a_binding_that_imports_outside_the_standard_library(
    tmp_path: Path,
) -> None:
    from clio_agent.plugins.check import check

    report = check(_plugin(tmp_path, "import json\nimport requests\nexport = 1\n"))
    found = [d for t in report.targets for d in t.diagnostics if d.code == "kernel_binding_import"]
    assert len(found) == 1 and "'requests'" in found[0].message and ":2 " in found[0].message
    clean = check(_plugin(tmp_path / "ok", "import json\nexport = json\n"))
    assert not [
        d for t in clean.targets for d in t.diagnostics if d.code.startswith("kernel_binding")
    ]


def test_a_malformed_kernel_binding_is_a_manifest_error(tmp_path: Path) -> None:
    from clio_agent.plugins.kernel_bindings import check_shape
    from clio_agent.plugins.manifest import ManifestError

    with pytest.raises(ManifestError, match="under ai.iowarp.clio/"):
        check_shape({"browser": {"source": "kernel/browser.py"}}, "p")
    with pytest.raises(ManifestError, match="reserved"):
        check_shape({"tools": {"source": "ai.iowarp.clio/k.py"}}, "p")
