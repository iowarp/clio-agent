"""Conversation states and checks for the continuity qualification (continuity_check.py).

Each state is created through the public REST API with plain requests, the way a person
would create it. A state records what the conversation looked like before the transition
(its title, messages and tool results) and the question that only its earlier turns
answer. :func:`verify_case` reopens it after the transition and checks the pass criteria:

* the conversation lists and opens with its title, messages and tool results intact;
* an interrupted turn reads as interrupted with a reason, and nothing stays ``running``;
* a new plain message gets a real answer that uses the earlier context.

The module uses the standard library only, so it drives any CLIO version's server,
including a released one in its own environment.
"""

from __future__ import annotations

import json
import secrets
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Any

BUSY = {"running", "stopping"}
#: How long one turn may take before the check calls it stalled.
TURN_TIMEOUT_S = 300.0


class Api:
    """A thin JSON client for one CLIO server."""

    def __init__(self, base: str, timeout: float = 60.0) -> None:
        self.base = base.rstrip("/")
        self.timeout = timeout

    def call(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.base + path, data=data, method=method)
        request.add_header("content-type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
                return response.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as error:
            raw = error.read()
            try:
                return error.code, json.loads(raw)
            except ValueError:
                return error.code, raw.decode(errors="replace")

    def ok(self, method: str, path: str, body: Any = None) -> Any:
        status, payload = self.call(method, path, body)
        if status >= 300:
            raise RuntimeError(f"{method} {path} -> {status}: {str(payload)[:400]}")
        return payload

    def session(self, sid: str) -> tuple[int, Any]:
        return self.call("GET", f"/v1/sessions/{sid}")

    def messages(self, sid: str) -> list[dict[str, Any]]:
        payload = self.ok("GET", f"/v1/sessions/{sid}/messages")
        rows = payload["messages"] if isinstance(payload, dict) else payload
        return sorted(rows, key=lambda m: (m.get("created_at", ""), m.get("id", "")))

    def create(self, title: str, approval_mode: str = "bypass") -> str:
        body = {"title": title, "approval_mode": approval_mode}
        return str(self.ok("POST", "/v1/sessions", body)["id"])

    def send(self, sid: str, text: str) -> tuple[int, Any]:
        return self.call("POST", f"/v1/sessions/{sid}/messages", {"text": text})


def wait_settled(
    api: Api, sid: str, timeout: float = TURN_TIMEOUT_S, turn: str | None = None
) -> str:
    """Poll until no turn runs (and ``turn``'s reply has ended); its status, or ``timeout``.

    The conversation can still read ``idle`` for a moment after a message was accepted
    as ``started``, so a known turn is followed to its reply's ``stop_reason``.
    """
    deadline = time.monotonic() + timeout
    time.sleep(1.0)
    while time.monotonic() < deadline:
        status, payload = api.session(sid)
        if status == 200 and payload.get("status") not in BUSY:
            if not turn or any(
                m.get("turn_id") == turn and m.get("role") == "assistant" and m.get("stop_reason")
                for m in api.messages(sid)
            ):
                return str(payload.get("status"))
        time.sleep(2.0)
    return "timeout"


def part_summary(part: dict[str, Any]) -> dict[str, Any]:
    """The parts of a message that must survive a transition unchanged."""
    kind = part.get("type")
    out: dict[str, Any] = {"id": part.get("id"), "type": kind}
    if kind in ("tool_call", "tool_result"):
        out["tool"] = str(part.get("tool_name") or "")
        out["call_id"] = part.get("call_id")
    if kind == "tool_result":
        out["content_chars"] = len(json.dumps(part.get("content"))) if part.get("content") else 0
        out["is_error"] = bool(part.get("is_error"))
        outcome = (part.get("metadata") or {}).get("outcome")
        out["outcome"] = outcome if isinstance(outcome, str) else ""
    elif kind == "text":
        out["text_chars"] = len(part.get("text") or "")
    return out


def message_summary(message: dict[str, Any]) -> dict[str, Any]:
    info = message.get("error_info") or {}
    return {
        "id": message.get("id"),
        "role": message.get("role"),
        "stop_reason": message.get("stop_reason", ""),
        "error": info.get("error", "") if isinstance(info, dict) else "",
        "parts": [
            part_summary(p) for p in message.get("parts", []) if p.get("type") != "injection"
        ],
    }


def snapshot(api: Api, sid: str) -> dict[str, Any]:
    status, session = api.session(sid)
    return {
        "http": status,
        "title": session.get("title") if isinstance(session, dict) else None,
        "status": session.get("status") if isinstance(session, dict) else None,
        "messages": [message_summary(m) for m in api.messages(sid)] if status == 200 else [],
    }


def answer_text(message: dict[str, Any]) -> str:
    return "\n".join(
        str(p.get("text") or "") for p in message.get("parts", []) if p.get("type") == "text"
    )


def tool_names(message: dict[str, Any]) -> list[str]:
    return [
        str(p.get("tool_name")) for p in message.get("parts", []) if p.get("type") == "tool_call"
    ]


@dataclass
class Case:
    """One conversation in one state, recorded before the transition."""

    state: str
    sid: str = ""
    title: str = ""
    kind: str = "idle"  # idle | running
    facts: dict[str, str] = field(default_factory=dict)
    question: str = ""
    expect: list[str] = field(default_factory=list)
    before: dict[str, Any] = field(default_factory=dict)
    setup_notes: list[str] = field(default_factory=list)


def _nonce() -> str:
    return secrets.token_hex(2)


def _turn(api: Api, sid: str, text: str, case: Case) -> dict[str, Any]:
    status, ack = api.send(sid, text)
    if status != 202 and status != 200:
        raise RuntimeError(f"setup message refused ({status}): {str(ack)[:300]}")
    settled = wait_settled(api, sid, turn=ack.get("message_id"))
    reply = [m for m in api.messages(sid) if m.get("role") == "assistant"]
    last = reply[-1] if reply else {}
    if settled != "idle" or not answer_text(last):
        raise RuntimeError(
            f"setup turn did not settle with an answer: {settled} {message_summary(last)}"
        )
    case.setup_notes.append(f"turn settled {settled}, tools={tool_names(last)}")
    return last


def _facts_turn(api: Api, case: Case) -> None:
    code, name = f"heron-{_nonce()}", f"Ines {_nonce().upper()}"
    case.facts.update({"codename": code, "lead": name})
    _turn(
        api,
        case.sid,
        f"For this project, the codename is {code} and the lead is {name}. "
        "Acknowledge in one short sentence.",
        case,
    )


def state_idle_answer(api: Api, label: str) -> Case:
    """State 1: idle after a normal answer."""
    case = Case(state="1-idle-answer", title=f"cc {label} idle answer")
    case.sid = api.create(case.title)
    _facts_turn(api, case)
    case.question = "What were the project's codename and lead I gave you earlier? One line."
    case.expect = [case.facts["codename"], case.facts["lead"]]
    return case


def state_idle_tools(api: Api, label: str) -> Case:
    """State 2: idle after a turn with several tool calls, file writes and a large output."""
    case = Case(state="2-idle-tools", title=f"cc {label} idle tools")
    case.sid = api.create(case.title)
    folder = f"cc-{_nonce()}"
    case.facts["folder"] = folder
    _turn(
        api,
        case.sid,
        f"In the workspace, make a folder {folder} with a Python script that prints the "
        "integers 1 to 3000 and their cubes, one pair per line, and a short README.md "
        "explaining it. Run the script and show me its full output, save the output as "
        "cubes.txt in the same folder, then read README.md back to check it, and tell me "
        "the cube of 2718.",
        case,
    )
    case.question = (
        "Which folder did you create for the cubes script, and what is the cube of 2718 "
        "you found? One line."
    )
    case.expect = [folder, "20079290232"]
    return case


def _start_running(api: Api, case: Case, text: str, until: str, wait: float) -> None:
    status, ack = api.send(case.sid, text)
    if status not in (200, 202):
        raise RuntimeError(f"running-state message refused ({status}): {str(ack)[:300]}")
    started = time.monotonic()
    deadline = started + wait
    while time.monotonic() < deadline:
        time.sleep(1.5)
        msgs = api.messages(case.sid)
        last = msgs[-1] if msgs else {}
        if last.get("role") != "assistant":
            continue
        parts = last.get("parts", [])
        if until == "model" and time.monotonic() > started + 15:
            # The ledger shows the answer when it is complete; 15 s into a long answer
            # the model call is still in flight.
            case.setup_notes.append(
                f"model call in flight 15s in; answer chars so far {len(answer_text(last))}, "
                f"stop_reason {last.get('stop_reason')!r}"
            )
            return
        calls = {p.get("call_id") for p in parts if p.get("type") == "tool_call"}
        results = {p.get("call_id") for p in parts if p.get("type") == "tool_result"}
        if until == "tool" and calls - results:
            case.setup_notes.append(f"tool call {sorted(calls - results)} running when cut")
            time.sleep(3.0)
            return
    status, payload = api.session(case.sid)
    case.setup_notes.append(
        f"running condition '{until}' not observed; status {payload.get('status')}"
    )


def state_running_stream(api: Api, label: str) -> Case:
    """State 3a: a turn mid model stream when the transition happens."""
    case = Case(state="3a-running-stream", title=f"cc {label} mid stream", kind="running")
    case.sid = api.create(case.title)
    _facts_turn(api, case)
    keeper = f"Orla {_nonce().upper()}"
    case.facts["keeper"] = keeper
    _start_running(
        api,
        case,
        f"Write a 4000-word short story about a lighthouse keeper named {keeper} who finds "
        "a message in a bottle. No tools; just write it here.",
        "model",
        120,
    )
    case.question = (
        "What were the project's codename, and the lighthouse keeper's name in the story I "
        "asked for? One line; don't continue the story."
    )
    case.expect = [case.facts["codename"], keeper]
    return case


def state_running_tool(api: Api, label: str) -> Case:
    """State 3b: a turn mid tool call (a long shell command) when the transition happens."""
    case = Case(state="3b-running-tool", title=f"cc {label} mid tool", kind="running")
    case.sid = api.create(case.title)
    _facts_turn(api, case)
    marker = f"finished-{_nonce()}"
    case.facts["marker"] = marker
    _start_running(
        api,
        case,
        f"Run `sleep 100; echo {marker}` in the shell and tell me what it printed.",
        "tool",
        150,
    )
    case.question = (
        "What were the project's codename, and the word my shell command was going to "
        "print? One line; don't run anything."
    )
    case.expect = [case.facts["codename"], marker]
    return case


#: Resolvers for states that wait on the person (``Case.kind``), set by continuity_more.
FOLLOWUPS: dict[str, Any] = {}

STATES = {
    "1": state_idle_answer,
    "2": state_idle_tools,
    "3a": state_running_stream,
    "3b": state_running_tool,
}


def _same_parts(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """Differences between a message's parts before and after (ids, types, tool results)."""
    problems: list[str] = []
    after_parts = {p["id"]: p for p in after["parts"]}
    for part in before["parts"]:
        got = after_parts.get(part["id"])
        if got is None:
            problems.append(f"{before['id']}: part {part['id']} ({part['type']}) missing")
        elif part["type"] == "tool_result" and got != part:
            problems.append(f"{before['id']}: tool result {part['id']} changed {part} -> {got}")
        elif part["type"] == "text" and got.get("text_chars", 0) < part.get("text_chars", 0):
            problems.append(f"{before['id']}: text part {part['id']} shrank")
    return problems


def normalise(text: str) -> str:
    return text.replace(",", "").replace(" ", "").replace("_", "").lower()


@dataclass
class Verdict:
    state: str
    sid: str
    passed: bool = False
    checks: dict[str, Any] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    answer: str = ""
    answer_seconds: float = 0.0


def verify_case(api: Api, case: Case) -> Verdict:
    """Reopen one conversation after the transition and check every pass criterion."""
    verdict = Verdict(state=case.state, sid=case.sid)
    status, listed = api.call("GET", "/v1/sessions?limit=200")
    rows = listed.get("sessions", listed) if isinstance(listed, dict) else listed
    ids = {r.get("id") for r in rows} if isinstance(rows, list) else set()
    verdict.checks["listed"] = case.sid in ids
    if case.sid not in ids:
        verdict.problems.append(f"not in the conversation list (HTTP {status})")
    # The interrupted turn must be closed by the restart, not left running.
    after = snapshot(api, case.sid)
    verdict.checks["open_http"] = after["http"]
    verdict.checks["status_on_open"] = after["status"]
    if after["http"] != 200:
        verdict.problems.append(f"open answered HTTP {after['http']}")
        return verdict
    if after["title"] != case.title:
        verdict.problems.append(f"title changed: {case.title!r} -> {after['title']!r}")
    by_id = {m["id"]: m for m in after["messages"]}
    before_msgs = case.before.get("messages", [])
    settled_before = before_msgs[:-1] if case.kind in ("running", "waiting") else before_msgs
    for message in settled_before:
        got = by_id.get(message["id"])
        if got is None:
            verdict.problems.append(f"message {message['id']} ({message['role']}) missing")
            continue
        verdict.problems.extend(_same_parts(message, got))
    if case.kind == "running":
        verdict.checks["interrupted"] = _interruption(after, case, verdict)
    followup = FOLLOWUPS.get(case.kind)
    if followup is not None:
        followup(api, case, verdict)
        after = snapshot(api, case.sid)
    if after["status"] in BUSY:
        # The restart resumed the interrupted work; it must settle on its own.
        settled = wait_settled(api, case.sid)
        verdict.checks["resumed_then"] = settled
        if settled in BUSY or settled == "timeout":
            verdict.problems.append(f"conversation still {settled} after the transition")
            return verdict
    _ask(api, case, verdict)
    verdict.passed = not verdict.problems
    return verdict


def _interruption(after: dict[str, Any], case: Case, verdict: Verdict) -> dict[str, Any]:
    """Find how the turn cut by the transition reads now."""
    last_user = max((i for i, m in enumerate(after["messages"]) if m["role"] == "user"), default=-1)
    tail = after["messages"][last_user + 1 :]
    info = {
        "status": after["status"],
        "tail": [
            {"id": m["id"], "stop_reason": m["stop_reason"], "error": m["error"]} for m in tail
        ],
    }
    reasons = [m["error"] for m in tail if m["error"]]
    finished = any(m["stop_reason"] == "end_turn" for m in tail)
    if not reasons and not finished and after["status"] not in BUSY:
        verdict.problems.append(f"interrupted turn has no reason: {info}")
    info["reasons"] = reasons
    info["finished_after_restart"] = finished
    return info


def _ask(api: Api, case: Case, verdict: Verdict) -> None:
    """Send the context question and check the real answer uses the earlier turns."""
    before_ids = {m["id"] for m in api.messages(case.sid)}
    started = time.monotonic()
    status, ack = api.send(case.sid, case.question)
    verdict.checks["ask_http"] = status
    if status not in (200, 202):
        verdict.problems.append(f"new message refused HTTP {status}: {str(ack)[:300]}")
        return
    turn = ack.get("message_id") if isinstance(ack, dict) else None
    settled = wait_settled(api, case.sid, turn=turn)
    verdict.answer_seconds = round(time.monotonic() - started, 1)
    verdict.checks["ask_settled"] = settled
    verdict.checks["ask_delivery"] = ack.get("delivery") if isinstance(ack, dict) else None
    new = [
        m
        for m in api.messages(case.sid)
        if m["role"] == "assistant" and (m.get("turn_id") == turn or m["id"] not in before_ids)
    ]
    if not new:
        verdict.problems.append(f"no reply recorded ({settled} after {verdict.answer_seconds}s)")
        return
    reply = new[-1]
    info = reply.get("error_info") or {}
    if reply.get("stop_reason") == "error" or info:
        verdict.problems.append(
            f"reply failed after {verdict.answer_seconds}s: {info.get('error')}: {info.get('message')}"
        )
    verdict.answer = answer_text(reply)
    verdict.checks["answer_tools"] = tool_names(reply)
    missing = [e for e in case.expect if normalise(e) not in normalise(verdict.answer)]
    if missing:
        verdict.problems.append(f"answer lacks earlier context {missing}: {verdict.answer[:300]!r}")
    forbid = case.facts.get("forbid")
    if forbid and normalise(forbid) in normalise(verdict.answer):
        verdict.problems.append(f"answer uses removed history {forbid!r}: {verdict.answer[:300]!r}")


def case_record(case: Case) -> dict[str, Any]:
    return asdict(case)


def case_from_record(row: dict[str, Any]) -> Case:
    return Case(**row)


def verdict_record(verdict: Verdict) -> dict[str, Any]:
    return asdict(verdict)
