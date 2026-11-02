"""Phase 7 qualification: the T scenarios of ``docs/design/sdk/tool-calling-plan.md``.

``uv run --frozen --no-sync python -m scripts.eval.tool_calling.qualify run --ids T2,T8a``
``uv run --frozen --no-sync python -m scripts.eval.tool_calling.qualify check``

``run`` drives each live scenario against a running stack: it creates a workspace
and a session (approval mode, plan mode and permission policies as the scenario
needs), sends its prompts, answers permission questions by the scenario's policy
and keeps every question it saw, then saves the session export and the
normalized record under ``.cloud-run/qualify/<id>.json``. ``check`` evaluates
every scenario's pass condition: live ones from those records, the rest from the
final evaluation's results (``config final``), and prints a table for the
ledger.

Many scenarios name the tool or the steps, so they verify a mechanism, not the
agent's choices (CLAUDE.md, "What a live scenario proves"); each row says which
kind of evidence it is. T6 and T16 need an open model and are local phase.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from scripts.eval.tool_calling import collect, tasks
from scripts.eval.tool_calling.http_client import GactClient
from scripts.eval.tool_calling.paths import DEFAULT_FIXTURES, DEFAULT_RESULTS, DEFAULT_WORK
from scripts.eval.tool_calling.runner import load_manifest
from scripts.eval.tool_calling.session_run import (
    ACTIVE,
    INTERACTION,
    ensure_workspace,
    prepare_workdir,
    write_json,
)

OUT = Path(".cloud-run/qualify")
MECHANISM = "mechanism verified with a tool-directed prompt; not evidence of agent judgement"
CHOICE = "plain request: evidence of the agent's choices"
LOCAL = "local phase (needs an open model or local hardware)"

Answer = Callable[[dict[str, Any], list[dict[str, Any]]], str]


def allow_all(row: dict[str, Any], seen: list[dict[str, Any]]) -> str:
    return "allow"


def tool_of(row: dict[str, Any]) -> str:
    """The tool a permission question is about."""
    return str((row.get("tool_call") or {}).get("tool_name") or row.get("tool_name") or "")


@dataclass
class Scenario:
    """One live qualification scenario."""

    id: str
    title: str
    prompts: list[str]
    evidence: str = MECHANISM
    fixture_task: str | None = None  # copy this evaluation task's fixture as the workspace
    files: dict[str, str] = field(default_factory=dict)  # extra files to write in the workdir
    approval_mode: str = "bypass"
    mode: str | None = None  # session mode, e.g. "plan"
    answer: Answer = allow_all
    deny_tools: tuple[str, ...] = ()  # session deny policies (tool name patterns)
    plugins: tuple[str, ...] = ()  # evaluation marketplace plugins added to the agent
    metadata: dict[str, Any] = field(default_factory=dict)
    between: Callable[[GactClient, str, int], str | None] | None = None
    # Called on every poll of the first turn with the seconds since it started.
    during: Callable[[GactClient, str, float], None] | None = None
    after: Callable[[GactClient, list[str]], dict[str, Any]] | None = None  # extra evidence
    timeout_s: float = 900.0
    # The value given to a question the turn asks the person (a plan_exit approval is
    # ``auto``); None leaves it open and records the turn as ``waiting_user``.
    question_answer: str | None = None


# ---------------------------------------------------------------- live driving


def _pending(client: GactClient, sid: str) -> list[dict[str, Any]]:
    body = client.get("/v1/permissions", session_id=sid, status="pending")
    return list(body.get("permissions") or [])


def _settled(client: GactClient, sid: str, stable: list[Any]) -> bool:
    messages = client.messages(sid, limit=5)
    newest = next((m for m in reversed(messages) if m.get("role") == "assistant"), None)
    if newest is None or not newest.get("stop_reason"):
        stable.clear()
        return False
    marker = (newest.get("id"), newest.get("updated_at"))
    if stable and stable[-1] == marker:
        return True
    stable.append(marker)
    return False


def wait_answering(
    client: GactClient,
    sid: str,
    answer: Answer,
    seen: list[dict[str, Any]],
    timeout_s: float,
    during: Callable[[GactClient, str, float], None] | None = None,
    question_answer: str | None = None,
) -> str:
    """Poll the turn to its end, answering permission questions as they come, and a
    question to the person with ``question_answer`` (else the turn ends ``waiting_user``)."""
    start, stable = time.monotonic(), []
    while time.monotonic() - start < timeout_s:
        if during is not None:
            during(client, sid, time.monotonic() - start)
        session = client.session(sid)
        status = str(session.get("status") or "")
        question = str((session.get("metadata") or {}).get("pending_user_question_id") or "")
        if status == "waiting_user" and question and not _pending(client, sid):
            if question_answer is None:
                return "waiting_user"
            if not any(r.get("id") == question for r in seen):
                seen.append({"id": question, "answered": question_answer, "kind": "question"})
                client.post(
                    f"/v1/sessions/{sid}/questions/{question}/answer", {"value": question_answer}
                )
            time.sleep(1.0)
            continue
        # A pending question can sit under a ``running`` session (the status does not
        # change while the gate waits), so the pending list is read on every poll.
        pending = _pending(client, sid)
        if status in INTERACTION or pending:
            stable.clear()
            for row in pending:
                if any(r.get("id") == row.get("id") for r in seen):
                    continue
                action = answer(row, seen)
                seen.append({**row, "answered": action, "answered_at": time.time()})
                client.post(f"/v1/permissions/{row['id']}", {"action": action})
        elif status not in ACTIVE and _settled(client, sid, stable):
            return "settled"
        time.sleep(1.0)
    return "timed_out"


def run_scenario(
    client: GactClient,
    sc: Scenario,
    manifest: dict[str, Any],
    *,
    fixtures: Path = DEFAULT_FIXTURES,
    work_root: Path = DEFAULT_WORK,
) -> dict[str, Any]:
    workdir = work_root / "qualify" / sc.id
    if workdir.exists():
        shutil.rmtree(workdir)
    if sc.fixture_task:
        prepare_workdir(fixtures, tasks.fixture_dir(sc.fixture_task, manifest), workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    for rel, text in sc.files.items():
        target = workdir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    workspace = ensure_workspace(client, f"qualify {sc.id}", workdir)
    body: dict[str, Any] = {
        "workspace_id": workspace["id"],
        "title": f"qualify {sc.id}: {sc.title}",
        "approval_mode": sc.approval_mode,
        "metadata": {
            "agent_ref": tasks.STANDARD_AGENT,
            "agent_delta": {
                "add": [f"{tasks.MARKETPLACE_NAME}/{p}" for p in sc.plugins],
                "remove": [],
            },
            **sc.metadata,
        },
        "model": {"provider_id": "claude_code", "model_id": "claude-sonnet-5-5"},
    }
    if sc.mode:
        body["mode"] = sc.mode
    sid = str(client.create_session(body)["id"])
    if sc.deny_tools:
        current = list(client.get("/v1/policies").get("policies") or [])
        rows = [
            {"scope": "session", "scope_id": sid, "tool_name_pattern": t, "action": "deny"}
            for t in sc.deny_tools
        ]
        client.request("PUT", "/v1/policies", {"policies": [*rows, *current]})
    seen: list[dict[str, Any]] = []
    states, sessions = [], [sid]
    for index, prompt in enumerate(sc.prompts):
        if index and sc.between is not None:
            switched = sc.between(client, sessions[-1], index)
            if switched:
                sessions.append(switched)
        target = sessions[-1]
        client.send(target, prompt)
        during = sc.during if index == 0 else None
        states.append(
            wait_answering(
                client, target, sc.answer, seen, sc.timeout_s, during, sc.question_answer
            )
        )
    exports = {s: client.export(s) for s in sessions}
    record = {
        "id": sc.id,
        "title": sc.title,
        "evidence": sc.evidence,
        "prompts": sc.prompts,
        "sessions": sessions,
        "states": states,
        "permissions": seen,
        "workdir": str(workdir),
        "files": sorted(str(p.relative_to(workdir)) for p in workdir.rglob("*") if p.is_file()),
        "normalized": {s: collect.normalize_export(e) for s, e in exports.items()},
        "extra": sc.after(client, sessions) if sc.after is not None else {},
        "exports": exports,
        "ran_at": time.time(),
    }
    write_json(OUT / f"{sc.id}.json", record)
    return record


# ---------------------------------------------------------------- scenarios

_BIG = (
    "".join(f"def helper_{n}(value):\n    return value + {n}\n\n" for n in range(1, 1666))
    + "def final_answer():\n    return 'the forty-second answer'\n"
)


def _cancel_after_15s(client: GactClient, sid: str, elapsed: float) -> None:
    flag = _CANCELLED.setdefault(sid, {"done": False})
    if not flag["done"] and elapsed > 15:
        flag["done"] = True
        flag["at"] = time.time()
        client.cancel(sid)


_CANCELLED: dict[str, dict[str, Any]] = {}


def _exact_and_work(client: GactClient, sessions: list[str]) -> dict[str, Any]:
    """What the model was last sent and the session's plan and todos."""
    sid = sessions[-1]
    return {
        "exact": client.get(f"/v1/sessions/{sid}/agent", exact="true").get("exact"),
        "work": client.get(f"/v1/sessions/{sid}/work"),
        "cancelled": _CANCELLED.get(sid),
    }


