#!/usr/bin/env python3
"""Kill Clio with SIGKILL during flow-heavy work and check that nothing acknowledged was lost.

The runtime model's durability rule (docs/design/install-and-startup-implementation.md):
every write -- messages, task events, mail, workflow state, session metadata -- is
committed to clio-core before it is acknowledged, so Clio can die at any moment and
lose nothing. This script tests that rule on a real server process:

1. It prepares a private store (clio-core, home, marketplace with a fan-out flow) the
   way ``scripts/create_reference_store.py`` does, and runs a real GACT server on it
   (``create_reference_store.py --serve``). Only the model is scripted: deterministic
   answers after a short think time, so turns, flow children and mail are in flight
   when the kill lands.
2. Each iteration starts flow-heavy work over HTTP -- fan-out conversations whose
   flow spawns child conversations, plain multi-turn chats, follow-ups sent while a
   conversation is busy (pending mail) and queued messages -- and records everything
   the API acknowledged: conversations created, messages accepted, the messages,
   tasks and flow runs the API reported back.
3. At a random point it sends SIGKILL to the server, restarts it on the same store,
   waits for ``ready`` and compares what the restarted server reports with what the
   API acknowledged before the kill. It then lets the interrupted work settle (the
   obligations lane resumes it) and compares again.

Findings are typed (``session_lost``, ``message_lost``, ``message_changed``,
``task_lost``, ``task_status_regressed``, ``flow_run_lost``, ``flow_outcome_lost``,
``mail_lost``, ``restart_failed``, ``not_settled``, ...) and written to a JSON report.
The exit code is 1 when anything acknowledged was lost.

Usage::

    uv run python scripts/crash_test.py --out /tmp/crash --iterations 5 --seed 7
    uv run python scripts/crash_test.py --out /tmp/crash --report crash.json --port 18991
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parent
REPO = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))

import create_reference_store as refstore  # noqa: E402

TERMINAL = {"completed", "failed", "cancelled", "interrupted", "task_state_uncertain"}


def prepare(out: Path, capacity: str, core_port: int = 0) -> dict[str, str]:
    """Create the private store directories, marketplace and environment.

    ``core_port`` pins the private clio-core's base port (it binds five from there);
    0 picks a free block.
    """
    for name in ("home", "runtime", "workspace"):
        (out / name).mkdir(parents=True, exist_ok=True)
    refstore._write_marketplace(out / "marketplace")
    env = refstore._environment(out, capacity)
    if core_port:
        env["CLIO_CORE_PORT"] = str(core_port)
    (out / "env.sh").write_text(
        "".join(f"export {key}={shlex.quote(value)}\n" for key, value in env.items()),
        encoding="utf-8",
    )
    return env


# --------------------------------------------------------------------------- #
# HTTP                                                                        #
# --------------------------------------------------------------------------- #


class Api:
    """A small JSON client; a refused connection or timeout is status 0."""

    def __init__(self, port: int) -> None:
        self.base = f"http://127.0.0.1:{port}/v1"

    def call(
        self, method: str, path: str, body: Any = None, timeout: float = 15.0
    ) -> tuple[int, Any]:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(  # noqa: S310 - loopback only
            self.base + path,
            data=data,
            method=method,
            headers={"content-type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
                return response.status, json.loads(response.read() or b"null")
        except urllib.error.HTTPError as error:
            try:
                return error.code, json.loads(error.read() or b"null")
            except ValueError:
                return error.code, None
        except (urllib.error.URLError, OSError, ValueError):
            return 0, None

    def messages(self, sid: str) -> list[dict[str, Any]] | None:
        rows: list[dict[str, Any]] = []
        path = f"/sessions/{sid}/messages"
        while True:
            status, body = self.call("GET", path, timeout=30)
            if status != 200 or not isinstance(body, dict):
                return None
            rows.extend(body.get("messages", []))
            cursor = body.get("next_cursor")
            if not cursor:
                return rows
            path = f"/sessions/{sid}/messages?before={cursor}"

    def tasks(self, sid: str) -> list[dict[str, Any]] | None:
        rows: list[dict[str, Any]] = []
        path = f"/sessions/{sid}/tasks/query?limit=200"
        while True:
            status, body = self.call("GET", path, timeout=30)
            if status != 200 or not isinstance(body, dict):
                return None
            rows.extend(body.get("tasks", []))
            cursor = body.get("cursor")
            if not cursor:
                return rows
            path = f"/sessions/{sid}/tasks/query?limit=200&cursor={cursor}"


def _text(message: dict[str, Any]) -> str:
    return "".join(str(part.get("text", "")) for part in message.get("parts", []))


# --------------------------------------------------------------------------- #
# The server under test                                                       #
# --------------------------------------------------------------------------- #


class RestartFailed(RuntimeError):
    """The server did not come back to ``ready`` on the store it was killed on."""

    def __init__(self, message: str, boot: dict[str, Any] | None = None, log: str = "") -> None:
        super().__init__(message)
        self.boot, self.log = boot or {}, log


class Server:
    """One server process at a time on the store; killed with SIGKILL and restarted."""

    def __init__(self, args: argparse.Namespace, env: dict[str, str]) -> None:
        self.args, self.env = args, env
        self.process: subprocess.Popen[bytes] | None = None
        self.starts = 0
        self.leftovers: list[dict[str, Any]] = []

    def core_pid(self) -> int | None:
        path = self.args.out / "runtime" / "clio-runtime.pid"
        try:
            return int(path.read_text(encoding="utf-8").split()[0])
        except (OSError, ValueError, IndexError):
            return None

    def start(self) -> dict[str, Any]:
        """Start the server and wait for ``task_runtime_ready``; return the boot marks."""
        self.starts += 1
        log = (self.args.out / f"server-{self.starts:02d}.log").open("w", encoding="utf-8")
        started = time.monotonic()
        self.process = subprocess.Popen(  # noqa: S603 - this script, fixed argv
            [
                sys.executable,
                "-B",
                str(SCRIPTS / "create_reference_store.py"),
                "--serve",
                "--out",
                str(self.args.out),
                "--port",
                str(self.args.port),
                "--think",
                str(self.args.think),
                "--answer-kb",
                "1",
                "--lm-call-kb",
                "4",
            ],
            env={**os.environ, **self.env},
            cwd=self.args.out / "workspace",
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        log.close()
        api = Api(self.args.port)
        deadline = started + self.args.ready_timeout
        boot: dict[str, Any] = {}
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RestartFailed(
                    f"server exited with {self.process.returncode} before ready "
                    f"(server-{self.starts:02d}.log)",
                    boot,
                    self._log_tail(),
                )
            status, body = api.call("GET", "/health", timeout=5)
            boot = (body or {}).get("boot", {}) if status == 200 else boot
            if "task_runtime_ready" in boot.get("marks", {}):
                return {"ready_s": round(time.monotonic() - started, 2), "marks": boot["marks"]}
            time.sleep(0.25)
        self.process.send_signal(signal.SIGUSR1)  # the server dumps its thread stacks
        time.sleep(2)
        raise RestartFailed(
            f"not ready within {self.args.ready_timeout}s (server-{self.starts:02d}.log)",
            boot,
            self._log_tail(),
        )

    def _log_tail(self, lines: int = 40) -> str:
        path = self.args.out / f"server-{self.starts:02d}.log"
        try:
            rows = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return ""
        return "\n".join(row for row in rows if "GET /v1/health" not in row)[-20000:]

    def kill9(self, *, core: bool) -> dict[str, Any]:
        """SIGKILL the server (and with ``core`` its clio-core daemon) like a crash."""
        import psutil  # noqa: PLC0415

        assert self.process is not None
        core_pid = self.core_pid()
        try:
            descendants = psutil.Process(self.process.pid).children(recursive=True)
        except psutil.NoSuchProcess:
            descendants = []
        os.kill(self.process.pid, signal.SIGKILL)
        self.process.wait()
        killed_core = False
        others = []
        for child in descendants:
            try:
                if child.pid == core_pid or child.name().startswith("clio_run"):
                    if core:
                        child.kill()
                        killed_core = True
                    continue
                # A crash leaves these orphaned; the harness owns them, so it ends them.
                others.append({"pid": child.pid, "name": child.name()})
                child.kill()
            except psutil.NoSuchProcess:
                continue
        if others:
            self.leftovers.append({"start": self.starts, "processes": others})
        self.process = None
        return {"core_killed": killed_core}

    def stop(self) -> dict[str, Any]:
        """Stop the server gracefully (it stops clio-core); report a forced stop."""
        if self.process is None:
            return {"stopped": "not running"}
        started = time.monotonic()
        self.process.send_signal(signal.SIGTERM)
        try:
            self.process.wait(timeout=60)
            forced = False
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
            forced = True
        self.process = None
        return {"shutdown_s": round(time.monotonic() - started, 2), "forced": forced}


def _stop_leftover_core(out: Path, env: dict[str, str]) -> bool:
    """Stop a clio-core daemon left behind (after a failed restart); report whether one was."""
    import measure_startup  # noqa: PLC0415

    return bool(measure_startup._stop_leftover_daemon(env, out))


# --------------------------------------------------------------------------- #
# What the API acknowledged                                                   #
# --------------------------------------------------------------------------- #


@dataclass
class Ledger:
    """Everything the API acknowledged, across every iteration (the store accumulates)."""

    sessions: dict[str, str] = field(default_factory=dict)  # created (201): sid -> title
    children: dict[str, str] = field(default_factory=dict)  # listed child sid -> root sid
    accepted: dict[str, dict[str, Any]] = field(default_factory=dict)  # POST /messages 202
    queued: dict[str, dict[str, Any]] = field(default_factory=dict)  # POST queued 201
    messages: dict[str, dict[str, Any]] = field(default_factory=dict)  # reported messages
    tasks: dict[str, dict[str, Any]] = field(default_factory=dict)  # reported tasks
    flows: dict[str, dict[str, Any]] = field(default_factory=dict)  # reported runs by run id
    latest: dict[str, str] = field(default_factory=dict)  # root task -> its latest run id
    mail: dict[str, dict[str, Any]] = field(default_factory=dict)  # reported pending mail
    lock: threading.Lock = field(default_factory=threading.Lock)

    def counts(self) -> dict[str, int]:
        return {
            name: len(getattr(self, name))
            for name in (
                "sessions",
                "children",
                "accepted",
                "queued",
                "messages",
                "tasks",
                "flows",
                "mail",
            )
        }


def observe_session(api: Api, ledger: Ledger, sid: str, root: str) -> dict[str, Any] | None:
    """Read one conversation's messages, tasks, flow runs and mail into the ledger."""
    messages = api.messages(sid)
    if messages is None:
        return None
    with ledger.lock:
        for row in messages:
            ledger.messages[row["id"]] = {
                "sid": sid,
                "role": row.get("role"),
                "text": _text(row),
            }
    if sid != root:
        return {"messages": len(messages)}
    tasks = api.tasks(sid)
    if tasks is None:
        return None
    flows = {}
    for task in tasks:
        owner = task.get("owner") or {}
        with ledger.lock:
            ledger.tasks[task["handle"]] = {
                "sid": owner.get("session_id"),
                "root": root,
                "status": task.get("status"),
                "terminal": bool(task.get("terminal_at")),
            }
            child = owner.get("session_id")
            if child and child != root:
                ledger.children.setdefault(child, root)
        if task.get("kind") == "Session" and owner.get("session_id") == root:
            status, run = api.call("GET", f"/sessions/{sid}/tasks/{task['handle']}/flow")
            if status == 200 and isinstance(run, dict):
                flows[str(run.get("run_id"))] = {
                    "sid": sid,
                    "handle": task["handle"],
                    "status": run.get("status"),
                    "result": str(run.get("result") or ""),
                    "calls": {
                        row["handle"]: row.get("status")
                        for row in run.get("calls", [])
                        if row.get("handle")
                    },
                }
    status, mailbox = api.call("GET", f"/sessions/{sid}/tasks/mailbox")
    with ledger.lock:
        ledger.flows.update(flows)
        ledger.latest.update({row["handle"]: run_id for run_id, row in flows.items()})
        if status == 200 and isinstance(mailbox, dict):
            for item in mailbox.get("pending", []):
                ledger.mail.setdefault(
                    item["id"], {"sid": sid, "kind": item.get("kind"), "handle": item.get("handle")}
                )
    pending = mailbox.get("pending", []) if status == 200 and isinstance(mailbox, dict) else []
    return {
        "messages": len(messages),
        "tasks": len(tasks),
        "flows": len(flows),
        "mail": len(pending),
    }


