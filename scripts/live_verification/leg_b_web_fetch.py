#!/usr/bin/env python3
"""Leg (ii): clio-kit web MCP ``task=required`` fetch, end-to-end (#1286, #1301).

The original #1274 user failure is reproduced through the DECLARED path only.
The pack supplies the local ``clio-kit mcp-server web`` fallback and the
user-level ``mcp.yaml`` supplies an optional durable ``--remote-url`` override;
this leg never uses the process-only ``POST /v1/mcp/servers`` lane.

This leg used to drive a bare session + workspace ``.clio/mcp.yaml``.
Investigation proved the bare-session builtin main's toolset is a hardcoded
4-tool list, so a declared server's tools never reach it no matter what
``mcp.yaml`` declares (#1301, deferred upstream -- the Python builtin main is
being dissolved). This leg now rides the WORKING path instead: a
purpose-built single-expert Agent Blueprint pack
(``agents/web-testing/``) whose ``AGENT.md`` declares ``mcp_servers: {web:
...}`` and whose ``main`` expert declares ``tools: [web_fetch, web_search,
web_fetch_events]`` -- the same mechanism every real marketplace pack (e.g.
``deep-researcher``) uses.  When a high-capacity endpoint is supplied, the leg
persists it through ``PUT /v1/mcp/configuration/web`` before activating the
pack. The normal user-over-pack declaration precedence then selects the saved
remote override across processes and workspaces.

Sequence: boot a gact ``run_server`` with isolated user configuration and its
OS cwd pinned to a fresh workspace dir -> health poll -> ``PUT /v1/policies``
allow-all -> create workspace (``root_path`` == the SAME dir) + session ->
persist the explicitly supplied Web Search endpoint, if any -> materialize the
``web-testing`` pack into that workspace dir -> install it onto the workspace
(``POST /v1/agent-blueprints/install``) -> activate it on the session
(``POST /v1/sessions/{sid}/agent-blueprint``) -> ``GET /v1/mcp/handshake``
asserts ``web`` reachable with its 3 tools (``fetch``/``fetch_events``/
``search`` -- verified live via a direct fastmcp stdio probe of ``clio-kit
mcp-server web`` during the #1286 package's own build/verify pass,
protocol_version ``2026-07-28``, ``server_capabilities.extensions`` carries
``io.modelcontextprotocol/tasks``) -> PRE-TURN READINESS GATE:
``GET /v1/agents?session_id=...`` asserts the resolved ``main`` agent's
``tools`` include ``web_fetch`` (proof the declared-server tool reached this
session's active agent, not merely that the pack's frontmatter parsed) --
then, unless ``--plumbing-only``, bind the provider and drive ONE turn: search,
fetch a stable official PDF with ``to_file=true``, and read its durable
conversion events. This deliberately exercises the document-conversion task
path rather than allowing a fast HTML fetch to masquerade as task progress.
Asserts ``web_search``, ``web_fetch``, and ``web_fetch_events`` all SUCCEEDED
(task-backed where applicable, not merely attempted) via the message
metadata's ``tools_called[].ok`` field.

``--plumbing-only`` stops after the readiness gate (before provider bind and
before any turn) -- the flag this package's own build/verify pass runs to
prove the ENTIRE toolset chain (server declaration through resolved agent
tools) with zero LM spend.

Verdict JSON: ``out/live-verification/leg_b_web_fetch.json``.

Usage::

    uv run python scripts/live_verification/leg_b_web_fetch.py --plumbing-only
    uv run python scripts/live_verification/leg_b_web_fetch.py \
        --web-remote-url http://clio-web-search:8080              # spends LM tokens
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _common as common  # noqa: E402

#: A small, long-lived official HDF Group PDF. At roughly 1 MiB it is large
#: enough to exercise download + asynchronous document conversion without
#: turning the qualification into a load test.
STABLE_PDF_URL = "https://docs.hdfgroup.org/archive/support/HDF5/Tutor/HDF5Intro.pdf"

PROMPT = f"""First call web_search for: HDF5 introduction tutorial PDF HDF Group.

Then call web_fetch on exactly this URL with to_file=true:
{STABLE_PDF_URL}

