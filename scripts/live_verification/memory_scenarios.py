"""Live scenarios for long-term memory (docs/design/sdk/memory-ledger.md, M2-M17).

Run against a running stack (``bash scripts/cloud/stack.sh start``):

    uv run --no-sync python scripts/live_verification/memory_scenarios.py remember
    uv run --no-sync python scripts/live_verification/memory_scenarios.py recall
    ...

Each scenario prints what it observed and writes it to ``.cloud-run/memory/<name>.json``
so the ledger row can cite the session ids, tool calls and documents it saw.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    client,
    create_session,
    post_message,
    session_messages,
    tool_calls_from_messages,
    wait_turn,
)

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / ".cloud-run" / "memory"
WS = "ws_default"
Call = Any


def _turn(call: Call, sid: str, text: str, ws: str = WS) -> list[dict[str, Any]]:
    post_message(call, sid, text)
    time.sleep(2)
    status = wait_turn(call, ws, sid, max_elapsed=900)
    messages = session_messages(call, sid)
    print(f"  [{sid}] {status}: {text[:60]!r}")
    return messages


def _tools(messages: list[dict[str, Any]]) -> list[str]:
    # A native builtin is recorded twice (the live observer keeps the boundary's
    # ``kwargs`` wrapper, a base-branch defect); count each call once.
    rows = [
        row
        for row in tool_calls_from_messages(messages)
        if row.get("telemetry_source") != "live_observer"
    ]
    return [str(row.get("name") or "") for row in rows]


def _answer(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "assistant":
            parts = message.get("parts") or message.get("blocks") or []
            return " ".join(str(p.get("text") or "") for p in parts if p.get("type") == "text")
    return ""


def _last_assistant(messages: list[dict[str, Any]]) -> dict[str, Any]:
    return next((m for m in reversed(messages) if m.get("role") == "assistant"), {})


def _docs(call: Call, scope: str = "all") -> list[dict[str, Any]]:
    return call("GET", "/v1/memory/documents", params={"scope": scope})["documents"]


def _save(name: str, evidence: dict[str, Any]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{name}.json"
    path.write_text(json.dumps(evidence, indent=2, default=str), encoding="utf-8")
    print(f"  evidence: {path.relative_to(ROOT)}")


def remember(call: Call) -> None:
    """M3: a stated fact is created with source user and appears in a new snapshot."""
    sid = create_session(call, WS, "M3 remember")
    messages = _turn(call, sid, "Remember that my Frontier project id is CSC123.")
    docs = [d for d in _docs(call) if "CSC123" in d["text"]]
    later = create_session(call, WS, "M3 snapshot check")
    _turn(call, later, "Hi, just say hello back.")
    snapshot = call("GET", f"/v1/sessions/{later}").get("metadata", {}).get("memory_snapshot", {})
    evidence = {
        "session": sid,
        "tools": _tools(messages),
        "documents": [
            {"name": d["name"], "facts": [(f["text"], f["source"]) for f in d["facts"]]}
            for d in docs
        ],
        "snapshot_session": later,
        "snapshot_lists_document": any(d["name"] in snapshot.get("text", "") for d in docs),
        "snapshot_text": snapshot.get("text", ""),
    }
    _save("m3_remember", evidence)
    print(json.dumps({k: v for k, v in evidence.items() if k != "snapshot_text"}, indent=2))


def recall(call: Call) -> None:
    """M4: a later session reads the fact and the answer carries memory_used."""
    sid = create_session(call, WS, "M4 recall")
    messages = _turn(call, sid, "What is my Frontier project id? Check what you remember about me.")
    last = _last_assistant(messages)
    evidence = {
        "session": sid,
        "tools": _tools(messages),
        "answer": _answer(messages),
        "memory_used": (last.get("metadata") or {}).get("memory_used"),
        "message": last.get("id"),
    }
    _save("m4_recall", evidence)
    print(json.dumps(evidence, indent=2))


def correct(call: Call) -> None:
    """M5: a correction supersedes the old fact; history keeps both versions."""
    sid = create_session(call, WS, "M5 correct")
    _turn(call, sid, "What do you remember about my Frontier project id?")
    messages = _turn(call, sid, "Actually my Frontier project id is CSC456, please update it.")
    docs = [d for d in _docs(call) if "CSC456" in d["text"]]
    versions = (
        call("GET", f"/v1/memory/documents/{docs[0]['name']}/versions")["versions"] if docs else []
    )
    evidence = {
        "session": sid,
        "tools": _tools(messages),
        "facts": [(f["text"], f["live"]) for d in docs for f in d["facts"]],
        "versions": [(v["n"], v["author"].get("kind"), v["at"]) for v in versions],
    }
    _save("m5_correct", evidence)
    print(json.dumps(evidence, indent=2))


def forget(call: Call) -> None:
    """M6: forgetting removes the line and records its id on the forgotten list."""
    sid = create_session(call, WS, "M6 forget")
    messages = _turn(call, sid, "Forget my Frontier project id.")
    remaining = [d["name"] for d in _docs(call) if "CSC456" in d["text"] or "CSC123" in d["text"]]
    live = [
        f["text"]
        for d in _docs(call)
        for f in d["facts"]
        if f["live"] and ("CSC456" in f["text"] or "CSC123" in f["text"])
    ]
    profile = call("GET", "/v1/memory/documents/user/profile/profile", ok=(200, 404))
    evidence = {
        "session": sid,
        "tools": _tools(messages),
        "documents_still_mentioning": remaining,
        "live_facts_left": live,
        "forgotten": profile.get("forgotten", []),
    }
    _save("m6_forget", evidence)
    print(json.dumps(evidence, indent=2))


def workspace_only(call: Call) -> None:
    """M7: a workspace without account memory sees only its own documents."""
    root = ROOT / ".cloud-run" / "memory" / "lab-ws"
    root.mkdir(parents=True, exist_ok=True)
    ws = call("POST", "/v1/workspaces", {"name": "memory-lab", "root_path": str(root)})
    wid = str(ws.get("id") or ws.get("workspace_id"))
    call("PUT", f"/v1/workspaces/{wid}/memory", {"use_account_memory": False})
    writer = create_session(call, wid, "M7 project fact")
    _turn(
        call,
        writer,
        "Remember for this project: the beamline data lives under /data/bl7.",
        ws=wid,
    )
    reader = create_session(call, wid, "M7 snapshot")
    messages = _turn(
        call, reader, "List everything you remember, including my preferences.", ws=wid
    )
    snapshot = call("GET", f"/v1/sessions/{reader}").get("metadata", {}).get("memory_snapshot", {})
    evidence = {
        "workspace": wid,
        "project_documents": [d["name"] for d in _docs(call, f"project/{wid}")],
        "reader": reader,
        "snapshot_text": snapshot.get("text", ""),
        "tools": _tools(messages),
        "answer": _answer(messages),
    }
    _save("m7_workspace_only", evidence)
    print(json.dumps(evidence, indent=2))


def child_and_fork(call: Call) -> None:
    """M8 and M9: a delegated child reads only; a fork sees memory as of the fork."""
    sid = create_session(call, WS, "M8 delegate")
    messages = _turn(
        call,
        sid,
        "Delegate this to a clio/standard subagent and wait for it: it should search my "
        "memory for my plot preferences, then try to remember 'prefers 300 dpi figures', "
        "and report exactly which memory tools it had. Do not do it yourself.",
    )
    children = [
        s
        for s in call("GET", "/v1/sessions", params={"workspace_id": WS}).get("sessions", [])
        if s.get("parent_session_id") == sid
    ]
    child_tools: dict[str, list[str]] = {}
    for child in children:
        child_tools[child["id"]] = _tools(session_messages(call, child["id"]))
    agent_tools = (
        call("GET", f"/v1/sessions/{children[0]['id']}/agent", ok=(200, 404)) if children else {}
    )
    fork = call("POST", f"/v1/sessions/{sid}/fork", {}, ok=(200, 201))
    fork_id = str(fork.get("id") or fork.get("session", {}).get("id"))
    fork_messages = _turn(call, fork_id, "Remember that I prefer dark plots.")
    evidence = {
        "session": sid,
        "parent_tools": _tools(messages),
        "children": child_tools,
        "child_agent": agent_tools,
        "fork": fork_id,
        "fork_snapshot_copied": call("GET", f"/v1/sessions/{fork_id}")
        .get("metadata", {})
        .get("memory_snapshot")
        == call("GET", f"/v1/sessions/{sid}").get("metadata", {}).get("memory_snapshot"),
        "fork_tools": _tools(fork_messages),
        "dark_plots_written": any("dark plots" in d["text"] for d in _docs(call)),
    }
    _save("m8_m9_child_fork", evidence)
    print(json.dumps(evidence, indent=2))


def concurrent(call: Call) -> None:
    """M16: two writers on one document; the loser retries on the new hash."""
    import threading

    name = next((d for d in _docs(call, "user") if d["category"] == "preferences"), None)
    if name is None:
        print("  no preferences document; run remember/background first")
        return
    doc = call("GET", f"/v1/memory/documents/{name['name']}")
    fact = next(f for f in doc["facts"] if f["live"])
    results: list[Any] = []

    def edit(text: str) -> None:
        r = client(BASE)(
            "POST",
            f"/v1/memory/documents/{doc['name']}/facts/{fact['id']}",
            {"fact": text, "expected_sha": doc["sha"]},
            ok=(200, 409),
            raw=True,
        )
        results.append((text, r.status_code, r.json()))

    threads = [threading.Thread(target=edit, args=(t,)) for t in ("edit one", "edit two")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    codes = sorted(code for _, code, _ in results)
    loser = next((body for _, code, body in results if code == 409), {})
    loser_text = next((text for text, code, _ in results if code == 409), "")
    # A real retry re-reads: the winner superseded the fact, so correct its replacement.
    fresh = call("GET", f"/v1/memory/documents/{doc['name']}")
    replaced = next(f for f in fresh["facts"] if f["id"] == fact["id"])
    current_id = replaced["superseded_by"] or fact["id"]
    retried = call(
        "POST",
        f"/v1/memory/documents/{doc['name']}/facts/{current_id}",
        {"fact": loser_text, "expected_sha": fresh["sha"]},
    )
    versions = call("GET", f"/v1/memory/documents/{doc['name']}/versions")["versions"]
    evidence = {
        "document": doc["name"],
        "codes": codes,
        "loser_error": loser.get("error"),
        "retry_live_facts": [f["text"] for f in retried.get("facts", []) if f["live"]],
        "history": [(v["n"], v["author"].get("kind"), v["at"]) for v in versions][-3:],
    }
    _save("m16_concurrent", evidence)
    print(json.dumps(evidence, indent=2))


def transfer(call: Call) -> None:
    """M14: export, delete all, import, import again."""
    import requests

    before = {d["name"]: d["text"] for d in _docs(call, "user")}
    archive = requests.get(f"{BASE}/v1/memory/export", params={"scope": "user"}, timeout=60)
    archive.raise_for_status()
    (OUT / "export.zip").parent.mkdir(parents=True, exist_ok=True)
    (OUT / "export.zip").write_bytes(archive.content)
    call("DELETE", "/v1/memory/documents", params={"scope": "user", "confirm": "true"})
    emptied = len(_docs(call, "user"))
    first = requests.post(f"{BASE}/v1/memory/import", data=archive.content, timeout=60).json()
    after = {d["name"]: d["text"] for d in _docs(call, "user")}
    second = requests.post(f"{BASE}/v1/memory/import", data=archive.content, timeout=60).json()
    evidence = {
        "documents": len(before),
        "after_delete": emptied,
        "first_import": first,
        "second_import": second,
        "byte_identical": before == after,
        "differing": sorted(n for n in before if before.get(n) != after.get(n)),
    }
    _save("m14_transfer", evidence)
    print(json.dumps(evidence, indent=2))


def assistant_import(call: Call) -> None:
    """M15: pasted assistant notes become imported proposals waiting for review."""
    text = (
        "Here is what I know about you:\n"
        "- You work on HDF5 performance at a national lab.\n"
        "- You prefer Python examples over C.\n"
        "- Your manager is Dana Reyes.\n"
    )
    report = call("POST", "/v1/memory/import/assistant", {"text": text, "scope": "user"})
    evidence = {"report": report}
    _save("m15_assistant_import_started", evidence)
    print(json.dumps(evidence, indent=2))


def proposals(call: Call) -> None:
    """Print the inbox and every proposal (M10, M11, M15, M17 evidence)."""
    rows = call("GET", "/v1/memory/proposals", params={"status": "all"})["proposals"]
    view = [
        {
            k: r[k]
            for k in ("id", "target", "change", "facts", "sources", "status", "origin", "handle")
        }
        for r in rows
    ]
    _save("proposals", {"proposals": view, "documents": [d["name"] for d in _docs(call)]})
    print(json.dumps(view, indent=2))


def idle_seed(call: Call) -> None:
    """M10/M11: a conversation with two preferences, a health remark and a tool result."""
    sid = create_session(call, WS, "M10 idle seed")
    _turn(call, sid, "For plots I always want SI units on the axes.")
    _turn(call, sid, "Also, I like short answers with the code first.")
    _turn(
        call,
        sid,
        "I have been dealing with migraines lately, so keep screens dim. Anyway, "
        "summarize the file .cloud-run/memory/page.txt for me.",
    )
    _save("m10_seed", {"session": sid})
    print(f"  seeded {sid}")


def recall_compacted(call: Call) -> None:
    """M2: ``recall_context`` returns the original steps after compaction."""
    sid = create_session(call, WS, "M2 recall after compaction")
    _turn(call, sid, "Note for later: the detector B12 calibration constant is 4.771e-3.")
    _turn(call, sid, "Also note: run 812 used a beam energy of 7.05 GeV.")
    compaction = call("POST", f"/v1/sessions/{sid}/compact", {})
    messages = _turn(
        call,
        sid,
        "From before the compaction: what was the B12 calibration constant and the beam "
        "energy of run 812? Use recall_context to read the original steps.",
    )
    evidence = {
        "session": sid,
        "compaction": compaction.get("id") or compaction.get("compaction_id") or compaction,
        "tools": _tools(messages),
        "answer": _answer(messages),
    }
    _save("m2_recall", evidence)
    print(json.dumps(evidence, indent=2))


def tool_seed(call: Call) -> None:
    """M11: facts read from files are sourced ``tool``; an instruction inside one is not."""
    sid = create_session(call, WS, "M11 tool seed")
    _turn(
        call,
        sid,
        "Read .cloud-run/memory/lab-roster.md and .cloud-run/memory/page.txt with your file "
        "tool and summarize both in a few lines.",
    )
    _turn(call, sid, "Thanks, that is useful.")
    _turn(call, sid, "That's all for now.")
    _save("m11_seed", {"session": sid})
    print(f"  seeded {sid}")


def background_off(call: Call) -> None:
    """M12, with ``memory.background`` off: seed an idle conversation, then remember."""
    seed = create_session(call, WS, "M12 background off")
    _turn(call, seed, "My batch jobs go to the debug queue on Polaris.")
    _turn(call, seed, "I usually request two nodes.")
    _turn(call, seed, "Okay, thanks.")
    writer = create_session(call, WS, "M12 remember")
    messages = _turn(call, writer, "Remember that my ALCF username is jcernuda.")
    evidence = {"seed": seed, "writer": writer, "tools": _tools(messages)}
    _save("m12", evidence)
    print(json.dumps(evidence, indent=2))


SCENARIOS = {
    "remember": remember,
    "recall": recall,
    "correct": correct,
    "forget": forget,
    "workspace": workspace_only,
    "child-fork": child_and_fork,
    "concurrent": concurrent,
    "transfer": transfer,
    "assistant-import": assistant_import,
    "proposals": proposals,
    "idle-seed": idle_seed,
    "recall-compacted": recall_compacted,
    "tool-seed": tool_seed,
    "background-off": background_off,
}
BASE = "http://127.0.0.1:8787"


def main() -> None:
    global BASE
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("scenario", choices=sorted(SCENARIOS))
    parser.add_argument("--base", default=BASE)
    args = parser.parse_args()
    BASE = args.base
    SCENARIOS[args.scenario](client(BASE))


if __name__ == "__main__":
    main()