def _edit_plan_between(client: GactClient, sid: str, index: int) -> str | None:
    """T18: the person edits the plan from the Work rail between the two turns."""
    work = client.get(f"/v1/sessions/{sid}/work")
    plan = work.get("plan") or {}
    text = str(plan.get("text") or plan.get("content") or "")  # /work serves ``text``
    if not text.strip():
        raise RuntimeError("T18: the agent wrote no plan before the edit")
    edited = text + "\n\n- Person's note: also list the hidden files.\n"
    client.request("PUT", f"/v1/sessions/{sid}/plan", {"content": edited})
    return None


def _fork(client: GactClient, sid: str, index: int) -> str | None:
    return str(client.post(f"/v1/sessions/{sid}/fork", {})["id"]) if index == 2 else None


def _t8b_answer(row: dict[str, Any], seen: list[dict[str, Any]]) -> str:
    if tool_of(row) == "run_code":
        return "allow"
    writes = [r for r in seen if tool_of(r) != "run_code"]
    return "deny" if len(writes) >= 2 else "allow"


def _t8c_answer(row: dict[str, Any], seen: list[dict[str, Any]]) -> str:
    if tool_of(row) == "run_code":
        return "allow"
    return "allow_script" if "allow_script" in (row.get("actions") or []) else "allow"


