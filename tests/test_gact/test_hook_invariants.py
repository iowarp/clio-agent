"""P2.5 #1073 — consolidated hook-invariant matrix + bounded Stop self-loops.

Two halves:

* **The consolidated invariant audit** — the six hooks-research §5.5 invariants
  proven TOGETHER (they landed incrementally across P2.2–P2.4; this asserts they
  hold as one system, and pins the D5 two-modify gap this slice fixed): stable id
  (not positional), tighten-only (a hook ``allow`` never lifts a permission ``deny``
  — D1), reads-never-gated in every mode (L-series), two ``modify`` ⇒ error not
  writer-wins (D5), ``additionalContext`` concatenated (D6), most-restrictive-wins,
  hook failure ≠ user rejection (R1/R2), fail-closed for deny-capable hooks (and NOT
  fail-closed for the non-deny-capable Stop), exit-2-wins over stdout JSON (C4), and
  a pre-execution rejection still audits (L3).

* **The bounded Stop self-loop** (the one genuinely-new invariant): a Stop hook
  ``deny`` re-drives one more turn, hard-bounded by a per-hook ``loopLimit`` + a
  global cap, with ``stop_hook_active`` present from the 2nd firing, settling with a
  typed ``stop_loop_cap`` reason on a trip — never an infinite loop.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from clio_agent.gact.app import _make_permission_gate, _tool_session_context, build_app
from clio_agent.gact.hooks import (
    PRE_TOOL_USE,
    STOP,
    HookDecision,
    HookOutcome,
    hook_reasons,
    install_global_dispatcher,
    read_stop_loop_state,
    run_stop_hooks,
)
from clio_agent.gact.hooks import stop_loop as stop_loop_mod
from clio_agent.gact.hooks.stop_loop import evaluate_stop_loop
from clio_agent.gact.permission_gate import DenyDecision
from tests.test_gact._hook_fixtures import (
    command_run,
    dispatcher_from_rows,
    make_command_dispatcher,
    write_hook_script,
)

pytestmark = pytest.mark.usefixtures("host_agent_executor")


class _Agent:
    def forward(self, question: str, session_id: str = "default"):  # noqa: ANN201
        class _P:
            answer = "ok"
            selected_expert = ""
            routing_rationale = ""

        return _P()


# --------------------------------------------------------------------------- #
# INVARIANT 1 — stable id (identity is by id, never positional/order)           #
# --------------------------------------------------------------------------- #


def test_invariant_stable_id_survives_config_reorder(tmp_path: Path) -> None:
    """Reordering the config rows must not change which hooks run or the id-keyed
    provenance — identity is the stable ``id``, never the list position."""

    from clio_agent.gact.hooks import HookEnvelope

    deny = write_hook_script(
        tmp_path, "d.py", "import json\nprint(json.dumps({'decision':'deny','reason':'x'}))\n"
    )
    allow = write_hook_script(tmp_path, "a.py", "import json\nprint(json.dumps({'decision':'allow'}))\n")
    rows = [
        {"id": "alpha", "on": [PRE_TOOL_USE], "run": command_run(allow)},
        {"id": "omega", "on": [PRE_TOOL_USE], "run": command_run(deny)},
    ]
    env = HookEnvelope(
        hook_event_name=PRE_TOOL_USE,
        tool_name="hdf5_write",
        tool_input={"path": "/tmp/x"},
        tool_annotations={"readOnly": False, "destructive": True, "openWorld": False},
    )
    forward = dispatcher_from_rows(rows).dispatch(PRE_TOOL_USE, env)
    reverse = dispatcher_from_rows(list(reversed(rows))).dispatch(PRE_TOOL_USE, env)
    assert forward.denied and reverse.denied
    assert {r["hook_id"] for r in forward.records} == {"alpha", "omega"}
    assert {r["hook_id"] for r in forward.records} == {r["hook_id"] for r in reverse.records}
    # The deny is attributed to the same stable id regardless of order.
    fwd_deny = {r["hook_id"] for r in forward.records if r.get("decision") == "deny"}
    rev_deny = {r["hook_id"] for r in reverse.records if r.get("decision") == "deny"}
    assert fwd_deny == rev_deny == {"omega"}


# --------------------------------------------------------------------------- #
# INVARIANT 2 (D1) — a hook allow NEVER overrides a permission deny (tighten)    #
# --------------------------------------------------------------------------- #


def test_invariant_hook_allow_cannot_override_permission_deny(tmp_path: Path) -> None:
    """D1: a PreToolUse hook that ALLOWS cannot lift the gate's own deny. An
    unclassified non-read tool with no session fails closed even under a hook allow."""

    body = "import json\nprint(json.dumps({'decision':'allow'}))\n"
    install_global_dispatcher(make_command_dispatcher(tmp_path, event=PRE_TOOL_USE, body=body))
    try:
        app = build_app(sessions_path=tmp_path / "s.json")
        gate = _make_permission_gate(app)
        assert gate("hdf5_write", {"path": "/tmp/x"}) == "deny"
    finally:
        install_global_dispatcher(None)


# --------------------------------------------------------------------------- #
# INVARIANT 3 (L-series) — reads are NEVER gated, in ANY mode                    #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("mode", ["edit", "plan", "architect"])
def test_invariant_reads_never_gated_in_any_mode(tmp_path: Path, mode: str) -> None:
    """A provably read-only call fast-allows before ANY hook or mode lock — a
    deny-everything PreToolUse hook and plan/architect mode both cannot gate a read.
    The write tool in the SAME session confirms the lock is genuinely active (so the
    read allow is the read fast-path, not a disabled gate)."""

    from fastapi.testclient import TestClient

    body = "import json\nprint(json.dumps({'decision':'deny','reason':'no reads!'}))\n"
    install_global_dispatcher(make_command_dispatcher(tmp_path, event=PRE_TOOL_USE, body=body))
    try:
        app = build_app(sessions_path=tmp_path / "s.json")
        with TestClient(app) as c:
            sid = c.post("/v1/sessions", json={"title": "t", "mode": mode}).json()["id"]
            gate = _make_permission_gate(app)
            with _tool_session_context(sid):
                assert gate("fs_read_file", {"filepath": "x"}) == "allow"
                # A non-read write tool is gated (hook deny in edit; plan/architect lock).
                assert gate("hdf5_write", {"path": "/tmp/x"}) == "deny"
    finally:
        install_global_dispatcher(None)


# --------------------------------------------------------------------------- #
# INVARIANT 4 (D5) — two `modify` is an ERROR, not writer-wins (the fixed gap)   #
# --------------------------------------------------------------------------- #


def test_invariant_two_modify_is_error_not_writer_wins() -> None:
    """D5: two hooks both returning a tool-input ``modify`` must BLOCK the call (an
    error), never apply an arbitrary one — and record the typed conflict reason. This
    is the gap P2.5 fixed: the merge previously applied first-by-id."""

    baseline = len(hook_reasons())
    outcome = HookOutcome.merge(
        [
            HookDecision(decision="modify", modify_input={"path": "/a"}, hook_id="one"),
            HookDecision(decision="modify", modify_input={"path": "/b"}, hook_id="two"),
        ],
        records=[],
    )
    assert outcome.denied, "conflicting modifies must block, not apply an arbitrary one"
    assert not outcome.is_modify
    assert outcome.modify_input is None
    assert "one" in outcome.reason and "two" in outcome.reason
    new = hook_reasons()[baseline:]
    assert any(r["reason"] == "hook_conflicting_intercept" for r in new)


def test_single_modify_still_applies() -> None:
    """Regression guard for the D5 fix: a LONE modify still applies (only a CONFLICT
    blocks) — the fix must not break the ordinary single-modify intercept."""

    outcome = HookOutcome.merge(
        [HookDecision(decision="modify", modify_input={"path": "/safe"}, hook_id="solo")],
        records=[],
    )
    assert outcome.decision == "modify"
    assert outcome.is_modify
    assert outcome.modify_input == {"path": "/safe"}


# --------------------------------------------------------------------------- #
# INVARIANT 5 (D6) — additionalContext from ALL hooks concatenated; restrict-win #
# --------------------------------------------------------------------------- #


def test_invariant_additional_context_concatenated_and_most_restrictive_wins() -> None:
    """D6 + most-restrictive-wins together: allow+deny ⇒ deny, and every hook's
    additionalContext concatenates in stable order regardless of the winner."""

    outcome = HookOutcome.merge(
        [
            HookDecision(decision="allow", additional_context="ctx-a", hook_id="a"),
            HookDecision(decision="deny", reason="blocked", additional_context="ctx-b", hook_id="b"),
        ],
        records=[],
    )
    assert outcome.decision == "deny"
    assert outcome.reason == "blocked"
    assert outcome.additional_context == "ctx-a\nctx-b"


def test_most_restrictive_ordering_full_ladder() -> None:
    """deny > ask > synthesize > modify > allow — the full ladder in one shot."""

    ladder = [
        HookDecision(decision="allow", hook_id="al"),
        HookDecision(decision="modify", modify_input={"x": 1}, hook_id="mo"),
        HookDecision(decision="synthesize", synthesize_result={"y": 2}, hook_id="sy"),
        HookDecision(decision="ask", hook_id="as"),
        HookDecision(decision="deny", reason="no", hook_id="de"),
    ]
    assert HookOutcome.merge(ladder, records=[]).decision == "deny"
    assert HookOutcome.merge(ladder[:-1], records=[]).decision == "ask"
    assert HookOutcome.merge(ladder[:-2], records=[]).decision == "synthesize"
    assert HookOutcome.merge(ladder[:-3], records=[]).decision == "modify"
    assert HookOutcome.merge(ladder[:1], records=[]).decision == "allow"


# --------------------------------------------------------------------------- #
# INVARIANT (R1/R2) — hook failure is TYPED, never surfaced as a user rejection  #
# + fail-closed for deny-capable; and NOT fail-closed for the non-deny Stop.     #
# --------------------------------------------------------------------------- #


def test_invariant_crash_fail_closed_deny_is_not_user_rejection(tmp_path: Path) -> None:
    """R2: a crashing deny-capable failClosed hook DENIES, with a message that names
    the infra failure and explicitly says it is NOT a user rejection (invariant 2)."""

    disp = make_command_dispatcher(
        tmp_path, event=PRE_TOOL_USE, body="import sys\nsys.exit(3)\n", fail_closed=True
    )
    from clio_agent.gact.hooks import HookEnvelope

    outcome = disp.dispatch(
        PRE_TOOL_USE,
        HookEnvelope(hook_event_name=PRE_TOOL_USE, tool_name="hdf5_write", tool_input={}),
    )
    assert outcome.denied
    assert "hook_crashed" in outcome.reason
    assert "not a user rejection" in outcome.reason


def test_invariant_exit2_wins_over_valid_stdout_json(tmp_path: Path) -> None:
    """C4: a hook that prints a VALID allow JSON to stdout but exits 2 must BLOCK —
    the exit code wins over the stdout body (Claude Code shipped the inverse as a bug)."""

    body = (
        "import json, sys\n"
        "print(json.dumps({'decision': 'allow'}))\n"
        "sys.stderr.write('blocked despite the allow json')\n"
        "sys.exit(2)\n"
    )
    disp = make_command_dispatcher(tmp_path, event=PRE_TOOL_USE, body=body)
    from clio_agent.gact.hooks import HookEnvelope

    outcome = disp.dispatch(
        PRE_TOOL_USE,
        HookEnvelope(hook_event_name=PRE_TOOL_USE, tool_name="hdf5_write", tool_input={}),
    )
    assert outcome.denied
    assert outcome.reason == "blocked despite the allow json"


# --------------------------------------------------------------------------- #
# INVARIANT (L3) — a pre-execution rejection still emits an audit record          #
# --------------------------------------------------------------------------- #


def test_invariant_pre_execution_hook_deny_still_audits(tmp_path: Path) -> None:
    """L3: a PreToolUse hook deny does not merely raise to the model — it emits a
    resolved-permission audit row (status ``auto_denied``, reason ``hook_deny``) so
    the rejection is queryable, not only visible as a raised error."""

    body = "import json\nprint(json.dumps({'decision':'deny','reason':'blocked'}))\n"
    install_global_dispatcher(
        make_command_dispatcher(tmp_path, event=PRE_TOOL_USE, body=body, hook_id="guard")
    )
    try:
        app = build_app(sessions_path=tmp_path / "s.json")
        gate = _make_permission_gate(app)
        before = set(app.state.permissions)
        decision = gate("hdf5_write", {"path": "/tmp/x"})
        assert isinstance(decision, DenyDecision)
        rows = [r for pid, r in app.state.permissions.items() if pid not in before]
        assert len(rows) == 1
        assert rows[0]["status"] == "auto_denied"
        assert rows[0]["reason"] == "hook_deny"
    finally:
        install_global_dispatcher(None)


# --------------------------------------------------------------------------- #
# BOUNDED STOP SELF-LOOP — evaluate_stop_loop (pure) + run_stop_hooks (wired)     #
# --------------------------------------------------------------------------- #


def _deny_outcome(*hook_ids: str, reason: str = "not done") -> HookOutcome:
    """Build a merged Stop outcome as if ``hook_ids`` each returned deny (block)."""

    return HookOutcome.merge(
        [HookDecision(decision="deny", reason=reason, hook_id=h) for h in hook_ids],
        records=[{"hook_id": h, "event": STOP, "decision": "deny"} for h in hook_ids],
    )


def _allow_outcome() -> HookOutcome:
    return HookOutcome.merge(
        [HookDecision(decision="allow", hook_id="gate")],
        records=[{"hook_id": "gate", "event": STOP, "decision": "completed"}],
    )


def test_evaluate_no_block_resets_and_does_not_redrive() -> None:
    """A Stop dispatch with no block ⇒ the agent is done: reset counters, no re-drive."""

    result = evaluate_stop_loop(
        _allow_outcome(), {"count": 3, "per_hook": {"h": 3}}, loop_limits={}, cap=8
    )
    assert not result.redrive and not result.capped
    assert result.new_state == {"count": 0, "per_hook": {}}


def test_evaluate_block_redrives_and_increments() -> None:
    result = evaluate_stop_loop(
        _deny_outcome("gate"), {"count": 1, "per_hook": {"gate": 1}}, loop_limits={}, cap=8
    )
    assert result.redrive and not result.capped
    assert result.new_state == {"count": 2, "per_hook": {"gate": 2}}
    assert result.reason == "not done"


def test_evaluate_global_cap_trips_typed() -> None:
    """At the global cap the next block does NOT re-drive: capped (scope=global)."""

    result = evaluate_stop_loop(
        _deny_outcome("gate"), {"count": 8, "per_hook": {"gate": 8}}, loop_limits={}, cap=8
    )
    assert not result.redrive
    assert result.capped and result.cap_scope == "global"
    assert result.new_state == {"count": 0, "per_hook": {}}


def test_evaluate_per_hook_loop_limit_trips_typed() -> None:
    """When the sole blocking hook already hit its per-hook loopLimit, it is not
    eligible: bounded stop with scope=per_hook (global cap untouched)."""

    result = evaluate_stop_loop(
        _deny_outcome("gate"),
        {"count": 2, "per_hook": {"gate": 2}},
        loop_limits={"gate": 2},
        cap=8,
    )
    assert not result.redrive
    assert result.capped and result.cap_scope == "per_hook"


def test_evaluate_one_hook_exhausted_another_still_eligible() -> None:
    """Two blockers, one at its per-hook limit and one under it ⇒ still re-drive
    (only the eligible one's tally advances)."""

    result = evaluate_stop_loop(
        _deny_outcome("done", "fresh"),
        {"count": 3, "per_hook": {"done": 2}},
        loop_limits={"done": 2, "fresh": 5},
        cap=8,
    )
    assert result.redrive
    assert result.new_state["count"] == 4
    assert result.new_state["per_hook"]["fresh"] == 1
    assert result.new_state["per_hook"]["done"] == 2  # exhausted, not advanced


# ---- run_stop_hooks (wired: dispatch + persist + enqueue) ---- #


def _stop_dispatcher_always_deny(tmp_path: Path):  # noqa: ANN202
    body = "import sys\nsys.stderr.write('tests still failing')\nsys.exit(2)\n"
    return make_command_dispatcher(tmp_path, event=STOP, body=body, hook_id="test-gate")


def test_stop_loop_always_blocks_redrives_to_cap_then_settles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Stop hook that ALWAYS blocks re-drives up to the (lowered) cap, enqueuing one
    re-drive each time, then settles DONE with a typed ``stop_loop_cap`` reason — never
    an infinite loop."""

    monkeypatch.setattr(stop_loop_mod, "global_cap", lambda: 3)
    install_global_dispatcher(_stop_dispatcher_always_deny(tmp_path))
    try:
        app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
        sess = app.state.sessions.create(workspace_id="w", title="t")
        baseline = len(hook_reasons())
        redrives = 0
        capped_at = None
        for turn in range(10):
            result = run_stop_hooks(
                app, session_id=sess.id, turn_id=f"t{turn}", cwd="", payload={}
            )
            if result.redrive:
                redrives += 1
            if result.capped:
                capped_at = turn
                break
        assert redrives == 3, "re-drives are bounded by the global cap"
        assert capped_at == 3, "the 4th firing (count==cap) trips the cap"
        caps = [r for r in hook_reasons()[baseline:] if r["reason"] == "stop_loop_cap"]
        assert caps and caps[-1]["scope"] == "global"
        # Each re-drive enqueued exactly one loop-inbox event (the seam that re-drives).
        assert len(app.state.loop_inboxes[sess.id].drain()) == 3
        # Counters reset after the cap so a fresh sequence starts clean.
        assert read_stop_loop_state(app.state.sessions.get(sess.id))["count"] == 0
    finally:
        install_global_dispatcher(None)


def test_stop_loop_blocks_twice_then_allows_three_firings(tmp_path: Path) -> None:
    """A Stop hook that blocks twice then allows ⇒ 2 re-drives then settle (3 finalize
    firings total), and ``stop_hook_active`` is present from the 2nd firing onward."""

    counter = tmp_path / "n.txt"
    log = tmp_path / "active_log.txt"
    body = (
        "import json, sys, os\n"
        f"envelope = json.load(sys.stdin)\n"
        f"active = 'stop_hook_active' in (envelope.get('payload') or {{}})\n"
        f"open({str(log)!r}, 'a', encoding='utf-8').write(str(active) + '\\n')\n"
        f"n = 0\n"
        f"if os.path.exists({str(counter)!r}):\n"
        f"    n = int(open({str(counter)!r}, encoding='utf-8').read() or '0')\n"
        f"open({str(counter)!r}, 'w', encoding='utf-8').write(str(n + 1))\n"
        f"if n < 2:\n"
        f"    sys.stderr.write('not done')\n"
        f"    sys.exit(2)\n"
        f"sys.exit(0)\n"
    )
    install_global_dispatcher(
        make_command_dispatcher(tmp_path, event=STOP, body=body, hook_id="gate")
    )
    try:
        app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
        sess = app.state.sessions.create(workspace_id="w", title="t")
        results = [
            run_stop_hooks(app, session_id=sess.id, turn_id=f"t{i}", cwd="", payload={})
            for i in range(3)
        ]
        assert [r.redrive for r in results] == [True, True, False]
        assert not any(r.capped for r in results)
        # stop_hook_active: absent on firing 1, present from firing 2.
        lines = log.read_text(encoding="utf-8").splitlines()
        assert lines == ["False", "True", "True"]
        # Sequence completed cleanly on the allow: counters reset.
        assert read_stop_loop_state(app.state.sessions.get(sess.id))["count"] == 0
    finally:
        install_global_dispatcher(None)


def test_stop_loop_per_hook_loop_limit_bounds_a_single_hook(tmp_path: Path) -> None:
    """A Stop hook with ``loopLimit: 2`` re-drives exactly twice then settles with a
    per-hook cap trip, even though the global cap is far higher."""

    script = write_hook_script(
        tmp_path, "gate.py", "import sys\nsys.stderr.write('nope')\nsys.exit(2)\n"
    )
    disp = dispatcher_from_rows(
        [{"id": "limited", "on": [STOP], "run": command_run(script), "loopLimit": 2}]
    )
    install_global_dispatcher(disp)
    try:
        app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
        sess = app.state.sessions.create(workspace_id="w", title="t")
        # Drive the sequence to its natural end: each re-drive is one more turn; the
        # sequence stops the moment a firing does not re-drive (no turn 4 occurs).
        results = []
        for i in range(6):
            result = run_stop_hooks(
                app, session_id=sess.id, turn_id=f"t{i}", cwd="", payload={}
            )
            results.append(result)
            if not result.redrive:
                break
        assert [r.redrive for r in results] == [True, True, False]
        assert results[-1].capped and results[-1].cap_scope == "per_hook"
    finally:
        install_global_dispatcher(None)


def test_stop_loop_infra_failure_never_fail_closes_into_a_redrive(
    tmp_path: Path,
) -> None:
    """A Stop hook that CRASHES (infra failure) must NOT fail-closed into a re-drive:
    Stop is not deny-capable, so a crash is typed-recorded and the turn settles done.
    This proves the Stop re-drive can never be triggered by an infra failure."""

    baseline = len(hook_reasons())
    disp = make_command_dispatcher(
        tmp_path, event=STOP, body="import sys\nsys.exit(3)\n", fail_closed=True, hook_id="crasher"
    )
    install_global_dispatcher(disp)
    try:
        app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
        sess = app.state.sessions.create(workspace_id="w", title="t")
        result = run_stop_hooks(app, session_id=sess.id, turn_id="t0", cwd="", payload={})
        assert not result.redrive, "a Stop infra failure must never re-drive"
        assert not result.capped
        # The failure is typed-recorded (never silent), distinct from any deny.
        crashed = [r for r in hook_reasons()[baseline:] if r["reason"] == "hook_crashed"]
        assert any(r.get("hook_id") == "crasher" for r in crashed)
        # No re-drive was enqueued.
        assert not app.state.loop_inboxes.get(sess.id, _Empty()).peek_nonempty()
    finally:
        install_global_dispatcher(None)


class _Empty:
    def peek_nonempty(self) -> bool:  # pragma: no cover - trivial
        return False


def test_stop_loop_tolerates_missing_session(tmp_path: Path) -> None:
    """run_stop_hooks tolerates a vanished session (no metadata write, no crash): the
    always-deny hook still fires and the (stateless) decision is computed."""

    install_global_dispatcher(_stop_dispatcher_always_deny(tmp_path))
    try:
        app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
        # A session id that was never created — prior state reads as empty (count 0).
        result = run_stop_hooks(app, session_id="sess_gone", turn_id="t0", cwd="", payload={})
        assert result.redrive  # count 0 < cap, hook denies -> re-drive decision
        assert app.state.sessions.get("sess_gone") is None  # nothing persisted
    finally:
        install_global_dispatcher(None)
