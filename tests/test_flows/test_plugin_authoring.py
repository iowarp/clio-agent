"""Running a flow whose plugin is not trusted asks the person, unless Clio wrote it under bypass."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.test_flows.conftest import default_program, post, wait_until


def _files(version: str = "0.1.0") -> dict[str, str]:
    return {
        "plugin.json": json.dumps(
            {
                "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
                "name": "tally",
                "version": version,
                "description": "Counts votes.",
                "author": {"name": "Clio"},
                "license": "BSD-3-Clause",
                "extensions": {"ai.iowarp.clio": {"requires": ["delegation"]}},
            }
        ),
        "ai.iowarp.clio/flows/tally.py": (
            "from clio_agent.sdk import Flow\n\n\n"
            "class Tally(Flow):\n"
            '    id = "tally"\n'
            '    description = "Counts votes."\n'
            "    shape = 'railroad-beta\\ntally = terminal(\"count\");'\n\n"
            "    async def forward(self, task, ctx):\n"
            f'        return {{"answer": "counted by {version}"}}\n'
        ),
    }


def _script(client: TestClient) -> dict[str, Any]:
    """Script the conversation's model: make the plugin, or run its flow, on request."""

    from clio_agent.gact.flow_tools import build_run_workflow_tool, start_flow
    from clio_agent.gact.plugin_authoring import make_plugin

    done: dict[str, Any] = {}

    def program(question: str = "", **kwargs: Any) -> Any:
        import dspy

        if question.startswith("make version"):
            version = question.split()[-1]
            done[f"made {version}"] = asyncio.run(make_plugin("tally", _files(version)))
            return dspy.Prediction(answer="made")
        if question.startswith("workflow tally"):
            workflow = {"steps": [{"id": "count", "target": "tally"}]}
            done[question] = asyncio.run(
                build_run_workflow_tool().func(workflow=workflow, task="votes")
            )
            return dspy.Prediction(answer="started")
        if question.startswith("describe run_flow"):
            from clio_agent.gact.flow_tools import _run_flow_description

            done[question] = _run_flow_description()
            return dspy.Prediction(answer="described")
        if question.startswith("run tally"):
            done[question] = asyncio.run(start_flow("tally", "count", None))
            return dspy.Prediction(answer="started")
        return default_program(question=question, **kwargs)

    client.app.state.flow_program["run"] = program
    return done


def _session(client: TestClient, approval_mode: str) -> str:
    response = client.post(
        "/v1/sessions",
        json={
            "title": "author",
            "metadata": {"agent_ref": "clio/orchestrator"},
            "approval_mode": approval_mode,
        },
    )
    assert response.status_code in (200, 201), response.text
    return response.json()["id"]


def _market(client: TestClient) -> dict[str, Any]:
    return client.app.state.plugin_service.store.marketplaces()["tally"]


def _delta(client: TestClient, sid: str) -> list[str]:
    return client.get(f"/v1/sessions/{sid}/agent").json()["delta"]["add"]


def _permission(client: TestClient, status: str = "pending") -> dict[str, Any]:
    def find() -> dict[str, Any] | None:
        rows = client.get("/v1/permissions").json()["permissions"]
        return next(
            (
                row
                for row in rows
                if row["tool_call"]["tool_name"] == "run_flow" and row["status"] == status
            ),
            None,
        )

    return wait_until(find, timeout=60)


def _idle(client: TestClient, sid: str) -> None:
    """Wait for the conversation to finish, as a person waits before writing again (a
    message sent while it works joins the queued turn)."""

    wait_until(lambda: client.get(f"/v1/sessions/{sid}").json()["status"] == "idle", timeout=60)


def _result_read(client: TestClient, sid: str, handle: str) -> None:
    """Wait until a started flow's result has reached the conversation and been read: a
    message written while that result is arriving joins the same turn."""

    def delivered() -> bool:
        rows = client.get(f"/v1/sessions/{sid}/messages").json()["messages"]
        return any(
            part.get("type") == "injection" and handle in str(part.get("text", ""))
            for row in rows
            for part in row["parts"]
        )

    wait_until(delivered, timeout=60)
    _idle(client, sid)


def _made(client: TestClient, sid: str, done: dict[str, Any], version: str) -> dict[str, Any]:
    _idle(client, sid)
    post(client, sid, f"make version {version}")
    return wait_until(lambda: done.get(f"made {version}"), timeout=60)