def snapshot(api: Api, ledger: Ledger, roots: list[str]) -> dict[str, int]:
    """One observer pass over the roots and their known children; returns what was busy."""
    busy = {"busy_sessions": 0, "active_tasks": 0, "pending_mail": 0, "running_flows": 0}
    for root in roots:
        seen = observe_session(api, ledger, root, root) or {}
        busy["pending_mail"] += seen.get("mail", 0)
    with ledger.lock:
        children = [sid for sid, root in ledger.children.items() if root in roots]
    for child in children:
        observe_session(api, ledger, child, ledger.children[child])
    status, body = api.call("GET", "/sessions?limit=100000", timeout=30)
    if status == 200 and isinstance(body, dict):
        rows = body.get("sessions", body.get("items", []))
        busy["busy_sessions"] = sum(
            1 for row in rows if row.get("status") in {"running", "busy", "queued", "stopping"}
        )
    with ledger.lock:
        busy["active_tasks"] = sum(
            1 for task in ledger.tasks.values() if task["root"] in roots and not task["terminal"]
        )
        busy["running_flows"] = sum(
            1
            for run_id in ledger.latest.values()
            if ledger.flows[run_id]["sid"] in roots
            and ledger.flows[run_id]["status"] not in TERMINAL
        )
    return busy


