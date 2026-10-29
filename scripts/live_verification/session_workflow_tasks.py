"""Actual-model coordination between distinct agents, with disposable run state.

Run only on the qualified isolated Linux Core candidate. Register the exact
--root, --report and service port in the operational ledger before launching.
The report keeps bounded identities and verdicts; transcripts, credentials,
processes and the private Core allocation are removed in finally.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
import traceback
from importlib.metadata import version
from pathlib import Path
from typing import Any

import _common as common
import _lifecycle_evidence as evidence
import psutil
from _lifecycle_auth import frozen_claude_auth
from _session_workflow_receipts import failure_receipts, model_receipt, provider_failure_receipt
from agent_async_tasks import frozen_codex_auth

CORE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(CORE))
from tests._cte_isolation import eagerly_attach_private_daemon, isolate_cte_env
from tests._process_hygiene import release_this_process_client

INTENT = "Human requested coordination between these distinct workflows"
SCOPE = f"scope='workspace',user_intent={INTENT!r}"


def stop_identity(identity: tuple[int, float]) -> bool:
    """Settle only a captured owned PID/birth pair, never a reused PID."""
    try:
        process = psutil.Process(identity[0])
        if process.create_time() != identity[1]:
            return True
        process.terminate()
        try:
            process.wait(5)
        except psutil.TimeoutExpired:
            process.kill()
            process.wait(5)
        return not process.is_running()
    except psutil.NoSuchProcess:
        return True


def testing_agent(call: Any, workspace: Path, wid: str, role: str) -> str:
    """Install an independent testing agent for ``role``; return its agent ref.

    Each role is its own marketplace and agent id, from the ``task-testing``
    template (``core``, ``workspace``, ``shell``; no delegation).
    """
    pack = common.materialize_testing_marketplace(
        CORE / "scripts/live_verification/agents/task-testing",
        workspace,
        {},
        name=f"workflow-{role}",
        agent_id=f"session-task-workflow-{role}",
    )
    prompt = common.testing_prompt(pack)
    header = prompt.read_text().split("---", 2)[1]
    prompt.write_text(
        "---" + header + "---\n"
        "Perform only the supplied human qualification mission yourself. Do not delegate.\n"
        "Execute requested tools, inspect their actual observations, and report honestly.\n"
        "Use only each tool's declared arguments. If input validation rejects a call before "
        "execution, correct its arguments and retry that unaccepted call. Never retry an "
        "accepted operation or a permission denial.\n"
        "On automatic task/mail wakes, report the newly supplied handles, statuses and text; "
        "do not spawn work or send acknowledgements unless the original mission authorizes it.\n"
        "When authorized to reply, send one ACK to the exact source session, preserving "
        "reply_to=message_id and correlation_id. Never reply to ACK messages.\n"
        "Use complete fenced tool_calls syntax when the provider uses that protocol.\n"
    )
    return common.install_testing_agent(call, pack)


def model_calls(call: Any, root: Path, sid: str, text: str, names: list[str]) -> list[Any]:
    """Require new production invocations, then the model's actual settled response."""
    before = {
        row["payload"]["call_id"] for name in names for row in evidence.tool_events(root, sid, name)
    }
    common.post_message(call, sid, text)
    result = []
    for name in names:
        event = evidence.require(
            lambda name=name: next(
                (
                    row
                    for row in evidence.tool_events(root, sid, name)
                    if row["payload"]["call_id"] not in before
                ),
                None,
            ),
            f"model {sid} invoked {name}",
        )
        result.append(event)
        before.add(event["payload"]["call_id"])
    evidence.require(lambda: evidence.settled_turn(call, sid), f"model {sid} settled")
    return result


def start_held(call: Any, root: Path, sid: str, tag: str, *, failure: bool = False) -> Any:
    """Have the actual model submit a real held process and commit to its task wait."""
    workspace = root / "workspace"
    paths = evidence.worker_files(workspace, tag, tag + "-result")
    if failure:
        with paths["script"].open("a") as stream:
            stream.write("raise SystemExit(7)\n")
    previous = len(evidence.tool_events(root, sid, "wait_tasks", complete=False))
    accepted = common.post_message(
        call,
        sid,
        f"Assignment {tag}. Start exactly one shell_bash(command="
        f"{common.quoted_command(sys.executable, str(paths['script']))!r},background=true,"
        f"timeout_s=0,cwd={str(workspace)!r}). Then wait_tasks on its returned handle "
        "without timeout. After the wait, read the sentinel file and report the actual outcome. "
        f"Human authorizes one reply to each coordination message using message_agent with {SCOPE}, "
        "exact source session, reply_to its message_id and original correlation_id. Never ACK an ACK. "
        "On later automatic task results report them once, without new task submissions.",
    )
    evidence.require(
        lambda: len(evidence.tool_events(root, sid, "wait_tasks", complete=False)) > previous,
        "peer model entered actual task wait",
    )
    row = evidence.require(
        lambda: next(
            (
                r
                for r in evidence.tasks(call, sid)
                if r["task_kind"] == "Shell" and r["effective_status"] == "running"
            ),
            None,
        ),
        "accepted shell still running",
    )
    assert not paths["done"].exists()
    return accepted["task_handle"], row["handle"], paths


