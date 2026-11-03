"""The injection system: sandboxed templates, plugin declarations, events, tool guidance.

SABOTAGE (each turns the named test red):

* ``template._ENV``: use a plain ``jinja2.Environment`` (no sandbox, no
  ``StrictUndefined``) -> ``test_templates_render_ternaries_and_refuse_unknown_names_and_code``.
* ``resolution.fire``: drop the ``tool:`` pattern check ->
  ``test_a_plugin_injection_fires_on_its_event_with_the_session_state``.
* ``resolution._plugin_texts``: re-raise a plugin's template error ->
  ``test_a_broken_plugin_injection_is_left_out_with_a_typed_reason``.
* ``resolution.render``: drop the plugin additions ->
  ``test_a_plugin_adds_to_a_core_event``.
* ``ClioReAct._call``: drop the ``after_tool_result`` line ->
  ``test_tool_guidance_reaches_the_model_in_the_tool_result``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import dspy
import pytest

from clio_agent import injections
from clio_agent.gact.agents.clio_react import ClioReAct
from clio_agent.injections import resolution, template, tool_guidance
from clio_agent.prompts import parse_prompt_text
from tests._scripted_engine import Reply, calls, scripted_lm

SID = "sess_inject"


def _plugin(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "demo"
    folder = root / "ai.iowarp.clio" / "injections"
    folder.mkdir(parents=True)
    for name, text in files.items():
        (folder / name).write_text(text, encoding="utf-8")
    return root


def _app(monkeypatch: pytest.MonkeyPatch, root: Path, metadata: dict[str, Any]) -> Any:
    from clio_agent.gact.agents import resolution as agent_resolution

    session = SimpleNamespace(
        metadata={"agent_resolution": "digest-1", **metadata}, workspace_root="/w"
    )
    plugins = [{"id": "demo", "root": str(root), "text_digest": str(root)}]
    monkeypatch.setattr(
        agent_resolution,
        "pinned_resolution",
        lambda app, digest, ref="": SimpleNamespace(plugins=plugins),
    )
    resolution._CACHE.clear()
    return SimpleNamespace(state=SimpleNamespace(sessions={SID: session}))


def test_templates_render_ternaries_and_refuse_unknown_names_and_code() -> None:
    text = '{{ "late" if steps > 3 else "early" }}{% if plan %} at {{ plan.location }}{% endif %}'
    assert (
        template.render_text(text, {"steps": 5, "plan": {"location": "/p.md"}}) == "late at /p.md"
    )
    for bad, values in (
        ("{{ plan.locaton }}", {"plan": {"location": "/p.md"}}),  # a misspelt attribute
        ("{{ nobody }}", {}),  # a name nobody supplied
        ("{{ ''.__class__.__mro__ }}", {}),  # Python internals
        ("{{ items.append(1) }}", {"items": []}),  # mutation
        ("{% if %}", {}),  # bad syntax
    ):
        with pytest.raises(injections.InjectionTemplateError):
            template.render_text(bad, values, "t")


def test_a_plugin_injection_fires_on_its_event_with_the_session_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _plugin(
        tmp_path,
        {
            "plan-pointer.md": (
                "---\nname: plan-pointer\nevent: turn_start\n---\n"
                '{% if plan.status == "approved" %}Consult the plan at {{ plan.location }}.'
                "{% endif %}"
            ),
            "notebook.md": (
                "---\nname: notebook\nevent: tool_result\ntool: run_*\n---\n"
                "{{ tool.name }} {{ 'failed' if tool.is_error else 'ran' }}; "
                "{{ todos.open }} todos open."
            ),
            "off.md": "---\nname: off\nevent: turn_start\nactive: false\n---\nNever shown.",
        },
    )
    app = _app(
        monkeypatch,
        root,
        {
            "plan_file": str(tmp_path / "plan.md"),
            "plan_status": "approved",
            "todos": [{"content": "a", "status": "pending"}],
        },
    )
    start = injections.fire("turn_start", {}, app=app, session_id=SID)
    assert start == [("demo.plan-pointer", f"Consult the plan at {tmp_path / 'plan.md'}.")]
    tool = {"tool": {"name": "run_code", "args": {}, "is_error": False, "chars": 3}}
    ran = injections.fire("tool_result", tool, app=app, session_id=SID, tool="run_code")
    assert ran == [("demo.notebook", "run_code ran; 1 todos open.")]
    other = {"tool": {"name": "shell_bash", "args": {}, "is_error": False, "chars": 3}}
    assert injections.fire("tool_result", other, app=app, session_id=SID, tool="shell_bash") == []


def test_a_broken_plugin_injection_is_left_out_with_a_typed_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _plugin(
        tmp_path,
        {
            "bad.md": "---\nname: bad\nevent: turn_start\n---\n{{ plan.locaton }}",
            "good.md": "---\nname: good\nevent: turn_start\n---\nStill here.",
        },
    )
    app = _app(monkeypatch, root, {"plan_file": "/p.md", "plan_status": "draft"})
    audited: list[dict[str, Any]] = []
    from clio_agent.runtime import stream_audit as audit_module

    monkeypatch.setattr(
        audit_module, "stream_audit", lambda stage, **kw: audited.append({stage: kw})
    )
    assert injections.fire("turn_start", {}, app=app, session_id=SID) == [
        ("demo.good", "Still here.")
    ]
    assert [list(row) for row in audited] == [[injections.PLUGIN_RENDER_FAILED]]


def test_a_plugin_adds_to_a_core_event(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _plugin(
        tmp_path,
        {"stale.md": "---\nname: stale\nevent: plan_stale\n---\nAlso note {{ steps }} steps."},
    )
    app = _app(monkeypatch, root, {})
    text = injections.render(
        "plan_stale", {"path": "/w/plan.md", "steps": 7}, app=app, session_id=SID
    )
    assert text is not None and "(/w/plan.md)" in text and text.endswith("Also note 7 steps.")


def test_guidance_is_appended_to_text_and_keeps_its_marks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        tool_guidance, "fire", lambda event, variables, tool="": [("demo.g", "Hint.")]
    )

    class Marked(str):
        pass

    marked = Marked("result")
    marked.notification_ids = ("n1",)
    value, notes = tool_guidance.after_tool_result("t", {}, marked, False)
    assert (str(value), notes) == ("result\n\nHint.", [("demo.g", "Hint.")])
    assert isinstance(value, Marked) and value.notification_ids == ("n1",)
    assert tool_guidance.after_tool_result("t", {}, {"x": 1}, False) == ({"x": 1}, [])


@pytest.mark.usefixtures("clio_core_plane")
def test_tool_guidance_reaches_the_model_in_the_tool_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fire(event: str, variables: dict[str, Any], tool: str = "") -> list[tuple[str, str]]:
        return (
            [("demo.g", f"Guidance for {variables['tool']['name']}.")] if tool == "lookup" else []
        )

    monkeypatch.setattr(tool_guidance, "fire", fire)

    def lookup(q: str) -> str:
        """Look something up."""
        return "FOUND"

    lm, engine = scripted_lm([calls(("lookup", {"q": "x"})), Reply(text="done")])
    with dspy.context(lm=lm):
        ClioReAct("question -> answer", tools=[lookup])(question="q")
    [tool_message] = [m for m in engine.requests[1].messages if m.role == "tool"]
    text = "".join(getattr(c, "text", "") for p in tool_message.parts for c in p.content)
    assert text == "FOUND\n\nGuidance for lookup."


def test_the_registry_accepts_jinja_in_injections_and_names_unknown_variables() -> None:
    good = parse_prompt_text(
        "---\nid: clio.reminder.x\nx_kind: reminder\nx_variables: steps\n---\n"
        '{{ "a" if steps else "b" }} {% if plan.exists %}{{ plan.location }}{% endif %}',
        scope="global",
    )
    assert good.validation_errors == []
    bad = parse_prompt_text(
        "---\nid: clio.reminder.y\nx_kind: reminder\nx_variables: steps\n---\n{{ nope }}",
        scope="global",
    )
    assert bad.validation_errors == ["unknown render placeholder: nope"]


def test_plugin_check_reports_bad_events_names_and_templates(tmp_path: Path) -> None:
    """SABOTAGE: drop the variable check in ``injections.check`` (or the
    ``injection_diagnostics`` line in ``plugins.check.check``) and broken
    declarations pass the check."""
    from clio_agent.injections.check import injection_diagnostics
    from clio_agent.plugins.records import PluginRecord

    root = _plugin(
        tmp_path,
        {
            "ok.md": "---\nname: ok\nevent: tool_result\ntool: run_*\n---\n{{ tool.name }}",
            "event.md": "---\nname: event\nevent: nope\n---\nx",
            "names.md": "---\nname: names\nevent: plan_stale\n---\n{{ path }} {{ secret }}",
            "syntax.md": "---\nname: syntax\nevent: turn_start\n---\n{% if %}",
        },
    )
    record = PluginRecord(
        id="demo",
        version="1",
        description="",
        kind="plugin",
        format="agent-plugins",
        root=str(root),
    )
    codes = sorted((d.code, d.message.split(":")[0]) for d in injection_diagnostics(record))
    assert codes == [
        ("injection_event_unknown", "demo.event"),
        ("injection_template_invalid", "demo.syntax"),
        ("injection_variable_unknown", "demo.names"),
    ]