# --------------------------------------------------------------------------- #
# Verification                                                                #
# --------------------------------------------------------------------------- #

#: A finding of these types means acknowledged data was lost or changed.
LOSS = {
    "session_lost",
    "session_changed",
    "child_session_lost",
    "message_lost",
    "message_changed",
    "mail_lost",
    "queued_message_lost",
    "task_lost",
    "task_status_regressed",
    "flow_run_lost",
    "flow_outcome_lost",
    "flow_call_lost",
    "flow_call_regressed",
    "restart_failed",
}
#: These mean the restarted server did not finish the interrupted work on its own.
LIVENESS = {"not_settled", "mail_stuck", "unreadable", "transiently_missing"}


def verify(api: Api, ledger: Ledger, phase: str) -> list[dict[str, Any]]:
    """Compare what the server reports now with everything the API acknowledged."""
    findings: list[dict[str, Any]] = []
    settled = phase == "settled"

    def add(kind: str, **detail: Any) -> None:
        findings.append({"type": kind, "phase": phase, **detail})

    with ledger.lock:
        sessions = dict(ledger.sessions)
        children = dict(ledger.children)
        accepted = dict(ledger.accepted)
        queued = dict(ledger.queued)
        messages = dict(ledger.messages)
        tasks = dict(ledger.tasks)
        flows = dict(ledger.flows)
        mail = dict(ledger.mail)

    for sid, title in sessions.items():
        status, body = api.call("GET", f"/sessions/{sid}")
        if status == 404:
            add("session_lost", session=sid, title=title)
        elif status != 200:
            add("unreadable", what="session", session=sid, status=status)
        elif body.get("title") != title:
            add("session_changed", session=sid, before=title, after=body.get("title"))
        elif settled and body.get("status") in {"running", "busy", "queued", "stopping"}:
            add("not_settled", what="session", session=sid, status=body.get("status"))
    for sid, root in children.items():
        status, body = api.call("GET", f"/sessions/{sid}")
        if status == 404:
            add("child_session_lost", session=sid, root=root)
        elif status != 200:
            add("unreadable", what="child session", session=sid, status=status)
        elif settled and body.get("status") in {"running", "busy", "queued", "stopping"}:
            add("not_settled", what="child session", session=sid, status=body.get("status"))

    by_sid: dict[str, list[str]] = {}
    for mid, row in [*accepted.items(), *messages.items()]:
        by_sid.setdefault(row["sid"], []).append(mid)
    for row in queued.values():
        by_sid.setdefault(row["sid"], [])
    for sid in by_sid:
        waiting = [qid for qid, row in queued.items() if row["sid"] == sid]
        held: set[str] = set()
        if waiting:
            # The queue first: a promotion between the two reads then shows as a message.
            status, body = api.call("GET", f"/sessions/{sid}/queued-messages")
            held = {row["id"] for row in (body or {}).get("queued_messages", [])}
            if status != 200:
                add("unreadable", what="queued messages", session=sid, status=status)
        rows = api.messages(sid)
        if rows is None:
            add("unreadable", what="messages", session=sid)
            continue
        now = {row["id"]: row for row in rows}
        client_ids = {(row.get("metadata") or {}).get("client_message_id") for row in rows}
        for mid, row in accepted.items():
            if row["sid"] == sid and mid not in now:
                kind = "mail_lost" if row["delivery"] != "start" else "message_lost"
                add(kind, session=sid, message=mid, delivery=row["delivery"], text=row["text"])
        for mid, row in messages.items():
            if row["sid"] != sid:
                continue
            current = now.get(mid)
            if current is None:
                add("message_lost", session=sid, message=mid, role=row["role"], reported=True)
            elif current.get("role") != row["role"] or (
                row["text"] and _text(current) != row["text"]
            ):
                add(
                    "message_changed",
                    session=sid,
                    message=mid,
                    role=row["role"],
                    before_chars=len(row["text"]),
                    after_chars=len(_text(current)),
                )
        if settled:
            # The authoritative pending state (a message's ``pending_steer`` flag is display).
            status, body = api.call("GET", f"/sessions/{sid}/pending-steers")
            for row in (body or {}).get("pending_steers", []) if status == 200 else []:
                add("mail_stuck", session=sid, message=row.get("id"), what="pending steer")
        if waiting:
            for qid in waiting:
                if qid not in held and queued[qid]["client_message_id"] not in client_ids:
                    add("queued_message_lost", session=sid, queued=qid)
                elif settled and qid in held:
                    add("mail_stuck", session=sid, queued=qid, what="queued message")

    roots = sorted({row["root"] for row in tasks.values()})
    for root in roots:
        rows = api.tasks(root)
        if rows is None:
            add("unreadable", what="tasks", session=root)
            continue
        now = {row["handle"]: row for row in rows}
        for handle, row in tasks.items():
            if row["root"] != root:
                continue
            current = now.get(handle)
            if current is None:
                add("task_lost", session=row["sid"], root=root, task=handle, status=row["status"])
                continue
            terminal = bool(current.get("terminal_at"))
            if row["terminal"] and (not terminal or current.get("status") != row["status"]):
                add(
                    "task_status_regressed",
                    task=handle,
                    root=root,
                    before=row["status"],
                    after=current.get("status"),
                )
            elif settled and not terminal:
                add(
                    "not_settled",
                    what="task",
                    task=handle,
                    root=root,
                    status=current.get("status"),
                    activity=current.get("activity"),
                )

    trees: dict[str, dict[str, dict[str, Any]]] = {}
    for handle in sorted({row["handle"] for row in flows.values()}):
        runs = {run_id: row for run_id, row in flows.items() if row["handle"] == handle}
        sid = next(iter(runs.values()))["sid"]
        status, run = api.call("GET", f"/sessions/{sid}/tasks/{handle}/flow")
        if status == 404:
            for run_id, row in runs.items():
                add("flow_run_lost", session=sid, task=handle, run=run_id, before=row["status"])
            continue
        if status != 200 or not isinstance(run, dict):
            add("unreadable", what="flow run", task=handle, status=status)
            continue
        for run_id, row in runs.items():
            if run_id == run.get("run_id"):
                current = {
                    r["handle"]: r.get("status") for r in run.get("calls", []) if r.get("handle")
                }
                if row["status"] == "completed" and (
                    run.get("status") != "completed"
                    or str(run.get("result") or "") != row["result"]
                ):
                    add(
                        "flow_outcome_lost",
                        session=sid,
                        task=handle,
                        run=run_id,
                        before=row["status"],
                        after=run.get("status"),
                    )
            else:
                # A later run of the same conversation supersedes this one in the flow
                # view; the earlier run's calls must still be tasks in the conversation.
                if sid not in trees:
                    trees[sid] = {r["handle"]: r for r in api.tasks(sid) or []}
                current = {h: trees[sid][h].get("status") for h in row["calls"] if h in trees[sid]}
            for call, before in row["calls"].items():
                if call not in current:
                    add(
                        "flow_call_lost",
                        session=sid,
                        task=handle,
                        run=run_id,
                        call=call,
                        before=before,
                        superseded=run_id != run.get("run_id"),
                    )
                elif before == "completed" and current[call] != "completed":
                    add(
                        "flow_call_regressed",
                        session=sid,
                        task=handle,
                        run=run_id,
                        call=call,
                        after=current[call],
                    )
        if settled and run.get("status") not in TERMINAL:
            add("not_settled", what="flow run", task=handle, status=run.get("status"))

    if settled:
        pending: dict[str, set[str]] = {}
        for item_id, row in mail.items():
            if row["sid"] not in pending:
                status, body = api.call("GET", f"/sessions/{row['sid']}/tasks/mailbox")
                pending[row["sid"]] = {
                    item["id"] for item in (body or {}).get("pending", []) if status == 200
                }
            if item_id in pending[row["sid"]]:
                add("mail_stuck", session=row["sid"], mail=item_id, kind=row["kind"])
    return findings