def notice(call: Any, sid: str, handle: str) -> Any:
    """Read exactly the subscriber's durable completion message."""
    return next(
        (
            r
            for r in evidence.mail(call, sid)
            if r.get("source_agent") == "task-supervisor" and r.get("correlation_id") == handle
        ),
        None,
    )


def cycle(call: Any, root: Path, wid: str, agents: list[str], number: int) -> Any:
    """Qualify mixed runs, mail, failed outcomes and Stop using actual model bridges."""
    a = common.create_session(call, wid, f"Observer workflow {number}")
    b = common.create_session(call, wid, f"Producer workflow {number}")
    for sid, pack in zip((a, b), agents, strict=True):
        common.use_agent(call, sid, pack)
    assert agents[0] != agents[1]
    run, shell, paths = start_held(call, root, b, f"workflow-{number}-busy")
    marker = "PEER_BUSY_" + secrets.token_hex(8)
    records = model_calls(
        call,
        root,
        a,
        f"The human authorizes coordinating the independent producer workflow {b}. "
        f"Call query_tasks(kind='Session',{SCOPE}) and identify its running handle {run}. "
        f"Call observe_tasks(tasks={[run, shell]!r},notify=true,{SCOPE}). "
        f"Call wait_tasks(tasks={[run, shell]!r},return_when='any',timeout_s=0.1,{SCOPE}); "
        "expiry must only end your wait. Read fs_read_file(filepath="
        f"{str(root / 'workspace/sentinel.txt')!r}) while both are running. Then finish. "
        "On automatic completion/mail wake, report new outcomes once and finish without ACKs.",
        ["query_tasks", "observe_tasks", "wait_tasks", "fs_read_file"],
    )
    queried, observed, waited = [evidence.observation(r) for r in records[:3]]
    assert any(r["handle"] == run and r["effective_status"] == "running" for r in queried["tasks"])
    assert {r["handle"] for r in observed["tasks"]} == {run, shell}
    assert waited["timed_out"] and set(waited["pending"]) == {run, shell}
    assert not paths["done"].exists(), "Model must do other work before accepted task settles"
    sent = model_calls(
        call,
        root,
        a,
        f"Send exactly one message_agent(target={run!r},message={marker!r},{SCOPE},"
        "correlation_id='busy-cross-workflow'). Then finish; do not wait or ACK future replies.",
        ["message_agent"],
    )[0]
    ack = evidence.observation(sent)
    assert ack["target_session_id"] == b
    assert not next(r for r in evidence.mail(call, b) if r["message_id"] == ack["message_id"])[
        "delivered_at"
    ]
    paths["release"].write_text("release actual worker")
    evidence.require(
        lambda: notice(call, a, run) and notice(call, a, shell), "independent subscribed outcomes"
    )
    evidence.require(
        lambda: all(notice(call, a, h).get("delivered_at") for h in (run, shell)),
        "idle observer automatically received both outcomes",
    )
    evidence.require(
        lambda: evidence.tool_events(root, b, "message_agent"),
        "producer replied after busy boundary",
    )
    evidence.require(lambda: evidence.settled_turn(call, a), "observer automatic wake settled")
    evidence.require(lambda: evidence.settled_turn(call, b), "producer turn settled")
    assert evidence.task(call, b, run)["effective_status"] == "completed"
    receipts = [model_receipt(call, a, h, "completed") for h in (run, shell)]
    reads = model_calls(
        call,
        root,
        a,
        f"Call wait_tasks(tasks={[run, shell, 'invalid-handle']!r},return_when='any',timeout_s=0,{SCOPE}), "
        f"then wait_tasks with the same tasks and scope but return_when='all',timeout_s=0. "
        f"Then get_task_result(handle={run!r},{SCOPE}). Report actual results and partial errors.",
        ["wait_tasks", "wait_tasks", "get_task_result"],
    )
    result = evidence.observation(reads[0])
    assert len(result["results"]) == 2 and len(result["errors"]) == 1 and not result["pending"]
    assert evidence.observation(reads[1])["results"] == result["results"]
    assert evidence.observation(reads[2])["status"] == "completed"
    assert len([r for r in evidence.mail(call, a) if r.get("correlation_id") == run]) == 1
    idle_marker = "PEER_IDLE_" + secrets.token_hex(8)
    before_reply = len(evidence.tool_events(root, b, "message_agent"))
    model_calls(
        call,
        root,
        a,
        f"Human authorizes one message_agent(target={run!r},message={idle_marker!r},{SCOPE},"
        "correlation_id='idle-cross-workflow'). Finish; never ACK a reply.",
        ["message_agent"],
    )
    evidence.require(
        lambda: len(evidence.tool_events(root, b, "message_agent")) == before_reply + 1,
        "old run handle woke stable idle mailbox and model replied",
    )
    evidence.require(lambda: evidence.settled_turn(call, b), "idle recipient settled")
    failed_run, failed_shell, failed = start_held(
        call, root, b, f"workflow-{number}-failure", failure=True
    )
    model_calls(
        call,
        root,
        a,
        f"Observe tasks {[failed_run, failed_shell]!r} with notify=true,{SCOPE}; then finish. "
        "Report each actual completion status when its automatic message wakes you. Do not ACK.",
        ["observe_tasks"],
    )
    failed["release"].write_text("release actual failing worker")
    evidence.require(lambda: notice(call, a, failed_shell), "failed shell queued outcome")
    assert '"status": "failed"' in notice(call, a, failed_shell)["text"]
    evidence.require(
        lambda: notice(call, a, failed_shell).get("delivered_at"), "failure woke idle observer"
    )
    evidence.require(lambda: evidence.settled_turn(call, b), "failure producer settled")
    evidence.require(lambda: evidence.settled_turn(call, a), "failure observer settled")
    receipts.append(model_receipt(call, a, failed_shell, "failed"))
    stopped_run, continuing_shell, continuing = start_held(call, root, b, f"workflow-{number}-stop")
    model_calls(
        call,
        root,
        a,
        f"Observe tasks {[stopped_run, continuing_shell]!r} with notify=true,{SCOPE}; then finish. "
        "Report automatic outcomes once; do not ACK or submit work.",
        ["observe_tasks"],
    )
    call("POST", f"/v1/sessions/{b}/cancel", ok=(204,))
    evidence.require(
        lambda: evidence.task(call, b, stopped_run)["effective_status"] == "interrupted",
        "Stop settled finite parent run as interrupted",
    )
    assert evidence.task(call, b, continuing_shell)["effective_status"] == "running"
    assert not continuing["done"].exists()
    continuing["release"].write_text("accepted work continues after Stop")
    evidence.require(
        lambda: notice(call, a, continuing_shell), "post-Stop task completion subscribed"
    )
    evidence.require(
        lambda: notice(call, a, continuing_shell).get("delivered_at"),
        "post-Stop completion woke observer",
    )
    evidence.require(
        lambda: evidence.settled_turn(call, b), "stopped owner task-result wake settled"
    )
    evidence.require(lambda: evidence.settled_turn(call, a), "stopped observer receipt settled")
    assert evidence.task(call, b, continuing_shell)["effective_status"] == "completed"
    receipts.extend(
        [
            model_receipt(call, a, stopped_run, "interrupted"),
            model_receipt(call, a, continuing_shell, "completed"),
            model_receipt(call, b, continuing_shell, "completed"),
        ]
    )
    denied = model_calls(
        call,
        root,
        a,
        f"Permission-boundary qualification: call cancel_tasks(tasks={run!r}) exactly once. "
        "This is a peer human-owned root: require a denial, do not retry, and report it. "
        "Do not send messages or start work.",
        ["cancel_tasks"],
    )[0]
    assert evidence.observation(denied)["errors"]
    assert evidence.task(call, b, run)["effective_status"] == "completed"
    for files in (paths, failed, continuing):
        evidence.assert_no_replay(files["starts"])
    return {
        "pass": True,
        "cycle": number,
        "sessions": [a, b],
        "agents": agents,
        "model_receipts": receipts,
        "handles": [run, shell, failed_run, failed_shell, stopped_run, continuing_shell],
        "invocations": [r["payload"]["call_id"] for r in records] + [sent["payload"]["call_id"]],
        "notices": [
            {
                "id": r["message_id"],
                "handle": r["correlation_id"],
                "delivered": bool(r["delivered_at"]),
            }
            for r in evidence.mail(call, a)
            if r.get("source_agent") == "task-supervisor"
        ],
    }


