"""The permission gate's question for a command to run outside CLIO's sandbox.

A ``shell_bash`` call with ``outside_sandbox`` (:mod:`clio_agent.runtime.sandbox_outside`)
is decided here, first in :func:`permission_gate._make_permission_gate`:
only a person allows it, for exactly that command, whatever the approval mode or the AI
reviewer would do. A question that names its own answers (allow once or deny, never a
standing rule) offers only those (:func:`answer_fields`).

The gate's own helpers stay in :mod:`clio_agent.gact.permission_gate` and are reached
through that module at call time, so this owner module adds no import cycle.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from clio_agent.gact.code_mode import grants as script_grants
from clio_agent.gact.runtime import tool_permission_layers as layers
from clio_agent.gact.runtime.permission_actions import STANDARD_ANSWERS
from clio_agent.runtime import sandbox_outside

if TYPE_CHECKING:
    from fastapi import FastAPI


def answer_fields(context: Mapping[str, Any] | None) -> dict[str, Any]:
    """The answers a pending permission row offers, and a run_code script's grant.

    A call from inside a run_code script shows the script and offers its grant. A
    question that names its own answers (a command outside the sandbox: allow once or
    deny, never a standing rule) offers only those.
    """
    answers = list((context or {}).get("answers") or STANDARD_ANSWERS)
    fields: dict[str, Any] = {}
    if answers != list(STANDARD_ANSWERS):
        fields["actions"] = answers
    fields.update(script_grants.row_fields(answers))
    return fields


def answered_or_outside(
    app: "FastAPI", name: str, args: Mapping[str, Any], context: Mapping[str, Any] | None
) -> str | None:
    """The gate's first decision; ``None`` passes the call on to its other layers.

    A call a person already approved (``native_gate``) is allowed, and one that asks to
    run outside the sandbox records the allow for its command. Any other call that asks to
    run outside the sandbox is put to the person (:func:`ask_outside_sandbox`).
    """
    outside = sandbox_outside.requested(name, args)
    if layers.answered(name):  # a person approved this very call (``native_gate``)
        if outside:
            sandbox_outside.approve(str(args.get("command") or ""))
        return "allow"
    if outside:
        return ask_outside_sandbox(app, name, args, context)
    return None


def ask_outside_sandbox(
    app: "FastAPI", name: str, args: Mapping[str, Any], context: Mapping[str, Any] | None
) -> str:
    """A command to run outside the sandbox: only a person allows it, for that command.

    A deny from a rule or the mode (plan, read-only) still wins. Otherwise the person is
    always asked, whatever the approval mode; the AI reviewer never decides it (an explicit
    ``ask``). An allow is recorded for exactly this command, which the shell uses once.
    """
    from clio_agent.gact import permission_gate as gate  # noqa: PLC0415 - avoid import cycle

    sid, current = gate._resolve_tool_session(app)
    mode = gate.policy_mode(current)
    policy_action, plan_deny_message = gate._policy_detail_for_tool(
        app, session_id=sid, session=current, tool_name=name, args=args, mode=mode
    )
    subject = gate.action_subject(name, args)
    if policy_action == "deny":
        layered = layers.Layered("deny", "rule", "outside_sandbox")
        return gate._layered_deny(app, sid, name, args, subject, layered, mode, plan_deny_message)
    card = {
        **(context or {}),
        "kind": "native",
        "summary": sandbox_outside.card_summary(args),
        "answers": ["allow", "deny"],
    }
    decision = gate._ask_person(
        app,
        sid,
        name,
        args,
        card,
        subject,
        gate.script_grants.approval_mode(current),
        "ask",
        gate.question_cancel_token(app, sid),
    )
    if decision == "allow":
        sandbox_outside.approve(str(args.get("command") or ""))
    return decision
