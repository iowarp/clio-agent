"""Run the release-blocking MCP Apps probe against a durable candidate server."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[4]
CAMPAIGN = Path(__file__).resolve().parents[1]
LIVE_SCRIPTS = ROOT / "scripts" / "live_verification"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(LIVE_SCRIPTS))

import _common as common  # noqa: E402

from tests.test_tools.mcp_exerciser import EXERCISER_PATH  # noqa: E402

DEFAULT_OUT = ROOT / "out" / "live-verification" / "release_v0_9_2" / "mcp_apps.json"
DEFAULT_WORKSPACE = ROOT / "out" / "live-verification" / "release_v0_9_2" / "workspaces" / "mcp-app"
TOOL_NAME = "v2ex_ui_echo"


def _mcp_app_parts(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return persisted MCP App parts from a session transcript."""

    return [
        part
        for message in messages
        for part in (message.get("parts") or [])
        if part.get("type") == "mcp_app"
    ]


def run_persistent(args: argparse.Namespace) -> int:
    """Create a durable apps-ui session on an already-running release server."""

    call = common.client(args.base_url.rstrip("/"))
    if not common.wait_health(call):
        raise RuntimeError(f"release candidate never became healthy at {args.base_url}")

    args.workspace_dir.mkdir(parents=True, exist_ok=True)
    command = common.quoted_command(sys.executable, str(EXERCISER_PATH))
    pack_root = common.materialize_testing_pack(
        CAMPAIGN / "blueprints" / "v2ex-avenues",
        args.workspace_dir,
        {"v2ex": command},
    )

    common.allow_all(call)
    workspace_id = common.create_workspace(call, "Release v0.9.2 MCP Apps", args.workspace_dir)
    session_id = common.create_session(call, workspace_id, "Release v0.9.2 MCP Apps")
    installed = common.install_blueprint(call, pack_root, workspace_id)
    blueprint_id = str(installed.get("id") or "")
    common.activate_blueprint(call, session_id, blueprint_id)
    provider = common.bind_provider(call, provider=args.provider, model=args.model)

    resolved = common.resolved_agent_tools(call, session_id, workspace_id=workspace_id)
    resolved_names = {name for names in resolved.values() for name in names}
    if TOOL_NAME not in resolved_names:
        raise RuntimeError(f"{TOOL_NAME} was not resolved by the active blueprint: {resolved}")

    prompt = (
        f"Call the {TOOL_NAME} tool exactly once with payload='{args.payload}'. "
        "Do not call another tool. Tell me when the interactive result is ready."
    )
    common.post_message(call, session_id, prompt)
    turn_status = common.wait_turn(call, workspace_id, session_id, max_elapsed=args.turn_timeout_s)
    messages = common.session_messages(call, session_id)
    common.dump_json(args.out.parent / "mcp_apps_messages.json", messages)

    tool_calls = common.find_tool_calls(messages, "_ui_echo")
    tool_call = tool_calls[-1] if tool_calls else {}
    parts = _mcp_app_parts(messages)
    part = parts[-1] if parts else {}
    app_id = str(part.get("app_instance_id") or "")
    data_ref = str(part.get("data_ref") or "")
    resolved_resource: dict[str, Any] = {}
    if app_id and data_ref:
        resolved_resource = call(
            "GET",
            f"/v1/sessions/{session_id}/mcp-apps/{app_id}",
            params={"data_ref": data_ref},
        )
    resource = resolved_resource.get("resource") or {}
    tool_result = str(tool_call.get("result") or "")

    passed = bool(
        turn_status in {"idle", "completed"}
        and common.tool_call_ok(tool_call)
        and args.payload in tool_result
        and app_id
        and data_ref
        and resource.get("mime_type") == "text/html;profile=mcp-app"
    )
    verdict = {
        "gate": "mcp_apps",
        "pass": passed,
        "base_url": args.base_url,
        "provider": provider,
        "workspace_id": workspace_id,
        "session_id": session_id,
        "blueprint_id": blueprint_id,
        "tool": TOOL_NAME,
        "payload": args.payload,
        "turn_status": turn_status,
        "resolved_tools": resolved,
        "tool_call": tool_call,
        "mcp_app_part": part,
        "resource": {
            "mime_type": resource.get("mime_type"),
            "uri": resource.get("uri"),
            "payload_delivered_by_tool": args.payload in tool_result,
        },
        "browser_pending": True,
    }
    common.write_verdict(args.out, verdict)
    return 0 if passed else 1


def main() -> int:
    """Run the durable release-specific MCP Apps probe."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8787")
    parser.add_argument("--provider", default="codex")
    parser.add_argument("--model", default="gpt-5.6-luna")
    parser.add_argument("--payload", default="release-v0.9.2-ui-probe")
    parser.add_argument("--turn-timeout-s", type=float, default=900.0)
    parser.add_argument("--workspace-dir", type=Path, default=DEFAULT_WORKSPACE)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    return run_persistent(args)


if __name__ == "__main__":
    raise SystemExit(main())
