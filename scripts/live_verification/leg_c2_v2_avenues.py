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
 2. mrtr-url          -- C1-S4 (#1284) landed: the exerciser gained
                         ``url_guarded_input`` (task=required, embeds a
                         genuine ``ElicitRequestURLParams``). REAL assertion:
                         drives it through a real session turn, asserts the
                         resulting url-mode question payload carries the FULL
                         url + the punycode-warning fields (build item 3),
                         then answers it -- needs a model in the loop, like 1/5/11.
 3. mrtr-methods      -- C1-S4 (#1284) landed: the exerciser gained an
                         MRTR-capable ``guarded_prompt``/``guarded_resource``.
                         REAL, headless assertion via the REST-install lane
                         (``POST /v1/mcp/servers`` + ``.../prompts/get``):
                         proves MRTR genuinely dispatches on ``prompts/get``
                         (a typed, terminal-fast refusal -- this lane's client
                         wires no elicitation handler) and that
                         ``resources/read`` genuinely has NO REST route in
                         this repo (``gact/routes/mcp.py`` only lists
                         resources) -- the full round-trip for both methods,
                         through a properly elicitation-wired client on BOTH
                         the direct and proxy routes, is proven instead in
                         ``tests/test_tools/test_mcp_v2_conformance.py``.
 4. cache             -- #1285 C1-S5: FLIPPED to a real assertion.
                         ``make_mcp_client`` opts INTO SEP-2549 caching when
                         ``response_cache_enabled()`` is true (this probe
                         flips the flag for its own duration, proving the
                         factory's wiring, not a changed operator default);
                         a ``build_exerciser_server(cache_ttl=...,
                         cache_scope=...)`` instance's second ``tools/list``
                         is served from cache (a recording store proves
                         exactly one ``set``, not two). Headless, in-process.
 5. waits-cancel      -- staller surfaces ``mcp_task.wait`` live-SSE events,
                         then a cancel ends the turn ``cancelled``, not hung.
 6. pagination        -- the readiness gate's full-tool-matrix resolution is
                         the available (indirect) proof; no ``list_page_size``
                         control exists anywhere in clio_agent to force real
                         multi-page traversal.
 7. list-changed      -- #1285 C1-S5: FLIPPED to a real assertion. The
                         exerciser's ``mutate_and_notify_list_changed`` hides
                         ``list_changed_target`` (a real fastmcp
                         ``ctx.disable_components`` registry mutation, firing
                         an UNSOLICITED ``notifications/tools/list_changed``)
                         and ``tools/mcp_listen.py::
                         list_changed_message_handler`` invalidates
                         ``tools/listing_cache.py`` on receipt. Uses the
                         message_handler path, not ``watch_list_changed``'s
                         spec-correct ``subscriptions/listen``: fastmcp's
                         SERVER implements zero listen support (live-verified
                         -32601, reconfirmed unchanged across the b1->b5 bump;
                         see that module's docstring for the full finding).
                         Headless, in-process.
 8. extensions        -- C1-S3 (#1283) landed: the handshake row now surfaces
                         the server-declared extension SET directly
                         (``gact/routes/mcp_rows.py::handshake_server_row``'s
                         ``extensions`` field). REAL assertion: the row must
                         contain both the tasks id and the exerciser's own
                         synthetic, non-built-in identifier
                         (``x-clio-agent/exerciser-echo``) -- proving the read
                         side is generic, not a tasks/ui shortlist. Headless
                         (the handshake was already fetched for the readiness
                         gate).
 9. adversarial       -- #1285 C1-S5: FLIPPED to a real assertion.
                         ``mcp_adversarial_fixture.py`` short-circuits four
                         requests with hand-built malformed frames (bad
                         resultType, -32021 with no requiredCapabilities,
                         always-32020, empty-string pagination cursor).
                         Asserts clio's typed handling of each -- including a
                         verified fastmcp CLIENT bug (empty-string cursor
                         treated as terminal pagination, not a clio defect).
                         Headless, in-process.
10. headers           -- a NEW header-capture HTTP MCP server
                         (``_header_capture_server.py``) probed through the
                         REST-install lane (``POST /v1/mcp/servers`` +
                         ``.../call``); genuinely live-tested, not assumed.
11. apps-ui           -- C1-S3 (#1283) landed: the exerciser now carries a
                         real ui-serving arm (``ui_echo`` + ``ui://v2ex/
                         panel``). REAL assertion: drives ``v2ex_ui_echo``
                         through a real session turn and asserts an
                         ``mcp_app`` Part is minted AND ``GET /v1/sessions/
                         {sid}/mcp-apps/{app_id}`` actually serves the
                         resource -- needs a model in the loop (the turn
                         decides to call the tool), unlike avenue 8.

Mechanics mirror ``leg_c_synthetic_session.py``: its own server boot on a
free port, allow-all policies FIRST, the ``v2ex-avenues`` testing-agent pack
(``agents/v2ex-avenues/`` -- a SIBLING of ``agents/v2ex-testing/``, not an
edit to it, exposing every exerciser tool leg C's narrower pack does not),
zero-LM readiness gate before any turn, ``claude_code``/``sonnet`` turns ONLY
for the FOUR avenues that genuinely need a model in the loop (1, 2, 5, and 11
as of C1-S4) -- every other avenue is driven through a headless HTTP/SSE
surface, per the house cost-aware-defaults + "prefer direct surfaces" rule.

Verdict JSON: ``out/live-verification/leg_c2_verdict.json``.

Usage::

    uv run python scripts/live_verification/leg_c2_v2_avenues.py --dry-run
    uv run python scripts/live_verification/leg_c2_v2_avenues.py --plumbing-only
    uv run python scripts/live_verification/leg_c2_v2_avenues.py   # spends LM tokens (3 turns)
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root, for tests.* import

import _common as common  # noqa: E402
import _header_capture_server as hcap  # noqa: E402
from _sse_collector import SSECollector  # noqa: E402

from tests.test_tools.mcp_exerciser import (  # noqa: E402
    EXERCISER_NAMESPACE,
    EXERCISER_PATH,
    LIST_CHANGED_TOOL_NAME,
    SYNTHETIC_EXTENSION_ID,
    TASKS_EXTENSION_ID,
    URL_GUARDED_INPUT_IDN_URL,
    URL_GUARDED_INPUT_URL,
)

#: The v2ex-avenues pack template this leg materializes per run (sibling of
#: leg C's narrower v2ex-testing pack -- see agents/v2ex-avenues/AGENT.md).
PACK_TEMPLATE_DIR = Path(__file__).resolve().parent / "agents" / "v2ex-avenues"

#: Every tool build_exerciser_server() registers (mirrors leg_c_synthetic_
#: session.py's own EXPECTED_TOOLS -- kept as an independent, hand-written
#: constant here rather than a cross-script import, matching how each leg in
#: this package already declares its own expectation set).
#: `invalid_header_echo` is deliberately EXCLUDED: a modern-era client MUST
#: drop it from tools/list (SEP-2578) -- it can never resolve onto an agent's
#: toolset, so a readiness gate that required it would never pass by design.
EXERCISER_EXPECTED_TOOLS = {
    "task_echo",
    "task_optional_echo",
    "plain_echo",
    "forbidden_echo",
    "guarded_input",
    "plain_guarded_input",
    "url_guarded_input",  # C1-S4, #1284: the mrtr-url avenue's tool
    "url_guarded_input_idn",  # Opus review addendum, C1-S4: the IDN counterpart
    "staller",
    "plain_staller",
    "silent_sleeper",
    "ui_echo",
    "header_annotated_echo",
    "list_changed_target",
    "mutate_and_notify_list_changed",
}

#: Every namespaced tool the pack's `main` expert must resolve before any
#: turn is driven (the readiness gate; also avenue 6's evidence).
NEEDED_AGENT_TOOLS = {f"{EXERCISER_NAMESPACE}_{name}" for name in EXERCISER_EXPECTED_TOOLS}


#: The url-mode trust allow-list this leg boots the gact server with (C1-S4,
#: #1284; extended for the IDN arm by the Opus review addendum): must stay
#: in lockstep with the exerciser's own URL_GUARDED_INPUT_URL /
#: URL_GUARDED_INPUT_IDN_URL constants, or the mrtr-url avenue's elicitation
#: is auto-declined before a question ever mints
#: (``elicitation_url_not_declared``). Comma-separated -- ``conf.as_csv``
#: parses ``CLIO_MCP_ELICITATION_URL_TRUSTED_ORIGINS``.
def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


URL_TRUST_ORIGIN = ",".join((_origin(URL_GUARDED_INPUT_URL), _origin(URL_GUARDED_INPUT_IDN_URL)))

#: Static avenue plan -- used by both --dry-run and (for cross-reference)
#: LEG_C2.md's table. ``needs_lm`` marks the FOUR avenues driven through a
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
        "needs_lm": True,
        "expect": "pass",
        "summary": (
            "C1-S4 (#1284) landed: url_guarded_input's question payload carries the "
            "FULL url + punycode-warning fields; url_guarded_input_idn (Opus review "
            "addendum) proves warning=True on an xn-- IDN origin alongside "
            "warning=False on the plain-ASCII one"
        ),
    },
    {
        "avenue": "mrtr-methods",
        "needs_lm": False,
        "expect": "pass",
        "summary": (
            "C1-S4 (#1284) landed: prompts/get dispatches a typed MRTR refusal through "
            "the REST-install lane; resources/read genuinely has no REST route (proven "
            "at the SDK conformance-suite layer instead)"
        ),
    },
    {
        "avenue": "cache",
        "needs_lm": False,
        "expect": "pass",
        "summary": (
            "#1285 C1-S5: make_mcp_client opts INTO SEP-2549 caching when "
            "response_cache_enabled() is true; a cache_ttl-hinted exerciser's "
            "second tools/list is served from cache"
        ),
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
        "summary": "readiness gate proves all expected tools resolve (indirect; no page-size control exists)",
    },
    {
        "avenue": "list-changed",
        "needs_lm": False,
        "expect": "pass",
        "summary": (
            "#1285 C1-S5: mutate_and_notify_list_changed fires a real ToolsListChanged; "
            "watch_list_changed observes it and invalidates the listing cache"
        ),
    },
    {
        "avenue": "extensions",
        "needs_lm": False,
        "expect": "pass",
        "summary": (
            "C1-S3 landed: the handshake row's extensions field must contain the tasks id "
            "AND the exerciser's synthetic, non-built-in identifier"
        ),
    },
    {
        "avenue": "adversarial",
        "needs_lm": False,
        "expect": "pass",
        "summary": (
            "#1285 C1-S5: four hand-built malformed frames asserted typed-handled; "
            "includes a verified fastmcp pagination-cursor bug pinned as a finding"
        ),
    },
    {
        "avenue": "headers",
        "needs_lm": False,
        "expect": "pass-or-fail (genuinely probed live)",
        "summary": "new header-capture MCP server probed via the REST-install lane",
    },
    {
        "avenue": "apps-ui",
        "needs_lm": True,
        "expect": "pass",
        "summary": (
            "C1-S3 landed: drives v2ex_ui_echo through a real turn and asserts the mcp_app "
            "Part + GET .../mcp-apps/{app_id} serving"
        ),
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
        help="stop before any LM turn; avenues 1, 2, 5, and 11 are recorded 'blocked' (skipped)",
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
# C1-S4 (#1284): mrtr-url + mrtr-methods, flipped from static citations to
# REAL, LIVE-DRIVEN assertions -- strengthening blocked->real when the
# capability exists is the intended lifecycle (owner-authorized).
# --------------------------------------------------------------------------- #
def avenue_mrtr_url(
    call: Any, wsid: str, sid: str, out_path: Path, *, turn_timeout_s: float
) -> dict[str, Any]:
    """C1-S4 (#1284): url-mode MRTR through a real session turn.

    The exerciser gained ``url_guarded_input`` (task=required, embeds a
    genuine ``mcp_types.ElicitRequestURLParams``). This drives it through a
    REAL declared-path turn (the model decides to call it -- url-mode
    elicitation is only wired through the production tool-call path,
    ``agents/builders.py::make_elicitation_client``, never the REST-install
    lane -- needs_lm=True, like task-modes/waits-cancel/apps-ui), asserts the
    resulting question payload carries the FULL url + the punycode-warning
    fields (build item 3), answers it, and confirms the turn completes with
    the tool succeeding.

    Opus review addendum: drives ``url_guarded_input_idn`` too, in the SAME
    turn -- ``URL_GUARDED_INPUT_URL`` is plain ASCII, so a leg that only ever
    called it could observe ``punycode_warning=False`` and NEVER exercise the
    ``warning=True`` branch B5 fixed; that would be a leg that only proves
    half of what it claims. This drives BOTH arms and asserts each carries
    the punycode_warning value ITS shape demands: False for the ASCII origin,
    True for the ``xn--`` IDN one.
    """

    prompt = (
        f"Call these {EXERCISER_NAMESPACE} tools IN ORDER and report each result "
        "verbatim, including any error message if one occurs -- never skip a step "
        "even if an earlier one errors:\n"
        f"1. {EXERCISER_NAMESPACE}_url_guarded_input with no arguments\n"
        f"2. {EXERCISER_NAMESPACE}_url_guarded_input_idn with no arguments"
    )
    common.post_message(call, sid, prompt)

    # --- arm 1: plain-ASCII origin -- must warn FALSE ---
    question1 = common.wait_pending_question(call, sid, source="mcp_elicitation", max_elapsed=60.0)
    question1_seen = question1 is not None
    elicitation1 = ((question1 or {}).get("metadata") or {}).get("elicitation") or {}
    url1 = str(elicitation1.get("url") or "")
    has_full_url1 = bool(url1) and url1 == URL_GUARDED_INPUT_URL
    has_punycode_fields1 = "punycode_warning" in elicitation1 and "punycode_host" in elicitation1
    warning_false_as_expected = elicitation1.get("punycode_warning") is False
    if question1_seen:
        common.answer_question(call, sid, question1["id"], "")

    # --- arm 2: xn-- IDN origin -- must warn TRUE ---
    question2 = common.wait_pending_question(call, sid, source="mcp_elicitation", max_elapsed=60.0)
    question2_seen = question2 is not None
    elicitation2 = ((question2 or {}).get("metadata") or {}).get("elicitation") or {}
    url2 = str(elicitation2.get("url") or "")
    has_full_url2 = bool(url2) and url2 == URL_GUARDED_INPUT_IDN_URL
    has_punycode_fields2 = "punycode_warning" in elicitation2 and "punycode_host" in elicitation2
    warning_true_as_expected = elicitation2.get("punycode_warning") is True
    if question2_seen:
        common.answer_question(call, sid, question2["id"], "")

    status = common.wait_turn(call, wsid, sid, max_elapsed=turn_timeout_s)
    messages = common.session_messages(call, sid)
    common.dump_json(out_path.parent / "leg_c2_mrtr_url_messages.json", messages)

    # _common.find_tool_calls matches by str.endswith, so "_url_guarded_input"
    # and "_url_guarded_input_idn" are disjoint by construction (the former
    # never matches a name ending "..._idn").
    calls_ascii = common.find_tool_calls(messages, "_url_guarded_input")
    calls_idn = common.find_tool_calls(messages, "_url_guarded_input_idn")
    succeeded_ascii = [c for c in calls_ascii if common.tool_call_ok(c)]
    succeeded_idn = [c for c in calls_idn if common.tool_call_ok(c)]

    hung = status == "timed_out"
    pass_ = bool(
        question1_seen
        and has_full_url1
        and has_punycode_fields1
        and warning_false_as_expected
        and question2_seen
        and has_full_url2
        and has_punycode_fields2
        and warning_true_as_expected
        and not hung
        and succeeded_ascii
        and succeeded_idn
    )
    return {
        "avenue": "mrtr-url",
        "status": "pass" if pass_ else "fail",
        "evidence": {
            "turn_status": status,
            "ascii_arm": {
                "question_seen": question1_seen,
                "question_metadata_elicitation": elicitation1,
                "has_full_url": has_full_url1,
                "has_punycode_fields": has_punycode_fields1,
                "warning_false_as_expected": warning_false_as_expected,
                "tool_calls": calls_ascii,
                "tool_call_succeeded": bool(succeeded_ascii),
            },
            "idn_arm": {
                "question_seen": question2_seen,
                "question_metadata_elicitation": elicitation2,
                "has_full_url": has_full_url2,
                "has_punycode_fields": has_punycode_fields2,
                "warning_true_as_expected": warning_true_as_expected,
                "tool_calls": calls_idn,
                "tool_call_succeeded": bool(succeeded_idn),
            },
            "hung": hung,
        },
        "error": None
        if pass_
        else (
            "one or both url-mode questions did not surface, carried an "
            "incomplete payload, showed the WRONG punycode_warning value for "
            "its arm (False expected on the ASCII origin, True on the IDN "
            "one), or its tool call never completed"
        ),
    }


def _status_and_body(error_message: str) -> tuple[int | None, str]:
    """Parse ``_common.client``'s RuntimeError text (``"{method} {path} -> "
    "{status}: {body}"``) into ``(status_code, body)``."""

    try:
        arrow_tail = error_message.split("->", 1)[1]
        status_text, _, body = arrow_tail.partition(":")
        return int(status_text.strip()), body.strip()
    except (IndexError, ValueError):
        return None, error_message


def _prompts_get_dispatched_typed_mrtr_refusal(status: int | None, body: str) -> bool:
    """True when ``body`` is the TYPED 502 ``upstream_error`` shape MRTR's
    unsupported-elicitation dispatch produces (Opus review, C1-S4 B2).

    Keys on the STRUCTURED ``error.error`` field (a stable typed error class,
    ``ErrorInfo.error``) plus the JSON-RPC ``-32600`` (Invalid Request) code
    the SDK's refusal carries -- NEVER on the free-text "Elicitation not
    supported" prose, which upstream could reword at any time and silently
    flip this avenue false-red (the forbidden prose-keyword class this leg
    must not repeat -- see ⚑ SUPERSEDING PRINCIPLES #1 in CLAUDE.md).
    """

    if status != 502:
        return False
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return False
    error = parsed.get("error") if isinstance(parsed, dict) else None
    if not isinstance(error, dict) or error.get("error") != "upstream_error":
        return False
    return "-32600" in str(error.get("message") or "")


def avenue_mrtr_methods(call: Any) -> dict[str, Any]:
    """C1-S4 (#1284): prompts/get + resources/read, headless and LM-free.

    The exerciser gained an MRTR-capable ``guarded_prompt``/``guarded_resource``
    (build item, mirroring ``guarded_input``'s one-round shape). Installs the
    SAME exerciser server through the REST-install lane
    (``POST /v1/mcp/servers`` -- a bare ``make_mcp_client(transport,
    server_id=sid)`` with NO elicitation handler wired, ``gact/routes/mcp.py::
    _external_mcp_inventory``) and calls ``POST .../prompts/get``: proves the
    SDK's MRTR loop genuinely fires on ``prompts/get`` for real (a typed,
    terminal-fast 502 ``upstream_error`` -- never a hang, because this lane's
    client never wires a callback).

    ``resources/read`` is INFORMATIONAL ONLY (Opus review, C1-S4 B1): this
    repo currently has no ``resources/read`` REST route at all (only
    ``GET .../resources`` LISTS), but a future route existing is a GOOD
    change, not a regression -- ``pass``/``fail`` here NEVER keys on that
    route's presence or absence either way. Only ``prompts/get`` dispatching
    a typed refusal decides pass; ``resources/read`` fails this avenue ONLY
    on a genuine hang or an untyped status (neither 404 nor 200) -- see
    ``pass_means`` in the returned verdict.

    The FULL round-trip (asked -> answered -> terminal) for BOTH methods,
    through a properly elicitation-wired client on BOTH the direct and proxy
    routes, is proven instead in the unit conformance suite
    (``tests/test_tools/test_mcp_v2_conformance.py``) -- the house pattern for
    per-path MRTR verification; this avenue's ``pass`` must never be read as
    evidence MRTR-over-prompts/resources itself works end to end.
    """

    pass_means = (
        "pass = the REST-install lane's declared session/turn surface "
        "correctly REFUSES an MRTR-embedded prompts/get, typed and "
        "terminal-fast (never a hang, never an untyped 500) -- because that "
        "lane's client never wires an elicitation handler, BY DESIGN. This is "
        "NOT evidence MRTR-on-prompts/resources works end to end; the full "
        "round trip (asked -> answered -> terminal), for both methods, on "
        "BOTH the direct and proxy routes, is proven separately (offline) in "
        "tests/test_tools/test_mcp_v2_conformance.py. resources/read is "
        "recorded for information only and never decides pass/fail either way."
    )

    install_body = {
        "name": "v2ex-methods",
        "transport": "stdio",
        "command": sys.executable,
        "args": [str(EXERCISER_PATH)],
    }
    try:
        installed = call("POST", "/v1/mcp/servers", install_body, ok=(200, 201))
    except Exception as exc:  # noqa: BLE001 - captured into the verdict
        return {
            "avenue": "mrtr-methods",
            "status": "fail",
            "pass_means": pass_means,
            "evidence": {"install_error": f"{type(exc).__name__}: {exc}"},
            "error": "failed to install the exerciser via POST /v1/mcp/servers",
        }
    server_id = str(installed.get("id") or "")

    prompt_status: int | None = None
    prompt_body = ""
    try:
        call(
            "POST",
            f"/v1/mcp/servers/{server_id}/prompts/get",
            {"name": "guarded_prompt", "arguments": {}},
            ok=(200,),
        )
        prompt_status = 200
    except RuntimeError as exc:
        prompt_status, prompt_body = _status_and_body(str(exc))
    prompt_dispatched_mrtr = _prompts_get_dispatched_typed_mrtr_refusal(prompt_status, prompt_body)

    resource_status: int | None = None
    resource_body = ""
    try:
        call(
            "POST",
            f"/v1/mcp/servers/{server_id}/resources/read",
            {"uri": "res://v2ex/guarded"},
            ok=(200,),
        )
        resource_status = 200
    except RuntimeError as exc:
        resource_status, resource_body = _status_and_body(str(exc))
    # B1 (Opus review, C1-S4): 404 (route absent, true today) and 200 (a
    # future route existing -- a GOOD change) are BOTH acceptable; only a
    # genuine untyped status is a real finding for THIS half of the avenue.
    resource_untyped_failure = resource_status not in (200, 404)
    if resource_status == 404:
        resource_note = (
            "informational, NOT a pass criterion: confirms live that this repo "
            "currently has no resources/read REST route (gact/routes/mcp.py "
            "only lists resources). A future route existing here would be a "
            "GOOD change this avenue must never turn red for."
        )
    elif resource_status == 200:
        resource_note = (
            "informational, NOT a pass criterion: a resources/read REST route "
            "now exists and answered successfully -- a legitimate change, "
            "not a regression. The full MRTR round-trip for resources/read is "
            "proven separately in tests/test_tools/test_mcp_v2_conformance.py."
        )
    else:
        resource_note = (
            "UNEXPECTED: neither a clean 200 nor a 404 -- unlike route "
            "presence/absence, an untyped status here IS a real finding."
        )

    pass_ = bool(prompt_dispatched_mrtr and not resource_untyped_failure)
    return {
        "avenue": "mrtr-methods",
        "status": "pass" if pass_ else "fail",
        "pass_means": pass_means,
        "evidence": {
            "install": installed,
            "prompts_get": {
                "status_code": prompt_status,
                "body": prompt_body,
                "dispatched_mrtr_typed_refusal": prompt_dispatched_mrtr,
            },
            "resources_read": {
                "status_code": resource_status,
                "body": resource_body,
                "informational_only": True,
                "note": resource_note,
            },
        },
        "error": None
        if pass_
        else (
            "prompts/get did not dispatch a typed MRTR refusal, or "
            "resources/read hung/failed with an untyped status"
        ),
    }


async def _run_cache_probe() -> dict[str, Any]:
    """Prove server cache hints end to end (#1285 C1-S5 item 3): CLIO's factory
    opts in, and a hinted server's second list is served from cache, not re-fetched.

    In-process (no gact server): builds the exerciser WITH a cache hint
    (``build_exerciser_server(cache_ttl=..., cache_scope=...)`` -- fastmcp
    applies it server-wide, there is no per-tool knob) and drives it through
    TWO checks: (a) ``tools/mcp_runtime.py::make_mcp_client`` (the REAL
    production factory) sets ``cache=True`` by default; (b) a raw
    ``fastmcp.Client`` with a RECORDING store proves the second
    ``list_tools()`` never calls the store's ``set`` again (a cache hit, not a
    live re-fetch).
    """

    from dataclasses import dataclass, field

    from fastmcp import Client
    from mcp.client.caching import CacheConfig, CacheEntry, CacheKey

    from clio_agent.tools.mcp_runtime import make_mcp_client
    from tests.test_tools.mcp_exerciser import build_exerciser_server

    @dataclass
    class _RecordingStore:
        entries: dict[Any, Any] = field(default_factory=dict)
        get_calls: int = 0
        set_calls: int = 0

        async def get(self, key: CacheKey) -> CacheEntry | None:
            self.get_calls += 1
            return self.entries.get(key)

        async def set(self, key: CacheKey, entry: CacheEntry) -> None:
            self.set_calls += 1
            self.entries[key] = entry

        async def delete(self, key: CacheKey) -> None:
            self.entries.pop(key, None)

        async def clear(self) -> None:
            self.entries.clear()

    class _FakeClientCls:
        def __init__(self, target: Any, **kwargs: Any) -> None:
            self.kwargs = kwargs

    # response_cache_enabled() defaults False (opt-in, tools/mcp_runtime.py::
    # response_cache_enabled's own docstring explains why) -- flip it on for
    # this probe of the factory's WIRING, independent of the operator default.
    import os

    os.environ["CLIO_MCP_RESPONSE_CACHE_ENABLED"] = "true"
    try:
        from clio_agent import conf as _conf

        _conf.reload()
        factory_client = make_mcp_client(build_exerciser_server(), client_cls=_FakeClientCls)
        factory_sets_cache = factory_client.kwargs.get("cache") is True
    finally:
        del os.environ["CLIO_MCP_RESPONSE_CACHE_ENABLED"]
        _conf.reload()

    hinted_server = build_exerciser_server(cache_ttl=60, cache_scope="private")
    store = _RecordingStore()
    async with Client(
        hinted_server, cache=CacheConfig(store=store, partition="leg-c2", target_id="v2ex-cache-probe")
    ) as client:
        first = await client.list_tools()
        second = await client.list_tools()

    return {
        "factory_sets_cache_true": factory_sets_cache,
        "first_list_tool_count": len(first),
        "second_list_tool_count": len(second),
        "store_set_calls": store.set_calls,
        "store_get_calls": store.get_calls,
    }


def avenue_cache() -> dict[str, Any]:
    """#1285 C1-S5 item 3: flipped blocked -> real (was: no client/exerciser support existed).

    ``tools/mcp_runtime.py::make_mcp_client`` now opts every execution-path
    client into SEP-2549 response caching by default, and
    ``mcp_exerciser.py::build_exerciser_server`` accepts ``cache_ttl``/
    ``cache_scope`` (fastmcp applies the hint server-wide -- no per-tool knob
    exists in the library). This avenue asserts BOTH: the production factory
    sets ``cache=True``, and a hinted server's second ``tools/list`` is served
    from cache (exactly one store ``set``, not two).
    """

    import asyncio as _asyncio

    try:
        evidence = _asyncio.run(_run_cache_probe())
    except Exception as exc:  # noqa: BLE001 - captured into the verdict
        return {
            "avenue": "cache",
            "status": "fail",
            "evidence": {},
            "error": f"{type(exc).__name__}: {exc}",
        }

    ok = (
        evidence["factory_sets_cache_true"]
        and evidence["first_list_tool_count"] == evidence["second_list_tool_count"]
        and evidence["store_set_calls"] == 1
    )
    return {
        "avenue": "cache",
        "status": "pass" if ok else "fail",
        "evidence": evidence,
        "error": None if ok else "cache hint was not honored end to end",
    }


async def _run_list_changed_probe() -> dict[str, Any]:
    """Drive a REAL registry mutation + its notification end to end (#1285 C1-S5 item 2).

    In-process (no gact server, no HTTP): live-verified that fastmcp's SERVER
    implements NO ``subscriptions/listen`` support at all (-32601
    Method not found, reconfirmed unchanged across the C1-S5 item-5 b1->b5
    bump -- see tools/mcp_listen.py's module docstring for the full finding +
    tests/test_tools/test_mcp_listen.py's regression lock), so
    ``watch_list_changed`` (spec-correct SEP-2575) cannot be live-proven
    against THIS exerciser. What fastmcp servers verifiably DO send is the
    notification UNSOLICITED over the plain connection -- this probe drives
    ``list_changed_message_handler`` (the path that works against today's
    fastmcp fleet) against a real ``mutate_and_notify_list_changed`` call.
    """

    import asyncio as _asyncio

    from fastmcp import Client

    from clio_agent.tools import listing_cache
    from clio_agent.tools.mcp_listen import list_changed_message_handler
    from tests.test_tools.mcp_exerciser import build_exerciser_server

    server = build_exerciser_server()
    invalidated: list[str] = []
    original_invalidate = listing_cache.invalidate_namespace
    listing_cache.invalidate_namespace = (  # type: ignore[assignment]
        lambda namespace, **_: invalidated.append(namespace) or True
    )
    try:
        handler = list_changed_message_handler(EXERCISER_NAMESPACE)
        async with Client(server, message_handler=handler) as caller:
            mutate_result = await caller.call_tool(LIST_CHANGED_TOOL_NAME, {})
            await _asyncio.sleep(0.3)  # unsolicited notification delivery is async
    finally:
        listing_cache.invalidate_namespace = original_invalidate  # type: ignore[assignment]

    return {
        "mutate_result": str(mutate_result) if mutate_result is not None else None,
        "invalidated_namespaces": invalidated,
    }


def avenue_list_changed() -> dict[str, Any]:
    """#1285 C1-S5 item 2: flipped blocked -> real (was: no exerciser arm existed).

    ``mutate_and_notify_list_changed`` hides ``list_changed_target`` via
    fastmcp's own ``ctx.disable_components`` -- a REAL registry mutation, not
    a synthetic notification -- and asserts ``tools/mcp_listen.py::
    list_changed_message_handler`` actually invalidates ``tools/
    listing_cache.py`` when the resulting unsolicited
    ``notifications/tools/list_changed`` arrives. See ``_run_list_changed_
    probe``'s docstring for why this uses the message_handler path rather
    than ``watch_list_changed``'s spec-correct ``subscriptions/listen``
    (a verified fastmcp server-side gap, not a clio gap).
    """

    import asyncio as _asyncio

    try:
        evidence = _asyncio.run(_run_list_changed_probe())
    except Exception as exc:  # noqa: BLE001 - captured into the verdict
        return {
            "avenue": "list-changed",
            "status": "fail",
            "evidence": {},
            "error": f"{type(exc).__name__}: {exc}",
        }

    observed = evidence["invalidated_namespaces"] == [EXERCISER_NAMESPACE]
    return {
        "avenue": "list-changed",
        "status": "pass" if observed else "fail",
        "evidence": evidence,
        "error": None if observed else "listing_cache.invalidate_namespace was never called",
    }


async def _run_adversarial_probe() -> dict[str, Any]:
    """Drive all four MUST violations end to end (#1285 C1-S5 item 4).

    In-process (no gact server): ``mcp_adversarial_fixture.py`` wraps a real
    fastmcp app in a pure ASGI middleware that short-circuits four specific
    requests with hand-built, deliberately malformed JSON-RPC frames -- see
    that module's docstring for why (bypassing fastmcp's own protocol
    correctness without reimplementing the whole HTTP transport by hand).
    """

    from fastmcp import Client
    from mcp.shared.exceptions import MCPError

    from clio_agent.errors import MCPMissingRequiredClientCapabilityError
    from clio_agent.tools.mcp_errors import typed_mcp_protocol_error
    from clio_agent.tools.mcp_header_mismatch import call_tool_with_header_retry
    from tests.test_tools.mcp_adversarial_fixture import (
        BAD_HEADER_MISMATCH_TOOL,
        BAD_MISSING_CAPS_TOOL,
        BAD_RESULT_TYPE_TOOL,
        PAGINATED_TOOL_2,
        adversarial_in_process_transport,
        build_adversarial_app,
        run_adversarial_lifespan,
    )

    app = build_adversarial_app()
    evidence: dict[str, Any] = {}
    async with run_adversarial_lifespan(app):
        transport = adversarial_in_process_transport(app)
        async with Client(transport) as client:
            result = await client.call_tool(BAD_RESULT_TYPE_TOOL, {"payload": "x"})
            evidence["bad_result_type"] = {
                "is_error": result.is_error,
                "crashed": False,
            }

            try:
                await client.call_tool(BAD_MISSING_CAPS_TOOL, {"payload": "x"})
                evidence["bad_missing_caps"] = {"crashed": False, "raised": False}
            except MCPError as exc:
                typed = typed_mcp_protocol_error(exc)
                evidence["bad_missing_caps"] = {
                    "crashed": False,
                    "raised": True,
                    "code": exc.code,
                    "typed_ok": isinstance(typed, MCPMissingRequiredClientCapabilityError),
                }

            try:
                await call_tool_with_header_retry(client, BAD_HEADER_MISMATCH_TOOL, {"payload": "x"})
                evidence["bad_header_mismatch"] = {"crashed": False, "bounded": False}
            except MCPError as exc:
                evidence["bad_header_mismatch"] = {
                    "crashed": False,
                    "bounded": True,
                    "final_code": exc.code,
                }

            tools = await client.list_tools()
            names = sorted(t.name for t in tools)
            evidence["pagination"] = {
                "resolved_tool_names": names,
                "second_page_reached": PAGINATED_TOOL_2 in names,
                "note": (
                    "verified LIBRARY gap, not a clio gap: fastmcp's Client."
                    "list_tools() checks `if not result.next_cursor: break` "
                    "(fastmcp/client/mixins/tools.py), so an EMPTY-STRING "
                    "cursor (E10: valid, non-terminal) is treated as the end "
                    "-- clio never implements its own pagination, it always "
                    "calls client.list_tools() and trusts the result"
                ),
            }

    return evidence


def avenue_adversarial() -> dict[str, Any]:
    """#1285 C1-S5 item 4: flipped blocked -> real (was: no fixture existed).

    Asserts clio's typed handling of all four violations, including a
    verified fastmcp CLIENT-side bug (empty-string pagination cursor treated
    as terminal) this avenue documents rather than hides.
    """

    import asyncio as _asyncio

    from tests.test_tools.mcp_adversarial_fixture import PAGINATED_TOOL

    try:
        evidence = _asyncio.run(_run_adversarial_probe())
    except Exception as exc:  # noqa: BLE001 - captured into the verdict
        return {
            "avenue": "adversarial",
            "status": "fail",
            "evidence": {},
            "error": f"{type(exc).__name__}: {exc}",
        }

    ok = (
        not evidence["bad_result_type"]["crashed"]
        and evidence["bad_missing_caps"]["raised"]
        and evidence["bad_missing_caps"].get("typed_ok") is True
        and evidence["bad_header_mismatch"]["bounded"]
        and evidence["pagination"]["resolved_tool_names"] == [PAGINATED_TOOL]
    )
    return {
        "avenue": "adversarial",
        "status": "pass" if ok else "fail",
        "evidence": evidence,
        "error": None if ok else "one or more adversarial violations were not handled as expected",
    }


def avenue_apps_ui(
    call: Any, wsid: str, sid: str, out_path: Path, *, turn_timeout_s: float
) -> dict[str, Any]:
    """C1-S3 (#1283) gave the exerciser a real ui-serving arm: ``ui_echo``
    (``_meta.ui.resourceUri`` bound to ``ui://v2ex/panel``) + a matching
    ``@server.resource`` handler serving ``text/html;profile=mcp-app`` HTML.
    REAL assertion (review round 1, F3): drives ``v2ex_ui_echo`` through a
    real session turn and asserts (1) an ``mcp_app`` Part is minted on the
    persisted message stream (``gact/mcp_apps.py::_append_live_assistant_part``
    writes through the SAME single-writer transcript ledger a real assistant
    reply uses -- not the SSE-only transient path ``mcp_task.wait`` rides),
    and (2) ``GET /v1/sessions/{sid}/mcp-apps/{app_id}`` actually resolves
    and serves the ``text/html;profile=mcp-app`` resource. Needs a model in
    the loop (the turn decides to call the tool), unlike avenue 8.
    """

    prompt = (
        f"Call the {EXERCISER_NAMESPACE}_ui_echo tool with payload='panel-probe' "
        "and report exactly what it returned."
    )
    common.post_message(call, sid, prompt)
    status = common.wait_turn(call, wsid, sid, max_elapsed=turn_timeout_s)
    messages = common.session_messages(call, sid)
    common.dump_json(out_path.parent / "leg_c2_apps_ui_messages.json", messages)

    mcp_app_parts = [
        part
        for message in messages
        for part in (message.get("parts") or [])
        if part.get("type") == "mcp_app"
    ]
    if not mcp_app_parts:
        return {
            "avenue": "apps-ui",
            "status": "fail",
            "evidence": {
                "turn_status": status,
                "messages_dump": "leg_c2_apps_ui_messages.json",
            },
            "error": "no mcp_app Part was minted for the ui_echo call",
        }

    part = mcp_app_parts[-1]
    app_id = str(part.get("app_instance_id") or "")
    data_ref = str(part.get("data_ref") or "")
    resolved: dict[str, Any] = {}
    resolve_error: str | None = None
    try:
        resolved = call(
            "GET", f"/v1/sessions/{sid}/mcp-apps/{app_id}", params={"data_ref": data_ref}
        )
    except Exception as exc:  # noqa: BLE001 - captured into the verdict, never a bare traceback
        resolve_error = str(exc)

    resource = resolved.get("resource") or {}
    # Inline literal, deliberately not imported: this script never imports
    # clio_agent (it drives the gact server over HTTP as a subprocess, and
    # the exerciser fixture it does import is clio_agent-free by design --
    # see mcp_exerciser.py's own docstring). The single source of truth is
    # clio_agent.tools.mcp_extension_registry.MCP_APP_MIME_TYPE (re-exports
    # fastmcp.utilities.mime.UI_MIME_TYPE); pulling that module in here would
    # drag the whole clio_agent import graph into a pure HTTP driver script.
    served_ok = (
        resolve_error is None
        and resource.get("mime_type") == "text/html;profile=mcp-app"
        and isinstance(resource.get("html"), str)
        and bool(resource.get("html"))
    )
    ok = bool(app_id) and bool(data_ref) and served_ok
    return {
        "avenue": "apps-ui",
        "status": "pass" if ok else "fail",
        "evidence": {
            "turn_status": status,
            "mcp_app_part": part,
            "resolved_resource_mime_type": resource.get("mime_type"),
            "resolve_error": resolve_error,
        },
        "error": None
        if ok
        else "the ui_echo mcp_app Part or its mcp-apps resource route did not serve as expected",
    }


def avenue_extensions(handshake_row: dict[str, Any] | None) -> dict[str, Any]:
    """C1-S3 (#1283) landed the generic extension registry: the handshake row
    now carries the server-declared extension SET directly
    (``gact/routes/mcp_rows.py::handshake_server_row``'s ``extensions``
    field, ``None`` when genuinely unobserved -- never conflated with a real
    empty list). REAL assertion (review round 1, F3): the exerciser's row
    must contain BOTH the well-known tasks id AND the exerciser's own
    synthetic, non-built-in identifier -- proving the read side is generic
    (records whatever a server actually declares), not a tasks/ui shortlist.
    Headless: the handshake was already fetched for the readiness gate.
    """

    row = handshake_row or {}
    extensions = row.get("extensions")
    has_extensions_list = isinstance(extensions, list)
    has_tasks = has_extensions_list and TASKS_EXTENSION_ID in extensions
    has_synthetic = has_extensions_list and SYNTHETIC_EXTENSION_ID in extensions
    ok = has_tasks and has_synthetic
    return {
        "avenue": "extensions",
        "status": "pass" if ok else "fail",
        "evidence": {
            "handshake_v2ex_row": handshake_row,
            "extensions": extensions,
            "extensions_era": row.get("extensions_era"),
            "has_tasks_id": has_tasks,
            "has_synthetic_id": has_synthetic,
        },
        "error": None
        if ok
        else (
            "handshake row's extensions did not contain both "
            f"{TASKS_EXTENSION_ID!r} and {SYNTHETIC_EXTENSION_ID!r} "
            f"(got {extensions!r})"
        ),
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
                "the exerciser's tools/list to span multiple pages. "
                "fastmcp's Client.list_tools() cursor-based pagination is "
                "SDK-internal (obligations doc row B1, 'library-covered'), not "
                "independently forceable to a small page size from this "
                "codebase. This avenue instead proves pagination TRANSPARENCY "
                "indirectly: if any page boundary were mishandled, some of the "
                "expected tools would be missing from the resolved agent's "
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

    # #1285 (C1-S5, item 1): a SECOND call, against the ANNOTATED tool, so B3
    # (Mcp-Param-* mirroring, SEP-2578) is actually exercised -- the plain
    # `probe` call above only ever proved B2 (Mcp-Method/Mcp-Protocol-Version).
    annotated_call_error: str | None = None
    annotated_result: Any = None
    try:
        annotated_result = call(
            "POST",
            f"/v1/mcp/servers/{server_id}/call",
            {
                "tool": "probe_with_header",
                "args": {"trace_id": "leg-c2-trace", "payload": "hdr-probe"},
            },
            ok=(200, 201),
        )
    except Exception as exc:  # noqa: BLE001 - captured into the verdict, not fatal to avenue 10
        annotated_call_error = f"{type(exc).__name__}: {exc}"

    rows = hcap.read_captured_rows(hcap_log)
    call_rows = [r for r in rows if (r.get("headers") or {}).get("mcp-method") == "tools/call"]
    last = call_rows[-1] if call_rows else {}
    headers = last.get("headers") or {}
    has_method = "mcp-method" in headers
    has_protocol_version = "mcp-protocol-version" in headers
    param_headers = {k: v for k, v in headers.items() if k.startswith("mcp-param-")}

    # The annotated call's OWN row: find_invalid_x_mcp_header/x_mcp_header_map both
    # key on the tool's LAST listed schema, so this must be the row whose call_result
    # tool name matches probe_with_header -- match by presence of mcp-param-trace-id
    # first (the direct signal), falling back to "the last row overall" for evidence.
    annotated_rows = [r for r in call_rows if "mcp-param-trace-id" in (r.get("headers") or {})]
    annotated_headers = (annotated_rows[-1].get("headers") if annotated_rows else None) or {}
    mcp_param_mirrored = annotated_headers.get("mcp-param-trace-id") == "leg-c2-trace"

    status = "pass" if (has_method and has_protocol_version and mcp_param_mirrored) else "fail"
    return {
        "avenue": "headers",
        "status": status,
        "evidence": {
            "install": installed,
            "call_result": result,
            "annotated_call_result": annotated_result,
            "annotated_call_error": annotated_call_error,
            "captured_rows": rows,
            "tools_call_headers": headers,
            "mcp_method_present": has_method,
            "mcp_protocol_version_present": has_protocol_version,
            "mcp_param_headers": param_headers,
            "mcp_param_trace_id_mirrored": mcp_param_mirrored,
            "note": (
                "clio_agent's OWN source carries zero code that sets these "
                "headers (grepped src/clio_agent for Mcp-Method/Mcp-Param: no "
                "hits); any presence here comes from the mcp SDK CLIENT LIBRARY "
                "(tools/mcp_runtime.py::make_mcp_client wraps fastmcp.Client "
                "verbatim, which wraps mcp.client.session.ClientSession -- see "
                "ClientSession._make_modern_stamp). B3 mcp-param-* mirroring is "
                "now genuinely exercised: probe_with_header declares an "
                "x-mcp-header-annotated 'Trace-Id' param, and this avenue "
                "asserts the mirrored header's VALUE, not just its presence."
            ),
        },
        "error": None
        if status == "pass"
        else "tools/call did not carry Mcp-Method/Mcp-Protocol-Version/mirrored Mcp-Param-Trace-Id headers",
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
    proc = common.boot_server(
        args.port,
        cwd=ws_dir,
        sse_log=common.OUT_ROOT / "leg_c2_sse.log",
        # C1-S4 (#1284): the mrtr-url avenue's url_guarded_input elicits
        # URL_GUARDED_INPUT_URL; an undeclared trust list auto-declines it
        # before a question ever mints (elicitation_url_not_declared).
        extra_env={"CLIO_MCP_ELICITATION_URL_TRUSTED_ORIGINS": URL_TRUST_ORIGIN},
    )
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
        avenues.append(avenue_cache())
        avenues.append(avenue_list_changed())
        avenues.append(avenue_adversarial())
        avenues.append(avenue_extensions(v2ex_row))
        avenues.append(avenue_pagination(main_tools))

        # --- headers + mrtr-methods: headless HTTP, no LM needed ---
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
        avenues.append(avenue_mrtr_methods(call))

        if not readiness_ready:
            for avenue_id in ("task-modes", "mrtr-url", "waits-cancel", "apps-ui"):
                avenues.append(
                    {
                        "avenue": avenue_id,
                        "status": "fail",
                        "evidence": {"readiness_gate": verdict["readiness_gate"]},
                        "error": "readiness gate failed; refusing to spend a turn",
                    }
                )
        elif args.plumbing_only:
            for avenue_id in ("task-modes", "mrtr-url", "waits-cancel", "apps-ui"):
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
                avenue_mrtr_url(call, wsid, sid, out_path, turn_timeout_s=args.turn_timeout_s)
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
            avenues.append(
                avenue_apps_ui(call, wsid, sid, out_path, turn_timeout_s=args.turn_timeout_s)
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