def test_make_plugin_installs_it_untrusted_and_names_its_flows(flow_app: TestClient) -> None:
    done = _script(flow_app)
    sid = _session(flow_app, "ask")
    made = _made(flow_app, sid, done, "0.1.0")
    assert made["installed"] and made["flows"] == ["tally"], made
    assert not _market(flow_app)["trusted"]
    review = flow_app.get("/v1/plugins/tally/review").json()
    assert review["authored_by_clio"] is True
    assert review["flows"][0] == {
        "id": "tally",
        "description": "Counts votes.",
        "shape": 'railroad-beta\ntally = terminal("count");',
        "file": "ai.iowarp.clio/flows/tally.py",
    }
    assert review["reach"] == []


def test_running_a_flow_clio_wrote_asks_and_allow_trusts_adds_and_runs(
    flow_app: TestClient,
) -> None:
    done = _script(flow_app)
    sid = _session(flow_app, "ask")
    _made(flow_app, sid, done, "0.1.0")
    post(flow_app, sid, "run tally")
    row = _permission(flow_app)
    asked = row["tool_call"]["input"]
    assert asked["flow"] == "tally" and asked["author"] == "Clio" and asked["authored_by_clio"]
    assert asked["file"] == "ai.iowarp.clio/flows/tally.py"
    assert asked["review_url"] == "/v1/plugins/tally/review"
    flow_app.post(f"/v1/permissions/{row['id']}", json={"action": "allow"})
    started = wait_until(lambda: done.get("run tally"), timeout=60)
    assert started["accepted"], started
    assert _market(flow_app)["trust_reason"] == "allowed_to_run"
    assert "tally/tally" in _delta(flow_app, sid)


def test_denying_the_run_leaves_it_untrusted(flow_app: TestClient) -> None:
    done = _script(flow_app)
    sid = _session(flow_app, "ask")
    _made(flow_app, sid, done, "0.1.0")
    post(flow_app, sid, "run tally")
    flow_app.post(f"/v1/permissions/{_permission(flow_app)['id']}", json={"action": "deny"})
    refused = wait_until(lambda: done.get("run tally"), timeout=60)
    assert refused["accepted"] is False and refused["reason"] == "flow_not_trusted"
    assert not _market(flow_app)["trusted"]
    assert "tally/tally" not in _delta(flow_app, sid)


def test_under_bypass_a_flow_clio_wrote_runs_on_the_record(flow_app: TestClient) -> None:
    done = _script(flow_app)
    sid = _session(flow_app, "bypass")
    _made(flow_app, sid, done, "0.1.0")
    post(flow_app, sid, "run tally")
    started = wait_until(lambda: done.get("run tally"), timeout=60)
    assert started["accepted"], started
    assert _market(flow_app)["trust_reason"] == "authored_bypass"
    audit = _permission(flow_app, status="auto_approved")
    assert audit["tool_call"]["input"]["authored_by_clio"] is True


def test_a_third_party_flow_is_asked_about_even_under_bypass(
    flow_app: TestClient, tmp_path: Path
) -> None:
    done = _script(flow_app)
    folder = tmp_path / "tally"
    for path, text in _files().items():
        (folder / path).parent.mkdir(parents=True, exist_ok=True)
        (folder / path).write_text(text)
    flow_app.app.state.plugin_service.store.install(str(folder), name="tally")
    sid = _session(flow_app, "bypass")
    post(flow_app, sid, "run tally")
    row = _permission(flow_app)
    asked = row["tool_call"]["input"]
    assert asked["authored_by_clio"] is False and asked["author"] == "tally"
    flow_app.post(f"/v1/permissions/{row['id']}", json={"action": "deny"})
    assert wait_until(lambda: done.get("run tally"), timeout=60)["accepted"] is False


def test_a_file_changed_after_clio_wrote_it_makes_it_third_party(flow_app: TestClient) -> None:
    from clio_agent.plugins.authored import authored_root

    done = _script(flow_app)
    sid = _session(flow_app, "bypass")
    _made(flow_app, sid, done, "0.1.0")
    store = flow_app.app.state.plugin_service.store
    # Edited where it was written, then reinstalled from there: no longer Clio's files.
    flow = authored_root(store.root) / "tally/ai.iowarp.clio/flows/tally.py"
    flow.write_text(flow.read_text() + "\nimport subprocess\n")
    shutil.rmtree(flow.parent / "__pycache__", ignore_errors=True)
    store.install(str(flow.parents[2]), name="tally")
    post(flow_app, sid, "run tally")
    row = _permission(flow_app)
    assert row["tool_call"]["input"]["authored_by_clio"] is False
    flow_app.post(f"/v1/permissions/{row['id']}", json={"action": "deny"})
    wait_until(lambda: done.get("run tally"), timeout=60)


