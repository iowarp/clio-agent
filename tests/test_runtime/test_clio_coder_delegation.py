"""Delegating coding work to a spawned Clio Coder instance, against scripted fake servers.

The fakes speak the two real surfaces: ``run --json-events terminal`` (one JSONL turn) and
``acp`` (JSON-RPC over stdio). Each fixes a file in the workspace the way a real instance would,
so the hand-back, the parent's verification and worktree cleanup are exercised end to end.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import shlex
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

from clio_agent.runtime.clio_coder import (
    ClioCoderError,
    ClioCoderInstall,
    ClioCoderTarget,
    DelegationRequest,
    InstanceHome,
    RunOptions,
    delegate_coding_task,
)
from clio_agent.runtime.clio_coder.acp import allow_permissions_once, reject_permissions
from clio_agent.runtime.clio_coder.delegate import compose_task
from clio_agent.runtime.clio_coder.results import outcome_for_exit, summarize_usage
from clio_agent.runtime.clio_coder.workspace import (
    ACCEPTANCE_CHECK_ID,
    adopt_directory,
    create_worktree,
    declare_acceptance_check,
)

FAKE_CLIO_CODER = textwrap.dedent(
    """
    import json, os, subprocess, sys, time
    from pathlib import Path

    def out(message):
        sys.stdout.write(json.dumps(message) + "\\n")
        sys.stdout.flush()

    def fix(cwd, extra=""):
        Path(cwd, "calc.py").write_text("def add(a, b):\\n    return a + b\\n" + extra)
        Path(cwd, "__pycache__").mkdir(exist_ok=True)
        Path(cwd, "__pycache__", "calc.cpython-314.pyc").write_bytes(b"\\0")

    def env_report(cwd):
        keys = sorted(k for k in os.environ if k.startswith(("CLIO_", "SECRET")))
        Path(os.environ["CLIO_CODER_STATE_DIR"], "env.json").write_text(
            json.dumps({k: os.environ[k] for k in keys} | {"TMPDIR": os.environ["TMPDIR"]})
        )

    args = sys.argv[1:]
    if args[-1:] == ["--help"]:
        flag = {"run": "verify"}.get(args[0], "session-file")
        offered = Path(__file__).with_name(flag + "-flag").exists()
        print("Usage: clio-coder [options]", "  --timeout <seconds>",
              f"  --{flag} <value>" if offered else "", sep="\\n")
        sys.exit(0)
    cwd = args[args.index("--cwd") + 1] if "--cwd" in args else os.getcwd()
    env_report(cwd)
    if "--session-file" in args:
        Path(args[args.index("--session-file") + 1]).write_text(json.dumps({
            "version": 1, "sessionId": "s-tui", "outcome": "quit", "exitCode": 0, "turns": 2,
            "model": {"target": "t", "model": "tui-model"},
            "usage": {"tokens": {"display": "1,234", "fact": {}},
                      "cost": {"display": "not measured", "fact": {}}}}))
    if args[0] == "run":
        ceiling = os.environ.get("CLIO_CODER_AUTONOMY_CEILING")
        if "yolo" in args and ceiling not in (None, "", "yolo"):
            print("    at boot (fake.js:1)", file=sys.stderr)
            print(f"clio-coder run: dispatcher runs at autonomy {ceiling}", file=sys.stderr)
            sys.exit(2)
        task = args[-1]
        Path(os.environ["CLIO_CODER_STATE_DIR"], "task.txt").write_text(task)
        out({"type": "session", "schemaVersion": 1, "id": "s-headless", "cwd": cwd,
             "model": "fake-model"})
        if "@HANG" in task:
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            Path(os.environ["CLIO_CODER_STATE_DIR"], "hang-child.pid").write_text(str(child.pid))
            time.sleep(60)
        fix(cwd)
        out({"type": "tool_execution_end", "toolName": "edit", "isError": False})
        out({"type": "notice", "level": "info", "text": "checks passed"})
        out({"type": "agent_end", "usage": {"input": 10, "output": 2, "cost": 0}})
        code = 1 if "@FAIL" in task else 0
        checks = []
        for i, flag in enumerate(a for a in args if a == "--verify"):
            command = args[[j for j, a in enumerate(args) if a == "--verify"][i] + 1]
            ran = subprocess.run(command, shell=True, cwd=cwd)
            checks.append({"id": "run-acceptance", "command": command,
                           "exitCode": ran.returncode, "passed": ran.returncode == 0,
                           "durationMs": 1, "timedOut": False, "ranAfterLastEdit": False})
        Path(os.environ["CLIO_CODER_STATE_DIR"], "args.json").write_text(json.dumps(args))
        out({"type": "turn_end", "exitCode": code, "text": "fixed add",
             "outcome": "failed" if code else "succeeded",
             "outcomeDetail": "limitation" if code else None,
             "safety": {"decisions": 1, "blockedAttempts": 0}}
            | ({"verify": checks} if checks else {}))
        sys.exit(code)

    session = {"id": None, "mode": "default", "cancel": False}
    store = Path(os.environ["CLIO_CODER_STATE_DIR"], "sessions.json")
    for line in sys.stdin:
        msg = json.loads(line)
        method, params = msg.get("method"), msg.get("params") or {}
        if "id" not in msg:
            if method == "session/cancel":
                session["cancel"] = True
            continue
        if method is None:
            continue
        reply = {}
        if method == "initialize":
            reply = {"protocolVersion": 1, "agentCapabilities": {}, "agentInfo": {"name": "fake"}}
        elif method == "session/new":
            session["id"] = "s-acp"
            store.write_text(json.dumps({"s-acp": {"turns": 0, "args": args}}))
            reply = {"sessionId": "s-acp", "modes": {"currentModeId": "default"},
                     "configOptions": [{"id": "model", "currentValue": "fake-model"}]}
        elif method == "session/resume":
            known = json.loads(store.read_text()) if store.exists() else {}
            if params["sessionId"] not in known:
                out({"jsonrpc": "2.0", "id": msg["id"],
                     "error": {"code": -32002, "message": "session not found"}})
                continue
            session["id"] = params["sessionId"]
            known[session["id"]]["args"] = args
            store.write_text(json.dumps(known))
            reply = {"modes": {"currentModeId": "default"},
                     "configOptions": [{"id": "model", "currentValue": "fake-model"}]}
        elif method == "session/set_mode":
            session["mode"] = params["modeId"]
        elif method == "session/prompt":
            text = params["prompt"][0]["text"]
            update = lambda u: out({"jsonrpc": "2.0", "method": "session/update",
                                    "params": {"sessionId": "s-acp", "update": u}})
            if "@DIRTY" in text:
                Path(cwd, "calc.py").write_text("half written")
                Path(cwd, "scratch.txt").write_text("partial")
            if "@CRASH" in text:
                os._exit(3)
            if "@TURNFAIL" in text:
                sys.stderr.write("[clio-coder:acp] turn failed: model no-such-model not found\\n")
                sys.stderr.flush()
                out({"jsonrpc": "2.0", "id": msg["id"],
                     "error": {"code": -32603, "message": "turn_failed",
                               "data": {"_meta": {"clio-coder/error": {"version": 1, "code": "turn_failed"}}}}})
                continue
            if "@HANG" in text:
                while not session["cancel"]:
                    line = sys.stdin.readline()
                    if not line:
                        break
                    if json.loads(line).get("method") == "session/cancel":
                        session["cancel"] = True
                out({"jsonrpc": "2.0", "id": msg["id"], "result": {"stopReason": "cancelled"}})
                continue
            out({"jsonrpc": "2.0", "id": 900, "method": "session/request_permission",
                 "params": {"sessionId": "s-acp", "toolCall": {"toolCallId": "t1"},
                            "options": [
                                {"optionId": "allow-once", "kind": "allow_once"},
                                {"optionId": "reject-once", "kind": "reject_once"},
                                {"optionId": "reject-and-stop", "kind": "reject_once"}]}})
            answer = json.loads(sys.stdin.readline())
            chosen = answer["result"]["outcome"].get("optionId")
            Path(cwd, ".decision").write_text(str(chosen))
            update({"sessionUpdate": "tool_call", "toolCallId": "t1", "title": "edit",
                    "status": "in_progress"})
            fix(cwd, "\\ndef sub(a, b):\\n    return a - b\\n" if "@SUB" in text else "")
            if "@BREAK" in text:
                Path(cwd, "calc.py").write_text("def add(a, b):\\n    return a * b\\n")
            update({"sessionUpdate": "tool_call_update", "toolCallId": "t1",
                    "status": "completed"})
            known = json.loads(store.read_text())
            known[session["id"]]["turns"] += 1
            store.write_text(json.dumps(known))
            update({"sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "done: " + session["mode"]}})
            reply = {"stopReason": "end_turn", "_meta": {"clio-coder/usage": {"quantities": {
                "input": {"fact": {"measured": 5}}, "output": {"fact": {"measured": 1}}}}}}
        out({"jsonrpc": "2.0", "id": msg["id"], "result": reply})
    """
)

CHECK = [sys.executable, "-B", "-c", "import calc; assert calc.add(2, 3) == 5"]


@pytest.fixture
def install(tmp_path: Path) -> ClioCoderInstall:
    script = tmp_path / "fake_clio_coder.py"
    script.write_text(FAKE_CLIO_CODER, "utf-8")
    return ClioCoderInstall((sys.executable, "-B", str(script)))


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    (path / "calc.py").write_text("def add(a, b):\n    return a - b\n", "utf-8")
    for args in (
        ["init", "-q", "-b", "main"],
        ["add", "calc.py"],
        ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"],
    ):
        subprocess.run(["git", *args], cwd=path, check=True)
    return path


def _branches(repo: Path) -> list[str]:
    out = subprocess.run(
        ["git", "branch", "--format=%(refname:short)"], cwd=repo, capture_output=True, text=True
    )
    return out.stdout.split()


def _request(repo: Path, tmp_path: Path, install: ClioCoderInstall, task: str, **kw):
    return DelegationRequest(
        task=task,
        repo=repo,
        state_root=tmp_path / "state",
        install=install,
        target=ClioCoderTarget(model="fake-model", url="http://fake", api_key_env="FAKE_KEY"),
        verify_command=CHECK,
        **kw,
    )


async def test_caller_cancel_stops_headless_child_and_removes_worktree(
    repo: Path, tmp_path: Path, install: ClioCoderInstall, monkeypatch
) -> None:
    from clio_agent.runtime.clio_coder import drivers

    instances = []
    original_spawn = drivers.spawn_instance

    async def tracked_spawn(*args, **kwargs):
        instance = await original_spawn(*args, **kwargs)
        instances.append(instance)
        return instance

    monkeypatch.setattr(drivers, "spawn_instance", tracked_spawn)
    ready = asyncio.Event()
    work = asyncio.create_task(
        delegate_coding_task(
            _request(repo, tmp_path, install, "@HANG"),
            sink=lambda event: ready.set() if event.get("kind") == "session" else None,
        )
    )
    try:
        await asyncio.wait_for(ready.wait(), 10)
        work.cancel()
        with pytest.raises(asyncio.CancelledError):
            await work
        assert instances[0].process.returncode is not None
        assert _branches(repo) == ["main"]
        assert not list((tmp_path / "state" / "worktrees").iterdir())
    finally:
        if not work.done():
            work.cancel()
            with pytest.raises(asyncio.CancelledError):
                await work


async def test_golden_cancel_checks_owned_processes_and_worktree_cleanup(
    tmp_path: Path, install: ClioCoderInstall, monkeypatch
) -> None:
    from scripts import clio_coder_latency as latency

    monkeypatch.setattr(ClioCoderInstall, "resolve", lambda: install)
    monkeypatch.setattr(latency, "TASK", "@HANG")
    template = tmp_path / "template"
    template.mkdir()
    result = await latency.cancel_run(tmp_path / "demo", template, "fake", "fake-model")
    assert result["passed"], result
    assert result["survivors"] == []
    assert result["observed_processes"]


async def test_a_clio_coder_with_run_verify_runs_the_check_itself_without_a_catalog_file(
    repo: Path, tmp_path: Path, install: ClioCoderInstall
) -> None:
    (Path(install.argv[-1]).parent / "verify-flag").touch()
    result = await delegate_coding_task(_request(repo, tmp_path, install, "fix add"))

    state = tmp_path / "state" / "home" / "state"
    args = json.loads((state / "args.json").read_text())
    check = args[args.index("--verify") + 1]
    assert check == shlex.join(CHECK)
    assert args[args.index("--verify-timeout") + 1] == "120"
    assert "your run's acceptance check" in (state / "task.txt").read_text()
    assert not (Path(result.workspace) / ".clio-coder" / "verifiers.yaml").exists()
    assert [c["passed"] for c in result.child_checks] == [True]
    # The parent's own check still decides.
    assert result.verified and result.changed_files == ["calc.py"]


async def test_headless_delegation_hands_back_a_verified_worktree_diff(
    repo: Path, tmp_path: Path, install: ClioCoderInstall, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLIO_AUTH_TOKEN", "parent-secret")
    monkeypatch.setenv("CLIO_CODER_STATE_DIR", "/operator/state")
    events: list[dict] = []
    result = await delegate_coding_task(
        _request(repo, tmp_path, install, "fix add"), sink=events.append
    )

    assert (result.surface, result.outcome, result.exit_code) == ("headless", "succeeded", 0)
    assert result.verified and result.session_id == "s-headless"
    assert result.changed_files == ["calc.py"] and "+    return a + b" in result.diff
    assert result.usage["input"] == 10 and result.tools[0].name == "edit"
    assert [event["kind"] for event in events][:2] == ["session", "tool"]
    # The operator's checkout is untouched; the work waits on its own branch.
    assert "a - b" in (repo / "calc.py").read_text()
    assert result.branch in _branches(repo) and Path(result.workspace).is_dir()
    # The branch carries the change itself, without the check's or the instance's artifacts.
    tip = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", result.branch], capture_output=True, text=True
    ).stdout.strip()
    assert result.commit == tip != result.base_commit
    committed = subprocess.run(
        ["git", "-C", str(repo), "diff", "--name-only", result.base_commit, tip],
        capture_output=True,
        text=True,
    ).stdout.split()
    assert committed == ["calc.py"]
    shown = subprocess.run(
        ["git", "-C", str(repo), "show", f"{tip}:calc.py"], capture_output=True, text=True
    ).stdout
    assert "a + b" in shown
    # The acceptance check is declared to the instance but never handed back as a change.
    verifiers = yaml.safe_load(
        (Path(result.workspace) / ".clio-coder" / "verifiers.yaml").read_text()
    )
    assert verifiers["checks"][0]["id"] == ACCEPTANCE_CHECK_ID
    state = tmp_path / "state" / "home" / "state"
    seen = json.loads((state / "env.json").read_text())
    assert "CLIO_AUTH_TOKEN" not in seen and seen["CLIO_CODER_STATE_DIR"] == str(state)
    assert seen["CLIO_CODER_INTEROP_HOME"] == "none"
    settings = yaml.safe_load((tmp_path / "state/home/config/settings.yaml").read_text())
    assert settings["targets"][0]["auth"] == {"apiKeyEnvVar": "FAKE_KEY"}
    subprocess.run(
        ["git", "-C", str(repo), "apply", "--check"], input=result.diff, text=True, check=True
    )
    subprocess.run(["git", "-C", str(repo), "apply"], input=result.diff, text=True, check=True)
    subprocess.run(CHECK, cwd=repo, check=True)


@pytest.mark.skipif(os.name == "nt", reason="the check below is a POSIX shell command")
async def test_checkout_paths_in_the_task_and_check_are_mapped_into_the_worktree(
    repo: Path, tmp_path: Path, install: ClioCoderInstall
) -> None:
    root = Path(
        subprocess.check_output(
            ["git", "-C", str(repo), "rev-parse", "--show-toplevel"], text=True
        ).strip()
    )
    check = f"cd {root} && {sys.executable} -B -c 'import calc; assert calc.add(2, 3) == 5'"
    request = _request(repo, tmp_path, install, f"fix {root}/calc.py", keep_worktree=False)
    request = dataclasses.replace(request, verify_command=["/bin/sh", "-c", check])
    result = await delegate_coding_task(request)

    # The checkout still has the bug, so the check only passes because it ran in the worktree.
    assert result.verified, result.verification
    seen = (tmp_path / "state" / "home" / "state" / "task.txt").read_text()
    assert f"fix {result.workspace}/calc.py" in seen and f"{root}/" not in seen


async def test_headless_limitation_is_reported_alongside_parent_verification(
    repo: Path, tmp_path: Path, install: ClioCoderInstall
) -> None:
    result = await delegate_coding_task(_request(repo, tmp_path, install, "fix then @FAIL"))
    assert (result.outcome, result.outcome_detail, result.exit_code) == (
        "failed",
        "limitation",
        1,
    )
    assert result.verified
    assert result.to_dict()["verified"] is True


async def test_headless_watchdog_stops_a_hung_instance_and_discards_nothing_kept(
    repo: Path, tmp_path: Path, install: ClioCoderInstall, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("clio_agent.runtime.clio_coder.drivers._WATCHDOG_SLACK_SECONDS", 0)
    request = _request(
        repo, tmp_path, install, "@HANG", options=RunOptions(timeout=1), keep_worktree=False
    )
    result = await delegate_coding_task(request)
    assert (result.outcome, result.outcome_detail) == ("timed_out", "parent_watchdog")
    assert not result.verified and not Path(result.workspace).exists()
    assert _branches(repo) == ["main"]


async def test_a_run_refused_before_its_session_reports_why(
    repo: Path, tmp_path: Path, install: ClioCoderInstall, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLIO_CODER_AUTONOMY_CEILING", "default")
    request = _request(
        repo, tmp_path, install, "fix add", surface="headless", options=RunOptions(autonomy="yolo")
    )
    result = await delegate_coding_task(request)

    assert (result.outcome, result.exit_code, result.session_id) == ("failed", 2, None)
    assert result.error == {"message": "clio-coder run: dispatcher runs at autonomy default"}


async def test_acp_multi_turn_session_applies_mode_and_permission_policy(
    repo: Path, tmp_path: Path, install: ClioCoderInstall
) -> None:
    request = _request(
        repo,
        tmp_path,
        install,
        "fix add",
        follow_ups=["now add @SUB"],
        options=RunOptions(autonomy="yolo"),
    )
    result = await delegate_coding_task(request, on_permission=allow_permissions_once)

    assert (result.surface, result.outcome, result.outcome_detail) == (
        "acp",
        "succeeded",
        "end_turn",
    )
    assert result.verified and result.model == "fake-model"
    assert result.text == "done: yolo" and result.usage["input"] == 10
    assert "+def sub(a, b):" in result.diff and ".decision" in result.changed_files
    assert (Path(result.workspace) / ".decision").read_text() == "allow-once"
    assert [(tool.name, tool.status) for tool in result.tools] == [("edit", "completed")]


async def test_acp_default_policy_rejects_once_and_timeout_cancels_the_turn(
    repo: Path, tmp_path: Path, install: ClioCoderInstall
) -> None:
    rejected = await delegate_coding_task(
        _request(repo, tmp_path, install, "fix add", surface="acp"),
        on_permission=reject_permissions,
    )
    assert (Path(rejected.workspace) / ".decision").read_text() == "reject-once"

    hung = await delegate_coding_task(
        _request(
            repo,
            tmp_path,
            install,
            "@HANG",
            surface="acp",
            options=RunOptions(timeout=1),
            run_id="hung",
        )
    )
    assert (hung.outcome, hung.outcome_detail) == ("timed_out", "parent_timeout")
    assert hung.exit_code == 0  # the instance settled the cancel and exited on stdin close


async def test_follow_ups_need_the_acp_surface(
    repo: Path, tmp_path: Path, install: ClioCoderInstall
) -> None:
    request = _request(repo, tmp_path, install, "x", follow_ups=["y"], surface="headless")
    with pytest.raises(ValueError, match="ACP"):
        await delegate_coding_task(request)


def test_instance_home_refuses_relative_and_real_homes(tmp_path: Path) -> None:
    with pytest.raises(ClioCoderError):
        InstanceHome(Path("relative"))
    with pytest.raises(ClioCoderError):
        InstanceHome(Path.home())
    with pytest.raises(ClioCoderError):
        InstanceHome(Path.home() / ".config" / "clio-coder" / "nested")
    env = InstanceHome(tmp_path / "home").environment(
        {"PATH": "/bin", "CLIO_SECRET_X": "s", "CLIO_GACT_BEARER_TOKEN": "t", "CLIO_CODER_X": "y"}
    )
    assert env["PATH"] == "/bin" and env["TMPDIR"] == str(tmp_path / "home" / "tmp")
    assert not {"CLIO_SECRET_X", "CLIO_GACT_BEARER_TOKEN", "CLIO_CODER_X"} & env.keys()


def test_a_later_route_is_added_to_the_home_and_its_key_reaches_only_the_child(
    tmp_path: Path,
) -> None:
    import yaml

    home = InstanceHome(tmp_path / "home", extra_env={"CLIO_DELEGATED_MODEL_API_KEY": "k"})
    first = ClioCoderTarget(model="m1", target_id="clio-a", url="http://a/v1")
    home.prepare(first)
    home.prepare(ClioCoderTarget(model="m2", target_id="clio-b", api_key_env="K"))
    home.prepare(ClioCoderTarget(model="m3", target_id="clio-a", url="http://a2/v1"))
    settings = yaml.safe_load((tmp_path / "home" / "config" / "settings.yaml").read_text())
    assert [(t["id"], t["defaultModel"]) for t in settings["targets"]] == [
        ("clio-b", "m2"),
        ("clio-a", "m3"),
    ]
    assert settings["chat"]["target"] == "clio-a" and first.runtime == "openai-compat"
    assert home.environment({"PATH": "/bin"})["CLIO_DELEGATED_MODEL_API_KEY"] == "k"
    assert "CLIO_DELEGATED_MODEL_API_KEY" not in os.environ


@pytest.mark.skipif(os.name == "nt", reason="the fake install is a POSIX shell script")
def test_doctor_and_tool_require_the_minimum_clio_coder_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clio_agent.gact.clio_coder_tool import clio_coder_unavailable_reason
    from clio_agent.runtime.clio_coder.doctor import probe_clio_coder

    def install(name: str, output: str) -> None:
        script = tmp_path / name
        script.write_text(f"#!/bin/sh\necho '{output}'\n")
        script.chmod(0o755)
        monkeypatch.setenv("CLIO_AGENT_CLIO_CODER_BIN", str(script))

    install("new", "Clio Coder 0.7.0")
    row = probe_clio_coder()
    assert (row.state.value, row.details["version"], row.config_source) == (
        "ready",
        "0.7.0",
        "CLIO_AGENT_CLIO_CODER_BIN",
    )
    assert clio_coder_unavailable_reason() is None
    install("old", "Clio Coder 0.6.13")
    assert probe_clio_coder().state.value == "misconfigured"
    assert clio_coder_unavailable_reason() == "clio_coder_too_old"
    install("mute", "")
    assert probe_clio_coder().state.value == "degraded"
    assert clio_coder_unavailable_reason() == "clio_coder_version_unreadable"
    monkeypatch.setenv("CLIO_AGENT_CLIO_CODER_BIN", str(tmp_path / "missing"))
    assert probe_clio_coder().state.value == "misconfigured"
    assert "Correct or unset" in probe_clio_coder().next_action
    monkeypatch.setenv("CLIO_AGENT_CLIO_CODER_BIN", "")
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert probe_clio_coder().state.value == "skipped"
    assert clio_coder_unavailable_reason() == "clio_coder_not_installed"


def test_child_environment_keeps_only_narrowing_clio_coder_limits(tmp_path: Path) -> None:
    base = {
        "PATH": "/bin",
        "CLIO_AUTH_TOKEN": "parent-secret",
        "CLIO_CODER_STATE_DIR": "/operator/state",
        "CLIO_CODER_AUTONOMY_CEILING": "default",
        "CLIO_CODER_YOLO_CONFINEMENT": "contained",
    }
    env = InstanceHome(tmp_path / "home").environment(base)

    assert "CLIO_AUTH_TOKEN" not in env and env["CLIO_CODER_STATE_DIR"] != "/operator/state"
    # A dispatcher above CLIO capped autonomy; the delegated run must inherit that cap.
    assert env["CLIO_CODER_AUTONOMY_CEILING"] == "default"
    assert env["CLIO_CODER_YOLO_CONFINEMENT"] == "contained"


def test_install_resolution_prefers_explicit_then_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLIO_AGENT_CLIO_CODER_BIN", sys.executable)
    assert ClioCoderInstall.resolve().argv == (sys.executable,)
    monkeypatch.setenv("CLIO_AGENT_CLIO_CODER_BIN", str(tmp_path / "missing"))
    assert ClioCoderInstall.resolve(sys.executable).argv == (sys.executable,)
    with pytest.raises(ClioCoderError, match="missing or not executable"):
        ClioCoderInstall.resolve()
    with pytest.raises(ClioCoderError):
        ClioCoderInstall.resolve(str(tmp_path / "missing.js"))


def test_failed_version_command_is_not_a_supported_install(tmp_path: Path) -> None:
    script = tmp_path / "failed_version.py"
    script.write_text("print('Clio Coder 0.7.0')\nraise SystemExit(1)\n")
    install = ClioCoderInstall((sys.executable, str(script)))
    with pytest.raises(ClioCoderError, match="Cannot read Clio Coder's version") as raised:
        install.require_supported()
    assert raised.value.error_type == "clio_coder_version_unreadable"


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable fixture")
def test_relative_binary_override_survives_the_child_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = tmp_path / "clio-coder"
    entry.write_text("#!/bin/sh\necho 'Clio Coder 0.7.0'\n")
    entry.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    install = ClioCoderInstall.resolve("./clio-coder")
    elsewhere = tmp_path / "worktree"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert install.require_supported() == (0, 7, 0)


def test_acceptance_check_merges_into_a_worktree_catalog_only(repo: Path, tmp_path: Path) -> None:
    workspace = create_worktree(repo, tmp_path / "wt", "r1")
    catalog = workspace.path / ".clio-coder" / "verifiers.yaml"
    catalog.parent.mkdir()
    catalog.write_text(yaml.safe_dump({"version": 2, "checks": [{"id": "lint", "command": []}]}))
    assert declare_acceptance_check(workspace, ["make", "test"], 30)
    assert declare_acceptance_check(workspace, ["make", "check"], 30)
    checks = yaml.safe_load(catalog.read_text())["checks"]
    assert [check["id"] for check in checks] == ["lint", ACCEPTANCE_CHECK_ID]
    assert checks[1]["command"] == ["make", "check"]
    assert not declare_acceptance_check(adopt_directory(repo), ["make"], 30)
    assert "verifier `clio-agent-acceptance`" in compose_task("t", ["make"], True)
    assert compose_task("t", None, False) == "t"


def test_acceptance_check_is_declared_only_as_a_shell_free_command(
    repo: Path, tmp_path: Path
) -> None:
    # Clio Coder's catalog refuses shell executables, so shell text is unwrapped or not declared.
    workspace = create_worktree(repo, tmp_path / "wt", "r1")
    catalog = workspace.path / ".clio-coder" / "verifiers.yaml"
    assert declare_acceptance_check(workspace, ["/bin/sh", "-c", "pytest -q 'tests/a b.py'"], 30)
    assert yaml.safe_load(catalog.read_text())["checks"][0]["command"] == [
        "pytest",
        "-q",
        "tests/a b.py",
    ]
    for shell_only in (["/bin/sh", "-c", "pytest -q && ruff check"], ["bash", "x.sh"]):
        assert not declare_acceptance_check(workspace, shell_only, 30)
        assert yaml.safe_load(catalog.read_text())["checks"] == []


def test_exit_codes_and_usage_facts_fold_to_outcomes() -> None:
    assert [outcome_for_exit(code) for code in (0, 1, 4, 124, 143, -15, None)] == [
        "succeeded",
        "failed",
        "cost_capped",
        "timed_out",
        "canceled",
        "canceled",
        "canceled",
    ]
    acp = summarize_usage({"quantities": {"input": {"fact": {"measured": 7}}}})
    assert acp["input"] == 7


@pytest.mark.skipif(os.name == "nt", reason="the terminal host here is a POSIX shell")
async def test_a_tui_session_launches_fenced_and_hands_back_like_a_delegation(
    repo: Path, tmp_path: Path, install: ClioCoderInstall, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clio_agent.runtime.clio_coder.interactive import (
        finish_interactive_session,
        prepare_interactive_session,
    )

    monkeypatch.setenv("CLIO_AUTH_TOKEN", "parent-secret")
    session = prepare_interactive_session(
        _request(repo, tmp_path, install, f"fix {repo.resolve()}/calc.py"), autonomy="yolo"
    )
    assert session.launch.cwd == session.workspace.path != repo.resolve()
    assert session.launch.argv[-2:] == ("--autonomy", "yolo")
    assert f"fix {session.workspace.path}/calc.py" in session.brief()

    opened = session.launch.terminal_open_args({"CLIO_AUTH_TOKEN": "x", "PATH": "/bin"})
    assert opened["argv"] == list(session.launch.argv)
    assert opened["env_clear"] and "CLIO_AUTH_TOKEN" not in opened["env"]
    assert opened["cwd"] == str(session.workspace.path) and "CLIO_CODER_STATE_DIR" in opened["env"]

    exit_file = tmp_path / "tui.exit"
    command = session.launch.shell_command(exit_file=exit_file)
    subprocess.run(["/bin/sh", "-c", command], stdin=subprocess.DEVNULL, check=True, timeout=60)
    assert exit_file.read_text().strip() == "0"
    seen = json.loads((tmp_path / "state" / "home" / "state" / "env.json").read_text())
    assert "CLIO_AUTH_TOKEN" not in seen
    assert seen["CLIO_CODER_STATE_DIR"] == str(tmp_path / "state" / "home" / "state")

    # What the person and the TUI changed in the worktree is handed back, committed and checked.
    (session.workspace.path / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    result = await finish_interactive_session(session)
    assert (result.surface, result.outcome, result.verified) == ("tui", "succeeded", True)
    assert result.changed_files == ["calc.py"] and result.commit
    tip = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", result.branch], capture_output=True, text=True
    ).stdout.strip()
    assert tip == result.commit
    assert session.session_file is None and result.session_id is None


@pytest.mark.skipif(os.name == "nt", reason="the terminal host here is a POSIX shell")
@pytest.mark.parametrize("fixed", [True, False])
async def test_tui_shell_acceptance_runs_at_hand_back_without_an_invalid_catalog(
    repo: Path, tmp_path: Path, install: ClioCoderInstall, fixed: bool
) -> None:
    from clio_agent.runtime.clio_coder.interactive import (
        finish_interactive_session,
        prepare_interactive_session,
    )

    request = _request(repo, tmp_path, install, "fix calc")
    request.verify_command = ["/bin/sh", "-c", shlex.join(CHECK)]
    session = prepare_interactive_session(request)
    assert not (session.workspace.path / ".clio-coder" / "verifiers.yaml").exists()
    assert "CLIO runs the shell acceptance check after you quit" in session.brief()
    if fixed:
        (session.workspace.path / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    result = await finish_interactive_session(session, keep_worktree=False)
    assert result.verified is fixed and bool(result.commit) is fixed
    assert result.notices == session.notices


@pytest.mark.skipif(os.name == "nt", reason="the terminal host here is a POSIX shell")
async def test_a_tui_that_writes_a_session_record_hands_back_its_session_and_outcome(
    repo: Path, tmp_path: Path, install: ClioCoderInstall
) -> None:
    from clio_agent.runtime.clio_coder.interactive import (
        finish_interactive_session,
        prepare_interactive_session,
    )

    (Path(install.argv[-1]).parent / "session-file-flag").touch()
    session = prepare_interactive_session(_request(repo, tmp_path, install, "fix calc"))
    assert session.session_file is not None
    assert str(session.session_file) in session.launch.argv
    command = session.launch.shell_command()
    subprocess.run(["/bin/sh", "-c", command], stdin=subprocess.DEVNULL, check=True, timeout=60)

    # The host saw no exit status; the record supplies it with the session, model and usage.
    result = await finish_interactive_session(session, exit_code=None)
    assert (result.outcome, result.exit_code, result.session_id) == ("succeeded", 0, "s-tui")
    assert result.model == "tui-model" and result.usage["tokens"] == "1,234"

    (session.workspace.path / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    host_failed = await finish_interactive_session(session, exit_code=1)
    assert host_failed.outcome == "failed" and host_failed.exit_code == 0
    assert host_failed.error and host_failed.error["error"] == "clio_coder_terminal_failed"
    assert host_failed.verified

    session.session_file.write_text(
        json.dumps({"version": 1, "outcome": "interrupted", "exitCode": 130})
    )
    again = await finish_interactive_session(session, exit_code=0, keep_worktree=False)
    assert (again.outcome, again.exit_code) == ("canceled", 130)


def test_instance_runtime_and_terminal_environment_are_host_independent(tmp_path: Path) -> None:
    from clio_agent.runtime.clio_coder.interactive import TerminalLaunch

    home = InstanceHome(tmp_path / "instance home", extra_env={"XDG_RUNTIME_DIR": "/stray"})
    home.prepare(None)
    env = home.environment(
        {"XDG_RUNTIME_DIR": "/host/runtime", "CLIO_AUTH_TOKEN": "private", "PATH": "/bin"}
    )
    assert env["XDG_RUNTIME_DIR"] == str(home.root / "runtime")
    assert (home.root / "runtime").stat().st_mode & 0o777 == 0o700
    launch = TerminalLaunch(
        (r"C:\Program Files\Clio\clio.exe", "--session-file", r"C:\work space\session.json"),
        tmp_path,
        env,
    ).terminal_open_args()
    assert launch["argv"][0] == r"C:\Program Files\Clio\clio.exe"
    assert launch["env_clear"] is True and launch["env"] == env


@pytest.mark.parametrize(
    "record",
    [
        None,
        "not JSON",
        "[]",
        '{"exitCode": true}',
        '{"version": true, "exitCode": 0, "outcome": "quit"}',
        '{"version": 1, "exitCode": 0, "outcome": []}',
        '{"version": 1, "exitCode": 0, "outcome": "quit", "sessionId": 3}',
    ],
)
async def test_missing_or_invalid_tui_records_are_not_successful_handbacks(
    repo: Path, tmp_path: Path, install: ClioCoderInstall, record: str | None
) -> None:
    from clio_agent.runtime.clio_coder.interactive import (
        finish_interactive_session,
        prepare_interactive_session,
    )

    (Path(install.argv[-1]).parent / "session-file-flag").touch()
    session = prepare_interactive_session(_request(repo, tmp_path, install, "fix calc"))
    assert session.session_file is not None
    if record is not None:
        session.session_file.write_text(record)
    result = await finish_interactive_session(session, exit_code=0, keep_worktree=False)
    assert result.outcome == "failed"
    assert result.error and result.error["error"] == "clio_coder_protocol_error"


async def test_interactive_commit_failure_is_not_a_success(
    repo: Path, tmp_path: Path, install: ClioCoderInstall, monkeypatch
) -> None:
    from clio_agent.runtime.clio_coder import interactive

    session = interactive.prepare_interactive_session(_request(repo, tmp_path, install, "fix calc"))

    def failed_commit(*args):
        raise ClioCoderError("clio_coder_workspace_error", "commit failed")

    monkeypatch.setattr(interactive, "commit_changes", failed_commit)
    result = await interactive.finish_interactive_session(session, keep_worktree=False)
    assert result.outcome == "failed"
    assert result.error["message"] == "commit failed"
