#!/usr/bin/env python3
"""Leg: the synthetic v2 exerciser (C1-S0, #1280) driven ON A REAL SESSION.

Every existing exerciser conformance test drives it through an in-process
``AsyncMCPToolExecutor``/``Client(server)`` -- never through a live gact
session with a real model turn. This leg closes that gap: declare the
exerciser as a purpose-built single-expert Agent Blueprint pack's declared
MCP server (``agents/v2ex-testing/`` -- ``AGENT.md`` frontmatter's
``mcp_servers: {v2ex: "<python>" "<EXERCISER_PATH>"}``, materialized at run
time -- see the quoting note below and ``_common.py::
materialize_testing_pack``), boot a gact server, and drive it through two
real turns on ONE session.

This leg used to declare the exerciser via a bare session + workspace
``.clio/mcp.yaml``. Investigation proved the bare-session builtin main's
toolset is a hardcoded 4-tool list, so a declared server's tools never reach
it no matter what ``mcp.yaml`` declares (#1301, deferred upstream -- the
Python builtin main is being dissolved). This leg now rides the WORKING path
instead: the ``v2ex-testing`` pack's ``main`` expert declares ``tools:
[v2ex_task_echo, v2ex_guarded_input]`` -- the same Agent Blueprint mechanism
every real marketplace pack uses. The workspace ``mcp.yaml`` declaration this
leg used to write is now REDUNDANT (the pack frontmatter declares the
server) and has been dropped.

Turn 1 proves live task=required plumbing through a real model turn: instruct
the agent to call ``task_echo`` with a given payload and report the result;
assert the call succeeded via message metadata.

Turn 2 (MRTR) proves the live HITL surface an ``input_required`` round rides:
instruct the agent to call ``guarded_input``. Its single elicit round is
answered by the SAME surface a native ``ask_user`` question uses (confirmed
by tracing the code, not assumed): the tasks-drive's ``_answer_round``
(``tools/mcp_tasks.py``) calls the client's elicitation callback, which for
every declared-server client (proxy OR direct route -- both are built with
the handlers ``agent.py::_build_tool_gateway`` captures from
``elicitation_correlation.make_correlated_handlers()``) resolves to
``gact/elicitation_bridge.py::handle_elicitation``, which mints a
``UserQuestion`` on the SAME ``/v1/sessions/{sid}/questions`` surface a native
ask-user question uses. So the headless answer route IS
``POST /v1/sessions/{sid}/questions/{question_id}/answer`` -- this leg polls
for the pending question (source ``mcp_elicitation``), answers it headlessly,
then asserts the round completed with the answered value. No interactive-only
gap was found; if a live run ever finds the question never surfaces within
the wait window, the verdict below fails NAMED (never silently passes).

``--plumbing-only`` stops after the PRE-TURN READINESS GATE (``v2ex``
reachable with its 9 tools at handshake, AND the resolved ``main`` agent's
``tools`` include ``v2ex_task_echo``/``v2ex_guarded_input`` --
``GET /v1/agents?session_id=...``) -- before provider bind, before either
turn.

Verdict JSON: ``out/live-verification/leg_c_synthetic_session.json``.

Usage::

    uv run python scripts/live_verification/leg_c_synthetic_session.py --plumbing-only
    uv run python scripts/live_verification/leg_c_synthetic_session.py   # spends LM tokens
"""

from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root, for tests.* import

import _common as common  # noqa: E402

from tests.test_tools.mcp_exerciser import (  # noqa: E402
    EXERCISER_NAMESPACE,
    EXERCISER_PATH,
)

