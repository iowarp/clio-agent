"""Late results must not reduce a child research packet to its last addendum."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import dspy
import pytest
from fastapi.testclient import TestClient

from clio_agent.gact import context
from clio_agent.gact.agents import clio_react
from clio_agent.gact.agents.clio_react import ClioReAct
from clio_agent.gact.flow_tools import start_flow
from clio_agent.gact.task_publication import publish_task_notice
from tests._scripted_engine import Reply, scripted_lm
from tests.test_flows.conftest import default_program, new_session, post, wait_until
from tests.test_gact.task_records import record_delegation_fixture


def test_workflow_receives_the_packet_after_a_late_result_and_addendum(
    flow_app: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    packet = "Primary source: measurement 17, uncertainty 2; retain this evidence.\n" * 280
    addendum = "Provenance confirmed."
    started: dict[str, Any] = {}
    reviewed: list[str] = []
    delivered = False
    original = clio_react._call_lm

    def call(lm: Any, request: Any) -> Any:
        nonlocal delivered
        result = original(lm, request)
        if not delivered:
            delivered = True
            app, sid = context.active_app(), context.active_session_id()
            child = record_delegation_fixture(
                app,
                sid,
                handle="task_late_source",
                completed=True,
                result={"answer": "source checked"},
            )
            publish_task_notice(app.state.task_history, child)
        return result

    monkeypatch.setattr(clio_react, "_call_lm", call)

    def program(question: str = "", **kwargs: Any) -> Any:
        if question == "Investigate the source" and not started:
            started.update(
                asyncio.run(
                    start_flow(
                        "workflow",
                        {
                            "task": "A defensible finding",
                            "workflow": {
                                "steps": [
                                    {
                                        "id": "research",
                                        "target": "self",
                                        "input": "Produce evidence",
                                    },
                                    {"id": "review", "target": "self", "after": ["research"]},
                                ]
                            },
                        },
                        None,
                    )
                )
            )
            return dspy.Prediction(answer="Research started")
        if question == "Produce evidence":
            lm, _ = scripted_lm(
                [Reply(text=packet, thinking="private reasoning"), Reply(text=addendum)]
            )
            scope = context.set_react_scope("orchestrator")
            session = context.set_react_session(context.active_session_id())
            kind = context.set_react_kind("react")
            try:
                with dspy.context(lm=lm):
                    return ClioReAct("question -> answer", tools=[])(question=question)
            finally:
                for token in (kind, session, scope):
                    context.reset(token)
        if '"inputs"' in question:
            reviewed.append(question)
            return dspy.Prediction(answer="Review completed")
        return default_program(question=question, **kwargs)

    flow_app.app.state.flow_program["run"] = program
    sid = new_session(flow_app, "clio/orchestrator")
    post(flow_app, sid, "Investigate the source")
    wait_until(lambda: reviewed, timeout=90)
    assert delivered
    assert json.dumps(packet)[1:-1] in reviewed[0]
    assert addendum in reviewed[0]
    assert "private reasoning" not in reviewed[0]
    history = flow_app.app.state.task_history
    task = wait_until(
        lambda: history.task_snapshot(started["handle"])
        if history.task_snapshot(started["handle"]).status == "completed"
        else None,
        timeout=90,
    )
    result = history.read_content(task.result)
    assert packet in result["answer"] and addendum in result["answer"]
