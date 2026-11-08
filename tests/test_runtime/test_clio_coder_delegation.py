"""Delegating coding work to a spawned Clio Coder instance, against scripted fake servers.

The fakes speak the two real surfaces: ``run --json-events terminal`` (one JSONL turn) and
``acp`` (JSON-RPC over stdio). Each fixes a file in the workspace the way a real instance would,
so the hand-back, the parent's verification and worktree cleanup are exercised end to end.
"""

from __future__ import annotations

import json
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
    import json, os, sys, time
    from pathlib import Path

    def out(message):
        sys.stdout.write(json.dumps(message) + "\\n")
        sys.stdout.flush()

    def fix(cwd, extra=""):
        Path(cwd, "calc.py").write_text("def add(a, b):\\n    return a + b\\n" + extra)

    def env_report(cwd):
        keys = sorted(k for k in os.environ if k.startswith(("CLIO_", "SECRET")))
        Path(os.environ["CLIO_CODER_STATE_DIR"], "env.json").write_text(
            json.dumps({k: os.environ[k] for k in keys} | {"TMPDIR": os.environ["TMPDIR"]})
        )

    args = sys.argv[1:]
    cwd = args[args.index("--cwd") + 1]
    env_report(cwd)
    if args[0] == "run":
        task = args[-1]
        out({"type": "session", "schemaVersion": 1, "id": "s-headless", "cwd": cwd,
             "model": "fake-model"})
        if "@HANG" in task:
            time.sleep(60)
        fix(cwd)
        out({"type": "tool_execution_end", "toolName": "edit", "isError": False})
        out({"type": "notice", "level": "info", "text": "checks passed"})
        out({"type": "agent_end", "usage": {"input": 10, "output": 2, "cost": 0}})
        code = 1 if "@FAIL" in task else 0
        out({"type": "turn_end", "exitCode": code, "text": "fixed add",
             "outcome": "failed" if code else "succeeded",
             "outcomeDetail": "limitation" if code else None,
             "safety": {"decisions": 1, "blockedAttempts": 0}})
        sys.exit(code)

    session = {"id": None, "mode": "default", "cancel": False}
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
            reply = {"sessionId": "s-acp", "modes": {"currentModeId": "default"},
                     "configOptions": [{"id": "model", "currentValue": "fake-model"}]}
        elif method == "session/set_mode":
            session["mode"] = params["modeId"]
        elif method == "session/prompt":
            text = params["prompt"][0]["text"]
            update = lambda u: out({"jsonrpc": "2.0", "method": "session/update",
                                    "params": {"sessionId": "s-acp", "update": u}})
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
            update({"sessionUpdate": "tool_call_update", "toolCallId": "t1",
                    "status": "completed"})
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


def test_install_resolution_prefers_explicit_then_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLIO_AGENT_CLIO_CODER_BIN", "/opt/clio-coder")
    assert ClioCoderInstall.resolve().argv == ("/opt/clio-coder",)
    assert ClioCoderInstall.resolve("/x/clio").argv == ("/x/clio",)
    with pytest.raises(ClioCoderError):
        ClioCoderInstall.resolve(str(tmp_path / "missing.js"))


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