SCENARIOS: dict[str, Scenario] = {
    s.id: s
    for s in (
        Scenario(
            "T2",
            "Read a 5,000-line file",
            [
                "What does final_answer in big_module.py return, and on which line is it "
                "defined? Quote the line."
            ],
            evidence=CHOICE,
            files={"big_module.py": _BIG},
        ),
        Scenario(
            "T4",
            "Survey three subsystems",
            ["Survey this repository's three subsystems and say briefly what each does."],
            evidence=CHOICE,
            fixture_task="P3",
        ),
        Scenario(
            "T8a",
            "auto-edits: a script that writes, then calls a denied tool",
            [
                "Use run_code once. In the script, write the text 'alpha' to out/a.txt with "
                "the file-writing tool, then try to run `echo hi` with the shell tool inside "
                "a try/except and print whether it raised and the error."
            ],
            approval_mode="auto-edits",
            deny_tools=("shell_bash",),
        ),
        Scenario(
            "T8b",
            "ask: three gathered writes, one question at a time",
            [
                "Use run_code once. In the script, write 'one', 'two' and 'three' to out/1.txt, "
                "out/2.txt and out/3.txt with the file-writing tool, all three at once with "
                "asyncio.gather(..., return_exceptions=True), and print each result."
            ],
            approval_mode="ask",
            answer=_t8b_answer,
        ),
        Scenario(
            "T8c",
            "ask: allow for the rest of this script",
            [
                "Use run_code once. In the script, write 'one', 'two' and 'three' to out/1.txt, "
                "out/2.txt and out/3.txt with the file-writing tool, one after another.",
                "Use run_code once more to write 'four' to out/4.txt with the file-writing tool.",
            ],
            approval_mode="ask",
            answer=_t8c_answer,
        ),
        Scenario(
            "T9",
            "run_code in plan mode",
            [
                "This is a read-only check of what plan mode allows, so run it now rather than "
                "planning it: use run_code to print the names under tools.fs, then, in the same "
                "script, try to create scratch.txt in the workspace with Python's open() inside "
                "try/except and print what happened."
            ],
            mode="plan",
        ),
        Scenario(
            "T10",
            "A script loops on a tool",
            [
                "Use run_code to call the directory-listing tool on '.' 600 times in a loop "
                "and print how many calls succeeded."
            ],
        ),
        Scenario(
            "T11",
            "Cancel a turn during a long script",
            [
                "Use run_code to run a loop that prints the step number and sleeps one second, "
                "120 times.",
                "Use run_code to print 6 * 7.",
            ],
            during=_cancel_after_15s,
            after=_exact_and_work,
        ),
        Scenario(
            "T13",
            "An MCP Apps view",
            ["Show me the garage panel."],
            evidence=CHOICE,
            plugins=("eval-dynamic",),
        ),
        Scenario(
            "T15",
            "An approved plan across a mid-turn compaction",
            [
                "Start by writing your plan with plan_update (objective, threads), then do "
                "the task: " + tasks.task("H3", {}).prompt
            ],
            fixture_task="H1",
            # Low enough that the turn compacts mid-way (H3's 25% is never reached).
            metadata={
                "context_preferences": {"automatic_compaction": True, "autocompact_pct": 0.05}
            },
            after=_exact_and_work,
            timeout_s=2400.0,
        ),
        Scenario(
            "T18",
            "Plan and todos on the Work rail, and a person's plan edit",
            [
                "Write a short plan with plan_update and todos with write_todos for listing "
                "this folder's files into FILES.md, then stop without doing it.",
                "Continue with the plan.",
            ],
            fixture_task="P2",
            between=_edit_plan_between,
            after=_exact_and_work,
        ),
        Scenario(
            "T20",
            "A deferred server adds a tool mid-session",
            [
                "Search your tools for garage controls, unlock the garage controls, then "
                "search for a tool that opens the garage door, call it, and tell me the door "
                "state.",
                "Which garage tools can you use now?",
            ],
            plugins=("eval-dynamic",),
            after=_exact_and_work,
        ),
        Scenario(
            "T22",
            "An agent that cannot offer search_tools",
            ["What can you control in this home? Answer from your tool list, call nothing."],
            evidence=CHOICE,
            plugins=("eval-home",),
            after=_exact_and_work,
        ),
        Scenario(
            "T12",
            "Kernel state across calls, and a fork",
            [
                "Use run_code to set x = 42 and print it.",
                "Use run_code to print x.",
                "Use run_code to print x, and say what happened.",
            ],
            between=_fork,
        ),
        Scenario(
            "T17",
            "Plan mode",
            [
                "Enter plan mode with plan_enter, write a short plan for adding a NOTES.md file "
                "that lists this folder's files, then ask to exit with plan_exit. After "
                "approval, write todos, do it, and revise the plan with plan_update to record "
                "that it is done."
            ],
            fixture_task="P2",
            question_answer="auto",
        ),
        Scenario(
            "T19",
            "A broad query naming a deferred tool",
            [
                "Call search_tools with the query 'please adjust heating, maybe with "
                "climate_set_temperature or something similar' and tell me the first tool it "
                "returned. Do not call any other tool."
            ],
            plugins=("eval-home",),
        ),
        Scenario(
            "T21",
            "A script calls a deferred tool with no search first",
            [
                "Without calling search_tools, use run_code to call "
                "tools.synthetic_home.climate_list() directly and print the result."
            ],
            plugins=("eval-home",),
        ),
    )
}