def _key(finding: dict[str, Any]) -> str:
    return json.dumps({k: v for k, v in finding.items() if k != "phase"}, sort_keys=True)


def confirmed(api: Api, ledger: Ledger, phase: str, recheck_s: float = 3.0) -> list[dict[str, Any]]:
    """Verify, and re-verify a loss once: the restarted server is moving work concurrently.

    A loss that is gone on the second look was only invisible for a moment (for example a
    queued message between its queue entry and its transcript row); it is reported as
    ``transiently_missing``, not as a loss.
    """
    findings = verify(api, ledger, phase)
    if not any(finding["type"] in LOSS for finding in findings):
        return findings
    time.sleep(recheck_s)
    again = {_key(finding) for finding in verify(api, ledger, phase)}
    return [
        finding
        if finding["type"] not in LOSS or _key(finding) in again
        else {**finding, "type": "transiently_missing", "as": finding["type"]}
        for finding in findings
    ]


# --------------------------------------------------------------------------- #
# Flow-heavy work                                                             #
# --------------------------------------------------------------------------- #


class Workload:
    """Starts fan-outs and chats, then keeps sending follow-ups, steers and queued mail."""

    def __init__(self, api: Api, ledger: Ledger, rng: random.Random, iteration: int) -> None:
        self.api, self.ledger, self.rng, self.iteration = api, ledger, rng, iteration
        self.roots: list[str] = []
        self.stop = threading.Event()
        self.sent = 0

    def _session(self, agent: str, title: str) -> str | None:
        status, body = self.api.call(
            "POST", "/sessions", {"title": title, "metadata": {"agent_ref": agent}}
        )
        if status not in (200, 201) or not isinstance(body, dict):
            return None
        with self.ledger.lock:
            self.ledger.sessions[body["id"]] = title
        self.roots.append(body["id"])
        return str(body["id"])

    def _post(self, sid: str, text: str) -> None:
        status, body = self.api.call(
            "POST", f"/sessions/{sid}/messages", {"parts": [{"type": "text", "text": text}]}
        )
        if status == 202 and isinstance(body, dict):
            with self.ledger.lock:
                self.ledger.accepted[body["message_id"]] = {
                    "sid": sid,
                    "text": text,
                    "delivery": body.get("delivery"),
                    "state": body.get("state"),
                }
            self.sent += 1

    def _queue(self, sid: str, text: str) -> None:
        client_id = f"crash-{self.iteration}-{self.rng.getrandbits(48):012x}"
        status, body = self.api.call(
            "POST",
            f"/sessions/{sid}/queued-messages",
            {"parts": [{"type": "text", "text": text}], "client_message_id": client_id},
        )
        if status == 201 and isinstance(body, dict):
            with self.ledger.lock:
                self.ledger.queued[body["id"]] = {
                    "sid": sid,
                    "text": text,
                    "client_message_id": client_id,
                }
            self.sent += 1

    def run(self, flows: int, chats: int) -> None:
        tag = f"i{self.iteration}"
        for index in range(flows):
            if self.stop.is_set():
                return
            sid = self._session("refstore-fanout", f"crash {tag} fan-out {index}")
            if sid:
                self._post(sid, f"{self.rng.randint(2, 4)} survey {tag}-{index}")
        for index in range(chats):
            if self.stop.is_set():
                return
            sid = self._session("refstore-chat", f"crash {tag} chat {index}")
            if sid:
                self._post(sid, f"chat {tag}-{index} question 0")
        step = 0
        while not self.stop.wait(self.rng.uniform(0.1, 0.5)) and self.roots:
            step += 1
            sid = self.rng.choice(self.roots)
            action = self.rng.random()
            if action < 0.55:  # a follow-up: a steer (mail) when the conversation is busy
                self._post(sid, f"follow-up {tag}-{step}")
            elif action < 0.8:
                self._queue(sid, f"queued {tag}-{step}")
            else:  # another fan-out round in the same conversation
                self._post(sid, f"{self.rng.randint(2, 3)} again {tag}-{step}")


