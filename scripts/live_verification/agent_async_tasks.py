"""Exercise task delivery with actual models and an isolated source API.

Credentials are read from a private, non-refreshable copy. This harness never
launches Desktop, replaces an installed client, or restarts a remote service.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import _common as common
from _async_payload import PayloadServer
from _lifecycle_auth import frozen_claude_auth
from _question_task_models import (
    answer_question_after_independent_work,
    capture_question_failure,
    cleanup_question_state,
    disposable_question_paths,
    question_mission,
)
from _source_identity import source_identity
from _task_evidence import model_task_evidence
from _task_missions import folder_input, mission
from _task_outcomes import outcomes

CORE = Path(__file__).resolve().parents[2]
TARGET = (
    "https://raw.githubusercontent.com/modelcontextprotocol/modelcontextprotocol/main/README.md"
)


def frozen_codex_auth(destination: Path) -> dict[str, Any]:
    """Copy a currently valid access token without a usable refresh token."""
    original = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "auth.json"
    payload = json.loads(original.read_text(encoding="utf-8"))
    tokens = payload.get("tokens", {})
    token = tokens.get("access_token", "")
    claims = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "==="))
    remaining = int(claims.get("exp", 0)) - int(time.time())
    if remaining < 1800:
        raise RuntimeError("Live model gate blocked: existing access token expires too soon")
    tokens["refresh_token"] = ""
    destination.mkdir(parents=True)
    target = destination / "auth.json"
    target.write_text(json.dumps(payload), encoding="utf-8")
    return {
        "source_sha256": hashlib.sha256(original.read_bytes()).hexdigest(),
        "valid_for_seconds": remaining,
        "refresh_disabled": True,
    }


def main() -> int:
    """Record a real model turn through the product's provider and tool bridge."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proof", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18835)
    parser.add_argument("--model")
    parser.add_argument("--provider", choices=["codex", "claude_code"], default="codex")
    parser.add_argument("--claude-auth-source", type=Path)
    parser.add_argument("--transport", choices=["websocket", "sse"], default="websocket")
    parser.add_argument("--phase", choices=["baseline", "acceptance"], default="baseline")
    parser.add_argument("--slow-payload", action="store_true")
    parser.add_argument(
        "--kind",
        choices=["MCP", "Shell", "Subagent", "Download", "Indexing", "Question"],
        default="MCP",
    )
    parser.add_argument("--question-mode", choices=["wait", "idle", "stop"], default="wait")
    parser.add_argument("--question-answer-delay", type=float, default=0)
    parser.add_argument("--max-elapsed", type=float, default=600)
    parser.add_argument("--storage-files", type=int)
    parser.add_argument("--web-dir", type=Path)
    parser.add_argument("--hold-until", type=Path)
    args = parser.parse_args()
    if args.web_dir is not None and not (args.web_dir / "index.html").is_file():
        raise ValueError("The qualified UI build is unavailable")
    if args.hold_until is not None and args.hold_until.exists():
        raise ValueError("A UI release marker must be a fresh owned path")
    proof = args.proof.resolve()
    if proof == CORE or not proof.is_relative_to(CORE):
        raise ValueError("Live qualification proof/state must be inside this repository")
    proof.mkdir(parents=True, exist_ok=False)
    workspace = proof / "workspace"
    workspace.mkdir()
    (workspace / "sentinel.txt").write_text("independent action completed\n", encoding="utf-8")
    args.model = args.model or ("gpt-6-luna" if args.provider == "codex" else "haiku")
    if args.provider == "codex":
        auth = frozen_codex_auth(proof / "private-codex")
    else:
        if args.claude_auth_source is None:
            raise ValueError("An explicit read-only Claude authentication source is required")
        auth = frozen_claude_auth(args.claude_auth_source, proof / "private-claude")
    source = (
        folder_input(proof, args.kind, file_count=args.storage_files)
        if args.kind in {"Indexing", "Download"}
        else None
    )
    payload_server = PayloadServer(proof) if args.slow_payload else None
    target = payload_server.url if payload_server is not None else TARGET
    # The MCP kind's web server comes from the default marketplace's ``web``
    # plugin, which the web-testing agent carries (``clio-kit mcp-server web``).
    pack = common.materialize_testing_marketplace(
        CORE
        / "scripts/live_verification/agents"
        / (
            "web-testing"
            if args.kind == "MCP"
            else "task-subagent-testing"
            if args.kind == "Subagent"
            else "task-testing"
        ),
        workspace,
        {},
    )
    prompt_mission = (
        question_mission(workspace, args.question_mode)
        if args.kind == "Question"
        else mission(args.kind, workspace, source=source)
        if args.kind not in {"MCP", "Download"}
        else None
    )
    environment = dict(os.environ)
    for directory, keys in {
        "tmp": ("TEMP", "TMP", "TMPDIR"),
        "cache": ("XDG_CACHE_HOME", "PIP_CACHE_DIR", "MPLCONFIGDIR"),
        "config": ("XDG_CONFIG_HOME", "FASTMCP_HOME"),
        "data": ("XDG_DATA_HOME",),
        "state": ("XDG_STATE_HOME",),
        "local": ("LOCALAPPDATA",),
        "roaming": ("APPDATA",),
        "home": ("HOME", "USERPROFILE"),
    }.items():
        directory_path = proof / directory
        directory_path.mkdir()
        environment.update({key: str(directory_path) for key in keys})
    environment.update(
        {
            "PYTHONPATH": str(CORE / "src"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "CLIO_AGENT_HOME": str(proof / "clio-home"),
            "CLIO_KIT_CACHE_DIR": str(proof / "clio-kit-cache"),
            "UV_CACHE_DIR": str(proof / "uv-cache"),
            "CODEX_HOME": str(proof / "private-codex"),
            "WEB_STATE_DIR": str(proof / "web-state"),
            "CLIO_ALLOWED_ROOTS": str(workspace),
            "CLIO_STREAM_AUDIT_LOG": str(proof / "stream.jsonl"),
        }
    )
    if args.web_dir is not None:
        environment["CLIO_WEB_DIR"] = str(args.web_dir.resolve())
    if args.provider == "claude_code":
        for key in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"):
            environment.pop(key, None)
        environment["CLAUDE_CONFIG_DIR"] = str(proof / "private-claude")
    if payload_server is not None:
        # Supported configuration, confined to this source service and its owned
        # HTTP data server. Installed Web MCP settings are untouched.
        environment["WEB_ALLOW_PRIVATE_HOSTS"] = "true"
    if not common.port_is_free(args.port):
        raise RuntimeError("Owned live-proof port is already occupied")
    server_code = (
        "import faulthandler; faulthandler.dump_traceback_later(45, repeat=True); "
        "from clio_agent.gact.app import build_app; import uvicorn; "
        "a=build_app(); uvicorn.run(a,host='127.0.0.1',port=" + str(args.port) + ")"
    )
    verdict: dict[str, Any] = {
        "model": args.model,
        "provider": args.provider,
        "auth": auth,
        "scope": f"real model-initiated {args.phase}",
        "loaded_source": source_identity(CORE),
    }
    common.dump_json(proof / "loaded-source.json", verdict["loaded_source"])
    common.dump_json(
        proof / "owned-resources.json",
        {
            "api_port": args.port,
            "proof": str(proof),
            "disposable_paths": [str(path) for path in disposable_question_paths(proof)]
            if args.kind == "Question"
            else [],
            "installed_state": "untouched",
            "service_process": "new child of this harness",
        },
    )
    with (proof / "server.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            [sys.executable, "-c", server_code],
            cwd=workspace,
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            call = common.client(f"http://127.0.0.1:{args.port}")
            if not common.wait_health(call, max_elapsed=120):
                raise RuntimeError("Isolated source API did not become healthy")
            wid = common.create_workspace(call, "async-tasks-baseline", workspace)
            sid = common.create_session(call, wid, "async-tasks-baseline")
            verdict.update(workspace_id=wid, session_id=sid)
            common.allow_all(call)
            if args.kind == "Download" and source is not None:
                registered = call(
                    "POST",
                    f"/v1/workspaces/{wid}/sources",
                    {
                        "provider": "local",
                        "root": source["root"],
                        "label": "Owned live download",
                        "mode": "read_only",
                    },
                )
                source["source_id"] = registered["id"]
                prompt_mission = mission(args.kind, workspace, source=source)
            verdict["owned_source"] = source
            common.use_agent(call, sid, common.install_testing_agent(call, pack))
            # Normal product binding, with discovery/readiness guards intact.
            call(
                "PUT",
                "/v1/providers/lm",
                {
                    "provider": args.provider,
                    "model": args.model,
                    "api_base": "",
                    **({"transport": args.transport} if args.provider == "codex" else {}),
                },
            )
            common.expanding_wait(
                lambda: call("GET", "/v1/providers/lm").get("state") == "ready",
                what="actual selected model ready",
                max_elapsed=120,
            )
            verdict["resolved_tools"] = common.resolved_agent_tools(call, sid, workspace_id=wid)
            common.dump_json(
                proof / "service-ready.json",
                {
                    "url": f"http://127.0.0.1:{args.port}",
                    "session_id": sid,
                    "workspace_id": wid,
                    "web_dir": str(args.web_dir) if args.web_dir else None,
                },
            )
            started = time.monotonic()
            prompt = (
                f"Call web_fetch on exactly {target}. Then call web_search for 'Model Context "
                "Protocol specification', count=1. Report exactly what web_fetch returned to "
                "you: a task handle or fetched document content. Do not claim background "
                "execution unless you actually received a handle. Use these two tools only."
            )
            if args.phase == "acceptance":
                prompt = (
                    f"Call web_fetch on exactly {target}. If it returns a task handle, immediately "
                    "call query_tasks(kind='MCP') before waiting. Then call web_search for 'Model "
                    "Context Protocol specification', count=1. Immediately query_tasks(kind='MCP') "
                    "again to establish whether the fetch still runs after the search. "
                    "Use wait_tasks on all handles you "
                    "received, then get_task_result on the fetch handle. Report the actual returned "
                    "observations and whether they are handles or documents. Do not infer that work "
                    "was still running unless the task snapshot proves it."
                )
            if prompt_mission is not None:
                prompt = prompt_mission
            common.post_message(call, sid, prompt)
            if args.kind == "Question":
                verdict["question_checkpoint"] = answer_question_after_independent_work(
                    call,
                    wid,
                    sid,
                    args.question_mode,
                    max_elapsed=args.max_elapsed,
                    answer_delay_s=args.question_answer_delay,
                )
            verdict["turn_status"] = common.wait_turn(call, wid, sid, max_elapsed=args.max_elapsed)
            verdict["elapsed_seconds"] = time.monotonic() - started
            messages = common.session_messages(call, sid)
            common.dump_json(proof / "messages.json", messages)
            verdict["tools_called"] = common.tool_calls_from_messages(messages)
            verdict["tasks"] = call("GET", f"/v1/sessions/{sid}/async-processes")
            if args.kind == "Subagent":
                for task in verdict["tasks"]["processes"]:
                    if task.get("task_kind") == "Subagent" and task.get("child_session_id"):
                        common.dump_json(
                            proof / (task["handle"] + "-child-messages.json"),
                            common.session_messages(call, task["child_session_id"]),
                        )
            verdict["model_evidence"] = model_task_evidence(messages, args.kind)
            verdict["owner_outcomes"] = outcomes(
                proof, args.kind, verdict["tasks"], verdict["model_evidence"], source
            )
            if args.kind == "Question":
                verdict["owner_outcomes"]["pass"] &= verdict["question_checkpoint"][
                    "still_pending_before_answer"
                ]
            if payload_server is not None:
                verdict["owned_payload"] = {
                    "bytes": len(payload_server.payload),
                    "sha256": payload_server.sha256,
                    "url": target,
                    "private_host_configuration": "isolated owned HTTP fixture only",
                }
            verdict["source_unchanged_during_run"] = (
                source_identity(CORE) == verdict["loaded_source"]
            )
            verdict["pass"] = (
                (
                    bool(common.find_tool_calls(messages, "_fetch"))
                    if args.phase == "baseline" and args.kind == "MCP"
                    else verdict["model_evidence"]["pass"] and verdict["owner_outcomes"]["pass"]
                )
                and verdict["turn_status"] in {"idle", "completed"}
                and verdict["source_unchanged_during_run"]
            )
        except Exception as exc:
            verdict.update(pass_=False, error=f"{type(exc).__name__}: {exc}")
            verdict["pass"] = False
            if args.kind == "Question" and verdict.get("session_id"):
                verdict["failure_snapshot"] = capture_question_failure(proof, verdict["session_id"])
        finally:
            if args.hold_until is not None:
                common.dump_json(proof / "verdict-before-ui.json", verdict)
                common.expanding_wait(
                    args.hold_until.exists,
                    what="owned UI qualification release marker",
                    max_elapsed=1800,
                )
            common.terminate_server(process)
            if payload_server is not None:
                payload_server.close()
            if args.kind == "Question":
                try:
                    verdict["disposable_state_absent"] = cleanup_question_state(proof)
                except OSError as exc:
                    verdict["cleanup_error"] = f"{type(exc).__name__}: {exc}"
                    verdict["pass"] = False
            common.dump_json(proof / "verdict.json", verdict)
    print(
        json.dumps(
            {"pass": verdict.get("pass"), "error": verdict.get("error"), "proof": str(proof)}
        )
    )
    return 0 if verdict.get("pass") else 1


if __name__ == "__main__":
    raise SystemExit(main())
