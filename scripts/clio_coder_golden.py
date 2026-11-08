"""Run the CLIO / Clio Coder golden checks. See docs/clio-coder-delegation.md."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import psutil

from clio_agent.runtime.clio_coder.launch import ClioCoderError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.clio_coder_latency import (  # noqa: E402
    TASK,
    cancel_run,
    fixture_repo,
    git,
    measured_run,
    save,
)

RUNTIME_TEST = "tests/test_runtime/test_clio_coder_delegation.py"
GROUPS = {
    "Mode A hand-back and apply (scripted child)": [
        "test_headless_delegation_hands_back_a_verified_worktree_diff",
    ],
    "Mode C multi-turn (scripted ACP stub; not ix-acp qualification)": [
        "test_acp_multi_turn_session_applies_mode_and_permission_policy",
    ],
    "autonomy ceiling refusal": [
        "test_delegated_autonomy_inherits_the_conversation_and_never_exceeds_it",
        "test_a_run_refused_before_its_session_reports_why",
    ],
    "model resolution: one local target and catalog": [
        "test_with_nothing_configured_every_layer_collapses_to_the_conversations_model",
        "test_catalog_override_uses_the_selected_providers_endpoint",
    ],
    "cancel": [
        "test_caller_cancel_stops_headless_child_and_removes_worktree",
        "test_acp_default_policy_rejects_once_and_timeout_cancels_the_turn",
    ],
}


def verdict(name: str, passed: bool, detail: str = "") -> bool:
    print(f"{'PASS' if passed else 'FAIL'} {name}" + (f": {detail}" if detail else ""), flush=True)
    return passed


def offline(out: Path, protocol_only: bool) -> bool:
    """Run existing protocol regressions once; preserve the runner's failure status."""
    xml = out / "offline.xml"
    command = [
        sys.executable,
        "-B",
        "scripts/run_tests.py",
        RUNTIME_TEST,
        "tests/test_gact/test_clio_coder_tool.py",
        "tests/test_gact/test_clio_coder_route.py",
        "-q",
        f"--junitxml={xml}",
    ]
    if protocol_only:
        command.append("--confcutdir=tests/test_runtime")
    done = subprocess.run(command, cwd=ROOT, check=False)
    cases = ET.parse(xml).getroot().findall(".//testcase") if xml.exists() else []
    by_name = {case.attrib["name"]: case for case in cases}
    complete = done.returncode == 0
    for label, names in GROUPS.items():
        passed = all(
            name in by_name
            and not any(
                by_name[name].find(tag) is not None for tag in ("error", "failure", "skipped")
            )
            for name in names
        )
        complete = verdict(label, passed) and complete
    return complete


