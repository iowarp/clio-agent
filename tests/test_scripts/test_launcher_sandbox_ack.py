"""The launcher warns at start when agent tools run without an OS sandbox.

CLIO fails open when no write fence resolves, so ``clio``/``clio start``/``clio web``
must show a prominent warning. An interactive terminal must confirm it; other runs
only warn. Declining stops a server that this invocation started.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

LAUNCHER = Path(__file__).resolve().parents[2] / "install" / "clio"
BASH = shutil.which("bash") or "bash"

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX launcher")

_INACTIVE = {
    "name": "sandbox",
    "status": "degraded",
    "summary": "No OS write-confinement (mechanism=none, reason=codex_not_installed).",
    "reason": "codex_not_installed",
    "next_action": "Install codex: npm install -g @openai/codex",
    "active": False,
    "resolved": True,
    "mechanism": "none",
    "acknowledgement_required": True,
    "warning": "Protected execution is off. Agent tools can change any file.",
}
_ACTIVE = {
    **_INACTIVE,
    "status": "ready",
    "reason": "fence_active",
    "active": True,
    "mechanism": "codex",
    "acknowledgement_required": False,
    "warning": None,
}
_UNRESOLVED = {**_INACTIVE, "status": "ready", "resolved": False, "acknowledgement_required": False}
_STARTING = {"error": {"error": "task_runtime_starting", "recoverable": True}}


def _function(name: str) -> str:
    text = LAUNCHER.read_text(encoding="utf-8")
    start = text.index(f"{name}() {{")
    return text[start : text.index("\n}\n", start) + 3]


def _setup(root: Path, bodies: list[dict]) -> dict[str, str]:
    tools = root / "tools"
    tools.mkdir()
    for name in ("cat", "seq", "sleep"):
        found = shutil.which(name)
        assert found, name
        (tools / name).symlink_to(found)
    responses = root / "responses"
    responses.mkdir()
    for index, body in enumerate(bodies, start=1):
        (responses / f"{index}.json").write_text(json.dumps(body))
    (responses / "last.json").write_text(json.dumps(bodies[-1]))
    (tools / "curl").write_text(
        "#!/bin/sh\n"
        f'n=$(cat "{root}/count" 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" >"{root}/count"\n'
        f'if [ -f "{responses}/$n.json" ]; then cat "{responses}/$n.json";'
        f' else cat "{responses}/last.json"; fi\n'
    )
    (tools / "curl").chmod(0o755)
    venv_bin = root / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").symlink_to(sys.executable)
    return {"PATH": str(tools), "HOME": str(root)}


def _run(
    root: Path,
    bodies: list[dict],
    *,
    answer: str | None = None,
    started: int = 1,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {**_setup(root, bodies), **(extra_env or {})}
    script = (
        "set -euo pipefail\n"
        "RED=''; RESET=''\n"
        'warn() { echo "WARN $*" >&2; }\n'
        'err() { echo "ERR $*" >&2; }\n'
        "stop_server() { echo STOPPED; }\n"
        f'CLIO_VENV="{root / "venv"}"\n'
        "CLIO_PORT=1\n"
        f"SERVER_WAS_STARTED={started}\n"
        f"{_function('sandbox_ack')}"
        "if sandbox_ack; then echo PROCEED; else echo REFUSED; fi\n"
    )
    if answer is None:
        return subprocess.run(
            [BASH, "-c", script],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
            check=False,
        )
    primary, secondary = os.openpty()
    try:
        os.write(primary, answer.encode())
        return subprocess.run(
            [BASH, "-c", script],
            stdin=secondary,
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
            check=False,
        )
    finally:
        os.close(primary)
        os.close(secondary)


def test_active_sandbox_starts_silently(tmp_path: Path) -> None:
    result = _run(tmp_path, [_ACTIVE], answer="n\n")
    assert result.stdout.strip() == "PROCEED"
    assert "SECURITY WARNING" not in result.stderr


def test_non_interactive_run_warns_and_continues(tmp_path: Path) -> None:
    result = _run(tmp_path, [_INACTIVE])
    assert "PROCEED" in result.stdout
    assert "SECURITY WARNING: protected execution (OS sandbox) is OFF" in result.stderr
    assert _INACTIVE["warning"] in result.stderr
    assert "reason: codex_not_installed" in result.stderr
    assert "next:   Install codex" in result.stderr
    assert "not an interactive terminal" in result.stderr


def test_interactive_run_must_confirm(tmp_path: Path) -> None:
    result = _run(tmp_path, [_INACTIVE], answer="y\n")
    assert "PROCEED" in result.stdout
    assert "Continue without protected execution? [y/N]" in result.stderr


@pytest.mark.parametrize("answer", ["n\n", "\n", "maybe\n"])
def test_interactive_decline_stops_the_server_it_started(tmp_path: Path, answer: str) -> None:
    result = _run(tmp_path, [_INACTIVE], answer=answer)
    assert result.stdout.split() == ["STOPPED", "REFUSED"]
    assert "not acknowledged" in result.stderr


def test_decline_leaves_a_server_it_did_not_start_running(tmp_path: Path) -> None:
    result = _run(tmp_path, [_INACTIVE], answer="n\n", started=0)
    assert result.stdout.split() == ["REFUSED"]


def test_env_acknowledgement_skips_the_prompt(tmp_path: Path) -> None:
    result = _run(tmp_path, [_INACTIVE], answer="n\n", extra_env={"CLIO_ACCEPT_UNSANDBOXED": "1"})
    assert "PROCEED" in result.stdout
    assert "SECURITY WARNING" in result.stderr
    assert "Continue without" not in result.stderr


def test_waits_for_boot_to_resolve_the_backend(tmp_path: Path) -> None:
    result = _run(tmp_path, [_STARTING, _UNRESOLVED, _INACTIVE])
    assert "PROCEED" in result.stdout
    assert "SECURITY WARNING" in result.stderr
    assert (tmp_path / "count").read_text().strip() == "3"


def test_server_without_acknowledgement_fields_falls_back_to_status(tmp_path: Path) -> None:
    legacy = {
        key: value
        for key, value in _INACTIVE.items()
        if key not in {"active", "resolved", "mechanism", "acknowledgement_required", "warning"}
    }
    result = _run(tmp_path, [legacy])
    assert "SECURITY WARNING" in result.stderr
    assert _INACTIVE["summary"] in result.stderr


def test_launcher_acknowledges_on_every_start_path() -> None:
    text = LAUNCHER.read_text(encoding="utf-8")
    assert "start)        update_check; start_server && sandbox_ack ;;" in text
    restart = text[text.index("    restart)") : text.index("    update)")]
    assert restart.rstrip().endswith("stop_server; start_server && sandbox_ack ;;")
    web = text[text.index('""|web|--web)') : text.index("desktop|--desktop)")]
    assert web.index("sandbox_ack || return 1") < web.index("open_url")