def _observe_until(
    api: Api, ledger: Ledger, roots: list[str], stop: threading.Event, last: dict[str, Any]
) -> None:
    while not stop.is_set():
        try:
            busy = snapshot(api, ledger, roots)
        except Exception as error:  # noqa: BLE001 - the server may die mid-request
            last["error"] = repr(error)
            continue
        last.update(busy, at=time.monotonic())


def probe_in_flight(api: Api, ledger: Ledger, roots: list[str]) -> dict[str, int]:
    """A quick look, just before the kill, at what is running (the observer pass may be old).

    Counts busy conversations and the flow runs of this iteration's conversations that
    are still running; each read is bounded so the kill is not delayed by a slow server.
    """
    seen = {"busy_sessions": 0, "running_flows": 0}
    status, body = api.call("GET", "/sessions?limit=100000", timeout=3)
    if status == 200 and isinstance(body, dict):
        seen["busy_sessions"] = sum(
            1
            for row in body.get("sessions", body.get("items", []))
            if row.get("status") in {"running", "busy", "queued", "stopping"}
        )
    with ledger.lock:
        handles = {
            handle: task["sid"]
            for handle, task in ledger.tasks.items()
            if task["sid"] in roots and task["root"] == task["sid"]
        }
    for handle, sid in handles.items():
        status, run = api.call("GET", f"/sessions/{sid}/tasks/{handle}/flow", timeout=3)
        if status == 200 and isinstance(run, dict) and run.get("status") not in TERMINAL:
            seen["running_flows"] += 1
    return seen


