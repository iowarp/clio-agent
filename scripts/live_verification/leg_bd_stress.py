#!/usr/bin/env python3
"""Leg B+D stress gate: web MCP + v2ex exerciser, ONE agent, ONE turn (#1286, C1-S6 addendum).

Owner addendum to the C1-S6 EXPANDED verification: before the expensive
multi-session marketplace `deep-researcher` pack is run (``leg_d_deep_
researcher.md``, driven separately by the orchestrator against
``external/clio-agent-marketplace/deep-researcher/`` -- NOT this script's
scope), prove the SAME two-server, multi-step shape that pack relies on
against a cheap, deterministic, purpose-built pack: ``agents/v2-stress/``
declares BOTH the clio-kit ``web`` server (real network fetch/search,
task=required) and the synthetic ``v2ex`` exerciser
(``tests/test_tools/mcp_exerciser.py``) on ONE `main` expert, and this leg
drives ONE session through ONE turn that forces, in a single agent flow:

  (a) a ``web_search`` call;
  (b) a task-backed ``web_fetch`` of a small, stable PUBLIC PDF (Unicode 15.0
      chapter 1, https://www.unicode.org/versions/Unicode15.0.0/ch01.pdf -- a
      version-pinned, immutable path on a long-lived host, verified reachable
      2026-09-03; the rfc-editor .pdf paths 404) -- plus a PLAIN HTML fetch of
      the SAME stable URL leg B already proved
      (https://www.iana.org/help/example-domains) as a contrasting second
      call in the SAME turn;
  (c) a task-backed ``v2ex_task_echo`` call carrying a nonce the agent must
      round-trip verbatim into its final answer;
  (d) the ``v2ex_guarded_input`` MRTR arm, answered HEADLESSLY mid-turn via
      the SAME questions route leg C's turn 2 uses
      (``POST /v1/sessions/{sid}/questions/{question_id}/answer`` --
      ``gact/elicitation_bridge.py``'s traced HITL surface).

Mechanics mirror leg B/C exactly: own server boot on a free port, allow-all
policies FIRST, materialize the ``v2-stress`` pack (``_common.py::
materialize_testing_pack`` patches BOTH ``mcp_servers`` entries -- ``web``'s
committed value is already portable, ``v2ex``'s is resolved at run time),
install + activate it, a zero-LM PRE-TURN READINESS GATE
(``GET /v1/agents?session_id=...`` must already carry every one of the six
declared tools), then ONE ``claude_code``/``sonnet`` turn.

Verdict JSON: ``out/live-verification/leg_bd_stress_verdict.json``. See
``LEG_BD_STRESS.md`` for what this proves and how to read a failing verdict.

Usage::

    uv run python scripts/live_verification/leg_bd_stress.py --plumbing-only
    uv run python scripts/live_verification/leg_bd_stress.py   # spends LM tokens (1 turn)
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

#: Deliberately small + stable: RFC Editor is a canonical, unchanging host;
#: RFC 8259 (the JSON spec) is short (~16 pages), keeping a live docling
#: pdf->md conversion cheap. Orchestrator note: verify reachability live
#: before relying on this leg's PDF-fetch sub-check -- this package cannot
#: itself probe the network (no live runs from this authoring session).
STABLE_PDF_URL = "https://www.unicode.org/versions/Unicode15.0.0/ch01.pdf"

#: The SAME stable canonical page leg_b_web_fetch.py already proved fetchable
#: live -- reused here as the PLAIN-HTML contrast fetch in the SAME turn.
STABLE_HTML_URL = "https://www.iana.org/help/example-domains"

#: The web-testing pack's own search query shape kept deliberately boring:
#: this leg proves plumbing, not research quality.
SEARCH_QUERY = "RFC 8259 JSON data interchange format"

EXPECTED_WEB_TOOLS = {"fetch", "fetch_events", "search"}
EXPECTED_V2EX_TOOLS = {
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

#: The v2-stress pack template this leg materializes per run.
PACK_TEMPLATE_DIR = Path(__file__).resolve().parent / "agents" / "v2-stress"

#: The six tools the pack's `main` expert declares -- must ALL resolve before
#: any turn is driven (the readiness gate).
NEEDED_AGENT_TOOLS = {
    "web_fetch",
    "web_search",
    f"{EXERCISER_NAMESPACE}_task_echo",
    f"{EXERCISER_NAMESPACE}_task_optional_echo",
    f"{EXERCISER_NAMESPACE}_guarded_input",
    f"{EXERCISER_NAMESPACE}_staller",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--port", type=int, default=17984)
    parser.add_argument("--provider", default="claude_code")
    parser.add_argument("--model", default="sonnet")
    parser.add_argument("--out", default=str(common.OUT_ROOT / "leg_bd_stress_verdict.json"))
    parser.add_argument(
        "--ws-dir",
        default=str(common.OUT_ROOT / "ws-v2-stress"),
        help="workspace dir (also the server process's OS cwd -- see _common.py)",
    )
    parser.add_argument("--turn-timeout-s", type=float, default=1200.0)
    parser.add_argument("--question-wait-s", type=float, default=300.0)
    parser.add_argument(
        "--plumbing-only",
        action="store_true",
        help="stop after the readiness gate; never bind a provider or drive a turn",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the planned prompt + assertions and exit; boots nothing",
    )
    return parser


def _build_prompt(nonce: str) -> str:
    return (
        "Carry out these steps IN ORDER, using exactly the tool named for each step:\n\n"
        f"1. Call web_search with query='{SEARCH_QUERY}'.\n"
        f"2. Call web_fetch on this PDF URL: {STABLE_PDF_URL}\n"
        f"3. Call web_fetch on this HTML URL: {STABLE_HTML_URL}\n"
        f"4. Call {EXERCISER_NAMESPACE}_task_echo with payload='{nonce}'.\n"
        f"5. Call {EXERCISER_NAMESPACE}_guarded_input. It will ask you one question -- that "
        "question is answered externally; just make the call and wait for its final result.\n\n"
        f"In your final answer, report each step's result and copy this EXACT nonce "
        f"verbatim: {nonce}"
    )


def _assert_handshake(call: Any, wsid: str, sid: str) -> dict[str, Any]:
    handshake = call("GET", "/v1/mcp/handshake", params={"workspace_id": wsid, "session_id": sid})
    rows = handshake.get("servers") or []
    web_row = next((r for r in rows if r.get("name") == "web"), None)
    v2ex_row = next((r for r in rows if r.get("name") == EXERCISER_NAMESPACE), None)
    web_ready = bool(
        web_row
        and web_row.get("reachable")
        and EXPECTED_WEB_TOOLS.issubset(set(web_row.get("tools") or []))
    )
    v2ex_ready = bool(
        v2ex_row
        and v2ex_row.get("reachable")
        and EXPECTED_V2EX_TOOLS.issubset(set(v2ex_row.get("tools") or []))
    )
    return {
        "handshake_rows": rows,
        "web_row": web_row,
        "v2ex_row": v2ex_row,
        "web_ready": web_ready,
        "v2ex_ready": v2ex_ready,
    }


def _assert_readiness(call: Any, sid: str, wsid: str) -> dict[str, Any]:
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
    nonce = f"clio-stress-{uuid.uuid4().hex[:8]}"
    prompt = _build_prompt(nonce)

    if args.dry_run:
        import json

        print(
            json.dumps(
                {
                    "leg": "bd_stress",
                    "dry_run": True,
                    "nonce": nonce,
                    "prompt": prompt,
                    "needed_agent_tools": sorted(NEEDED_AGENT_TOOLS),
                    "stable_pdf_url": STABLE_PDF_URL,
                    "stable_html_url": STABLE_HTML_URL,
                },
                indent=2,
            )
        )
        return 0

    out_path = Path(args.out)
    ws_dir = Path(args.ws_dir).resolve()
    ws_dir.mkdir(parents=True, exist_ok=True)

    command = common.quoted_command(sys.executable, str(EXERCISER_PATH))
    pack_root = common.materialize_testing_pack(
        PACK_TEMPLATE_DIR,
        ws_dir,
        {
            "web": common.quoted_command("clio-kit", "mcp-server", "web"),
            EXERCISER_NAMESPACE: command,
        },
    )

    base = f"http://127.0.0.1:{args.port}"
    verdict: dict[str, Any] = {
        "leg": "bd_stress",
        "pack_root": str(pack_root),
        "nonce": nonce,
        "prompt": prompt,
        "stable_pdf_url": STABLE_PDF_URL,
        "stable_html_url": STABLE_HTML_URL,
        "plumbing_only": args.plumbing_only,
    }
    if not common.port_is_free(args.port):
        verdict["error"] = f"port {args.port} is not free"
        common.write_verdict(out_path, {**verdict, "pass": False})
        return 1

    proc = common.boot_server(
        args.port, cwd=ws_dir, sse_log=common.OUT_ROOT / "leg_bd_stress_sse.log"
    )
    try:
        call = common.client(base)
        if not common.wait_health(call):
            verdict["error"] = "gact server never became healthy"
            common.write_verdict(out_path, {**verdict, "pass": False})
            return 1

        common.allow_all(call)
        wsid = common.create_workspace(call, "live-verification-v2-stress", ws_dir)
        sid = common.create_session(call, wsid, "leg-bd-stress")
        verdict["workspace_id"] = wsid
        verdict["session_id"] = sid

        install_result = common.install_blueprint(call, pack_root, wsid)
        blueprint_id = str(install_result.get("id") or "")
        common.activate_blueprint(call, sid, blueprint_id)
        verdict["blueprint_id"] = blueprint_id

        handshake_result = _assert_handshake(call, wsid, sid)
        verdict.update(handshake_result)

        readiness_result = _assert_readiness(call, sid, wsid)
        verdict.update(readiness_result)
        readiness_ok = readiness_result["readiness_gate"]["ready"]
        handshake_ok = handshake_result["web_ready"] and handshake_result["v2ex_ready"]

        if args.plumbing_only:
            verdict["pass"] = bool(handshake_ok and readiness_ok)
            common.write_verdict(out_path, verdict)
            return 0 if verdict["pass"] else 1

        if not (handshake_ok and readiness_ok):
            verdict["error"] = (
                "web and/or v2ex not ready/complete at handshake, or the resolved agent's "
                "tools are missing a needed tool; refusing to spend a turn"
            )
            verdict["pass"] = False
            common.write_verdict(out_path, verdict)
            return 1

        common.bind_provider(call, provider=args.provider, model=args.model)
        verdict["provider"] = {"provider": args.provider, "model": args.model}

        common.post_message(call, sid, prompt)

        answer_value = f"clio-answer-{uuid.uuid4().hex[:8]}"
        question = common.wait_pending_question(
            call, sid, source="mcp_elicitation", max_elapsed=args.question_wait_s
        )
        question_surfaced = bool(question)
        if question is not None:
            common.answer_question(call, sid, question["id"], answer_value)

        status = common.wait_turn(call, wsid, sid, max_elapsed=args.turn_timeout_s)
        verdict["turn_status"] = status
        verdict["question_surfaced"] = question_surfaced
        verdict["answer_value"] = answer_value
        if question is not None:
            elicitation = (question.get("metadata") or {}).get("elicitation") or {}
            verdict["question"] = question
            verdict["question_mode"] = elicitation.get("mode")

        messages = common.session_messages(call, sid)
        common.dump_json(out_path.parent / "leg_bd_stress_messages.json", messages)

        search_calls = common.find_tool_calls(messages, "_search")
        fetch_calls = [
            c
            for c in common.find_tool_calls(messages, "_fetch")
            if "events" not in str(c.get("name") or "")
        ]
        pdf_fetch_calls = [c for c in fetch_calls if STABLE_PDF_URL in str(c.get("args") or "")]
        html_fetch_calls = [c for c in fetch_calls if STABLE_HTML_URL in str(c.get("args") or "")]
        task_echo_calls = [
            c
            for c in common.find_tool_calls(messages, "_task_echo")
            if "optional" not in str(c.get("name") or "")
        ]
        guarded_calls = [
            c
            for c in common.find_tool_calls(messages, "_guarded_input")
            if "plain" not in str(c.get("name") or "")
        ]

        search_ok = [c for c in search_calls if common.tool_call_ok(c)]
        pdf_ok = [c for c in pdf_fetch_calls if common.tool_call_ok(c)]
        html_ok = [c for c in html_fetch_calls if common.tool_call_ok(c)]
        task_echo_ok = [c for c in task_echo_calls if common.tool_call_ok(c)]
        guarded_ok = [c for c in guarded_calls if common.tool_call_ok(c)]

        pdf_result_text = " ".join(str(c.get("result") or "") for c in pdf_ok)
        docling_plausible = (
            len(pdf_result_text) > 200
        )  # heuristic; hand-review the actual markdown quality
        nonce_in_task_echo_result = any(nonce in str(c.get("result") or "") for c in task_echo_ok)

        # Message wire shape (gact/parts.py::Part): text content lives in
        # `parts[].text` for parts of `type == "text"` -- there is no
        # top-level message "text"/"content" field (gact/types.py::Message).
        final_text = ""
        for m in reversed(messages):
            if m.get("role") != "assistant":
                continue
            texts = [
                str(p.get("text") or "")
                for p in (m.get("parts") or [])
                if isinstance(p, dict) and p.get("type") == "text"
            ]
            final_text = "\n".join(t for t in texts if t)
            if final_text:
                break
        nonce_in_final_answer = nonce in final_text

        requirements = {
            "web_search_succeeded": bool(search_ok),
            "web_fetch_pdf_succeeded": bool(pdf_ok),
            "web_fetch_pdf_docling_output_plausible": docling_plausible,
            "web_fetch_html_succeeded": bool(html_ok),
            "v2ex_task_echo_succeeded": bool(task_echo_ok),
            "nonce_round_tripped_in_task_echo_result": nonce_in_task_echo_result,
            "nonce_round_tripped_in_final_answer": nonce_in_final_answer,
            "guarded_input_question_answered": question_surfaced,
            "guarded_input_succeeded": bool(guarded_ok),
        }
        verdict["evidence"] = {
            "search_calls": search_calls,
            "fetch_calls": fetch_calls,
            "pdf_fetch_calls": pdf_fetch_calls,
            "html_fetch_calls": html_fetch_calls,
            "task_echo_calls": task_echo_calls,
            "guarded_input_calls": guarded_calls,
            "pdf_result_char_count": len(pdf_result_text),
            "final_answer_text": final_text,
        }
        verdict["requirements"] = requirements

        hung = status == "timed_out"
        verdict["pass"] = bool(
            not hung and status in ("idle", "completed") and all(requirements.values())
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
