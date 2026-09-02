#!/usr/bin/env python3
"""Leg (ii): clio-kit web MCP ``task=required`` fetch, end-to-end (#1286).

The original #1274 user failure, reproduced through the DECLARED path only
(``.clio/mcp.yaml`` -> ``load_mcp_servers`` -> ``build_gateway`` ->
capability-keyed direct route) -- the false-green trap this leg exists to
avoid is proving anything through ``POST /v1/mcp/servers`` (the REST-install
lane), which was never the defective path.

Sequence: boot a gact ``run_server`` with its OS cwd pinned to a fresh
workspace dir declaring ``web: clio-kit mcp-server web`` in
``<workspace>/.clio/mcp.yaml`` -> health poll -> ``PUT /v1/policies`` allow-all
-> create workspace (``root_path`` == the SAME dir, see ``_common.py``'s
module docstring for why that equality is load-bearing) + session ->
``GET /v1/mcp/handshake`` asserts ``web`` reachable with its 3 tools
(``fetch``/``fetch_events``/``search`` -- verified live via a direct fastmcp
stdio probe of ``clio-kit mcp-server web`` during this package's own
build/verify pass, protocol_version ``2026-07-28``, ``server_capabilities.
extensions`` carries ``io.modelcontextprotocol/tasks``) -- then, unless
``--plumbing-only``, bind the provider and drive ONE turn: fetch a stable
canonical URL + one-line summary. Asserts a ``web_fetch`` tool call
SUCCEEDED (task-backed, not merely attempted) via the message metadata's
``tools_called[].ok`` field.

``--plumbing-only`` stops after the handshake assertion (before provider
bind and before any turn) -- the flag this package's own build/verify pass
runs to prove the plumbing end-to-end minus the model-driven turn.

Verdict JSON: ``out/live-verification/leg_b_web_fetch.json``.

Usage::

    uv run python scripts/live_verification/leg_b_web_fetch.py --plumbing-only
    uv run python scripts/live_verification/leg_b_web_fetch.py   # spends LM tokens
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _common as common  # noqa: E402

#: A long-lived, stable canonical page -- deliberately boring/unchanging so a
#: content-drift never causes a false failure (this leg proves PLUMBING, not
#: research quality).
STABLE_URL = "https://www.iana.org/help/example-domains"

PROMPT = f"Fetch this URL: {STABLE_URL}\n\nThen give me a one-line summary of what the page says."

EXPECTED_WEB_TOOLS = {"fetch", "fetch_events", "search"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=17980)
    parser.add_argument("--provider", default="claude_code")
    parser.add_argument("--model", default="sonnet")
    parser.add_argument("--out", default=str(common.OUT_ROOT / "leg_b_web_fetch.json"))
    parser.add_argument(
        "--ws-dir",
        default=str(common.OUT_ROOT / "ws-web"),
        help="workspace dir (also the server process's OS cwd -- see _common.py)",
    )
    parser.add_argument("--turn-timeout-s", type=float, default=900.0)
    parser.add_argument(
        "--plumbing-only",
        action="store_true",
        help="stop after the handshake assertion; never bind a provider or drive a turn",
    )
    return parser


def _assert_web_handshake(call: Any, wsid: str) -> dict[str, Any]:
    handshake = call("GET", "/v1/mcp/handshake", params={"workspace_id": wsid})
    rows = handshake.get("servers") or []
    web_row = next((r for r in rows if r.get("name") == "web"), None)
    result: dict[str, Any] = {"handshake_rows": rows, "web_row": web_row}
    if web_row is None:
        result["web_ready"] = False
        result["web_tools_match"] = False
        return result
    result["web_ready"] = bool(web_row.get("reachable"))
    result["web_tools_match"] = EXPECTED_WEB_TOOLS.issubset(set(web_row.get("tools") or []))
    return result


def main() -> int:
    args = build_parser().parse_args()
    out_path = Path(args.out)
    ws_dir = Path(args.ws_dir).resolve()
    ws_dir.mkdir(parents=True, exist_ok=True)

    mcp_yaml = common.write_mcp_yaml(
        ws_dir, {"web": common.quoted_command("clio-kit", "mcp-server", "web")}
    )

    base = f"http://127.0.0.1:{args.port}"
    verdict: dict[str, Any] = {
        "leg": "b_web_fetch",
        "prompt": PROMPT,
        "mcp_yaml": str(mcp_yaml),
        "plumbing_only": args.plumbing_only,
    }
    if not common.port_is_free(args.port):
        verdict["error"] = f"port {args.port} is not free"
        common.write_verdict(out_path, {**verdict, "pass": False})
        return 1

    proc = common.boot_server(
        args.port, cwd=ws_dir, sse_log=common.OUT_ROOT / "leg_b_web_fetch_sse.log"
    )
    try:
        call = common.client(base)
        if not common.wait_health(call):
            verdict["error"] = "gact server never became healthy"
            common.write_verdict(out_path, {**verdict, "pass": False})
            return 1

        common.allow_all(call)
        wsid = common.create_workspace(call, "live-verification-web", ws_dir)
        sid = common.create_session(call, wsid, "leg-b-web-fetch")
        verdict["workspace_id"] = wsid
        verdict["session_id"] = sid

        handshake_result = _assert_web_handshake(call, wsid)
        verdict.update(handshake_result)

        if args.plumbing_only:
            verdict["pass"] = bool(
                handshake_result["web_ready"] and handshake_result["web_tools_match"]
            )
            common.write_verdict(out_path, verdict)
            return 0 if verdict["pass"] else 1

        if not (handshake_result["web_ready"] and handshake_result["web_tools_match"]):
            verdict["error"] = "web MCP not ready/complete at handshake; refusing to spend a turn"
            verdict["pass"] = False
            common.write_verdict(out_path, verdict)
            return 1

        common.bind_provider(call, provider=args.provider, model=args.model)
        verdict["provider"] = {"provider": args.provider, "model": args.model}

        common.post_message(call, sid, PROMPT)
        status = common.wait_turn(call, wsid, sid, max_elapsed=args.turn_timeout_s)
        verdict["turn_status"] = status

        messages = common.session_messages(call, sid)
        common.dump_json(out_path.parent / "leg_b_web_fetch_messages.json", messages)
        fetch_calls = common.find_tool_calls(messages, "_fetch") + common.find_tool_calls(
            messages, "web_fetch"
        )
        fetch_calls = [c for c in fetch_calls if "fetch_events" not in str(c.get("name") or "")]
        succeeded = [c for c in fetch_calls if common.tool_call_ok(c)]
        verdict["web_fetch_calls"] = fetch_calls
        verdict["web_fetch_succeeded"] = bool(succeeded)
        verdict["pass"] = bool(
            status in ("idle", "completed") and succeeded and handshake_result["web_ready"]
        )
        common.write_verdict(out_path, verdict)
        return 0 if verdict["pass"] else 1
    except Exception as exc:  # noqa: BLE001 - captured into the verdict, never a bare traceback
        import traceback

        verdict["error"] = f"{type(exc).__name__}: {exc}"
        verdict["traceback"] = traceback.format_exc()
        verdict["pass"] = False
        common.write_verdict(out_path, verdict)
        return 1
    finally:
        common.terminate_server(proc)


if __name__ == "__main__":
    raise SystemExit(main())