def test_a_new_version_asks_again_before_it_runs(flow_app: TestClient) -> None:
    """Changed code is asked about again; a change to plugin.json alone is not."""

    done = _script(flow_app)
    sid = _session(flow_app, "ask")
    _made(flow_app, sid, done, "0.1.0")
    post(flow_app, sid, "run tally")
    flow_app.post(f"/v1/permissions/{_permission(flow_app)['id']}", json={"action": "allow"})
    started = wait_until(lambda: done.get("run tally"), timeout=60)
    assert started["accepted"], started
    _result_read(flow_app, sid, started["handle"])
    _made(flow_app, sid, done, "0.2.0")
    _idle(flow_app, sid)
    post(flow_app, sid, "run tally again")
    row = _permission(flow_app)
    assert row["tool_call"]["input"]["version"] == "0.2.0"
    flow_app.post(f"/v1/permissions/{row['id']}", json={"action": "allow"})
    assert wait_until(lambda: done.get("run tally again"), timeout=60)["accepted"]


def test_a_workflow_step_that_runs_an_untrusted_flow_asks_before_the_workflow_starts(
    flow_app: TestClient,
) -> None:
    done = _script(flow_app)
    sid = _session(flow_app, "ask")
    _made(flow_app, sid, done, "0.1.0")
    post(flow_app, sid, "workflow tally")
    row = _permission(flow_app)
    assert row["tool_call"]["input"]["flow"] == "tally"
    flow_app.post(f"/v1/permissions/{row['id']}", json={"action": "allow"})
    started = wait_until(lambda: done.get("workflow tally"), timeout=60)
    assert started["accepted"] and started["flow"] == "workflow", started
    assert _market(flow_app)["trust_reason"] == "allowed_to_run"


def test_denying_a_workflow_steps_flow_refuses_the_workflow_naming_the_step(
    flow_app: TestClient,
) -> None:
    done = _script(flow_app)
    sid = _session(flow_app, "ask")
    _made(flow_app, sid, done, "0.1.0")
    post(flow_app, sid, "workflow tally")
    flow_app.post(f"/v1/permissions/{_permission(flow_app)['id']}", json={"action": "deny"})
    refused = wait_until(lambda: done.get("workflow tally"), timeout=60)
    assert refused["accepted"] is False and refused["reason"] == "flow_not_trusted"
    assert refused["step"] == "count"


def test_a_running_flow_that_calls_an_untrusted_flow_gets_a_typed_refusal(
    flow_app: TestClient,
) -> None:
    """A running flow's agent is fixed, so it cannot ask; it says why, not "unknown"."""

    from clio_agent.flows.outcomes import FlowNotTrusted
    from clio_agent.gact.plugin_authoring import refuse_untrusted_flow

    done = _script(flow_app)
    sid = _session(flow_app, "ask")
    _made(flow_app, sid, done, "0.1.0")
    with pytest.raises(FlowNotTrusted) as raised:
        refuse_untrusted_flow(flow_app.app, "tally")
    assert raised.value.to_dict()["plugin"] == "tally"
    refuse_untrusted_flow(flow_app.app, "nothing-declares-this")


def test_the_model_is_told_which_installed_flows_are_not_trusted_yet(
    flow_app: TestClient,
) -> None:
    """Without this the model cannot see an untrusted flow, so it never asks to run it."""

    done = _script(flow_app)
    sid = _session(flow_app, "ask")
    _made(flow_app, sid, done, "0.1.0")
    _idle(flow_app, sid)
    post(flow_app, sid, "describe run_flow")
    described = wait_until(lambda: done.get("describe run_flow"), timeout=60)
    assert "Installed but not trusted yet" in described
    assert "- tally: Counts votes. (the tally plugin, by Clio)" in described


def test_a_trusted_flow_outside_this_agent_is_added_and_runs_without_asking(
    flow_app: TestClient,
) -> None:
    """Trusted in one conversation, the flow runs in another: its plugin joins that
    conversation's agent, with no second question."""

    done = _script(flow_app)
    first = _session(flow_app, "ask")
    _made(flow_app, first, done, "0.1.0")
    post(flow_app, first, "run tally")
    flow_app.post(f"/v1/permissions/{_permission(flow_app)['id']}", json={"action": "allow"})
    assert wait_until(lambda: done.get("run tally"), timeout=60)["accepted"]
    second = _session(flow_app, "ask")
    assert "tally/tally" not in _delta(flow_app, second)
    post(flow_app, second, "run tally here")
    started = wait_until(lambda: done.get("run tally here"), timeout=60)
    assert started["accepted"], started
    assert "tally/tally" in _delta(flow_app, second)
    pending = [
        row
        for row in flow_app.get("/v1/permissions").json()["permissions"]
        if row["tool_call"]["tool_name"] == "run_flow" and row["status"] == "pending"
    ]
    assert pending == []
