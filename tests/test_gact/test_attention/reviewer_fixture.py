"""Recorded finding for browser review; never presented as a live SPOTTER verdict."""

from dataclasses import replace

from clio_schemas.attention import DECAYED_MAX
from fastapi import FastAPI

from clio_agent.gact.action_cards import action_card_part
from clio_agent.gact.attention import routes
from clio_agent.gact.attention.content_references import message_content_references
from clio_agent.gact.attention.finding_evidence import finding_attention_action
from clio_agent.gact.attention.lookup import lookup_attention
from clio_agent.gact.parts import Part
from tests.test_gact.test_action_cards import _register_fake_child_task
from tests.test_gact.test_attention._support import _message


def add_reviewer_fixture(app: FastAPI, session_id: str) -> None:
    """Attach an isolated reviewer task and verified capture-backed finding."""
    messages = app.state.messages[session_id]
    task = next(iter(app.state.agent_task_registry.for_parent(session_id)), None)
    if task is None:
        task = _register_fake_child_task(app, parent_sid=session_id, blueprint_id="spotter-ai")
    task = replace(task, run_label="Recorded SPOTTER review", live_state="waiting")
    app.state.agent_task_registry.register(task)
    app.state.sessions.update(task.child_session_id, title="Recorded reviewer (no inference)")
    from clio_schemas.connected_resources import ContentSelection

    raw = next(
        row["reference"]
        for row in message_content_references(messages[-1])
        if row["reference"]["part_id"] == "call_sel"
    )
    ref = ContentSelection.model_validate(
        {**raw, "selection": {"kind": "text", "start": 0, "end": 59}}
    )
    call = routes.session_lm_calls(app, session_id)[0]
    view = lookup_attention(
        session_id=session_id,
        messages=messages,
        calls=[call],
        store=routes.attention_store(app),
        renderer_for=routes._renderer,
        selections=[ref],
        direction="generated_to_source",
        profile=DECAYED_MAX,
    )["views"][0]
    action, uncertainty = finding_attention_action(
        app,
        session_id,
        task.task_id,
        {
            "response_id": call.response_id,
            "steps": view["selected_steps"],
            "profile": view["profile"],
            "profile_revision": view["profile_revision"],
            "capture_sha256": view["capture_sha256"],
            "uncertainty": "Recorded capture only. Attention strength does not establish poisoning.",
        },
    )
    if not action["enabled"]:
        raise ValueError(action["behavior"]["reason"])
    messages[-1].parts.append(
        action_card_part(
            source="spotter-ai",
            severity="info",
            title="Recorded finding: inspect source use",
            body=f"This browser replay tests an exact attention receipt. Uncertainty: {uncertainty}",
            actions=[
                action,
                {
                    "id": "discuss",
                    "label": "Discuss",
                    "enabled": True,
                    "behavior": {"kind": "focus_session", "handle_id": task.task_id},
                },
            ],
        )
    )
    child = _message(
        "recorded-review",
        "recorded-review",
        "assistant",
        [
            Part(
                id="review-text",
                type="text",
                text=(
                    "This is a recorded reviewer conversation for UI qualification. Inspect evidence "
                    "restores the reviewed token steps with the decayed-max profile. A high attention "
                    "score alone is not a poisoning finding. Fresh Delta inference remains required."
                ),
            )
        ],
    )
    child.session_id = task.child_session_id
    app.state.messages[task.child_session_id] = [child]