def main() -> int:
    """Start owned real runtime, qualify models, then remove all disposable state."""
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--provider", choices=["codex", "claude_code"], default="codex")
    parser.add_argument("--transport", choices=["websocket", "sse"], default="websocket")
    parser.add_argument(
        "--suite",
        choices=["workflows", "subagents", "subagent-human", "ui", "mcp"],
        default="workflows",
    )
    parser.add_argument("--port", type=int, default=18862)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--owned-auth-source", type=Path)
    args = parser.parse_args()
    owned_auth = args.owned_auth_source
    auth_inode: int | None = None
    if owned_auth is not None:
        assert args.provider == "claude_code"
        assert owned_auth.parent == CORE.parent and owned_auth.name.startswith(
            "owned-claude-token-"
        )
        assert owned_auth.is_file() and not owned_auth.is_symlink()
        auth_inode = owned_auth.stat().st_ino
    root = args.root.resolve()
    assert root.parent == CORE.parent and root.name.startswith("session-workflows-live-")
    assert args.report.resolve().parent == CORE.parent
    assert not root.exists() and common.port_is_free(args.port)
    root.mkdir()
    inode = root.stat().st_ino
    workspace = root / "workspace"
    workspace.mkdir()
    (workspace / "sentinel.txt").write_text("Actual independent filesystem observation")
    report: dict[str, Any] = {
        "pass": False,
        "provider": args.provider,
        "suite": args.suite,
        "cycles": [],
    }
    if args.report.exists():
        prior = json.loads(args.report.read_text())
        report["previous_attempt"] = {
            key: prior.get(key) for key in ("source", "pass", "error", "cleanup_pass", "finished")
        }
    server = None
    core_identity = None
    isolation = None
    call = None
    source = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=CORE).decode().strip()
    report.update(
        source=source, python=sys.version, core_package=version("iowarp-core"), started=time.time()
    )
    try:
        env = os.environ
        env.update(
            PYTHONPATH=str(CORE / "src"),
            PYTHONDONTWRITEBYTECODE="1",
            CLIO_AGENT_HOME=str(root / "clio-home"),
            CLIO_ALLOWED_ROOTS=str(workspace),
            # Plugin stdio servers name a bare ``python``: make it this interpreter.
            PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", ""),
        )
        if args.provider == "codex":
            report["auth"] = frozen_codex_auth(root / "private-codex")
            env["CODEX_HOME"] = str(root / "private-codex")
            selection = {
                "provider": "codex",
                "model": "gpt-6-luna",
                "api_base": "",
                "transport": args.transport,
            }
            if args.transport == "sse":
                env["CLIO_QUALIFICATION_SSE_DIAGNOSTICS"] = str(root / "sse-sizes.json")
        else:
            for key in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"):
                env.pop(key, None)
            report["auth"] = frozen_claude_auth(
                owned_auth or Path(env["CLIO_TASK_PROOF_AUTH_SOURCE"]), root / "private-claude"
            )
            env["CLAUDE_CONFIG_DIR"] = str(root / "private-claude")
            selection = {"provider": "claude_code", "model": "haiku", "api_base": ""}
        isolation = isolate_cte_env(root / "core", env)
        assert eagerly_attach_private_daemon(), "Isolated actual Core attach failed"
        pid, birth = (isolation.state_dir / "clio-runtime.pid").read_text().split()
        core_identity = int(pid), float(birth)
        report["core_identity"] = core_identity
        with (root / "service.log").open("w") as log:
            server = subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__).with_name("_lifecycle_server.py")),
                    str(args.port),
                ],
                env=env,
                cwd=workspace,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        report["server_identity"] = [server.pid, psutil.Process(server.pid).create_time()]
        call = common.client(f"http://127.0.0.1:{args.port}")
        assert common.wait_health(call, max_elapsed=120)
        wid = common.create_workspace(call, "Distinct task workflow qualification", workspace)
        common.allow_all(call)
        if args.suite == "workflows":
            packs = [testing_agent(call, workspace, wid, role) for role in ("observer", "producer")]
        elif args.suite == "mcp":
            from _mcp_model_tasks import testing_agent as mcp_testing_agent

            packs = [mcp_testing_agent(call, workspace, wid)]
        else:
            # The coordinator already carries the shell plugin (subagent-human needs it).
            pack = common.materialize_testing_marketplace(
                CORE / "scripts/live_verification/agents/task-subagent-testing", workspace, {}
            )
            if args.provider == "claude_code":
                for expert in common.testing_prompts(pack):
                    with expert.open("a") as prompt:
                        prompt.write(
                            "\nInvoke CLIO tools through the complete fenced tool_calls protocol "
                            "in the system instructions. Include the opening ```tool_calls fence, "
                            "JSON call list and closing ``` fence; bare JSON invokes no tool.\n"
                        )
            packs = [common.install_testing_agent(call, pack)]
        call("PUT", "/v1/providers/lm", selection)
        evidence.require(
            lambda: call("GET", "/v1/providers/lm").get("state") == "ready", "real provider ready"
        )
        report["model"] = selection
        for number in range(1, args.repeats + 1):
            if args.suite == "workflows":
                result = cycle(call, root, wid, packs, number)
            elif args.suite == "mcp":
                from _mcp_model_tasks import cycle as mcp_cycle

                result = mcp_cycle(call, root, wid, packs[0], number)
            else:
                from _lifecycle_human_controls import human_control_cycle
                from _lifecycle_ui import ui_cycle
                from subagent_lifecycle import lifecycle_cycle

                if args.suite == "ui":
                    raw = ui_cycle(call, root, wid, packs[0], server.pid)
                else:
                    run_cycle = (
                        lifecycle_cycle if args.suite == "subagents" else human_control_cycle
                    )
                    raw = run_cycle(call, root, wid, packs[0], number, server.pid)
                # Keep bounded identities/verdicts; full transient records are
                # removed with the run root, including each helper's snapshots.
                result = {
                    "pass": raw["pass"],
                    "cycle": number,
                    "identities": {
                        key: raw[key]
                        for key in (
                            "parent",
                            "child",
                            "original",
                            "shell",
                            "successor",
                            "cancelled_process",
                            "cancelled_process_absent",
                            "worker_absent",
                            "browser",
                        )
                        if key in raw
                    },
                    "invocations": [
                        value["invocation"]["payload"]["call_id"]
                        for value in raw.values()
                        if isinstance(value, dict) and isinstance(value.get("invocation"), dict)
                    ],
                }
            report["cycles"].append(result)
            print(json.dumps(report["cycles"][-1]), flush=True)
        report["pass"] = True
    except Exception as exc:
        report.update(
            error=f"{type(exc).__name__}: {exc}", traceback=traceback.format_exc()[-6000:]
        )
        try:
            report["provider_failure"] = provider_failure_receipt(root)
        except (OSError, ValueError) as diagnostic_error:
            report["provider_failure_error"] = str(diagnostic_error)[:600]
        if call is not None:
            try:
                report["failure_context"] = failure_receipts(call, root)
            except (OSError, RuntimeError, ValueError) as diagnostic_error:
                report["failure_context_error"] = str(diagnostic_error)[:600]
        print(json.dumps({"error": report["error"]}), flush=True)
    finally:
        if owned_auth is not None:
            assert owned_auth.stat().st_ino == auth_inode and not owned_auth.is_symlink()
            owned_auth.unlink()
            report["auth_source_absent"] = not owned_auth.exists()
        sizes = root / "sse-sizes.json"
        if sizes.exists():
            report["sse_diagnostics"] = json.loads(sizes.read_text())
        # Release physical owned workers before shutting their actual owners down.
        for path in workspace.glob("*-script.py"):
            path.with_name(path.name.replace("-script.py", "-release.txt")).write_text(
                "finally release"
            )
        if server is not None:
            descendants = (
                [
                    (p.pid, p.create_time())
                    for p in psutil.Process(server.pid).children(recursive=True)
                ]
                if server.poll() is None
                else []
            )
            common.terminate_server(server)
            report["server_exit"] = server.poll()
            descendant_results = [stop_identity(identity) for identity in descendants]
            report["descendants_absent"] = all(descendant_results)
        release_this_process_client()
        report["core_absent"] = core_identity is None or stop_identity(core_identity)
        report["listener_absent"] = common.port_is_free(args.port)
        assert root.stat().st_ino == inode and not root.is_symlink()
        if (
            report["core_absent"]
            and report.get("descendants_absent", True)
            and report["listener_absent"]
            and (server is None or server.poll() is not None)
        ):
            shutil.rmtree(root)
        report["scratch_absent"] = not root.exists()
        report["finished"] = time.time()
        report["cleanup_pass"] = (
            report["scratch_absent"] and report["core_absent"] and report["listener_absent"]
        )
        report["pass"] = report["pass"] and report["cleanup_pass"]
        report["harness_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        args.report.write_text(json.dumps(report, indent=2))
        print(
            json.dumps({k: v for k, v in report.items() if k not in {"cycles", "traceback"}}),
            flush=True,
        )
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
