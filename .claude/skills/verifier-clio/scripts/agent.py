"""Drive the running CLIO agent over its REST API and print what it did.

Every command appends to the run's evidence (``$VERIFY_DIR``) and records the
sessions and workspaces it creates in ``resources.json`` so ``down.sh`` can
remove them. Run with the repository's Python (``uv run --no-sync python``).

  agent.py ready                               wait until sessions and workspaces answer
  agent.py workspace NAME                      scratch workspace rooted in the run dir
  agent.py session TITLE [--workspace ID] [--agent REF] [--add PLUGIN ...] [--approval MODE]
  agent.py say SID TEXT [--timeout S]          send, wait for the turn, print the turn
  agent.py command SID NAME INPUT              run a slash command as the person, print the turn
  agent.py turn SID                            print the newest assistant turn again
  agent.py get PATH | post PATH JSON | patch PATH JSON
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import requests

BASE = os.environ.get("CLIO_URL", "http://127.0.0.1:8787")
ROOT = Path(__file__).resolve().parents[4]
RUN = Path(os.environ.get("VERIFY_DIR") or ROOT / ".cloud-run" / "verify" / "current").resolve()
S = requests.Session()


def call(method: str, path: str, *, ok: tuple[int, ...] = (), **kw: Any) -> Any:
    """One request; a status >= 400 that is not in ``ok`` raises with the body."""

    for attempt in range(2):
        try:
            r = S.request(method, BASE + path, timeout=kw.pop("timeout", 120), **kw)
            break
        except requests.ConnectionError:
            # uvicorn drops idle keep-alive sockets; one retry covers that race.
            if attempt:
                raise
    if r.status_code >= 400 and r.status_code not in ok:
        raise SystemExit(f"{method} {path} -> {r.status_code}: {r.text[:1500]}")
    try:
        return r.json() if r.content else None
    except ValueError:
        return r.text


def record(kind: str, value: str) -> None:
    RUN.mkdir(parents=True, exist_ok=True)
    path = RUN / "resources.json"
    data = json.loads(path.read_text()) if path.exists() else {}
    data.setdefault(kind, [])
    if value not in data[kind]:
        data[kind].append(value)
    path.write_text(json.dumps(data, indent=2))


def evidence(name: str, text: str) -> Path:
    RUN.mkdir(parents=True, exist_ok=True)
    path = RUN / name
    path.write_text(text)
    return path


def ready(timeout: float = 600) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            a = S.get(BASE + "/v1/sessions", params={"limit": 1}, timeout=10)
            b = S.get(BASE + "/v1/workspaces", timeout=10)
            if a.status_code == 200 and b.status_code == 200:
                print("ready")
                return
        except requests.RequestException:
            pass
        time.sleep(3)
    raise SystemExit("agent did not become ready")


def assistant_turns(sid: str) -> list[dict[str, Any]]:
    # The messages API lists newest first.
    msgs = call("GET", f"/v1/sessions/{sid}/messages")["messages"]
    return [m for m in msgs if m["role"] == "assistant"]


def render(message: dict[str, Any]) -> str:
    out = [f"--- {message['id']} ({message.get('created_at', '')})"]
    for p in message.get("parts", []):
        kind = p.get("type")
        if kind == "text":
            out.append(f"TEXT: {p.get('text', '')}")
        elif kind == "tool_call":
            out.append(
                f"CALL {p.get('tool_name')}: {json.dumps(p.get('input'), default=str)[:600]}"
            )
        elif kind == "tool_result":
            content = p.get("content")
            text = content if isinstance(content, str) else json.dumps(content, default=str)
            err = p.get("error") or ""
            out.append(
                f"RESULT {p.get('tool_name')}{' ERROR ' + str(err) if err else ''}: {text[:900]}"
            )
        elif kind in {"error", "notice"}:
            out.append(f"{kind.upper()}: {json.dumps(p, default=str)[:600]}")
    meta = message.get("metadata") or {}
    if meta.get("error") or meta.get("details"):
        out.append(
            f"META: {json.dumps({k: meta.get(k) for k in ('error', 'details')}, default=str)[:800]}"
        )
    return "\n".join(out)


def wait_turn(sid: str, before: set[str], timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = call("GET", f"/v1/sessions/{sid}").get("status")
        fresh = [m for m in assistant_turns(sid) if m["id"] not in before]
        if fresh and status == "idle":
            return fresh[0]
        time.sleep(4)
    raise SystemExit(f"turn in {sid} did not settle within {timeout}s")


def show(sid: str, message: dict[str, Any], label: str) -> None:
    text = render(message)
    path = evidence(f"{label}.txt", text + "\n")
    print(text)
    print(f"[evidence] {path}")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ready")
    w = sub.add_parser("workspace")
    w.add_argument("name")
    s = sub.add_parser("session")
    s.add_argument("title")
    s.add_argument("--workspace", default="ws_default")
    s.add_argument("--agent", default="")
    s.add_argument("--add", nargs="*", default=[])
    s.add_argument("--approval", default="")
    y = sub.add_parser("say")
    y.add_argument("sid")
    y.add_argument("text")
    y.add_argument("--timeout", type=float, default=900)
    c = sub.add_parser("command")
    c.add_argument("sid")
    c.add_argument("name")
    c.add_argument("input")
    c.add_argument("--timeout", type=float, default=900)
    t = sub.add_parser("turn")
    t.add_argument("sid")
    for verb in ("get", "post", "patch", "delete"):
        v = sub.add_parser(verb)
        v.add_argument("path")
        if verb in ("post", "patch"):
            v.add_argument("body", nargs="?", default="{}")
    a = ap.parse_args()

    if a.cmd == "ready":
        ready()
    elif a.cmd == "workspace":
        root = RUN / f"ws-{a.name}"
        root.mkdir(parents=True, exist_ok=True)
        ws = call(
            "POST", "/v1/workspaces", json={"name": f"verify-{a.name}", "root_path": str(root)}
        )
        record("workspaces", ws["id"])
        print(ws["id"], root)
    elif a.cmd == "session":
        body = {"workspace_id": a.workspace, "title": a.title}
        if a.agent:
            body["agent"] = {"id": a.agent}
        sess = call("POST", "/v1/sessions", json=body)
        sid = sess["id"]
        record("sessions", sid)
        if a.add:
            call("PATCH", f"/v1/sessions/{sid}/agent", json={"add": a.add, "remove": []})
        if a.approval:
            call("PATCH", f"/v1/sessions/{sid}", json={"approval_mode": a.approval})
        print(sid)
    elif a.cmd == "say":
        before = {m["id"] for m in assistant_turns(a.sid)}
        body = {"parts": [{"type": "text", "text": a.text}]}
        accepted = call("POST", f"/v1/sessions/{a.sid}/messages", json=body, ok=(409, 422, 503))
        if isinstance(accepted, dict) and accepted.get("error"):
            print("REFUSED:", json.dumps(accepted)[:1500])
            return
        show(a.sid, wait_turn(a.sid, before, a.timeout), f"turn-{a.sid}-{int(time.time())}")
    elif a.cmd == "command":
        before = {m["id"] for m in assistant_turns(a.sid)}
        res = call(
            "POST",
            f"/v1/sessions/{a.sid}/commands/{a.name}",
            json={"input": a.input},
            ok=(403, 409, 422),
        )
        print("COMMAND:", json.dumps(res, default=str)[:800])
        if (res or {}).get("result", {}).get("type") == "turn":
            show(a.sid, wait_turn(a.sid, before, a.timeout), f"command-{a.sid}-{int(time.time())}")
    elif a.cmd == "turn":
        show(a.sid, assistant_turns(a.sid)[0], f"turn-{a.sid}-latest")
    else:
        body = json.loads(getattr(a, "body", "{}") or "{}") if a.cmd in ("post", "patch") else None
        r = S.request(a.cmd.upper(), BASE + a.path, json=body, timeout=120)
        print(f"HTTP {r.status_code}")
        try:
            print(json.dumps(r.json(), indent=2, default=str))
        except ValueError:
            print(r.text)


if __name__ == "__main__":
    sys.exit(main())