# ---------------------------------------------------------------- checks


def _calls(record: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        call
        for norm in record["normalized"].values()
        for session in norm["sessions"]
        for call in session["tool_calls"]
    ]


def _final_text(record: dict[str, Any]) -> str:
    texts = []
    for norm in record["normalized"].values():
        texts.append(str((norm.get("outcome") or {}).get("final_text") or ""))
    return "\n".join(texts)


def _results(task: str, config: str = "final") -> list[dict[str, Any]]:
    return [
        json.loads(p.read_text(encoding="utf-8"))
        for p in sorted((DEFAULT_RESULTS / config / task).glob("run-*.json"))
    ]


def _result_calls(result: dict[str, Any]) -> list[dict[str, Any]]:
    return [c for s in result["sessions"] for c in s["tool_calls"]]


def _shell(call: dict[str, Any]) -> str:
    return str((call.get("args") or {}).get("command") or "")


def check_t1() -> tuple[str, list[str]]:
    runs = _results("P3")
    if not runs:
        return "pending", ["no final P3 runs"]
    notes, ok = [], True
    for r in runs:
        calls = _result_calls(r)
        paging = [
            c for c in calls if c["tool"] == "shell_bash" and re.search(r"\bsed\s+-n\b", _shell(c))
        ]
        ranged = [
            c
            for c in calls
            if c["tool"] == "fs_read_file" and (c["args"].get("offset") or c["args"].get("limit"))
        ]
        files_mode = [
            c
            for c in calls
            if c["tool"] == "fs_search_text" and c["args"].get("mode", "files") == "files"
        ]
        ok &= not paging
        notes.append(
            f"run {r['run_index']} {r['session_id']}: sed paging {len(paging)}, ranged reads "
            f"{len(ranged)}, files-mode searches {len(files_mode)}"
        )
    return ("pass" if ok else "fail"), notes