EXPECTED_TOOLS = {
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

#: The v2ex-testing pack template this leg materializes per run.
PACK_TEMPLATE_DIR = Path(__file__).resolve().parent / "agents" / "v2ex-testing"

#: The root ``main`` expert's tools that must be resolved BEFORE any turn is
#: driven (mind the ``<namespace>_<tool>`` naming -- the readiness gate that
#: proves the blueprint path, not the old hardcoded-4-tool bare-session path).
NEEDED_AGENT_TOOLS = {
    f"{EXERCISER_NAMESPACE}_task_echo",
    f"{EXERCISER_NAMESPACE}_guarded_input",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=17981)
    parser.add_argument("--provider", default="claude_code")
    parser.add_argument("--model", default="sonnet")
    parser.add_argument("--out", default=str(common.OUT_ROOT / "leg_c_synthetic_session.json"))
    parser.add_argument(
        "--ws-dir",
        default=str(common.OUT_ROOT / "ws-v2ex"),
        help="workspace dir (also the server process's OS cwd -- see _common.py)",
    )
    parser.add_argument("--turn-timeout-s", type=float, default=900.0)
    parser.add_argument("--question-wait-s", type=float, default=180.0)
    parser.add_argument(
        "--plumbing-only",
        action="store_true",
        help="stop after the readiness gate; never bind a provider or drive a turn",
    )
    return parser


def _assert_v2ex_handshake(call: Any, wsid: str, sid: str) -> dict[str, Any]:
    # ``session_id`` is REQUIRED to see the pack-declared server -- see
    # leg_b_web_fetch.py's ``_assert_web_handshake`` for the same note.
    handshake = call("GET", "/v1/mcp/handshake", params={"workspace_id": wsid, "session_id": sid})
    rows = handshake.get("servers") or []
    row = next((r for r in rows if r.get("name") == EXERCISER_NAMESPACE), None)
    result: dict[str, Any] = {"handshake_rows": rows, "v2ex_row": row}
    if row is None:
        result["v2ex_ready"] = False
        result["v2ex_tools_match"] = False
        return result
    result["v2ex_ready"] = bool(row.get("reachable"))
    result["v2ex_tools_match"] = EXPECTED_TOOLS.issubset(set(row.get("tools") or []))
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
    args = build_parser().parse_args()
    out_path = Path(args.out)
    ws_dir = Path(args.ws_dir).resolve()
    ws_dir.mkdir(parents=True, exist_ok=True)

    # Quoting constraint discovered building this package: tools/mcp_config.py's
    # string-command form parses via shlex.split (POSIX mode) -- an unquoted
    # Windows backslash path is silently mangled (backslashes eaten). Verified
    # both the failure and the fix (double-quote every token) directly against
    # shlex.split, matching mcp_config's exact call. The SAME format governs an
    # Agent Blueprint's ``mcp_servers`` frontmatter value, so the exerciser's
    # command must be resolved at RUN time (never a hardcoded drive path
    # committed to the template) -- materialize_testing_pack patches it in.
    command = common.quoted_command(sys.executable, str(EXERCISER_PATH))
    pack_root = common.materialize_testing_pack(
        PACK_TEMPLATE_DIR, ws_dir, {EXERCISER_NAMESPACE: command}
    )

    base = f"http://127.0.0.1:{args.port}"
    verdict: dict[str, Any] = {
        "leg": "c_synthetic_session",
        "pack_root": str(pack_root),
        "mcp_command": command,
        "namespace": EXERCISER_NAMESPACE,
        "plumbing_only": args.plumbing_only,
    }
    if not common.port_is_free(args.port):
        verdict["error"] = f"port {args.port} is not free"
        common.write_verdict(out_path, {**verdict, "pass": False})
        return 1

    proc = common.boot_server(
        args.port, cwd=ws_dir, sse_log=common.OUT_ROOT / "leg_c_synthetic_session_sse.log"
    )
    try:
        call = common.client(base)
        if not common.wait_health(call):
            verdict["error"] = "gact server never became healthy"
            common.write_verdict(out_path, {**verdict, "pass": False})
            return 1

        common.allow_all(call)
        wsid = common.create_workspace(call, "live-verification-v2ex", ws_dir)
        sid = common.create_session(call, wsid, "leg-c-synthetic-session")
        verdict["workspace_id"] = wsid
        verdict["session_id"] = sid

        install_result = common.install_blueprint(call, pack_root, wsid)
        blueprint_id = str(install_result.get("id") or "")
        common.activate_blueprint(call, sid, blueprint_id)
        verdict["blueprint_id"] = blueprint_id

        handshake_result = _assert_v2ex_handshake(call, wsid, sid)
        verdict.update(handshake_result)

        readiness_result = _assert_readiness(call, sid, wsid)
        verdict.update(readiness_result)
        readiness_ok = readiness_result["readiness_gate"]["ready"]

        handshake_ok = handshake_result["v2ex_ready"] and handshake_result["v2ex_tools_match"]

        if args.plumbing_only:
            verdict["pass"] = bool(handshake_ok and readiness_ok)
            common.write_verdict(out_path, verdict)
            return 0 if verdict["pass"] else 1

        if not (handshake_ok and readiness_ok):
            verdict["error"] = (
                "v2ex not ready/complete at handshake, or the resolved agent's "
                "tools are missing a needed tool; refusing to spend a turn"
            )
            verdict["pass"] = False
            common.write_verdict(out_path, verdict)
            return 1

        common.bind_provider(call, provider=args.provider, model=args.model)
        verdict["provider"] = {"provider": args.provider, "model": args.model}

        # --- Turn 1: task_echo (live task=required plumbing) -------------------
        payload = f"clio-verify-{uuid.uuid4().hex[:8]}"
        common.post_message(
            call,
            sid,
            f"Call the {EXERCISER_NAMESPACE}_task_echo tool with payload='{payload}' "
            "and report exactly what it returned.",
        )
        status_1 = common.wait_turn(call, wsid, sid, max_elapsed=args.turn_timeout_s)
        messages_1 = common.session_messages(call, sid)
        common.dump_json(out_path.parent / "leg_c_turn1_messages.json", messages_1)
        echo_calls = [
            c
            for c in common.find_tool_calls(messages_1, "task_echo")
            if "optional" not in str(c.get("name") or "")
        ]
        succeeded_echo = [c for c in echo_calls if common.tool_call_ok(c)]
        echo_result_has_payload = any(payload in str(c.get("result") or "") for c in succeeded_echo)
        verdict["turn1"] = {
            "status": status_1,
            "payload": payload,
            "task_echo_calls": echo_calls,
            "task_echo_succeeded": bool(succeeded_echo),
            "task_echo_result_has_payload": echo_result_has_payload,
            "pass": bool(
                status_1 in ("idle", "completed") and succeeded_echo and echo_result_has_payload
            ),
        }

        # --- Turn 2: guarded_input (MRTR round through the real HITL surface) --
        common.post_message(
            call,
            sid,
            f"Call the {EXERCISER_NAMESPACE}_guarded_input tool. It will ask you one "
            "question -- when it does, that question is answered externally; just "
            "make the call, wait for its final result, and report exactly what it "
            "returned.",
        )
        answer_value = f"clio-answer-{uuid.uuid4().hex[:8]}"
        question = common.wait_pending_question(
            call, sid, source="mcp_elicitation", max_elapsed=args.question_wait_s
        )
        turn2: dict[str, Any] = {"answer_value": answer_value, "question_surfaced": bool(question)}
        if question is not None:
            elicitation = (question.get("metadata") or {}).get("elicitation") or {}
            turn2["question"] = question
            turn2["question_mode"] = elicitation.get("mode")
            turn2["question_fields"] = elicitation.get("fields")
            common.answer_question(call, sid, question["id"], answer_value)
            status_2 = common.wait_turn(call, wsid, sid, max_elapsed=args.turn_timeout_s)
            messages_2 = common.session_messages(call, sid)
            common.dump_json(out_path.parent / "leg_c_turn2_messages.json", messages_2)
            guarded_calls = [
                c
                for c in common.find_tool_calls(messages_2, "guarded_input")
                if "plain" not in str(c.get("name") or "")
            ]
            succeeded_guarded = [c for c in guarded_calls if common.tool_call_ok(c)]
            answered_with_value = any(
                answer_value in str(c.get("result") or "") for c in succeeded_guarded
            )
            turn2.update(
                {
                    "status": status_2,
                    "guarded_input_calls": guarded_calls,
                    "guarded_input_succeeded": bool(succeeded_guarded),
                    "answered_with_our_value": answered_with_value,
                    "requires_interactive": False,
                    "pass": bool(
                        status_2 in ("idle", "completed")
                        and succeeded_guarded
                        and answered_with_value
                    ),
                }
            )
        else:
            # NAMED failure, never a silent pass: the question genuinely never
            # surfaced within the wait window (see module docstring -- this is
            # not expected, but the assertion stays honest either way).
            turn2.update(
                {
                    "status": "question_never_surfaced",
                    "requires_interactive": True,
                    "pass": False,
                }
            )
        verdict["turn2"] = turn2

        verdict["pass"] = bool(verdict["turn1"]["pass"] and turn2["pass"])
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