def _kill_point(busy: dict[str, Any]) -> list[str]:
    points = []
    if busy.get("running_flows"):
        points.append("mid-workflow")
    if busy.get("pending_mail"):
        points.append("mid-mail")
    if busy.get("busy_sessions") or busy.get("active_tasks"):
        points.append("mid-turn")
    return points or ["idle"]


def settle(api: Api, ledger: Ledger, timeout: float) -> dict[str, Any]:
    """Wait until every conversation is idle and every reported task and flow is terminal."""
    started = time.monotonic()
    with ledger.lock:
        roots = list(ledger.sessions)
    busy: dict[str, Any] = {}
    while time.monotonic() - started < timeout:
        busy = snapshot(api, ledger, roots)
        with ledger.lock:
            open_tasks = sum(1 for task in ledger.tasks.values() if not task["terminal"])
            open_flows = sum(
                1
                for run_id in ledger.latest.values()
                if ledger.flows[run_id]["status"] not in TERMINAL
            )
        if not busy["busy_sessions"] and not open_tasks and not open_flows:
            return {"settled": True, "seconds": round(time.monotonic() - started, 1)}
        time.sleep(1.0)
    return {
        "settled": False,
        "seconds": round(time.monotonic() - started, 1),
        "still_busy": busy,
    }