After web_fetch completes, call web_fetch_events with its conversion_id and
after_sequence=0. Report the search provider, PDF title, conversion ID, saved
Markdown and metadata paths, and the ordered conversion stages. You must
successfully call web_search, web_fetch, and web_fetch_events; do not use any
other tool."""

EXPECTED_WEB_TOOLS = {"fetch", "fetch_events", "search"}

#: The web-testing pack template this leg materializes per run.
PACK_TEMPLATE_DIR = Path(__file__).resolve().parent / "agents" / "web-testing"

#: The root ``main`` expert's tools that must be resolved BEFORE any turn is
#: driven (the readiness gate -- proves the blueprint path, not the old
#: hardcoded-4-tool bare-session path).
NEEDED_AGENT_TOOLS = {"web_fetch", "web_fetch_events", "web_search"}


def web_mcp_configuration(remote_url: str) -> dict[str, Any]:
    """Build the durable user-level Web MCP declaration used by the leg."""

    return {
        "name": "CLIO Web Search",
        "transport": "stdio",
        "command": "clio-kit",
        "args": ["mcp-server", "web", "--remote-url", remote_url],
    }


def _decoded(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text.startswith(("{", "[")):
        return value
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return value


def _walk(value: Any) -> Iterator[Any]:
    value = _decoded(value)
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _key_values(value: Any, keys: set[str]) -> list[Any]:
    found: list[Any] = []
    for node in _walk(value):
        if isinstance(node, dict):
            found.extend(node[key] for key in keys if key in node)
    return found


def document_conversion_evidence(
    fetch_calls: list[dict[str, Any]], event_calls: list[dict[str, Any]]
) -> dict[str, Any]:
    """Derive the document gate only from authoritative structured tool-call records."""

    fetch_ids = {
        str(value)
        for call in fetch_calls
        for value in _key_values(call.get("result"), {"conversion_id"})
        if value
    }
    event_ids = {
        str(value)
        for call in event_calls
        for value in [
            *_key_values(call.get("args"), {"conversion_id"}),
            *_key_values(call.get("result"), {"conversion_id"}),
        ]
        if value
    }
    markdown_paths = [
        str(value)
        for call in fetch_calls
        for value in _key_values(
            call.get("result"), {"markdown_path", "markdown_file", "saved_markdown_path"}
        )
        if value
    ]
    metadata_paths = [
        str(value)
        for call in fetch_calls
        for value in _key_values(
            call.get("result"), {"metadata_path", "metadata_file", "saved_metadata_path"}
        )
        if value
    ]
    artifact_refs = [
        node
        for call in fetch_calls
        for node in _walk(call.get("result"))
        if isinstance(node, dict)
        and node.get("artifact_id")
        and (node.get("uri") or node.get("path"))
    ]
    stages = [
        str(value)
        for call in event_calls
        for value in _key_values(call.get("result"), {"stage"})
        if value
    ]
    to_file = any(
        value is True
        for call in fetch_calls
        for value in _key_values(call.get("args"), {"to_file"})
    )
    shared_ids = sorted(fetch_ids & event_ids)
    return {
        "to_file": to_file,
        "fetch_conversion_ids": sorted(fetch_ids),
        "event_conversion_ids": sorted(event_ids),
        "shared_conversion_ids": shared_ids,
        "stages": stages,
        "markdown_paths": markdown_paths,
        "metadata_paths": metadata_paths,
        "artifact_refs": artifact_refs,
        "progress_observed": len(stages) >= 2 and "complete" in stages,
        "pass": bool(
            to_file
            and shared_ids
            and markdown_paths
            and metadata_paths
            and artifact_refs
            and len(stages) >= 2
            and "complete" in stages
        ),
    }


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
        "--web-remote-url",
        default=os.getenv("WEB_REMOTE_URL", ""),
        help=(
            "CLIO Web Search root used for PDF/document conversion. Defaults "
            "to WEB_REMOTE_URL; required unless --plumbing-only is used."
        ),
    )
    parser.add_argument(
        "--plumbing-only",
        action="store_true",
        help="stop after the readiness gate; never bind a provider or drive a turn",
    )
    return parser


def _assert_web_handshake(call: Any, wsid: str, sid: str) -> dict[str, Any]:
    # ``session_id`` is REQUIRED to see the pack-declared server: the
    # handshake route's spec resolution keys entirely off the session's
    # active blueprint (``gact/routes/mcp_specs.py::declared_mcp_specs`` ->
    # ``active_blueprint_id(app, session_id)``) -- an empty session_id
    # resolves no blueprint and therefore no pack-declared servers at all.
    handshake = call("GET", "/v1/mcp/handshake", params={"workspace_id": wsid, "session_id": sid})
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


def _assert_readiness(call: Any, sid: str, wsid: str) -> dict[str, Any]:
    """PRE-TURN READINESS GATE: the resolved ``main`` agent must already carry
    ``NEEDED_AGENT_TOOLS`` before any provider is bound or turn is driven."""

    resolved = common.resolved_agent_tools(call, sid, workspace_id=wsid)
    main_tools = set(resolved.get("main") or [])
    return {
        "resolved_agent_tools": resolved,
        "readiness_gate": {
            "needed_tools": sorted(NEEDED_AGENT_TOOLS),
            "main_tools": sorted(main_tools),
            "ready": NEEDED_AGENT_TOOLS.issubset(main_tools),
        },
    }


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if not args.plumbing_only and not args.web_remote_url:
        parser.error("--web-remote-url or WEB_REMOTE_URL is required for document qualification")
    out_path = Path(args.out)
    ws_dir = Path(args.ws_dir).resolve()
    ws_dir.mkdir(parents=True, exist_ok=True)

    pack_root = common.materialize_testing_pack(
        PACK_TEMPLATE_DIR, ws_dir, {"web": "clio-kit mcp-server web"}
    )

    base = f"http://127.0.0.1:{args.port}"
    verdict: dict[str, Any] = {
        "leg": "b_web_fetch",
        "prompt": PROMPT,
        "pack_root": str(pack_root),
        "plumbing_only": args.plumbing_only,
    }
    if not common.port_is_free(args.port):
        verdict["error"] = f"port {args.port} is not free"
        common.write_verdict(out_path, {**verdict, "pass": False})
        return 1

    user_config_dir = ws_dir / "user-config"
    proc = common.boot_server(
        args.port,
        cwd=ws_dir,
        sse_log=common.OUT_ROOT / "leg_b_web_fetch_sse.log",
        extra_env={"CLIO_USER_DIR": str(user_config_dir)},
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

        if args.web_remote_url:
            configuration = call(
                "PUT",
                "/v1/mcp/configuration/web",
                web_mcp_configuration(args.web_remote_url),
            )
        else:
            configuration = call("GET", "/v1/mcp/configuration/web")
        verdict["web_configuration"] = configuration

        install_result = common.install_blueprint(call, pack_root, wsid)
        blueprint_id = str(install_result.get("id") or "")
        common.activate_blueprint(call, sid, blueprint_id)
        verdict["blueprint_id"] = blueprint_id

        handshake_result = _assert_web_handshake(call, wsid, sid)
        verdict.update(handshake_result)

        readiness_result = _assert_readiness(call, sid, wsid)
        verdict.update(readiness_result)
        readiness_ok = readiness_result["readiness_gate"]["ready"]

        configuration_ok = (
            configuration.get("status") == "ready"
            if args.web_remote_url
            else configuration.get("status") == "local_fallback"
        )
        handshake_ok = (
            handshake_result["web_ready"]
            and handshake_result["web_tools_match"]
            and configuration_ok
        )

        if args.plumbing_only:
            verdict["pass"] = bool(handshake_ok and readiness_ok)
            common.write_verdict(out_path, verdict)
            return 0 if verdict["pass"] else 1

        if not (handshake_ok and readiness_ok):
            verdict["error"] = (
                "web MCP not ready/complete at handshake, or the resolved agent's "
                "tools are missing a needed tool; refusing to spend a turn"
            )
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
        search_calls = common.find_tool_calls(messages, "_search")
        fetch_calls = common.find_tool_calls(messages, "_fetch")
        fetch_calls = [c for c in fetch_calls if "fetch_events" not in str(c.get("name") or "")]
        event_calls = common.find_tool_calls(messages, "_fetch_events")
        search_succeeded = [c for c in search_calls if common.tool_call_ok(c)]
        fetch_succeeded = [c for c in fetch_calls if common.tool_call_ok(c)]
        events_succeeded = [c for c in event_calls if common.tool_call_ok(c)]
        verdict["web_search_calls"] = search_calls
        verdict["web_fetch_calls"] = fetch_calls
        verdict["web_fetch_event_calls"] = event_calls
        verdict["web_search_succeeded"] = bool(search_succeeded)
        verdict["web_fetch_succeeded"] = bool(fetch_succeeded)
        verdict["web_fetch_events_succeeded"] = bool(events_succeeded)
        conversion = document_conversion_evidence(fetch_succeeded, events_succeeded)
        verdict["document_conversion"] = conversion
        verdict["pass"] = bool(
            status in ("idle", "completed")
            and search_succeeded
            and fetch_succeeded
            and events_succeeded
            and conversion["pass"]
            and configuration.get("status") == "ready"
            and handshake_result["web_ready"]
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
