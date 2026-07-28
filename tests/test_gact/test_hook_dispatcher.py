"""P2.2 #1070 — the one hook dispatcher + subprocess adapter + ported consumers.

Exercises the real subprocess adapter (the industry exit-0/exit-2 wire), the
dispatcher's matching/merge/failure posture, the invariants (stable id, reads
never gated, tighten-only, hook-failure != user-rejection), and the atomic port of
the four live consumers of the deleted ``runtime/hooks.py``.
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import _make_permission_gate, build_app
from clio_agent.gact.hooks import (
    PRE_TOOL_USE,
    SEMANTIC_EVENT,
    USER_PROMPT_SUBMIT,
    HookDecision,
    HookDispatcher,
    HookEnvelope,
    HookOutcome,
    hook_reasons,
    install_global_dispatcher,
    parse_hook_entries,
    wire_annotations,
)
from clio_agent.gact.hooks.config import HookConfigError, _parse_entry
from clio_agent.gact.permission_gate import DenyDecision, _external_mcp_permission_context
from tests.test_gact._hook_fixtures import (
    command_run,
    dispatcher_from_rows,
    make_command_dispatcher,
    write_hook_script,
)

pytestmark = pytest.mark.usefixtures("host_agent_executor")


@dataclass
class _Pred:
    answer: str = "ok"
    selected_expert: str = ""
    routing_rationale: str = ""


class _Agent:
    def forward(self, question: str, session_id: str = "default") -> _Pred:
        return _Pred()


def _pre_tool_env(name: str = "hdf5_write", args: dict | None = None) -> HookEnvelope:
    return HookEnvelope(
        hook_event_name=PRE_TOOL_USE,
        session_id="sess-1",
        turn_id="turn-1",
        tool_name=name,
        tool_input=args or {"path": "/tmp/x"},
        tool_annotations={"readOnly": False, "destructive": True, "openWorld": False},
    )


# --------------------------------------------------------------------------- #
# Subprocess wire contract (hooks-research C1–C8)                                #
# --------------------------------------------------------------------------- #


def test_exit0_empty_stdout_allows(tmp_path: Path) -> None:
    """C1: exit 0 with empty stdout => allow, no context change."""

    disp = make_command_dispatcher(
        tmp_path, event=PRE_TOOL_USE, body="import sys\nsys.exit(0)\n"
    )
    outcome = disp.dispatch(PRE_TOOL_USE, _pre_tool_env())
    assert outcome.decision == "allow"
    assert not outcome.denied


def test_exit0_stdout_deny_blocks_with_reason(tmp_path: Path) -> None:
    """C2: exit 0 + stdout {decision:deny,reason} => blocked; reason reaches model."""

    body = (
        "import json, sys\n"
        'print(json.dumps({"decision": "deny", "reason": "no hdf5 today"}))\n'
        "sys.exit(0)\n"
    )
    disp = make_command_dispatcher(tmp_path, event=PRE_TOOL_USE, body=body)
    outcome = disp.dispatch(PRE_TOOL_USE, _pre_tool_env())
    assert outcome.denied
    assert outcome.reason == "no hdf5 today"


def test_exit2_stderr_is_deny_with_reason(tmp_path: Path) -> None:
    """C3: exit 2 + stderr => identical outcome to a deny-with-reason."""

    body = "import sys\nsys.stderr.write('blocked by scanner')\nsys.exit(2)\n"
    disp = make_command_dispatcher(tmp_path, event=PRE_TOOL_USE, body=body)
    outcome = disp.dispatch(PRE_TOOL_USE, _pre_tool_env())
    assert outcome.denied
    assert outcome.reason == "blocked by scanner"


def test_banner_tolerant_stdout_parse(tmp_path: Path) -> None:
    """C8: a shell-profile banner before the JSON must not break the parse."""

    body = (
        "import json, sys\n"
        "print('WELCOME to the corporate shell v3')\n"
        'print(json.dumps({"decision": "deny", "reason": "banner-safe"}))\n'
    )
    disp = make_command_dispatcher(tmp_path, event=PRE_TOOL_USE, body=body)
    outcome = disp.dispatch(PRE_TOOL_USE, _pre_tool_env())
    assert outcome.denied
    assert outcome.reason == "banner-safe"


def test_exit0_nonjson_stdout_is_allow_with_typed_reason(tmp_path: Path) -> None:
    """C7: exit 0 but non-JSON stdout on a tool event => allow + a diagnosable reason,
    never a silent fail-open treating the text as a control."""

    body = "print('just some noise, no json here')\n"
    disp = make_command_dispatcher(
        tmp_path, event=PRE_TOOL_USE, body=body, hook_id="noisy"
    )
    outcome = disp.dispatch(PRE_TOOL_USE, _pre_tool_env())
    assert outcome.decision == "allow"
    reasons = [r for r in hook_reasons() if r["reason"] == "hook_unparseable_stdout"]
    assert any(r.get("hook_id") == "noisy" for r in reasons)


def test_additional_context_concatenated(tmp_path: Path) -> None:
    """additionalContext from a hook is carried on the outcome."""

    body = (
        "import json\n"
        'print(json.dumps({"decision": "allow", "additionalContext": "remember X"}))\n'
    )
    disp = make_command_dispatcher(tmp_path, event=PRE_TOOL_USE, body=body)
    outcome = disp.dispatch(PRE_TOOL_USE, _pre_tool_env())
    assert outcome.additional_context == "remember X"


# --------------------------------------------------------------------------- #
# Failure posture (R1/C6) — hook failure != user rejection                       #
# --------------------------------------------------------------------------- #


def test_other_exit_nonblocking_when_not_fail_closed(tmp_path: Path) -> None:
    """C6: a non-0/2 exit is a non-blocking error; the tool proceeds when not failClosed."""

    disp = make_command_dispatcher(
        tmp_path, event=PRE_TOOL_USE, body="import sys\nsys.exit(3)\n", fail_closed=False
    )
    outcome = disp.dispatch(PRE_TOOL_USE, _pre_tool_env())
    assert not outcome.denied


def test_other_exit_denies_when_fail_closed(tmp_path: Path) -> None:
    """A non-0/2 exit on a deny-capable failClosed hook denies — with a message that
    says it is a hook failure, NOT a user rejection (invariant 2)."""

    disp = make_command_dispatcher(
        tmp_path, event=PRE_TOOL_USE, body="import sys\nsys.exit(3)\n", fail_closed=True
    )
    outcome = disp.dispatch(PRE_TOOL_USE, _pre_tool_env())
    assert outcome.denied
    assert "not a user rejection" in outcome.reason
    assert "hook_crashed" in outcome.reason


def test_timeout_typed_reason_not_user_rejection(tmp_path: Path) -> None:
    """R1: a hung hook is killed and reported as a typed hook_timeout — never a
    user rejection. failClosed decides the block."""

    body = "import time\ntime.sleep(30)\n"
    disp = make_command_dispatcher(
        tmp_path, event=PRE_TOOL_USE, body=body, fail_closed=True, timeout_ms=200,
        hook_id="slowpoke",
    )
    start = time.monotonic()
    outcome = disp.dispatch(PRE_TOOL_USE, _pre_tool_env())
    assert time.monotonic() - start < 10.0
    assert outcome.denied
    assert "hook_timeout" in outcome.reason
    assert "not a user rejection" in outcome.reason
    reasons = [r for r in hook_reasons() if r["reason"] == "hook_timeout"]
    assert any(r.get("hook_id") == "slowpoke" for r in reasons)


def test_timeout_nonblocking_when_not_fail_closed(tmp_path: Path) -> None:
    """A hung non-failClosed hook is non-blocking (proceeds), still typed."""

    disp = make_command_dispatcher(
        tmp_path, event=PRE_TOOL_USE, body="import time\ntime.sleep(30)\n",
        fail_closed=False, timeout_ms=200,
    )
    outcome = disp.dispatch(PRE_TOOL_USE, _pre_tool_env())
    assert not outcome.denied


# --------------------------------------------------------------------------- #
# POSIX resource leak (FIX 5) — a timed-out hook's process GROUP is killed,       #
# not just its direct PID, so a forking hook cannot leave an orphan running.      #
# --------------------------------------------------------------------------- #


def test_timeout_kills_child_process_not_just_the_hook(tmp_path: Path) -> None:
    """A hook that forks a long-lived child must not leak that child past a
    timeout kill. On POSIX this asserts the child is actually dead — proving the
    process-GROUP kill (``os.killpg``), not merely the direct-child kill. On
    Windows there is no POSIX process-group semantics from this spawn, so only
    the cross-platform outcome (typed timeout, denied, bounded wall-clock) is
    asserted — the killpg-specific assertion is POSIX-only and skipped there."""

    marker = tmp_path / "child_pid.txt"
    body = (
        "import subprocess, sys, time\n"
        f"child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        f"open({str(marker)!r}, 'w', encoding='utf-8').write(str(child.pid))\n"
        "time.sleep(30)\n"
    )
    disp = make_command_dispatcher(
        tmp_path, event=PRE_TOOL_USE, body=body, fail_closed=True, timeout_ms=500,
        hook_id="forker",
    )
    start = time.monotonic()
    outcome = disp.dispatch(PRE_TOOL_USE, _pre_tool_env())
    elapsed = time.monotonic() - start
    assert elapsed < 15.0
    assert outcome.denied
    assert "hook_timeout" in outcome.reason

    for _ in range(50):
        if marker.exists():
            break
        time.sleep(0.1)
    assert marker.exists(), "hook never reached the point of spawning its child"
    child_pid = int(marker.read_text(encoding="utf-8").strip())

    if sys.platform == "win32":
        # Lighter assertion: no POSIX process-group semantics to prove here; the
        # typed-timeout + denied + bounded-wall-clock contract above already covers
        # the cross-platform behavior (the taskkill /T path is exercised, not asserted).
        return

    # The process-GROUP kill (os.killpg) must have reaped the forked child too,
    # not just the hook's own PID — poll briefly since the kill is asynchronous.
    child_alive = True
    for _ in range(50):
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            child_alive = False
            break
        time.sleep(0.1)
    assert not child_alive, "timeout kill leaked the hook's forked child process"


# --------------------------------------------------------------------------- #
# Wire annotation projection (FIX 1) — destructive reads the real destructiveHint #
# --------------------------------------------------------------------------- #


def test_wire_annotations_bounded_nondestructive_write_reports_destructive_false() -> None:
    """A well-formed, POSITIVELY-declared bounded write (readOnlyHint: false,
    destructiveHint: false, openWorldHint: false) must wire as destructive: false
    — not the max-restrictive default derived by inverting readOnly — so a hook
    matcher can actually distinguish it from an unclassified/destructive tool."""

    annotations = {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False}
    assert wire_annotations(annotations) == {
        "readOnly": False,
        "destructive": False,
        "openWorld": False,
    }


def test_wire_annotations_absent_or_malformed_defaults_destructive_true() -> None:
    """Fail-safe (unchanged by the fix): no annotation block, an empty one, or a
    malformed (non-bool) ``destructiveHint`` all still report destructive: true."""

    assert wire_annotations(None) == {"readOnly": False, "destructive": True, "openWorld": True}
    assert wire_annotations({}) == {"readOnly": False, "destructive": True, "openWorld": True}
    assert wire_annotations({"destructiveHint": "false"}) == {
        "readOnly": False,
        "destructive": True,
        "openWorld": True,
    }


def test_bounded_nondestructive_write_hook_match_fires(tmp_path: Path) -> None:
    """The bug this fix closes: a hook matcher on ``annotations.destructive: false``
    must actually be able to match a bounded, positively-declared non-destructive
    write. Before the fix ``destructive`` was hard-derived as ``not readOnly``, so
    it was ALWAYS true for any non-read-only tool and this matcher could never fire."""

    body = "import json\nprint(json.dumps({'decision': 'deny', 'reason': 'bounded write seen'}))\n"
    disp = make_command_dispatcher(
        tmp_path, event=PRE_TOOL_USE, body=body, match={"annotations": {"destructive": False}}
    )
    bounded_write = HookEnvelope(
        hook_event_name=PRE_TOOL_USE,
        tool_name="fs_write_file",
        tool_input={},
        tool_annotations=wire_annotations(
            {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False}
        ),
    )
    assert disp.dispatch(PRE_TOOL_USE, bounded_write).denied
    # A genuinely destructive/unclassified tool (absent annotations -> fail-safe) must NOT match.
    destructive_tool = HookEnvelope(
        hook_event_name=PRE_TOOL_USE,
        tool_name="shell_bash",
        tool_input={},
        tool_annotations=wire_annotations(None),
    )
    assert not disp.dispatch(PRE_TOOL_USE, destructive_tool).denied


# --------------------------------------------------------------------------- #
# Matching (M1/M3) + merge (D4/D5/D6)                                            #
# --------------------------------------------------------------------------- #


def test_anchored_tool_regex_does_not_overmatch(tmp_path: Path) -> None:
    """M1: matcher ``Edit`` is anchored — it must NOT match ``NotebookEdit``."""

    body = "import json\nprint(json.dumps({'decision': 'deny', 'reason': 'nope'}))\n"
    disp = make_command_dispatcher(
        tmp_path, event=PRE_TOOL_USE, body=body, match={"tool": "Edit"}
    )
    # NotebookEdit must NOT be gated by the anchored ``^Edit$`` matcher.
    out_notebook = disp.dispatch(PRE_TOOL_USE, _pre_tool_env(name="NotebookEdit"))
    assert not out_notebook.denied
    # The exact tool name matches.
    out_edit = disp.dispatch(PRE_TOOL_USE, _pre_tool_env(name="Edit"))
    assert out_edit.denied


def test_annotation_match(tmp_path: Path) -> None:
    """M3/M4: an annotation matcher fires on a tool declaring that capability."""

    body = "import json\nprint(json.dumps({'decision': 'deny', 'reason': 'destructive!'}))\n"
    disp = make_command_dispatcher(
        tmp_path, event=PRE_TOOL_USE, body=body, match={"annotations": {"destructive": True}}
    )
    # destructive tool -> matches -> denied.
    assert disp.dispatch(PRE_TOOL_USE, _pre_tool_env()).denied
    # read-only tool -> annotation mismatch -> not gated.
    ro = HookEnvelope(
        hook_event_name=PRE_TOOL_USE,
        tool_name="fs_read_file",
        tool_input={},
        tool_annotations={"readOnly": True, "destructive": False, "openWorld": False},
    )
    assert not disp.dispatch(PRE_TOOL_USE, ro).denied


def test_args_pattern_match(tmp_path: Path) -> None:
    """An argsPattern matcher scans the serialized tool_input."""

    body = "import json\nprint(json.dumps({'decision': 'deny', 'reason': 'secret file'}))\n"
    script = write_hook_script(tmp_path, "h.py", body)
    disp = dispatcher_from_rows(
        [
            {
                "id": "secrets",
                "on": [PRE_TOOL_USE],
                "match": {"argsPattern": r"\.env"},
                "run": command_run(script),
            }
        ]
    )
    assert disp.dispatch(PRE_TOOL_USE, _pre_tool_env(args={"path": "/repo/.env"})).denied
    assert not disp.dispatch(PRE_TOOL_USE, _pre_tool_env(args={"path": "/repo/main.py"})).denied


def test_merge_most_restrictive_wins() -> None:
    """D4/D6: allow + deny => deny; every additionalContext concatenated."""

    decisions = [
        HookDecision(decision="allow", additional_context="a", hook_id="h1"),
        HookDecision(decision="deny", reason="blocked", additional_context="b", hook_id="h2"),
    ]
    outcome = HookOutcome.merge(decisions, records=[])
    assert outcome.decision == "deny"
    assert outcome.reason == "blocked"
    assert outcome.additional_context == "a\nb"


def test_two_hooks_both_run_records_keyed_by_stable_id(tmp_path: Path) -> None:
    """D4 + identity: with two matching hooks BOTH run and each provenance record is
    keyed by its stable id (never positional)."""

    allow_body = "import json\nprint(json.dumps({'decision': 'allow'}))\n"
    deny_body = "import json\nprint(json.dumps({'decision': 'deny', 'reason': 'x'}))\n"
    allow_script = write_hook_script(tmp_path, "allow.py", allow_body)
    deny_script = write_hook_script(tmp_path, "deny.py", deny_body)
    disp = dispatcher_from_rows(
        [
            {"id": "permit", "on": [PRE_TOOL_USE], "run": command_run(allow_script)},
            {"id": "forbid", "on": [PRE_TOOL_USE], "run": command_run(deny_script)},
        ]
    )
    outcome = disp.dispatch(PRE_TOOL_USE, _pre_tool_env())
    assert outcome.denied
    ids = {r["hook_id"] for r in outcome.records}
    assert ids == {"permit", "forbid"}


# --------------------------------------------------------------------------- #
# Config validation (M5/M6, stable id)                                           #
# --------------------------------------------------------------------------- #


def test_missing_id_is_rejected() -> None:
    with pytest.raises(HookConfigError):
        _parse_entry({"on": [PRE_TOOL_USE], "run": {"type": "command", "command": "x"}}, source="t")


def test_invalid_regex_skips_only_that_hook(tmp_path: Path) -> None:
    """M5: a malformed matcher regex drops only its hook, naming the id; others load."""

    good = write_hook_script(tmp_path, "g.py", "import sys\nsys.exit(0)\n")
    entries = parse_hook_entries(
        [
            {"id": "bad", "on": [PRE_TOOL_USE], "match": {"tool": "([unclosed"},
             "run": {"type": "command", "command": "x"}},
            {"id": "good", "on": [PRE_TOOL_USE], "run": command_run(good)},
        ],
        source="t",
    )
    assert [e.id for e in entries] == ["good"]


def test_unknown_event_dropped_rest_loads(tmp_path: Path) -> None:
    """M6: an unknown event name is filtered; a hook with only unknown events is dropped
    but the rest of the file still loads."""

    good = write_hook_script(tmp_path, "g.py", "import sys\nsys.exit(0)\n")
    entries = parse_hook_entries(
        [
            {"id": "ghost", "on": ["NoSuchEvent"], "run": command_run(good)},
            {"id": "real", "on": ["NoSuchEvent", PRE_TOOL_USE], "run": command_run(good)},
        ],
        source="t",
    )
    ids = {e.id for e in entries}
    assert ids == {"real"}
    assert entries[0].on == frozenset({PRE_TOOL_USE})


# --------------------------------------------------------------------------- #
# Gate integration — reads never gated, deny reaches model, tighten-only         #
# --------------------------------------------------------------------------- #


def test_read_only_tool_never_gated_by_a_hook(tmp_path: Path) -> None:
    """Invariant: a provably read-only call fast-allows BEFORE any hook — a
    deny-everything PreToolUse hook cannot gate ``fs_read_file``."""

    body = "import json\nprint(json.dumps({'decision': 'deny', 'reason': 'no reads!'}))\n"
    install_global_dispatcher(make_command_dispatcher(tmp_path, event=PRE_TOOL_USE, body=body))
    try:
        app = build_app(sessions_path=tmp_path / "s.json")
        gate = _make_permission_gate(app)
        assert gate("fs_read_file", {"filepath": "x"}) == "allow"
    finally:
        install_global_dispatcher(None)


def test_pre_tool_hook_deny_reaches_model_via_gate(tmp_path: Path) -> None:
    """A PreToolUse hook deny blocks a non-read call and its reason rides a
    DenyDecision (the string the executor raises to the model)."""

    body = "import json\nprint(json.dumps({'decision': 'deny', 'reason': 'no hdf5 today'}))\n"
    install_global_dispatcher(
        make_command_dispatcher(tmp_path, event=PRE_TOOL_USE, body=body, hook_id="hdf5-guard")
    )
    try:
        app = build_app(sessions_path=tmp_path / "s.json")
        gate = _make_permission_gate(app)
        decision = gate("hdf5_write", {"path": "/tmp/x"})
        assert isinstance(decision, DenyDecision)
        assert decision == "deny"
        assert decision.deny_message == "no hdf5 today"
    finally:
        install_global_dispatcher(None)


def test_hook_allow_does_not_override_downstream_deny(tmp_path: Path) -> None:
    """Tighten-only: a PreToolUse hook allow never lifts the gate's own deny. An
    unclassified non-read tool with no session fails closed even when a hook allows."""

    body = "import json\nprint(json.dumps({'decision': 'allow'}))\n"
    install_global_dispatcher(make_command_dispatcher(tmp_path, event=PRE_TOOL_USE, body=body))
    try:
        app = build_app(sessions_path=tmp_path / "s.json")
        gate = _make_permission_gate(app)
        # No session -> the non-read tool fails closed; the hook allow must not bypass it.
        assert gate("hdf5_write", {"path": "/tmp/x"}) == "deny"
    finally:
        install_global_dispatcher(None)


def test_external_mcp_read_only_hint_skips_hook(tmp_path: Path) -> None:
    """An external MCP readOnlyHint tool fast-allows before the hook, too."""

    body = "import json\nprint(json.dumps({'decision': 'deny', 'reason': 'x'}))\n"
    install_global_dispatcher(make_command_dispatcher(tmp_path, event=PRE_TOOL_USE, body=body))
    try:
        app = build_app(sessions_path=tmp_path / "s.json")
        gate = _make_permission_gate(app)
        decision = gate(
            "remote.lookup",
            {"resource_id": "r1"},
            _external_mcp_permission_context(
                {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True}
            ),
        )
        assert decision == "allow"
    finally:
        install_global_dispatcher(None)


# --------------------------------------------------------------------------- #
# Ported-consumer regression — the old events still fire at their points         #
# --------------------------------------------------------------------------- #


def test_user_prompt_submit_hook_vetoes_turn(tmp_path: Path) -> None:
    """UserPromptSubmit (ported pre_message): a hook deny vetoes the turn end-to-end
    (the session settles to error)."""

    body = (
        "import json, sys\n"
        "envelope = json.load(sys.stdin)\n"
        "if 'secret' in (envelope.get('prompt') or '').lower():\n"
        "    print(json.dumps({'decision': 'deny', 'reason': 'blocked by policy'}))\n"
        "sys.exit(0)\n"
    )
    install_global_dispatcher(
        make_command_dispatcher(tmp_path, event=USER_PROMPT_SUBMIT, body=body)
    )
    try:
        app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
        with TestClient(app) as c:
            sid = c.post("/v1/sessions", json={"title": "t"}).json()["id"]
            ack = c.post(
                f"/v1/sessions/{sid}/messages",
                json={"parts": [{"type": "text", "text": "tell me a secret"}]},
            )
            assert ack.status_code == 200
            for _ in range(60):
                sess = c.get(f"/v1/sessions/{sid}").json()
                if sess["status"] == "error":
                    break
                time.sleep(0.05)
            assert sess["status"] == "error"
    finally:
        install_global_dispatcher(None)


def test_stop_hook_runs_after_settle(tmp_path: Path) -> None:
    """Stop (ported post_message): the hook fires after the turn settles and can
    side-effect (write a marker)."""

    marker = tmp_path / "stop_fired.txt"
    body = (
        "import json, sys\n"
        "envelope = json.load(sys.stdin)\n"
        f"open({str(marker)!r}, 'w', encoding='utf-8').write(envelope['session_id'])\n"
    )
    install_global_dispatcher(make_command_dispatcher(tmp_path, event="Stop", body=body))
    try:
        app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
        with TestClient(app) as c:
            from .conftest import complete_turn

            sid = c.post("/v1/sessions", json={"title": "t"}).json()["id"]
            complete_turn(c, sid, "hello")
            assert marker.exists()
            assert marker.read_text(encoding="utf-8") == sid
    finally:
        install_global_dispatcher(None)


# --------------------------------------------------------------------------- #
# Baseline — capabilities served from the new dispatcher                         #
# --------------------------------------------------------------------------- #


def test_capabilities_report_new_dispatcher(tmp_path: Path) -> None:
    """/v1/capabilities x_clio_hook_* are served from the new dispatcher metadata."""

    body = "import sys\nsys.exit(0)\n"
    install_global_dispatcher(
        make_command_dispatcher(tmp_path, event=PRE_TOOL_USE, body=body, hook_id="probe")
    )
    try:
        app = build_app(sessions_path=tmp_path / "s.json")
        with TestClient(app) as c:
            caps = c.get("/v1/capabilities").json()["capabilities"]
            assert caps["hooks"] is True
            assert caps["x_clio_hook_backend"] == "declarative"
            assert caps["x_clio_hook_events"][PRE_TOOL_USE] == 1
            assert caps["x_clio_hook_events"][SEMANTIC_EVENT] == 0
    finally:
        install_global_dispatcher(None)


def test_no_dispatcher_installed_is_no_op() -> None:
    """A dispatcher with no entries never blocks and reports zero handlers."""

    disp = HookDispatcher([])
    assert not disp.dispatch(PRE_TOOL_USE, _pre_tool_env()).denied
    assert disp.metadata()["backend"] == "declarative"
    assert disp.metadata()["handler_counts"][PRE_TOOL_USE] == 0


def test_disabled_hook_excluded_from_metadata(tmp_path: Path) -> None:
    """FIX 4: ``metadata()``'s handler_counts/hook_count/hook_ids describe what
    ACTUALLY RUNS — mirroring :meth:`HookDispatcher.matching`'s ``entry.enabled``
    filter — so a disabled hook must not inflate the reported capability."""

    good = write_hook_script(tmp_path, "g.py", "import sys\nsys.exit(0)\n")
    disp = dispatcher_from_rows(
        [
            {"id": "on", "on": [PRE_TOOL_USE], "run": command_run(good)},
            {"id": "off", "on": [PRE_TOOL_USE], "run": command_run(good), "enabled": False},
        ]
    )
    meta = disp.metadata()
    assert meta["hook_count"] == 1
    assert meta["handler_counts"][PRE_TOOL_USE] == 1
    assert meta["hook_ids"] == ["on"]
    # The disabled entry still exists in the dispatcher (config is preserved) but
    # never matches/dispatches — matching() and metadata() must agree on that.
    assert len(disp.entries) == 2
    assert disp.matching(PRE_TOOL_USE, _pre_tool_env()) == [
        e for e in disp.entries if e.id == "on"
    ]