# --------------------------------------------------------------------------- #
# The test                                                                    #
# --------------------------------------------------------------------------- #


def iteration(
    args: argparse.Namespace, server: Server, ledger: Ledger, rng: random.Random, number: int
) -> dict[str, Any]:
    """Run work, kill the server at a random point, restart it and check the store."""
    api = Api(args.port)
    work = Workload(api, ledger, rng, number)
    kill_after = round(rng.uniform(args.kill_min, args.kill_max), 2)
    last: dict[str, Any] = {}
    stop_observer = threading.Event()
    worker = threading.Thread(target=work.run, args=(args.flows, args.chats), daemon=True)
    observer = threading.Thread(
        target=_observe_until, args=(api, ledger, work.roots, stop_observer, last), daemon=True
    )
    started = time.monotonic()
    worker.start()
    observer.start()
    time.sleep(kill_after)
    in_flight = {k: v for k, v in last.items() if k not in {"at", "error"}}
    in_flight.update(probe_in_flight(api, ledger, list(work.roots)))
    observed_age = round(time.monotonic() - last["at"], 2) if "at" in last else None
    core_kill = args.kill_core or (args.kill_core_every and number % args.kill_core_every == 0)
    killed = server.kill9(core=bool(core_kill))
    killed_at = round(time.monotonic() - started, 2)
    work.stop.set()
    stop_observer.set()
    worker.join(timeout=60)
    observer.join(timeout=60)
    record: dict[str, Any] = {
        "iteration": number,
        "kill_after_s": kill_after,
        "killed_at_s": killed_at,
        "kill_points": _kill_point(in_flight),
        "in_flight_at_kill": in_flight,
        "last_observation_age_s": observed_age,
        "acknowledged": {"requests_this_iteration": work.sent, **ledger.counts()},
        **killed,
    }
    try:
        record["restart"] = server.start()
    except RestartFailed as error:
        record["findings"] = [
            {
                "type": "restart_failed",
                "phase": "restart",
                "error": str(error),
                "boot": error.boot,
                "log_tail": error.log,
            }
        ]
        return record
    findings = confirmed(api, ledger, "restart")
    record["settle"] = settle(api, ledger, args.settle_timeout)
    findings += confirmed(api, ledger, "settled")
    record["findings"] = findings
    return record


