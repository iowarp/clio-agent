#!/usr/bin/env python3
"""Leg C2: the EXPANDED synthetic-v2 live-verification avenues (#1286, C1-S6).

Leg C (``leg_c_synthetic_session.py``) proved ONE avenue end to end: task=
required plumbing (``task_echo``) plus form-mode MRTR (``guarded_input``)
through a real gact session. The owner ruling this leg answers: "every avenue
of the mcp v2 needs to be live tested, to find bugs and problems." This
module drives the REMAINING avenues the C1-S0 exerciser (``tests/test_tools/
mcp_exerciser.py``) and the declared-path client can reach, each as an
INDEPENDENT sub-leg with its own ``{avenue, status, evidence, error}``
verdict -- one red/blocked avenue never blocks another's verdict, and a red
or blocked result is DESIRED output (it becomes failing-first slice work, not
something to hide).

Avenues (see ``LEG_C2.md`` for the full per-avenue writeup + citations):

 1. task-modes       -- optional/plain succeed; forbidden-explicit refuses (or
                         succeeds) but NEVER hangs.
 2. mrtr-url          -- BLOCKED: no url-mode elicitation arm exists anywhere
                         in this repo's exerciser (see module docstring below).
 3. mrtr-methods      -- BLOCKED: exerciser declares no resources/prompts, and
                         the declared session surface cannot reach them anyway.
 4. cache             -- BLOCKED: exerciser has no cache_ttl/cache_scope arm.
 5. waits-cancel      -- staller surfaces ``mcp_task.wait`` live-SSE events,
                         then a cancel ends the turn ``cancelled``, not hung.
 6. pagination        -- the readiness gate's full 9-tool resolution is the
                         available (indirect) proof; no ``list_page_size``
                         control exists anywhere in clio_agent to force real
                         multi-page traversal.
 7. list-changed      -- BLOCKED: exerciser's tool set never changes; no
                         listChanged arm.
 8. extensions        -- BLOCKED for a DIRECT assertion: the handshake row
                         never surfaces ``ServerCapabilities.extensions``;
                         ``execution_era`` is recorded as the closest indirect
                         signal available today.
 9. adversarial       -- BLOCKED: no MUST-violating raw-responder/ASGI-shim
                         fixture exists in this repo.
10. headers           -- a NEW header-capture HTTP MCP server
                         (``_header_capture_server.py``) probed through the
                         REST-install lane (``POST /v1/mcp/servers`` +
                         ``.../call``); genuinely live-tested, not assumed.
11. apps-ui           -- BLOCKED: no exerciser tool carries a ``ui://``
                         resource reference (``gact/mcp_apps.py``'s admission
                         requirements are cited as evidence for what an arm
                         would need).

Mechanics mirror ``leg_c_synthetic_session.py``: its own server boot on a
free port, allow-all policies FIRST, the ``v2ex-avenues`` testing-agent pack
(``agents/v2ex-avenues/`` -- a SIBLING of ``agents/v2ex-testing/``, not an
edit to it, exposing every exerciser tool leg C's narrower pack does not),
zero-LM readiness gate before any turn, ``claude_code``/``sonnet`` turns ONLY
for the two avenues that genuinely need a model in the loop (1 and 5) --
every other avenue is driven through a headless HTTP/SSE surface, per the
house cost-aware-defaults + "prefer direct surfaces" rule.

Verdict JSON: ``out/live-verification/leg_c2_verdict.json``.

Usage::

    uv run python scripts/live_verification/leg_c2_v2_avenues.py --dry-run
    uv run python scripts/live_verification/leg_c2_v2_avenues.py --plumbing-only
    uv run python scripts/live_verification/leg_c2_v2_avenues.py   # spends LM tokens (2 turns)
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root, for tests.* import

import _common as common  # noqa: E402
import _header_capture_server as hcap  # noqa: E402
from _sse_collector import SSECollector  # noqa: E402

from tests.test_tools.mcp_exerciser import (  # noqa: E402
    EXERCISER_NAMESPACE,
    EXERCISER_PATH,
)

#: The v2ex-avenues pack template this leg materializes per run (sibling of
#: leg C's narrower v2ex-testing pack -- see agents/v2ex-avenues/AGENT.md).
PACK_TEMPLATE_DIR = Path(__file__).resolve().parent / "agents" / "v2ex-avenues"

#: Every tool build_exerciser_server() registers (mirrors leg_c_synthetic_
#: session.py's own EXPECTED_TOOLS -- kept as an independent, hand-written
#: constant here rather than a cross-script import, matching how each leg in
#: this package already declares its own expectation set).
EXERCISER_EXPECTED_TOOLS = {
    "task_echo",
    "task_optional_echo",
    "plain_echo",
    "forbidden_echo",
    "guarded_input",
    "plain_guarded_input",
    "staller",
    "plain_staller",
    "silent_sleeper",
}

#: Every namespaced tool the pack's `main` expert must resolve before any
#: turn is driven (the readiness gate; also avenue 6's evidence).
NEEDED_AGENT_TOOLS = {f"{EXERCISER_NAMESPACE}_{name}" for name in EXERCISER_EXPECTED_TOOLS}

#: Static avenue plan -- used by both --dry-run and (for cross-reference)
#: LEG_C2.md's table. ``needs_lm`` marks the two avenues driven through a
#: real model turn; every other avenue is headless HTTP/SSE only.
AVENUE_PLAN: list[dict[str, Any]] = [
    {
        "avenue": "task-modes",
        "needs_lm": True,
        "expect": "pass",
        "summary": "optional/plain tools succeed; forbidden-explicit never hangs",
    },
    {
        "avenue": "mrtr-url",
        "needs_lm": False,
        "expect": "blocked",
        "summary": "no url-mode elicitation arm in the exerciser",
    },
    {
        "avenue": "mrtr-methods",
        "needs_lm": False,
        "expect": "blocked",
        "summary": "exerciser has no resources/prompts; declared path can't reach them anyway",
    },
    {
        "avenue": "cache",
        "needs_lm": False,
        "expect": "blocked",
        "summary": "exerciser has no cache_ttl/cache_scope arm",
    },
    {
        "avenue": "waits-cancel",
        "needs_lm": True,
        "expect": "pass",
        "summary": "mcp_task.wait SSE events observed live, then cancel ends the turn cancelled",
    },
    {
        "avenue": "pagination",
        "needs_lm": False,
        "expect": "pass",
        "summary": "readiness gate proves all 9 tools resolve (indirect; no page-size control exists)",
    },
    {
        "avenue": "list-changed",
        "needs_lm": False,
        "expect": "blocked",
        "summary": "exerciser's tool set never changes; no listChanged arm",
    },
    {
        "avenue": "extensions",
        "needs_lm": False,
        "expect": "blocked",
        "summary": "handshake row never surfaces ServerCapabilities.extensions directly",
    },
    {
        "avenue": "adversarial",
        "needs_lm": False,
        "expect": "blocked",
        "summary": "no MUST-violating raw-responder/ASGI-shim fixture exists in this repo",
    },
    {
        "avenue": "headers",
        "needs_lm": False,
        "expect": "pass-or-fail (genuinely probed live)",
        "summary": "new header-capture MCP server probed via the REST-install lane",
    },
    {
        "avenue": "apps-ui",
        "needs_lm": False,
        "expect": "blocked",
        "summary": "no exerciser tool carries a ui:// resource reference",
    },
]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--port", type=int, default=17982)
    parser.add_argument("--hcap-port", type=int, default=17983, help="header-capture server port")
    parser.add_argument("--provider", default="claude_code")
    parser.add_argument("--model", default="sonnet")
    parser.add_argument("--out", default=str(common.OUT_ROOT / "leg_c2_verdict.json"))
    parser.add_argument(
        "--ws-dir",
        default=str(common.OUT_ROOT / "ws-v2ex-avenues"),
        help="workspace dir (also the server process's OS cwd -- see _common.py)",
    )
    parser.add_argument("--turn-timeout-s", type=float, default=900.0)
    parser.add_argument("--wait-event-timeout-s", type=float, default=60.0)
    parser.add_argument("--cancel-timeout-s", type=float, default=180.0)
    parser.add_argument(
        "--plumbing-only",
        action="store_true",
        help="stop before any LM turn; avenues 1 and 5 are recorded 'blocked' (skipped)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the avenue plan and exit; boots nothing, calls nothing",
    )
    return parser


def _print_dry_run() -> None:
    print(json.dumps({"leg": "c2_v2_avenues", "dry_run": True, "avenues": AVENUE_PLAN}, indent=2))


# --------------------------------------------------------------------------- #
# Static (no server call needed) avenues -- each documents a genuine gap
# found by reading the exerciser/client source, per the task's own
# instruction that a gap is a FINDING, not a skip to hide.
# --------------------------------------------------------------------------- #
def avenue_mrtr_url() -> dict[str, Any]:
    return {
        "avenue": "mrtr-url",
        "status": "blocked",
        "evidence": {
            "checked": ["tests/test_tools/mcp_exerciser.py"],
            "finding": (
                "the exerciser's only elicitation helper is `_one_elicit()` "
                "(mcp_exerciser.py:56-64), which always builds a FORM-mode "
                "`mcp_types.ElicitRequestFormParams` request; both `guarded_input` "
                "and `plain_guarded_input` call only `_one_elicit`. There is no "
                "URL-mode arm anywhere in the exerciser. The only place in this "
                "repo that constructs `mcp_types.ElicitRequestURLParams` is "
                "tests/test_gact/test_elicitation_hitl.py, which feeds it DIRECTLY "
                "into `elicitation_bridge.handle_elicitation()` in isolation "
                "(bypassing a real MCP tool round-trip) -- no MCP TOOL in this "
                "repo emits a genuine url-mode elicitation request over the wire."
            ),
            "what_is_missing": (
                "a new exerciser tool (e.g. `url_guarded_input`) whose "
                "InputRequiredResult carries "
                "`mcp_types.ElicitRequestURLParams(message=..., url=...)` instead "
                "of the form params `_one_elicit` builds."
            ),
        },
        "error": None,
    }


def avenue_mrtr_methods() -> dict[str, Any]:
    return {
        "avenue": "mrtr-methods",
        "status": "blocked",
        "evidence": {
            "checked": ["tests/test_tools/mcp_exerciser.py", "src/clio_agent/gact/routes/mcp.py"],
            "finding": (
                "(a) build_exerciser_server() registers 9 `@server.tool`s and "
                "nothing else -- no `@server.resource`/`@server.prompt` handler "
                "exists, so there is no MRTR-capable prompts/get or resources/read "
                "arm to drive even in isolation. (b) independent of (a): the "
                "DECLARED session/turn surface this leg (and legs B/C) prove only "
                "exposes an expert's frontmatter `tools:` list to the model -- "
                "prompts/resources reach a session only through the SEPARATE "
                "REST-install-lane inventory routes (`GET /v1/mcp/servers/{sid}/"
                "resources`, `/prompts`, `POST .../prompts/get` -- gact/routes/"
                "mcp.py ~898-960), a bare direct client (`make_mcp_client`), NOT "
                "the gateway/executor path this campaign targets. Even a "
                "resource/prompt-serving exerciser would still need a different, "
                "non-declared-path leg to reach it through a session."
            ),
        },
        "error": None,
    }


def avenue_cache() -> dict[str, Any]:
    return {
        "avenue": "cache",
        "status": "blocked",
        "evidence": {
            "checked": [
                "tests/test_tools/mcp_exerciser.py",
                "repo-wide grep: cache_ttl|cache_scope|CacheConfig|cache_hint",
            ],
            "finding": (
                "no tool in the exerciser declares a cache hint (ttlMs/cacheScope); "
                "a repo-wide grep for cache_ttl/cache_scope/CacheConfig/cache_hint "
                "found zero hits inside src/clio_agent or tests/test_tools -- the "
                "client has no cache-hint handling to exercise yet at all. "
                "docs/design/mcp-client-unification-2026-08.md's C1-S5 line "
                "('server cache hints (ttlMs/cacheScope + the caching MUST-NOTs)') "
                "is where both the client support and an exerciser arm would land."
            ),
            "what_is_missing": (
                "a new exerciser tool returning a result annotated with a cache "
                "hint (ttlMs and/or cacheScope) so a call-twice-compare-behavior "
                "probe becomes possible."
            ),
        },
        "error": None,
    }


def avenue_list_changed() -> dict[str, Any]:
    return {
        "avenue": "list-changed",
        "status": "blocked",
        "evidence": {
            "checked": [
                "tests/test_tools/mcp_exerciser.py",
                "repo-wide grep: listChanged|list_changed",
            ],
            "finding": (
                "the exerciser's tool set is fixed at server-build time "
                "(build_exerciser_server() registers the same 9 tools every call) "
                "-- no tool dynamically adds/removes a tool or fires a "
                "`notifications/tools/list_changed` notification. A repo-wide "
                "grep for listChanged/list_changed found only unrelated hits "
                "(scripts/analyze_turn_waterfall.py, an ai-docs reference doc) -- "
                "no clio_agent client code and no exerciser arm exist for this "
                "today. docs/design/mcp-client-unification-2026-08.md's C1-S5 "
                "line names this explicitly: 'subscriptions/listen + listChanged "
                "as the listing-cache invalidation signal'."
            ),
            "what_is_missing": (
                "an exerciser tool that mutates its own tool registry (e.g. adds "
                "a tool at runtime) and sends listChanged, so a re-list can be "
                "asserted to observe the change."
            ),
        },
        "error": None,
    }


def avenue_adversarial() -> dict[str, Any]:
    return {
        "avenue": "adversarial",
        "status": "blocked",
        "evidence": {
            "checked": [
                "tests/test_tools/*",
                "docs/design/mcp-v2-understanding-2026-08.md",
                "docs/design/mcp-client-obligations-2026-07-28.md",
            ],
            "finding": (
                "no standalone MUST-violating raw-responder/ASGI-shim fixture "
                "exists anywhere in this repo -- searched tests/test_tools/ for "
                "'raw responder'/'ASGI shim'/'asgi_shim'/'raw_responder': zero "
                "hits. The C1-S0 slice (mcp_exerciser.py, mcp_v1_fixture.py) "
                "built a well-behaved modern exerciser and a well-behaved frozen "
                "v1 fixture, but no fixture that deliberately emits malformed/"
                "MUST-violating frames (e.g. a wrong-typed tasks/get response, a "
                "missing required field, an invalid taskSupport value)."
            ),
            "what_is_missing": (
                "a small hand-rolled ASGI app (bypassing fastmcp's own protocol "
                "correctness) returning deliberately protocol-violating JSON-RPC "
                "frames for specific methods, servable stand-alone as a second "
                "declared MCP server so a leg could mount it in a workspace "
                "mcp.yaml and assert the client reacts typed to each violation "
                "without hanging."
            ),
        },
        "error": None,
    }


def avenue_apps_ui(handshake_row: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "avenue": "apps-ui",
        "status": "blocked",
        "evidence": {
            "checked": ["tests/test_tools/mcp_exerciser.py", "src/clio_agent/gact/mcp_apps.py"],
            "handshake_v2ex_row": handshake_row,
            "finding": (
                "the exerciser declares no tool with a `ui://` resource reference "
                "at all: `mcp_apps.py::_resource_uri` (~line 104-108) requires a "
                "tool's `_meta.ui.resourceUri` (or the flat `_meta['ui/"
                "resourceUri']` key) to be a string starting with `ui://`; none "
                "of the exerciser's 9 tools carry any `_meta`, so `_resource_uri` "
                "would return `''` for every one of them and no MCP App would "
                "ever admit."
            ),
            "what_an_arm_would_need": (
                "(1) a tool whose FastMCP `_meta` declares `{'ui': {'resourceUri': "
                "'ui://<namespace>/<name>'}}`; (2) a matching "
                "`@server.resource('ui://...')` handler returning content with "
                "`mimeType == 'text/html;profile=mcp-app'` (mcp_apps."
                "MCP_APP_MIME_TYPE) and a `text` HTML body -- mcp_apps.py::"
                "_resource_payload (~174-193) rejects anything else (not exactly "
                "one content item, wrong mimeType, non-string text). Once "
                "admitted, 'arrived' on the session surface means: an assistant "
                "Part of type 'mcp_app' lands via `_append_live_assistant_part` "
                "(mcp_apps.py:434-448, `metadata={'stream_source': 'live', "
                "'protocol': '2026-01-26'}`) -- observable on `GET /v1/sessions/"
                "{sid}/messages` or the SSE feed -- and `GET /v1/sessions/{sid}/"
                "mcp-apps/{app_id}` (mcp_apps.py:617-637) resolves the actual "
                "resource HTML/CSP/permissions, also pinning "
                "`protocol_version: '2026-01-26'` (mcp_apps.py:626)."
            ),
        },
        "error": None,
    }


def avenue_extensions(handshake_row: dict[str, Any] | None) -> dict[str, Any]:
    era = (handshake_row or {}).get("execution_era")
    return {
        "avenue": "extensions",
        "status": "blocked",
        "evidence": {
            "handshake_v2ex_row": handshake_row,
            "execution_era": era,
            "finding": (
                "gact/routes/mcp_rows.py::handshake_server_row (the ONLY wire "
                "shape GET /v1/mcp/handshake returns) does not surface "
                "`ServerCapabilities.extensions` at all -- its fields are name/"
                "reachable/state/transport/tools_count/tools/error/latency_ms/"
                "protocol_version/server_version/instructions/execution_era/"
                "execution_downgrade_reason. No other HTTP route exposes the "
                "era/capability registry either (grepped gact/routes/*.py for "
                "execution_era/latest_task_capability/latest_mcp_connection_era: "
                "only mcp_rows.py and mcp.py reference them). So the raw "
                "extensions dict (the tasks id + the fastmcp ui splice) cannot "
                "be independently confirmed through any documented client-"
                "facing surface today -- this is exactly the generic extension "
                "registry docs/design/mcp-client-unification-2026-08.md's "
                "C1-S3(a) has not landed yet."
            ),
            "indirect_signal": (
                "execution_era for v2ex above should read 'modern' -- era "
                "detection (mcp_connection_era.py) is keyed off the negotiated "
                "protocol_version, and a genuinely modern, task-capable server "
                "like the exerciser only lands modern by the client reading "
                "capabilities.extensions during negotiation, so a 'modern' era "
                "is CONSISTENT WITH (but not independent proof of) the "
                "extensions dict actually carrying the tasks id."
            ),
        },
        "error": None,
    }


def avenue_pagination(resolved_main_tools: set[str]) -> dict[str, Any]:
    ok = NEEDED_AGENT_TOOLS.issubset(resolved_main_tools)
    return {
        "avenue": "pagination",
        "status": "pass" if ok else "fail",
        "evidence": {
            "expected_tools": sorted(NEEDED_AGENT_TOOLS),
            "resolved_main_tools": sorted(resolved_main_tools),
            "list_page_size_control_found": False,
            "note": (
                "no `list_page_size`/page-size config or CLI control was found "
                "anywhere in clio_agent (repo-wide grep for list_page_size/"
                "page_size/pagination/cursor across src/clio_agent/tools/*: zero "
                "MCP-tools/list-paging-related hits), so this leg cannot FORCE "
                "the exerciser's 9-tool tools/list to span multiple pages. "
                "fastmcp's Client.list_tools() cursor-based pagination is "
                "SDK-internal (obligations doc row B1, 'library-covered'), not "
                "independently forceable to a small page size from this "
                "codebase. This avenue instead proves pagination TRANSPARENCY "
                "indirectly: if any page boundary were mishandled, some of the "
                "9 expected tools would be missing from the resolved agent's "
                "toolset above -- they are not (when this avenue passes)."
            ),
        },
        "error": None
        if ok
        else "resolved agent tools missing some of the exerciser's declared set",
    }


# --------------------------------------------------------------------------- #
# Live avenues
# --------------------------------------------------------------------------- #
def avenue_task_modes(
    call: Any, wsid: str, sid: str, out_path: Path, *, turn_timeout_s: float
) -> dict[str, Any]:
    optional_payload = f"opt-{uuid.uuid4().hex[:8]}"
    plain_payload = f"plain-{uuid.uuid4().hex[:8]}"
    forbidden_payload = f"forbid-{uuid.uuid4().hex[:8]}"
    prompt = (
        f"Call these {EXERCISER_NAMESPACE} tools IN ORDER and report each result "
        "verbatim, including any error message if one occurs -- never skip a step "
        "even if an earlier one errors:\n"
        f"1. {EXERCISER_NAMESPACE}_task_optional_echo with payload='{optional_payload}'\n"
        f"2. {EXERCISER_NAMESPACE}_plain_echo with payload='{plain_payload}'\n"
        f"3. {EXERCISER_NAMESPACE}_forbidden_echo with payload='{forbidden_payload}'"
    )
    common.post_message(call, sid, prompt)
    status = common.wait_turn(call, wsid, sid, max_elapsed=turn_timeout_s)
    messages = common.session_messages(call, sid)
    common.dump_json(out_path.parent / "leg_c2_task_modes_messages.json", messages)

    optional_calls = common.find_tool_calls(messages, "_task_optional_echo")
    plain_calls = [
        c
        for c in common.find_tool_calls(messages, "_plain_echo")
        if "guarded" not in str(c.get("name") or "")
    ]
    forbidden_calls = common.find_tool_calls(messages, "_forbidden_echo")

    optional_ok = [c for c in optional_calls if common.tool_call_ok(c)]
    plain_ok = [c for c in plain_calls if common.tool_call_ok(c)]
    optional_has_payload = any(optional_payload in str(c.get("result") or "") for c in optional_ok)
    plain_has_payload = any(plain_payload in str(c.get("result") or "") for c in plain_ok)

    forbidden_attempted = bool(forbidden_calls)
    forbidden_ok = [c for c in forbidden_calls if common.tool_call_ok(c)]
    forbidden_errors = [c for c in forbidden_calls if not common.tool_call_ok(c)]
    forbidden_error_text = " ".join(str(c.get("error") or "") for c in forbidden_errors)
    forbidden_refused_typed = (
        any(code in forbidden_error_text for code in ("-32021", "-32022"))
        or "forbidden" in forbidden_error_text.lower()
    )

    hung = status == "timed_out"
    terminal_ok = status in ("idle", "completed", "error", "waiting_user")
    pass_ = bool(
        not hung
        and terminal_ok
        and optional_ok
        and optional_has_payload
        and plain_ok
        and plain_has_payload
        and forbidden_attempted
    )
    return {
        "avenue": "task-modes",
        "status": "pass" if pass_ else "fail",
        "evidence": {
            "turn_status": status,
            "optional_payload": optional_payload,
            "plain_payload": plain_payload,
            "forbidden_payload": forbidden_payload,
            "optional_calls": optional_calls,
            "plain_calls": plain_calls,
            "forbidden_calls": forbidden_calls,
            "optional_succeeded": bool(optional_ok),
            "plain_succeeded": bool(plain_ok),
            "forbidden_attempted": forbidden_attempted,
            "forbidden_succeeded": bool(forbidden_ok),
            "forbidden_refused_typed": forbidden_refused_typed,
            "hung": hung,
        },
        "error": None
        if pass_
        else (
            "turn hung (never reached a terminal status)"
            if hung
            else "optional/plain tool call did not succeed with the expected payload, "
            "or the forbidden-explicit arm was never attempted"
        ),
    }


def avenue_waits_cancel(
    call: Any,
    base: str,
    wsid: str,
    sid: str,
    out_path: Path,
    *,
    wait_event_timeout_s: float,
    cancel_timeout_s: float,
) -> dict[str, Any]:
    collector = SSECollector(base, sid).start()
    try:
        common.post_message(
            call,
            sid,
            f"Call the {EXERCISER_NAMESPACE}_staller tool with seconds=8 and steps=16, "
            "then report exactly what it returned.",
        )

        wait_event = collector.wait_for_event("mcp_task.wait", max_elapsed=wait_event_timeout_s)
        wait_event_seen = wait_event is not None

        cancel_response: dict[str, Any] | None = None
        cancel_error: str | None = None
        try:
            resp = call("POST", f"/v1/sessions/{sid}/cancel", ok=(200, 204), raw=True)
            cancel_response = {"status_code": resp.status_code}
        except Exception as exc:  # noqa: BLE001 - captured into evidence, never raised
            cancel_error = f"{type(exc).__name__}: {exc}"

        def _check() -> str | None:
            rows = call("GET", f"/v1/sessions?workspace_id={wsid}").get("sessions", [])
            row = next((r for r in rows if r.get("id") == sid), {})
            status = str(row.get("status") or "?")
            return (
                status
                if status in ("idle", "completed", "error", "waiting_user", "cancelled")
                else None
            )

        final_status = common.expanding_wait(
            _check,
            what=f"session {sid} to reach a terminal status after cancel",
            max_elapsed=cancel_timeout_s,
        )
        final_status = final_status or "timed_out"

        wait_events = collector.events_of_type("mcp_task.wait")
        common.dump_json(
            out_path.parent / "leg_c2_waits_cancel_events.json",
            {"all_events_of_interest": wait_events, "cancel_response": cancel_response},
        )

        hung = final_status == "timed_out"
        cancelled_confirmed = final_status == "cancelled"
        pass_ = bool(wait_event_seen and not hung and cancel_error is None and cancelled_confirmed)
        return {
            "avenue": "waits-cancel",
            "status": "pass" if pass_ else "fail",
            "evidence": {
                "wait_event_seen": wait_event_seen,
                "first_wait_event": wait_event,
                "wait_event_count": len(wait_events),
                "cancel_response": cancel_response,
                "cancel_error": cancel_error,
                "final_status": final_status,
                "hung": hung,
                "cancelled_confirmed": cancelled_confirmed,
            },
            "error": None
            if pass_
            else (
                "turn hung after cancel (never reached a terminal status)"
                if hung
                else "no mcp_task.wait SSE event observed"
                if not wait_event_seen
                else f"turn ended {final_status!r}, not 'cancelled'"
            ),
        }
    finally:
        collector.stop()


def avenue_headers(call: Any, hcap_port: int, hcap_log: Path) -> dict[str, Any]:
    install_body = {
        "name": "hcap",
        "transport": "http",
        "url": f"http://127.0.0.1:{hcap_port}/mcp",
    }
    try:
        installed = call("POST", "/v1/mcp/servers", install_body, ok=(200, 201))
    except Exception as exc:  # noqa: BLE001 - captured into the verdict
        return {
            "avenue": "headers",
            "status": "fail",
            "evidence": {"install_error": f"{type(exc).__name__}: {exc}"},
            "error": "failed to install the header-capture server via POST /v1/mcp/servers",
        }
    server_id = str(installed.get("id") or "")
    try:
        result = call(
            "POST",
            f"/v1/mcp/servers/{server_id}/call",
            {"tool": "probe", "args": {"payload": "hdr-probe"}},
            ok=(200, 201),
        )
    except Exception as exc:  # noqa: BLE001 - captured into the verdict
        return {
            "avenue": "headers",
            "status": "fail",
            "evidence": {"install": installed, "call_error": f"{type(exc).__name__}: {exc}"},
            "error": "the probe tool call through the REST-install lane failed",
        }

    rows = hcap.read_captured_rows(hcap_log)
    call_rows = [r for r in rows if (r.get("headers") or {}).get("mcp-method") == "tools/call"]
    last = call_rows[-1] if call_rows else {}
    headers = last.get("headers") or {}
    has_method = "mcp-method" in headers
    has_protocol_version = "mcp-protocol-version" in headers
    param_headers = {k: v for k, v in headers.items() if k.startswith("mcp-param-")}
    status = "pass" if (has_method and has_protocol_version) else "fail"
    return {
        "avenue": "headers",
        "status": status,
        "evidence": {
            "install": installed,
            "call_result": result,
            "captured_rows": rows,
            "tools_call_headers": headers,
            "mcp_method_present": has_method,
            "mcp_protocol_version_present": has_protocol_version,
            "mcp_param_headers": param_headers,
            "note": (
                "clio_agent's OWN source carries zero code that sets these "
                "headers (grepped src/clio_agent for Mcp-Method/Mcp-Param: no "
                "hits); any presence here comes from the fastmcp CLIENT LIBRARY "
                "(tools/mcp_runtime.py::make_mcp_client wraps fastmcp.Client "
                "verbatim). mcp-param-* mirroring (obligations doc row B3) is "
                "UNTESTABLE with this probe tool regardless of outcome: B3 only "
                "mirrors ANNOTATED header-worthy params (SEP-2578), and neither "
                "this capture tool nor any exerciser tool declares one -- a "
                "genuine capture-tool/exerciser gap, not evidence either way "
                "about B3."
            ),
        },
        "error": None
        if status == "pass"
        else "tools/call did not carry Mcp-Method/Mcp-Protocol-Version headers",
    }


# --------------------------------------------------------------------------- #
def main() -> int:
    args = build_parser().parse_args()
    if args.dry_run:
        _print_dry_run()
        return 0

    out_path = Path(args.out)
    ws_dir = Path(args.ws_dir).resolve()
    ws_dir.mkdir(parents=True, exist_ok=True)
    hcap_log = common.OUT_ROOT / "leg_c2_hcap_captured.jsonl"
    if hcap_log.exists():
        hcap_log.unlink()

    command = common.quoted_command(sys.executable, str(EXERCISER_PATH))
    pack_root = common.materialize_testing_pack(
        PACK_TEMPLATE_DIR, ws_dir, {EXERCISER_NAMESPACE: command}
    )

    base = f"http://127.0.0.1:{args.port}"
    avenues: list[dict[str, Any]] = []
    verdict: dict[str, Any] = {
        "leg": "c2_v2_avenues",
        "pack_root": str(pack_root),
        "mcp_command": command,
        "namespace": EXERCISER_NAMESPACE,
        "plumbing_only": args.plumbing_only,
    }

    if not common.port_is_free(args.port):
        verdict["error"] = f"port {args.port} is not free"
        common.write_verdict(out_path, {**verdict, "pass": False})
        return 1
    if not common.port_is_free(args.hcap_port):
        verdict["error"] = f"hcap port {args.hcap_port} is not free"
        common.write_verdict(out_path, {**verdict, "pass": False})
        return 1

    hcap_proc = None
    proc = common.boot_server(args.port, cwd=ws_dir, sse_log=common.OUT_ROOT / "leg_c2_sse.log")
    try:
        hcap_proc = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve().parent / "_header_capture_server.py"),
                "--port",
                str(args.hcap_port),
                "--log",
                str(hcap_log),
            ],
        )

        call = common.client(base)
        if not common.wait_health(call):
            verdict["error"] = "gact server never became healthy"
            common.write_verdict(out_path, {**verdict, "pass": False})
            return 1

        common.allow_all(call)
        wsid = common.create_workspace(call, "live-verification-v2ex-avenues", ws_dir)
        sid = common.create_session(call, wsid, "leg-c2-v2-avenues")
        verdict["workspace_id"] = wsid
        verdict["session_id"] = sid

        install_result = common.install_blueprint(call, pack_root, wsid)
        blueprint_id = str(install_result.get("id") or "")
        common.activate_blueprint(call, sid, blueprint_id)
        verdict["blueprint_id"] = blueprint_id

        handshake = call(
            "GET", "/v1/mcp/handshake", params={"workspace_id": wsid, "session_id": sid}
        )
        handshake_rows = handshake.get("servers") or []
        v2ex_row = next((r for r in handshake_rows if r.get("name") == EXERCISER_NAMESPACE), None)
        verdict["handshake_v2ex_row"] = v2ex_row

        resolved = common.resolved_agent_tools(call, sid, workspace_id=wsid)
        main_tools = set(resolved.get("main") or [])
        readiness_ready = NEEDED_AGENT_TOOLS.issubset(main_tools)
        verdict["readiness_gate"] = {
            "needed_tools": sorted(NEEDED_AGENT_TOOLS),
            "main_tools": sorted(main_tools),
            "ready": readiness_ready,
        }

        # --- static (no boot/network needed beyond what's already fetched) ---
        avenues.append(avenue_mrtr_url())
        avenues.append(avenue_mrtr_methods())
        avenues.append(avenue_cache())
        avenues.append(avenue_list_changed())
        avenues.append(avenue_adversarial())
        avenues.append(avenue_extensions(v2ex_row))
        avenues.append(avenue_apps_ui(v2ex_row))
        avenues.append(avenue_pagination(main_tools))

        # --- headers: headless HTTP, no LM needed ---
        hcap_ready = common.expanding_wait(
            lambda: hcap_proc.poll() is None and _hcap_reachable(args.hcap_port),
            what="header-capture server reachable",
            max_elapsed=60.0,
        )
        if not hcap_ready:
            avenues.append(
                {
                    "avenue": "headers",
                    "status": "fail",
                    "evidence": {},
                    "error": "the header-capture server never became reachable",
                }
            )
        else:
            avenues.append(avenue_headers(call, args.hcap_port, hcap_log))

        if not readiness_ready:
            for avenue_id in ("task-modes", "waits-cancel"):
                avenues.append(
                    {
                        "avenue": avenue_id,
                        "status": "fail",
                        "evidence": {"readiness_gate": verdict["readiness_gate"]},
                        "error": "readiness gate failed; refusing to spend a turn",
                    }
                )
        elif args.plumbing_only:
            for avenue_id in ("task-modes", "waits-cancel"):
                avenues.append(
                    {
                        "avenue": avenue_id,
                        "status": "blocked",
                        "evidence": {"reason": "plumbing-only run: no LM turn was driven"},
                        "error": None,
                    }
                )
        else:
            common.bind_provider(call, provider=args.provider, model=args.model)
            verdict["provider"] = {"provider": args.provider, "model": args.model}

            avenues.append(
                avenue_task_modes(call, wsid, sid, out_path, turn_timeout_s=args.turn_timeout_s)
            )
            avenues.append(
                avenue_waits_cancel(
                    call,
                    base,
                    wsid,
                    sid,
                    out_path,
                    wait_event_timeout_s=args.wait_event_timeout_s,
                    cancel_timeout_s=args.cancel_timeout_s,
                )
            )

        verdict["avenues"] = avenues
        verdict["pass"] = not any(a["status"] == "fail" for a in avenues)
        common.write_verdict(out_path, verdict)
        return 0 if verdict["pass"] else 1
    except Exception as exc:  # noqa: BLE001 - captured into the verdict, never a bare traceback
        import traceback

        verdict["avenues"] = avenues
        verdict["error"] = f"{type(exc).__name__}: {exc}"
        verdict["traceback"] = traceback.format_exc()
        verdict["pass"] = False
        common.write_verdict(out_path, verdict)
        return 1
    finally:
        common.terminate_server(proc)
        if hcap_proc is not None:
            common.terminate_server(hcap_proc)


def _hcap_reachable(port: int) -> bool:
    """Cheap TCP-only readiness probe (a bare GET / on the streamable-http
    mount is not guaranteed a 200, so this only proves the port accepts
    connections -- good enough before the REST-install lane's own real
    fastmcp handshake, which is the definitive proof)."""

    return not common.port_is_free(port)


if __name__ == "__main__":
    raise SystemExit(main())