def check_t2(rec: dict[str, Any]) -> tuple[str, list[str]]:
    calls = _calls(rec)
    reads = [c for c in calls if c["tool"] == "fs_read_file"]
    ranged = [c for c in reads if c["args"].get("offset") or c["args"].get("limit")]
    paging = [
        c
        for c in calls
        if c["tool"] == "shell_bash" and re.search(r"\b(sed|head|tail|cat)\b", _shell(c))
    ]
    text = _final_text(rec)
    right = "5000" in text or "4996" in text or "forty-second" in text
    notes = [
        f"reads {len(reads)} (ranged {len(ranged)}), shell paging {len(paging)}",
        f"answer cites the line: {right}",
    ]
    return ("pass" if reads and not paging and right else "fail"), notes


def check_t3() -> tuple[str, list[str]]:
    runs = _results("P1")
    if not runs:
        return "pending", ["no final P1 runs"]
    notes, ok = [], True
    for r in runs:
        calls = _result_calls(r)
        search = [c for c in calls if c["tool"] == "fs_search_text"]
        pages = [
            c
            for c in calls
            if c["tool"] == "fs_read_file"
            and not (c["args"].get("offset") or c["args"].get("limit"))
        ]
        ok &= bool(search) or any(c["tool"] == "shell_bash" and "rg" in _shell(c) for c in calls)
        notes.append(
            f"run {r['run_index']}: searches {len(search)}, unranged reads {len(pages)}, calls {len(calls)}"
        )
    return ("pass" if ok else "fail"), notes


def check_t4(rec: dict[str, Any]) -> tuple[str, list[str]]:
    calls = _calls(rec)
    parallel = [c for c in calls if c["tool"] == "spawn_agents_parallel"]
    explore = [
        c
        for c in parallel
        if c["args"].get("wait") in (True, "true")
        and all((t or {}).get("agent") == "explore" for t in (c["args"].get("tasks") or [{}]))
    ]
    children = [
        s for norm in rec["normalized"].values() for s in norm["sessions"] if s.get("depth", 0) > 0
    ]
    notes = [
        f"spawn_agents_parallel {len(parallel)} (explore, wait {len(explore)}), child sessions {len(children)}"
    ]
    return ("pass" if explore and len(children) >= 2 else "fail"), notes


def check_t5() -> tuple[str, list[str]]:
    notes, ok, seen = [], True, False
    for task in ("P8", "P9", "P10"):
        for r in _results(task):
            seen = True
            calls = _result_calls(r)
            names = [c["tool"] for c in calls]
            searched = "search_tools" in names
            direct = [n for n in names if n not in ("search_tools", "run_code")]
            notes.append(
                f"{task} run {r['run_index']}: search_tools {searched}, direct calls {direct[:4]}"
            )
            ok &= bool(direct)
    return (("pass" if ok else "fail") if seen else "pending"), notes


def check_t7() -> tuple[str, list[str]]:
    runs = _results("P6")
    if not runs:
        return "pending", ["no final P6 runs"]
    notes, ok = [], True
    for r in runs:
        calls = _result_calls(r)
        code = [c for c in calls if c["tool"] == "run_code"]
        gathered = [c for c in code if "gather" in str(c["args"].get("code") or "")]
        nested = [c for c in calls if c["tool"] == "records_get_record"]
        ok &= bool(gathered)
        notes.append(
            f"run {r['run_index']}: run_code {len(code)} (gather {len(gathered)}), nested get_record {len(nested)}"
        )
    return ("pass" if ok else "fail"), notes


