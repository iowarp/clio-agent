"""More conversation states for the continuity grid: waiting, children, compaction, history.

States (each registered in ``continuity_states.STATES``):

* ``3s`` stopped by the person mid shell call (a call left with no result);
* ``4`` waiting on a permission question in ask mode;
* ``5`` waiting on the agent's own question (``ask_user``; the setup prompt names it);
* ``7`` a background child task still running (``spawn_agent_task``; named in setup);
* ``8`` a conversation that was compacted;
* ``9r`` a conversation that was rewound; ``9f`` one that was forked.

A waiting state is resolved after the transition by :func:`resolve_pending`: the pending
question is answered if it survived, and if it did not, the turn must read as ended with
a reason. Prompts that name a tool are setup only (the plain-request rule): they put the
conversation in a state, they are not evidence of the agent's judgement.
"""

from __future__ import annotations

import time
from typing import Any

import continuity_states as cs


def pending_interactions(api: cs.Api, sid: str) -> list[dict[str, Any]]:
    status, payload = api.call("GET", f"/v1/sessions/{sid}/interactions")
    if status != 200 or not isinstance(payload, dict):
        return []
    return [row for row in payload.get("interactions", []) if row.get("status") == "pending"]


def _wait_pending(api: cs.Api, case: cs.Case, kind: str, timeout: float = 150.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = [r for r in pending_interactions(api, case.sid) if r.get("kind") == kind]
        if rows:
            case.facts["interaction"] = str(rows[0].get("id"))
            case.setup_notes.append(f"{kind} pending: {rows[0].get('id')} {rows[0].get('title')}")
            return
        time.sleep(2.0)
    status, session = api.session(case.sid)
    raise RuntimeError(f"no pending {kind} within {timeout:.0f}s; status {session.get('status')}")


def state_permission(api: cs.Api, label: str) -> cs.Case:
    """State 4: a turn waiting on a permission question in ask mode."""
    case = cs.Case(state="4-permission", title=f"cc {label} permission", kind="waiting")
    case.sid = api.create(case.title, approval_mode="ask")
    cs._facts_turn(api, case)
    name = f"todo-{cs._nonce()}.txt"
    case.facts.update({"file": name, "followup": "allow"})
    status, ack = api.send(
        case.sid, f"Create a file named {name} in the workspace containing the line 'buy milk'."
    )
    if status not in (200, 202):
        raise RuntimeError(f"permission-state message refused ({status}): {ack}")
    _wait_pending(api, case, "permission")
    case.question = (
        "What is the project codename, and which file did I ask you to create? One line."
    )
    case.expect = [case.facts["codename"], name]
    return case


def state_question(api: cs.Api, label: str) -> cs.Case:
    """State 5: a turn waiting on the agent's own question card."""
    case = cs.Case(state="5-question", title=f"cc {label} question", kind="waiting")
    case.sid = api.create(case.title)
    cs._facts_turn(api, case)
    boat = f"canoe {cs._nonce()}"
    case.facts.update({"followup": "answer", "answer": f"It is a red {boat}."})
    status, ack = api.send(
        case.sid,
        "Help me name my boat. Before suggesting anything, use ask_user to ask me what kind "
        "of boat it is, then suggest three names.",
    )
    if status not in (200, 202):
        raise RuntimeError(f"question-state message refused ({status}): {ack}")
    _wait_pending(api, case, "question")
    case.question = (
        "What is the project codename, and what kind of boat did I say I have? One line."
    )
    case.expect = [case.facts["codename"], "canoe"]
    return case


def state_child(api: cs.Api, label: str) -> cs.Case:
    """State 7: a background child task still running when the transition happens."""
    case = cs.Case(state="7-child", title=f"cc {label} child task", kind="child")
    case.sid = api.create(case.title)
    cs._facts_turn(api, case)
    topic = f"tide pools of cove {cs._nonce().upper()}"
    case.facts["topic"] = topic
    cs._turn(
        api,
        case.sid,
        "Start a background subagent with spawn_agent_task to write a 2500-word essay in five "
        f"sections about the {topic}. Don't wait for it; reply with one sentence once it has "
        "started.",
        case,
    )
    children = [row for row in _sessions(api) if row.get("parent_session_id") == case.sid]
    case.facts["children"] = ",".join(str(c.get("id")) for c in children)
    case.setup_notes.append(
        "children " + ", ".join(f"{c.get('id')}={c.get('status')}" for c in children)
    )
    case.question = (
        "What is the project codename, and what was the subagent's essay about? If it has "
        "finished, give me its first sentence. One short paragraph."
    )
    case.expect = [case.facts["codename"], topic.split()[-1]]
    return case


def _sessions(api: cs.Api) -> list[dict[str, Any]]:
    status, payload = api.call("GET", "/v1/sessions?limit=200")
    rows = payload.get("sessions", []) if isinstance(payload, dict) else []
    return rows if isinstance(rows, list) else []


def state_compacted(api: cs.Api, label: str) -> cs.Case:
    """State 8: a conversation whose context was compacted before the transition."""
    case = cs.Case(state="8-compacted", title=f"cc {label} compacted")
    case.sid = api.create(case.title)
    cs._facts_turn(api, case)
    budget = f"{int(cs._nonce(), 16) % 9000 + 1000} euros"
    case.facts["budget"] = budget
    cs._turn(api, case.sid, f"The project budget is {budget}. Acknowledge briefly.", case)
    cs._turn(api, case.sid, "Suggest a one-line motto for the project.", case)
    result = api.ok("POST", f"/v1/sessions/{case.sid}/compact", {})
    case.setup_notes.append(f"compacted: {str(result)[:200]}")
    case.question = "What are the project's codename and budget? One line."
    case.expect = [case.facts["codename"], budget]
    return case


def state_rewound(api: cs.Api, label: str) -> cs.Case:
    """State 9r: a conversation rewound past a turn that changed a fact."""
    case = cs.Case(state="9r-rewound", title=f"cc {label} rewound")
    case.sid = api.create(case.title)
    cs._facts_turn(api, case)
    keep = [m for m in api.messages(case.sid) if m["role"] == "assistant"][-1]["id"]
    later = f"falcon-{cs._nonce()}"
    cs._turn(api, case.sid, f"Change of plan: the codename is now {later}. Acknowledge.", case)
    result = api.ok("POST", f"/v1/sessions/{case.sid}/rewind", {"message_id": keep})
    case.setup_notes.append(f"rewound to {keep}: {str(result)[:160]}")
    case.question = "What is the project codename? One line."
    case.expect = [case.facts["codename"]]
    case.facts["forbid"] = later
    return case


def state_forked(api: cs.Api, label: str) -> cs.Case:
    """State 9f: a branch forked from a conversation after its first answer."""
    case = cs.Case(state="9f-forked", title="")
    parent = api.create(f"cc {label} fork parent")
    case.facts["parent"] = parent
    case.sid = parent
    cs._facts_turn(api, case)
    keep = [m for m in api.messages(parent) if m["role"] == "assistant"][-1]["id"]
    later = f"falcon-{cs._nonce()}"
    cs._turn(api, parent, f"Change of plan: the codename is now {later}. Acknowledge.", case)
    branch = api.ok("POST", f"/v1/sessions/{parent}/fork", {"at_message_id": keep})
    case.sid = str(branch["id"])
    case.title = str(branch.get("title") or "")
    case.setup_notes.append(f"forked {parent} at {keep} -> {case.sid}")
    case.question = "What is the project codename? One line."
    case.expect = [case.facts["codename"]]
    case.facts["forbid"] = later
    return case


def state_stopped_tool(api: cs.Api, label: str) -> cs.Case:
    """State 3s: the person pressed Stop during a long shell call, then the transition.

    A call left with no recorded result is the case a stalled turn leaves too.
    """
    case = cs.Case(state="3s-stopped-tool", title=f"cc {label} stopped tool")
    case.sid = api.create(case.title)
    cs._facts_turn(api, case)
    marker = f"finished-{cs._nonce()}"
    case.facts["marker"] = marker
    cs._start_running(
        api,
        case,
        f"Run `sleep 100; echo {marker}` in the shell and tell me what it printed.",
        "tool",
        150,
    )
    status, _ = api.call("POST", f"/v1/sessions/{case.sid}/cancel", {})
    settled = cs.wait_settled(api, case.sid, 120)
    case.setup_notes.append(f"Stop answered {status}; then {settled}")
    case.question = (
        "What is the project codename, and what word was my shell command going to print? "
        "One line; don't run anything."
    )
    case.expect = [case.facts["codename"], marker]
    return case


def resolve_pending(api: cs.Api, case: cs.Case, verdict: cs.Verdict) -> None:
    """After the transition: answer the waiting question, or see it ended with a reason."""
    rows = pending_interactions(api, case.sid)
    verdict.checks["pending_after"] = [f"{r.get('kind')}:{r.get('id')}" for r in rows]
    if not rows:
        last = [m for m in api.messages(case.sid) if m["role"] == "assistant"]
        tail = cs.message_summary(last[-1]) if last else {}
        verdict.checks["pending_gone"] = {k: tail.get(k) for k in ("stop_reason", "error")}
        if not tail.get("error") and tail.get("stop_reason") != "end_turn":
            verdict.problems.append(f"the waiting question vanished without a reason: {tail}")
        return
    # Answer as the person would until the turn settles: the resumed turn may ask again
    # (a write, then a shell check of what it wrote), each a new question.
    answered: list[str] = []
    for _ in range(6):
        row = rows[0]
        if row.get("kind") == "permission":
            body = {"action": "allow"}
        else:
            body = {"action": "answer", "answer": case.facts.get("answer", "")}
        status, payload = api.call(
            "POST", f"/v1/sessions/{case.sid}/interactions/{row.get('id')}/respond", body
        )
        answered.append(f"{row.get('kind')}:{status}")
        if status >= 300:
            verdict.problems.append(f"answering the surviving question failed {status}: {payload}")
            return
        deadline = time.monotonic() + cs.TURN_TIMEOUT_S
        while time.monotonic() < deadline:
            time.sleep(2.0)
            rows = pending_interactions(api, case.sid)
            status, session = api.session(case.sid)
            if rows or session.get("status") not in cs.BUSY:
                break
        if not rows:
            break
    verdict.checks["answered"] = answered
    settled = cs.wait_settled(api, case.sid)
    verdict.checks["after_answer"] = settled
    last = [m for m in api.messages(case.sid) if m["role"] == "assistant"][-1]
    info = last.get("error_info") or {}
    if info or settled != "idle":
        verdict.problems.append(f"the answered turn did not finish: {settled} {info}")


def check_children(api: cs.Api, case: cs.Case, verdict: cs.Verdict) -> None:
    """A child running at the transition resumes and finishes; it is not lost."""
    outcomes = {}
    for child in filter(None, case.facts.get("children", "").split(",")):
        settled = cs.wait_settled(api, child)
        replies = [m for m in api.messages(child) if m["role"] == "assistant"]
        last = cs.message_summary(replies[-1]) if replies else {}
        outcomes[child] = {"status": settled, **{k: last.get(k) for k in ("stop_reason", "error")}}
        if last.get("stop_reason") != "end_turn":
            verdict.problems.append(f"child {child} did not finish after the transition: {last}")
    verdict.checks["children"] = outcomes


cs.STATES.update(
    {
        "3s": state_stopped_tool,
        "4": state_permission,
        "5": state_question,
        "7": state_child,
        "8": state_compacted,
        "9r": state_rewound,
        "9f": state_forked,
    }
)
cs.FOLLOWUPS["waiting"] = resolve_pending
cs.FOLLOWUPS["child"] = check_children