def request(server: str, path: str, body: dict | None = None) -> Any:
    """Talk only to an explicitly selected loopback GACT server."""
    headers = {"Content-Type": "application/json"}
    token = os.environ.get("CLIO_GACT_BEARER_TOKEN") or os.environ.get("CLIO_AUTH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        server.rstrip("/") + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers=headers,
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        raw = response.read()
        return json.loads(raw) if raw else None


def handbacks(messages: list[dict]) -> list[dict]:
    """Accept typed tool results, never a mention of delegation in assistant prose."""
    rows = []
    for message in messages:
        for part in message.get("parts", []):
            if part.get("type") != "tool_result" or part.get("tool_name") != "run_clio_coder":
                continue
            value = part.get("structured_content")
            if isinstance(value, dict):
                rows.append(value)
                continue
            for block in part.get("content", []):
                if block.get("type") != "text":
                    continue
                try:
                    value = json.loads(block.get("text", ""))
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    rows.append(value)
    return rows


def plain_request(server: str, root: Path) -> dict:
    """Ask the live parent normally; require a verified headless hand-back and applied fix."""
    repo = fixture_repo(root)
    workspace = request(
        server, "/v1/workspaces", {"name": "Delegation golden", "root_path": str(repo)}
    )
    session = request(
        server,
        "/v1/sessions",
        {
            "workspace_id": workspace["id"],
            "title": "Plain coding request",
            "approval_mode": "bypass",
            "agent": {"id": "clio/standard"},
        },
    )
    sid = session["id"]
    started = time.monotonic()
    terminal = False
    try:
        request(
            server,
            f"/v1/sessions/{sid}/messages",
            {"text": TASK, "behavior": {"confirmation_policy": "bypass"}},
        )
        deadline = started + 300
        while time.monotonic() < deadline:
            page = request(server, f"/v1/sessions/{sid}/messages?limit=100")
            messages = page.get("messages", [])
            current = request(server, f"/v1/sessions/{sid}")
            if current.get("status") in ("error", "idle") and any(
                item.get("role") == "assistant" for item in messages
            ):
                terminal = True
                break
            time.sleep(0.5)
        if not terminal:
            raise TimeoutError(f"session {sid} did not finish in 300s")
        save(root / "messages.json", page)
        rows = handbacks(messages)
        accepted = any(
            row.get("surface") == "headless"
            and row.get("outcome") == "succeeded"
            and row.get("verified") is True
            and row.get("commit")
            for row in rows
        )
        checked = subprocess.run(
            [sys.executable, "-B", "-c", "import calc; assert calc.add(2, 3) == 5"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )
        return {
            "passed": bool(accepted) and checked.returncode == 0,
            "session": sid,
            "workspace": workspace["id"],
            "total_s": time.monotonic() - started,
            "handbacks": rows,
            "checkout_check_exit": checked.returncode,
            "note": "The parent must choose delegation and apply it; the demo never edits its fix.",
        }
    finally:
        if not terminal:
            request(server, f"/v1/sessions/{sid}/cancel", {})


async def live(args: argparse.Namespace) -> bool:
    from clio_agent.runtime import sandbox
    from clio_agent.runtime.clio_coder.doctor import probe_clio_coder

    doctor = probe_clio_coder()
    ready = doctor.state.value == "ready"
    verdict("Clio Coder binary", ready, doctor.summary)
    if not ready:
        return False
    sandbox.install_sandbox()
    parent = plain_request(args.server, args.out / "plain-request")
    save(args.out / "plain-request.json", parent)
    passed = verdict("Mode A plain request, delegation, check and apply", parent["passed"])
    rows = []
    for index in range(args.runs):
        row = await measured_run(
            args.out / f"headless-{index + 1}",
            args.home_template,
            args.target,
            args.model,
            "headless",
        )
        rows.append(row)
        save(args.out / "latency.json", rows)
        passed = verdict(f"Mode A runtime sample {index + 1}", row["passed"]) and passed
        if not row["passed"]:
            break
    # ix-acp owns the persistent Mode C public entry point and the land action.
    # Wire that surface after its integration, rather than inventing a second owner.
    passed = (
        verdict("Mode C live", False, "pending ix-acp integration; offline stub is available")
        and passed
    )
    canceled = await cancel_run(args.out / "cancel", args.home_template, args.target, args.model)
    save(args.out / "cancel.json", canceled)
    return verdict("live cancel", canceled["passed"], canceled["reason"]) and passed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out", type=Path, required=True, help="new directory for owned demo state"
    )
    parser.add_argument(
        "--live", action="store_true", help="spend real model calls on the named TC"
    )
    parser.add_argument(
        "--protocol-only", action="store_true", help="omit global native Core fixtures"
    )
    parser.add_argument("--server", help="isolated loopback GACT server; never a production server")
    parser.add_argument("--home-template", type=Path)
    parser.add_argument("--target")
    parser.add_argument("--model")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--tc", help="test candidate id for live evidence")
    args = parser.parse_args()
    args.out = args.out.resolve()
    if args.runs < 1:
        parser.error("--runs must be positive")
    if args.live:
        if not all((args.server, args.home_template, args.target, args.model, args.tc)):
            parser.error("--live needs --server, --home-template, --target, --model and --tc")
        parsed = urlparse(args.server)
        if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "::1", "localhost"):
            parser.error("--server must be a loopback HTTP URL")
    args.out.mkdir(parents=True, exist_ok=False)
    save(
        args.out / "candidate.json",
        {
            "tc": args.tc,
            "live": args.live,
            "clio_agent_sha": git(ROOT, "rev-parse", "HEAD").strip(),
        },
    )
    passed = offline(args.out, args.protocol_only)
    if args.live and passed:
        try:
            passed = asyncio.run(live(args))
        except (
            OSError,
            ValueError,
            RuntimeError,
            subprocess.SubprocessError,
            ClioCoderError,
            psutil.Error,
        ) as exc:
            save(args.out / "failure.json", {"type": type(exc).__name__, "message": str(exc)})
            passed = verdict("live golden demo", False, str(exc))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