def _iso(epoch: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def _asked(rec: dict[str, Any], tool: str) -> list[dict[str, Any]]:
    return [r for r in rec["permissions"] if tool_of(r) == tool]


def check_t8a(rec: dict[str, Any]) -> tuple[str, list[str]]:
    script_q = _asked(rec, "run_code")
    write_q = [r for r in rec["permissions"] if tool_of(r) != "run_code"]
    wrote = "out/a.txt" in rec["files"]
    text = _final_text(rec).lower()
    raised = any(w in text for w in ("raised", "denied", "error", "blocked"))
    shows_code = bool(script_q) and "a.txt" in json.dumps(script_q[0])
    notes = [
        f"script questions {len(script_q)} (shows code {shows_code}), other questions {len(write_q)}",
        f"file written {wrote}, denied call raised {raised}",
    ]
    return ("pass" if len(script_q) == 1 and not write_q and wrote and raised else "fail"), notes


def check_t8b(rec: dict[str, Any]) -> tuple[str, list[str]]:
    script_q = _asked(rec, "run_code")
    writes = [r for r in rec["permissions"] if tool_of(r) != "run_code"]
    # One question at a time: each write question was created after the previous answer.
    overlap = any(
        str(b.get("created_at") or "") < _iso(a["answered_at"])
        for a, b in zip(writes, writes[1:], strict=False)
    )
    files = [f for f in rec["files"] if f.startswith("out/")]
    shows_script = all("gather" in json.dumps(w) for w in writes)
    notes = [
        f"script questions {len(script_q)}, write questions {len(writes)} answered "
        f"{[w['answered'] for w in writes]}, each shows the script {shows_script}",
        f"files {files}",
    ]
    ok = len(script_q) == 1 and len(writes) == 3 and len(files) == 2 and not overlap
    return ("pass" if ok else "fail"), notes


def check_t8c(rec: dict[str, Any]) -> tuple[str, list[str]]:
    script_q = _asked(rec, "run_code")
    writes = [r for r in rec["permissions"] if tool_of(r) != "run_code"]
    files = sorted(f for f in rec["files"] if f.startswith("out/"))
    notes = [
        f"script questions {len(script_q)}, write questions {len(writes)} {[w['answered'] for w in writes]}",
        f"files {files}",
    ]
    ok = len(script_q) == 2 and len(writes) == 2 and len(files) == 4
    return ("pass" if ok else "fail"), notes


def check_t9(rec: dict[str, Any]) -> tuple[str, list[str]]:
    """The script ran (it printed the ``tools.fs`` names) and could not write the workspace.
    Write stubs stay in the table; each nested write meets the plan ACL like a direct call."""
    blob = json.dumps(rec["exports"])
    created = "scratch.txt" in rec["files"]
    ran = "read_file" in blob and "Script finished" in blob
    refused = bool(re.search(r"PermissionError|Read-only file system|Permission denied", blob))
    notes = [
        f"script ran {ran}",
        f"open() refused {refused}",
        f"scratch.txt created {created}",
    ]
    return ("pass" if ran and refused and not created else "fail"), notes


def check_t10(rec: dict[str, Any]) -> tuple[str, list[str]]:
    blob = json.dumps(rec["exports"]).lower()
    typed = "budget" in blob or "max_nested_calls" in blob or "nested call" in blob
    nested = [c for c in _calls(rec) if c["tool"] == "fs_list_directory"]
    notes = [f"nested list calls {len(nested)}", f"typed budget error in the observation {typed}"]
    return ("pass" if typed and len(nested) <= 500 else "fail"), notes


def check_t12(rec: dict[str, Any]) -> tuple[str, list[str]]:
    texts = [
        str((rec["normalized"][s].get("outcome") or {}).get("final_text") or "")
        for s in rec["sessions"]
    ]
    parent = texts[0] if texts else ""
    fork = texts[-1] if len(texts) > 1 else ""
    persisted = "42" in parent
    clean = "NameError" in json.dumps(rec["exports"][rec["sessions"][-1]]) or "not defined" in fork
    notes = [
        f"sessions {rec['sessions']}",
        f"x visible in the next call {persisted}",
        f"fork starts clean {clean}",
    ]
    return ("pass" if persisted and clean and len(rec["sessions"]) == 2 else "fail"), notes


def check_t14() -> tuple[str, list[str]]:
    runs = _results("H1")
    if not runs:
        return "pending", ["no final H1 runs"]
    notes, ok = [], True
    for r in runs:
        calls = _result_calls(r)
        plans = [c for c in calls if c["tool"] in ("plan_update", "plan_enter")]
        ok &= bool(plans)
        notes.append(f"run {r['run_index']}: plan writes {len(plans)}, calls {len(calls)}")
    return ("pass" if ok else "fail"), notes


def check_t17(rec: dict[str, Any]) -> tuple[str, list[str]]:
    names = [c["tool"] for c in _calls(rec)]
    order = [n for n in names if n in ("plan_enter", "plan_exit", "write_todos", "plan_update")]
    # Enter, write the plan, ask to exit; after approval, todos and a plan revision.
    ok = (
        "plan_enter" in order
        and "plan_exit" in order
        and order.index("plan_enter") < order.index("plan_exit")
        and "write_todos" in order[order.index("plan_exit") :]
        and "plan_update" in order[order.index("plan_exit") :]
    )
    return ("pass" if ok and "NOTES.md" in rec["files"] else "fail"), [
        f"order {order}",
        f"NOTES.md {'NOTES.md' in rec['files']}",
    ]


def check_t19(rec: dict[str, Any]) -> tuple[str, list[str]]:
    blob = json.dumps(rec["exports"])
    first = re.search(r"## ([\w-]+)  \(", blob)
    name = first.group(1) if first else ""
    # A server's tools carry its prefix (``synthetic-home_climate_set_temperature``).
    ok = name.split("_", 1)[-1] == "climate_set_temperature" or name == "climate_set_temperature"
    return ("pass" if ok else "fail"), [f"first result {name!r}"]


def check_t21(rec: dict[str, Any]) -> tuple[str, list[str]]:
    names = [c["tool"] for c in _calls(rec)]
    direct = [c for c in _calls(rec) if "climate_" in c["tool"]]
    ok = (
        "search_tools" not in names
        and bool(direct)
        and all(c.get("ok") is not False for c in direct)
    )
    return ("pass" if ok else "fail"), [f"tools {names}"]


def _exact_text(rec: dict[str, Any]) -> str:
    return json.dumps((rec.get("extra") or {}).get("exact") or {})


def check_t11(rec: dict[str, Any]) -> tuple[str, list[str]]:
    code = [c for c in _calls(rec) if c["tool"] == "run_code"]
    cancelled = bool((rec.get("extra") or {}).get("cancelled"))
    first_ok = [c.get("ok") for c in code[:1]]
    later = code[1:]
    second = bool(later) and later[-1].get("ok") is not False and "42" in _final_text(rec)
    notes = [
        f"cancel sent {cancelled}",
        f"first script ok={first_ok}",
        f"next script printed 42: {second}",
    ]
    return ("pass" if cancelled and second else "fail"), notes


def check_t13(rec: dict[str, Any]) -> tuple[str, list[str]]:
    blob = json.dumps(rec["exports"])
    panel = [c for c in _calls(rec) if c["tool"].endswith("panel")]
    app = "ui://garage/panel" in blob
    notes = [
        f"panel calls {len(panel)}",
        f"app resource referenced in the session {app}",
        "screenshot: see ledger",
    ]
    return ("pass" if panel and app else "fail"), notes


def check_t15(rec: dict[str, Any]) -> tuple[str, list[str]]:
    norm = next(iter(rec["normalized"].values()))
    compactions = sum(
        int((s.get("events") or {}).get("compactions") or 0) for s in norm["sessions"]
    )
    plan = str(
        (((rec.get("extra") or {}).get("work") or {}).get("plan") or {}).get("content") or ""
    )
    exact = _exact_text(rec)
    head = plan.strip().splitlines()[0] if plan.strip() else ""
    reinjected = bool(head) and json.dumps(head)[1:-1] in exact
    notes = [
        f"compactions {compactions}",
        f"plan written {bool(plan)}",
        f"plan in the last prompt sent {reinjected}",
    ]
    return ("pass" if compactions and plan and reinjected else "fail"), notes


def check_t18(rec: dict[str, Any]) -> tuple[str, list[str]]:
    exact = _exact_text(rec)
    work = (rec.get("extra") or {}).get("work") or {}
    plan = str((work.get("plan") or {}).get("text") or "")
    todos = work.get("todos") or []
    # The edit is offered once, as an injection at the next step; it stays in the history.
    history = json.dumps(rec["exports"])
    steered = "Person's note" in exact or "Person's note" in history.replace("\\u2019", "'")
    notes = [
        f"plan saved with the edit {'Person' in plan}",
        f"todos {len(todos)}",
        f"edit reached the model {steered}",
        "screenshots at 1440 and 390: see ledger",
    ]
    return ("pass" if "Person" in plan and steered and todos else "fail"), notes


def check_t20(rec: dict[str, Any]) -> tuple[str, list[str]]:
    names = [c["tool"] for c in _calls(rec)]
    opened = "garage_open_door" in names
    searched = names.count("search_tools")
    exact = _exact_text(rec)
    noted = "garage_open_door" in exact
    resets = json.dumps(rec["exports"]).count("prefix_mismatch")
    notes = [f"tools {names}", f"note names the new tool {noted}", f"prefix_mismatch {resets}"]
    return ("pass" if opened and searched >= 1 and noted and not resets else "fail"), notes


def check_t22(rec: dict[str, Any]) -> tuple[str, list[str]]:
    exact = (rec.get("extra") or {}).get("exact") or {}
    tools = json.dumps(exact.get("tools") if isinstance(exact, dict) else exact)
    listed = "climate_set_temperature" in tools
    no_search = "search_tools" not in tools
    notes = [
        f"deferred tools listed in full {listed}",
        f"search_tools absent {no_search}",
        "trace reason: see server log",
    ]
    return ("pass" if listed and no_search else "fail"), notes


LIVE_CHECKS: dict[str, Callable[[dict[str, Any]], tuple[str, list[str]]]] = {
    "T2": check_t2,
    "T4": check_t4,
    "T8a": check_t8a,
    "T8b": check_t8b,
    "T8c": check_t8c,
    "T9": check_t9,
    "T10": check_t10,
    "T11": check_t11,
    "T12": check_t12,
    "T13": check_t13,
    "T15": check_t15,
    "T18": check_t18,
    "T20": check_t20,
    "T22": check_t22,
    "T17": check_t17,
    "T19": check_t19,
    "T21": check_t21,
}
EVAL_CHECKS: dict[str, tuple[str, Callable[[], tuple[str, list[str]]]]] = {
    "T1": (CHOICE, check_t1),
    "T3": (CHOICE, check_t3),
    "T5": (CHOICE, check_t5),
    "T7": (CHOICE, check_t7),
    "T14": (CHOICE, check_t14),
}
LOCAL_ROWS = {"T6": "T5 on an open model", "T16": "H1 on an open model, before and after"}


def check_all(ids: Iterable[str] | None = None) -> list[tuple[str, str, str, list[str]]]:
    rows = []
    wanted = set(ids or [])
    for tid, (evidence, fn) in EVAL_CHECKS.items():
        if not wanted or tid in wanted:
            status, notes = fn()
            rows.append((tid, status, evidence, notes))
    for tid, fn in LIVE_CHECKS.items():
        if wanted and tid not in wanted:
            continue
        path = OUT / f"{tid}.json"
        if not path.exists():
            rows.append((tid, "pending", SCENARIOS[tid].evidence, ["not run"]))
            continue
        rec = json.loads(path.read_text(encoding="utf-8"))
        status, notes = fn(rec)
        notes = [f"sessions {rec['sessions']}", *notes]
        rows.append((tid, status, rec["evidence"], notes))
    for tid, what in LOCAL_ROWS.items():
        if not wanted or tid in wanted:
            rows.append((tid, "local", LOCAL, [what]))
    return sorted(rows, key=lambda r: (int(re.sub(r"\D", "", r[0])), r[0]))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="qualify", description=__doc__.split("\n")[0])
    parser.add_argument("--base-url", default="http://127.0.0.1:8787")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run")
    run.add_argument("--ids", default=",".join(SCENARIOS))
    run.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES)
    run.add_argument("--work-root", type=Path, default=DEFAULT_WORK)
    chk = sub.add_parser("check")
    chk.add_argument("--ids", default="")
    args = parser.parse_args(argv)
    if args.cmd == "run":
        client = GactClient(args.base_url)
        manifest = load_manifest(args.fixtures)
        for tid in [i for i in args.ids.split(",") if i]:
            sc = SCENARIOS[tid]
            started = time.time()
            rec = run_scenario(
                client, sc, manifest, fixtures=args.fixtures, work_root=args.work_root
            )
            print(
                f"{tid}: {rec['states']} in {time.time() - started:.0f}s, sessions {rec['sessions']}"
            )
        return 0
    for tid, status, evidence, notes in check_all([i for i in args.ids.split(",") if i]):
        print(f"{tid:5} {status:8} {evidence}")
        for note in notes:
            print(f"      {note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
