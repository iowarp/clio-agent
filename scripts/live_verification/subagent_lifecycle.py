"""Qualify lifecycle and scoped session mail with actual models and production owners.

Run through the approved private-Core launcher. Every service, process and input
belongs to this proof; no installed Desktop, authentication or defaults are changed.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import signal
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any

import _common as common
import _lifecycle_evidence as evidence
from _lifecycle_auth import frozen_claude_auth
from _lifecycle_peer import peer_cycle
from _lifecycle_recovery import recovery_cycle
from _lifecycle_ui import ui_cycle
from _source_identity import source_identity
from agent_async_tasks import frozen_codex_auth

CORE = Path(__file__).resolve().parents[2]


def lifecycle_cycle(
    call: Any, proof: Path, wid: str, agent: str, number: int, server_pid: int
) -> dict[str, Any]:
    """Prove real model pause/resume, retained restart, held mail and subtree cancellation."""
    output = proof / f"cycle-{number}"
    output.mkdir()
    workspace = proof / "workspace"
    sentinel = workspace / "sentinel.txt"
    nonce = secrets.token_hex(16)
    held_text = "HELD_MESSAGE_" + secrets.token_hex(8)
    paths = evidence.worker_files(workspace, f"cycle-{number}", nonce)
    sid = common.create_session(call, wid, f"lifecycle cycle {number}")
    common.use_agent(call, sid, agent)
    command = common.quoted_command(sys.executable, str(paths["script"]))
    assignment = (
        "Do exactly this assignment, preserving accepted operation identities across resumes. "
        f"Call shell_bash(command={command!r}, background=true, timeout_s=0, cwd={str(workspace)!r}) "
        "exactly once. Then wait_tasks on the accepted Shell handle with no timeout. "
        "If your model execution is paused, do not replay the Shell call on resume. "
        "Once the result arrives, report its exact RESULT_NONCE and any received HELD_MESSAGE. "
        "Do not read the script or other files. On retained-context restart, make a NEW "
        "get_task_result call in the NEW run on the existing Shell handle before reporting "
        "the already observed nonce. Reading a copied earlier result does not exercise the "
        "new run's result access, which this assignment must verify. Do not start another "
        "operation or reply to any agent message."
    )
    submission = common.post_message(
        call,
        sid,
        f"Call spawn_agent_task(agent='worker', task={assignment!r}) exactly once. "
        f"Then fs_read_file(filepath={str(sentinel)!r}) and finish. Leave its work running. "
        "When a completion wakes you, report it without new work, reply or restart.",
    )
    child = evidence.require(
        lambda: next(
            (row for row in evidence.tasks(call, sid) if row["task_kind"] == "Subagent"), None
        ),
        "actual model accepted Subagent handle",
    )
    handle, child_sid = child["handle"], child["child_session_id"]
    shell = evidence.require(
        lambda: next(
            (row for row in evidence.tasks(call, child_sid) if row["task_kind"] == "Shell"), None
        ),
        "child model accepted real background Shell",
    )
    evidence.require(lambda: paths["starts"].exists(), "actual worker process started")
    evidence.require(
        lambda: evidence.tool_events(proof, child_sid, "wait_tasks", complete=False),
        "child model entered unbounded wait",
    )
    evidence.require(lambda: evidence.settled_turn(call, sid), "parent did other work and finished")
    assert shell["effective_status"] == "running" and not paths["done"].exists()
    pause = evidence.model_turn(
        call,
        proof,
        sid,
        f"Call pause_agent(target={handle!r}), then query_tasks(kind='Subagent'), then finish. "
        "Do not cancel, resume, restart or wait.",
        "pause_agent",
    )
    evidence.require(
        lambda: evidence.task(call, sid, handle)["effective_status"] == "paused",
        "same Subagent handle paused after its model stopped",
    )
    before = call("GET", f"/v1/sessions/{child_sid}")
    assert not shell.get("cancel_requested")
    paths["release"].write_text("release owned work", encoding="utf8")
    evidence.require(
        lambda: evidence.task(call, child_sid, shell["handle"])["effective_status"] == "completed",
        "accepted Shell completed while Subagent held",
    )
    evidence.require_stable_pause(call, sid, handle)
    after = call("GET", f"/v1/sessions/{child_sid}")
    assert after["message_count"] == before["message_count"], "Completion woke a paused model"
    shell_done = evidence.retained_task(call, child_sid, shell["handle"])
    assert shell_done.get("notify_pending") and not shell_done.get("consumed_at")
    message = evidence.model_turn(
        call,
        proof,
        sid,
        f"Call message_agent(target={child_sid!r}, message={held_text!r}, wake=false). "
        "Finish without resume or other controls.",
        "message_agent",
    )
    assert evidence.mail(call, child_sid)[-1]["delivered_at"] == ""
    observe = evidence.model_turn(
        call,
        proof,
        sid,
        f"Call observe_tasks(tasks={handle!r}, timeout_s=0), then wait_tasks(tasks={handle!r}, timeout_s=0). "
        "Both should report pending held work honestly. Then finish without changing it.",
        "wait_tasks",
    )
    assert handle in evidence.observation(observe["invocation"])["pending"]
    resume = evidence.model_turn(
        call,
        proof,
        sid,
        f"Call resume_agent(target={handle!r}), then fs_read_file(filepath={str(sentinel)!r}) and finish. "
        "Do not start new tasks. Report any automatically supplied completion without replying.",
        "resume_agent",
    )
    settled = evidence.require(
        lambda: (
            row
            if (row := evidence.task(call, sid, handle))["effective_status"] == "completed"
            else None
        ),
        "resumed child model reported real background result",
    )
    child_messages = common.session_messages(call, child_sid)
    child_text = json.dumps(child_messages, ensure_ascii=False)
    assert nonce in child_text and held_text in child_text
    evidence.assert_no_replay(paths["starts"])
    delivered = [row for row in evidence.mail(call, child_sid) if row["text"] == held_text]
    assert len(delivered) == 1 and delivered[0]["delivered_at"]
    restart = evidence.model_turn(
        call,
        proof,
        sid,
        f"Call restart_agent(target={handle!r}) exactly once with retained context. "
        "Then finish. Do not resubmit the original assignment or any Shell command.",
        "restart_agent",
    )
    restart_result = evidence.observation(restart["invocation"])
    new_handle = restart_result["handle"]
    assert new_handle != handle and restart_result["supersedes_handle"] == handle
    successor = evidence.require(
        lambda: (
            row
            if (row := evidence.task(call, sid, new_handle))["effective_status"] == "completed"
            else None
        ),
        "actual retained-context successor completed without replay",
    )
    successor_messages = common.session_messages(call, successor["child_session_id"])
    assert nonce in json.dumps(successor_messages, ensure_ascii=False)
    assert not evidence.tool_events(proof, successor["child_session_id"], "shell_bash")
    historical = evidence.tool_events(proof, successor["child_session_id"], "get_task_result")
    assert historical and all(row["payload"].get("ok") for row in historical)
    evidence.assert_no_replay(paths["starts"])
    assert evidence.task(call, sid, handle)["raw_status"] == "completed"
    cancel_paths = evidence.worker_files(workspace, f"cancel-{number}", "must-not-complete")
    cancel_command = common.quoted_command(sys.executable, str(cancel_paths["script"]))
    cancel_assignment = (
        f"Call shell_bash(command={cancel_command!r}, background=true, timeout_s=0, cwd={str(workspace)!r}) "
        "exactly once, then wait_tasks on its accepted handle without a timeout. "
        "Do not read other files or submit additional work. If this run is cancelled or "
        "interrupted and then restarted with retained context, report that prior outcome "
        "after making a NEW get_task_result call in the NEW run on its existing Shell "
        "handle and finish. Reading a copied earlier result does not exercise the new "
        "run's result access, which this assignment must verify. "
        "Never replay the already accepted Shell operation."
    )
    evidence.model_turn(
        call,
        proof,
        sid,
        f"Call spawn_agent_task(agent='worker',task={cancel_assignment!r}) exactly once and finish. "
        "Do not wait on the child or cancel it yet.",
        "spawn_agent_task",
    )
    cancel_child = next(
        row
        for row in evidence.tasks(call, sid)
        if row["task_kind"] == "Subagent" and row["handle"] not in {handle, new_handle}
    )
    cancel_sid = cancel_child["child_session_id"]
    cancel_shell = evidence.require(
        lambda: next(
            (row for row in evidence.tasks(call, cancel_sid) if row["task_kind"] == "Shell"), None
        ),
        "cancellable real descendant Shell",
    )
    evidence.require(
        lambda: cancel_paths["starts"].exists(), "cancellable descendant process started"
    )
    process_identity = evidence.require(
        lambda: evidence.worker_identity(server_pid, cancel_paths["script"]),
        "exact owned descendant process identity before cancellation",
    )
    cancelled = evidence.model_turn(
        call,
        proof,
        sid,
        f"Call cancel_tasks(tasks={cancel_child['handle']!r}), then finish. "
        "Do not treat cancellation acknowledgement as settled cleanup.",
        "cancel_tasks",
    )
    final_cancel = evidence.require(
        lambda: (
            row
            if (row := evidence.task(call, sid, cancel_child["handle"]))["effective_status"]
            == "cancelled"
            else None
        ),
        "Subagent cancelled only after descendant owner settlement",
    )
    descendant = evidence.task(call, cancel_sid, cancel_shell["handle"])
    assert descendant["effective_status"] == "cancelled" and not cancel_paths["done"].exists()
    assert evidence.identity_absent(process_identity), "Terminal cancellation preceded process exit"
    cancelled_restart = evidence.model_turn(
        call,
        proof,
        sid,
        f"Call restart_agent(target={cancel_child['handle']!r}) exactly once with retained context. "
        "Finish without replaying or resubmitting any operation.",
        "restart_agent",
    )
    cancelled_successor_handle = evidence.observation(cancelled_restart["invocation"])["handle"]
    assert cancelled_successor_handle != cancel_child["handle"]
    cancelled_successor = evidence.require(
        lambda: (
            row
            if (row := evidence.task(call, sid, cancelled_successor_handle))["effective_status"]
            == "completed"
            else None
        ),
        "cancelled run restarted with retained evidence and no replay",
    )
    assert evidence.task(call, sid, cancel_child["handle"])["effective_status"] == "cancelled"
    assert not evidence.tool_events(proof, cancelled_successor["child_session_id"], "shell_bash")
    historical = evidence.tool_events(
        proof, cancelled_successor["child_session_id"], "get_task_result"
    )
    assert historical and all(row["payload"].get("ok") for row in historical)
    evidence.assert_no_replay(cancel_paths["starts"])
    for recipient in (
        sid,
        child_sid,
        successor["child_session_id"],
        cancel_sid,
        cancelled_successor["child_session_id"],
    ):
        evidence.save_session(call, output, recipient)
    row = {
        "pass": True,
        "parent": sid,
        "child": child_sid,
        "original": handle,
        "shell": shell["handle"],
        "successor": new_handle,
        "submission": submission,
        "pause": pause,
        "message": message,
        "observe": observe,
        "resume": resume,
        "restart": restart,
        "cancellation": cancelled,
        "settled": settled,
        "final_cancel": final_cancel,
        "descendant": descendant,
        "expected_nonce": nonce,
        "held_message": held_text,
        "cancelled_process": process_identity,
        "cancelled_process_absent": True,
        "cancelled_restart": cancelled_restart,
        "cancelled_successor": cancelled_successor,
    }
    common.dump_json(output / "verdict.json", row)
    print(f"LIVE cycle {number} passed", flush=True)
    return row


def main() -> int:
    """Run isolated actual model lifecycle gates and retain failures without reinterpretation."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proof", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18846)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--human-controls", action="store_true")
    parser.add_argument("--peer", action="store_true")
    parser.add_argument("--recovery", action="store_true")
    parser.add_argument("--ui", action="store_true")
    parser.add_argument("--web-dir", type=Path)
    parser.add_argument("--provider", choices=("codex", "claude_code"), default="codex")
    parser.add_argument("--model")
    parser.add_argument("--claude-auth-source", type=Path)
    args = parser.parse_args()
    proof = args.proof.resolve()
    proof.mkdir(parents=True, exist_ok=False)
    workspace = proof / "workspace"
    workspace.mkdir()
    (workspace / "sentinel.txt").write_text("independent real filesystem action", encoding="utf8")
    pack = common.materialize_testing_marketplace(
        CORE / "scripts/live_verification/agents/task-subagent-testing", workspace, {}
    )
    # The qualification coordinator carries the shell plugin for its separate peer leg.
    main_expert = common.testing_prompt(pack)
    main_expert.write_text(
        main_expert.read_text(encoding="utf8")
        + "\nExecute qualification requests directly unless they explicitly request spawning a worker. "
        "Never delegate peer discovery or messaging; use your own native memory and message tools.\n",
        encoding="utf8",
    )
    if args.human_controls:
        with main_expert.open("a", encoding="utf8") as prompt:
            prompt.write(
                "\nHuman-intervention receipt qualification: every final answer must first "
                "include a JSON receipt array for every newly injected human_intervention. "
                "Copy intervention_id, action, handle, phase, outcome and text exactly from "
                "those notices. The intervention_id is mandatory even when a simultaneous "
                "task completion summarizes the same operation. Report receipts before any "
                "completion summary; never replace the receipt array with a prose summary. "
                "This reporting requirement applies after every automatic wake too.\n"
            )
    if args.provider == "claude_code":
        # These missions exercise lifecycle behavior through the text-tool bridge.
        # Explicitly request its real call syntax; never reinterpret bare JSON as work.
        for expert in (main_expert, common.testing_prompt(pack, "worker")):
            with expert.open("a", encoding="utf8") as prompt:
                prompt.write(
                    "\nThis Claude SDK route invokes CLIO tools only through the complete "
                    "fenced tool_calls protocol in the system instructions. For every requested "
                    "action, output an opening ```tool_calls fence, the JSON call list, and "
                    "the closing ``` fence. A bare JSON list, XML tags or formatting alone "
                    "submits no operation. Verify both fences before ending an action response.\n"
                )
    model = args.model or ("gpt-6-luna" if args.provider == "codex" else "haiku")
    if args.provider == "codex":
        auth = frozen_codex_auth(proof / "private-codex")
    else:
        assert args.claude_auth_source is not None, (
            "An explicitly owned Claude credential source is required"
        )
        auth = frozen_claude_auth(args.claude_auth_source, proof / "private-claude")
    env = dict(
        os.environ,
        PYTHONPATH=str(CORE / "src"),
        PYTHONDONTWRITEBYTECODE="1",
        CLIO_AGENT_HOME=str(proof / "clio-home"),
        CODEX_HOME=str(proof / "private-codex"),
        PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", ""),
        CLIO_ALLOWED_ROOTS=str(workspace),
        CLIO_STREAM_AUDIT_LOG=str(proof / "stream.jsonl"),
    )
    if args.web_dir is not None:
        assert (args.web_dir / "index.html").is_file(), "Qualified UI bundle is absent"
        env["CLIO_WEB_DIR"] = str(args.web_dir.resolve())
    if args.provider == "claude_code":
        for key in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"):
            env.pop(key, None)
        env["CLAUDE_CONFIG_DIR"] = str(proof / "private-claude")
    selection = {"provider": args.provider, "model": model, "api_base": ""}
    if args.provider == "codex":
        selection["transport"] = "websocket"
    assert common.port_is_free(args.port), "Private source port occupied"
    verdict: dict[str, Any] = {
        "pass": False,
        "complete_qualification": False,
        "source": source_identity(CORE),
        "provider": args.provider,
        "model": model,
        "transport": "websocket" if args.provider == "codex" else "sdk",
        "auth": auth,
        "cycles": [],
        "started_at": time.time(),
    }
    process = None
    try:
        with (proof / "server.log").open("w", encoding="utf8") as log:
            server_argv = [
                sys.executable,
                str(Path(__file__).with_name("_lifecycle_server.py")),
                str(args.port),
            ]
            process = subprocess.Popen(
                server_argv,
                cwd=workspace,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            call = common.client(f"http://127.0.0.1:{args.port}")
            assert common.wait_health(call, max_elapsed=120)
            wid = common.create_workspace(call, "actual lifecycle qualification", workspace)
            common.allow_all(call)
            agent = common.install_testing_agent(call, pack)
            call(
                "PUT",
                "/v1/providers/lm",
                selection,
            )
            evidence.require(
                lambda: call("GET", "/v1/providers/lm").get("state") == "ready",
                f"actual {args.provider}/{model} ready",
            )
            for number in range(1, args.repeats + 1):
                cycle = lifecycle_cycle
                if args.human_controls:
                    from _lifecycle_human_controls import human_control_cycle

                    cycle = human_control_cycle
                verdict["cycles"].append(cycle(call, proof, wid, agent, number, process.pid))
                common.dump_json(proof / "verdict.json", verdict)
            if args.peer:
                verdict["peer"] = peer_cycle(call, proof, wid, agent)
            if args.recovery:

                def restart_service() -> int:
                    nonlocal process
                    assert process is not None
                    process.terminate()
                    try:
                        process.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        native_diagnostic_signal = getattr(signal, "SIGUSR2", None)
                        if native_diagnostic_signal is not None:
                            process.send_signal(native_diagnostic_signal)
                            time.sleep(0.2)
                        diagnostic_signal = getattr(signal, "SIGUSR1", None)
                        if diagnostic_signal is not None:
                            process.send_signal(diagnostic_signal)
                            time.sleep(1.0)
                        common.terminate_server(process)
                        raise AssertionError(
                            "Owned API did not settle graceful interruption"
                        ) from None
                    process = subprocess.Popen(
                        server_argv,
                        cwd=workspace,
                        env=env,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                    )
                    assert common.wait_health(call, max_elapsed=120)
                    # The private API's runtime-only model choice is not an installed
                    # default. Reapply the same operator selection after service boot.
                    call(
                        "PUT",
                        "/v1/providers/lm",
                        selection,
                    )
                    evidence.require(
                        lambda: call("GET", "/v1/providers/lm").get("state") == "ready",
                        "same private model selection restored after service boot",
                    )
                    return process.pid

                verdict["recovery"] = recovery_cycle(
                    call, proof, wid, agent, process.pid, restart_service
                )
            if args.ui:
                assert args.web_dir is not None, "UI gate requires the qualified production build"
                verdict["ui"] = ui_cycle(call, proof, wid, agent, process.pid)
            verdict["pass"] = True
    except Exception as exc:
        verdict["error"] = f"{type(exc).__name__}: {exc}"
        (proof / "failure.txt").write_text(traceback.format_exc(), encoding="utf8")
        print(verdict["error"], flush=True)
    finally:
        # Release only these owned inputs. App shutdown settles its own process owners.
        for path in workspace.glob("*-release.txt"):
            path.write_text("qualification cleanup", encoding="utf8")
        for script in workspace.glob("*-script.py"):
            script.with_name(script.name.replace("-script.py", "-release.txt")).write_text(
                "qualification cleanup", encoding="utf8"
            )
        if process is not None:
            common.terminate_server(process)
        verdict["source_unchanged_during_run"] = source_identity(CORE) == verdict["source"]
        verdict["pass"] = verdict["pass"] and verdict["source_unchanged_during_run"]
        verdict["finished_at"] = time.time()
        common.dump_json(proof / "verdict.json", verdict)
    return 0 if verdict["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