def run(args: argparse.Namespace) -> dict[str, Any]:
    out = args.out
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"{out} is not empty; pass a new directory")
    env = prepare(out, args.capacity, args.core_port)
    rng = random.Random(args.seed)
    ledger = Ledger()
    server = Server(args, env)
    report: dict[str, Any] = {
        "seed": args.seed,
        "think_s": args.think,
        "flows_per_iteration": args.flows,
        "chats_per_iteration": args.chats,
        "kill_window_s": [args.kill_min, args.kill_max],
        "store": str(out),
        "iterations": [],
    }
    started = time.monotonic()
    try:
        report["first_start"] = server.start()
        for number in range(1, args.iterations + 1):
            record = iteration(args, server, ledger, rng, number)
            report["iterations"].append(record)
            print(json.dumps(_brief(record)), flush=True)
            if any(f["type"] == "restart_failed" for f in record["findings"]):
                break
    finally:
        report["final_stop"] = server.stop()
        report["core_left_running"] = _stop_leftover_core(out, env)
        report["orphans_ended"] = server.leftovers
    findings = [f for record in report["iterations"] for f in record.get("findings", [])]
    by_type: dict[str, int] = {}
    for finding in findings:
        by_type[finding["type"]] = by_type.get(finding["type"], 0) + 1
    report["summary"] = {
        "iterations": len(report["iterations"]),
        "seconds": round(time.monotonic() - started, 1),
        "acknowledged": ledger.counts(),
        "kill_points": sorted({p for r in report["iterations"] for p in r["kill_points"]}),
        "findings_by_type": by_type,
        "lost": sum(n for kind, n in by_type.items() if kind in LOSS),
        "liveness": sum(n for kind, n in by_type.items() if kind in LIVENESS),
        "max_restart_ready_s": max(
            (r["restart"]["ready_s"] for r in report["iterations"] if "restart" in r),
            default=None,
        ),
    }
    return report


def _brief(record: dict[str, Any]) -> dict[str, Any]:
    kinds: dict[str, int] = {}
    for finding in record.get("findings", []):
        kinds[finding["type"]] = kinds.get(finding["type"], 0) + 1
    return {
        "iteration": record["iteration"],
        "killed_at_s": record["killed_at_s"],
        "kill_points": record["kill_points"],
        "in_flight": record["in_flight_at_kill"],
        "ready_s": record.get("restart", {}).get("ready_s"),
        "settle": record.get("settle"),
        "findings": kinds,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, required=True, help="new directory for the store")
    parser.add_argument("--report", type=Path, help="write the JSON report here")
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--seed", type=int, default=None, help="random seed (default: random)")
    parser.add_argument("--port", type=int, default=18991)
    parser.add_argument(
        "--core-port", type=int, default=0, help="private clio-core base port (0: a free block)"
    )
    parser.add_argument("--flows", type=int, default=3, help="fan-outs started per iteration")
    parser.add_argument("--chats", type=int, default=2, help="chats started per iteration")
    parser.add_argument("--think", type=float, default=0.5, help="scripted model seconds")
    parser.add_argument("--kill-min", type=float, default=1.0)
    parser.add_argument("--kill-max", type=float, default=12.0)
    parser.add_argument(
        "--kill-core", action="store_true", help="also SIGKILL clio-core every iteration"
    )
    parser.add_argument(
        "--kill-core-every", type=int, default=0, help="also SIGKILL clio-core every Nth iteration"
    )
    parser.add_argument("--ready-timeout", type=float, default=300.0)
    parser.add_argument("--settle-timeout", type=float, default=180.0)
    parser.add_argument("--capacity", default="256MB", help="clio-core file-tier capacity")
    parser.add_argument(
        "--keep", action="store_true", help="keep the store and server logs (kept on a loss)"
    )
    parser.add_argument(
        "--fail-on-liveness",
        action="store_true",
        help="also exit 1 when interrupted work does not settle after restart",
    )
    args = parser.parse_args()
    args.out = args.out.resolve()
    if args.seed is None:
        args.seed = random.SystemRandom().randrange(1 << 30)
    report = run(args)
    text = json.dumps(report, indent=2)
    if args.report:
        args.report.write_text(text + "\n", encoding="utf-8")
    print(json.dumps(report["summary"], indent=2))
    if args.keep or report["summary"]["lost"]:
        print(f"store and server logs kept in {args.out}", file=sys.stderr)
    else:
        shutil.rmtree(args.out, ignore_errors=True)
    failed = report["summary"]["lost"] or (args.fail_on_liveness and report["summary"]["liveness"])
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
