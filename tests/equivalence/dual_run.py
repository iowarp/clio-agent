"""Live dual-run A/B scaffold (design §4.1.B — the write-path proof obligation).

A captured corpus was produced by the OLD writer and lacks any records a NEW writer
would emit, so it CANNOT validate a write-path change (C2). This scaffold closes that
gap: it drives the SAME scripted turn inputs through the real loop under two writer
configurations and diffs the four surfaces. For S0 the two configs are identical (the
DETERMINISM baseline) — the harness's job here is to answer the plan's open question
empirically: *what is irreducibly non-deterministic, and is a clean masked diff a
valid equivalence signal?* (See ``determinism_report``.)

Two real producers are exercised (both are production paths, not stubs):

* the real ARC ReAct loop (``_RetainingReAct`` via the test_arc live-plane helpers)
  writes the working-set segments → the **context** and **trace** surfaces;
* a real gact ``build_app`` turn driven through ``TestClient`` writes the SSE bus +
  the persisted ledger → the **SSE** and **persistence** surfaces.

The scripted inputs deliberately include an **exotic tool output** (a non-JSON-native
value — the caveat-a path happy-path captures never exercise) and an **injected tool
crash** (caveat-b, the pre-execute/error path), because those are exactly where a
write-path fold silently diverges.

Must be driven from within pytest: the autouse fixtures in ``tests/conftest.py`` set
the env (``CLIO_LM_MODEL``, the default registry blueprint, ``CLIO_ARC_STORE=local``)
a real gact turn needs to settle.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional  # noqa: F401

import dspy
from dspy.utils.dummies import DummyLM
from fastapi.testclient import TestClient

from clio_agent.arc.live import _MemoryStore
from clio_agent.arc.memory import ARCMemory

# The real loop helpers (test_arc / test_gact are packages).
from tests.test_arc.conftest import live_plane_context  # noqa: E402
from tests.test_gact.test_post_messages import FakeClioAgent  # noqa: E402

from . import normalizers as N

_SESSION = "sess_equiv"
_SCOPE = "agentA"

# An exotic, NON-JSON-native tool output (caveat a): a tuple + a single-element
# frozenset nested in a dict. Deliberately ORDER-STABLE (no multi-element set, whose
# str() order is hash-seeded) so ``str()`` coercion is deterministic within a process
# — the encoder must coerce it consistently or the fold diverges.
EXOTIC_OBSERVATION: Any = {"rows": ("a", 1, 2.5), "flag": frozenset({"only"}), "n": 2}


@dataclass
class WriterConfig:
    """One writer configuration (the A or B of the A/B run).

    ``working_set_fold`` selects the #737 S2 regime: ``False`` = the old parallel
    working-set write, ``True`` = the working set as a FOLD of the canonical ``_events``
    log; ``None`` uses the production default. Diffing ``False`` vs ``True`` is the
    S2 write-path proof (§4.1.B). ``label`` names the leg in reports.
    """

    label: str = "baseline"
    working_set_fold: Optional[bool] = None
    answer: str = "final answer from the equivalence turn"


@dataclass
class SurfaceCapture:
    """The four frozen surfaces captured from one scripted run."""

    sse: list[Any]  # bus Event objects
    persistence: list[dict[str, Any]]  # persisted message payloads
    context: list[Any]  # working-set segments
    trace_live: list[Any]  # log segments, live view
    trace_replay_final: list[Any]  # log segments, replayed as_of the final logical_time


@dataclass
class DualRunReport:
    """The four per-surface diff reports plus the determinism verdict text."""

    reports: dict[str, N.DiffReport] = field(default_factory=dict)
    masked_note: str = ""

    @property
    def all_empty(self) -> bool:
        return all(r.empty for r in self.reports.values())

    def pretty(self) -> str:
        lines = [r.pretty() for r in self.reports.values()]
        if self.masked_note:
            lines.append("MASKING: " + self.masked_note)
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# ARC ReAct loop capture (context + trace) — the real _RetainingReAct
# --------------------------------------------------------------------------- #


def _script_loop(use_v2: bool = True) -> DummyLM:
    """Scripted LM matching the active loop: one exotic-tool step, then finish.

    The ``probe`` tool returns the exotic observation (caveat a). V2 finishes via the
    internal ``submit`` tool (no ``extract`` step); classic finishes via ``finish``
    and a trailing ``extract`` response.
    """

    if use_v2:
        return DummyLM(
            [
                {
                    "next_thought": "probe with an exotic tool output",
                    "tool_calls": {"tool_calls": [{"name": "probe", "args": {}}]},
                },
                {
                    "next_thought": "submit now",
                    "tool_calls": {"tool_calls": [{"name": "submit", "args": {"answer": "FINAL"}}]},
                },
            ]
        )
    return DummyLM(
        [
            {
                "next_thought": "probe with an exotic tool output",
                "next_tool_name": "probe",
                "next_tool_args": "{}",
            },
            {"next_thought": "done", "next_tool_name": "finish", "next_tool_args": "{}"},
            {"reasoning": "because", "answer": "FINAL_ANSWER"},
        ]
    )


def _capture_arc_surfaces(config: WriterConfig, tmp_dir: Path) -> tuple[list[Any], list[Any], list[Any]]:
    """Drive the real ARC ReAct loop; return (context, trace_live, trace_replay_final)."""

    import clio_agent.gact.agents.runtime as runtime

    def probe() -> Any:
        """A tool returning an exotic, non-JSON-native observation (caveat a)."""
        return EXOTIC_OBSERVATION

    # V2 is the only expert loop since the v0.8.0 cleanup.
    arc = ARCMemory(
        data_dir=str(tmp_dir / "arc_loop"),
        store=_MemoryStore(),
        working_set_fold=config.working_set_fold,
    )
    react_cls = runtime._retaining_react_cls()
    agent = react_cls("question -> answer", tools=[dspy.Tool(probe, name="probe")])
    lm = _script_loop(use_v2=True)
    with live_plane_context(arc, session=_SESSION, scope=_SCOPE):
        with dspy.context(lm=lm, adapter=dspy.ChatAdapter()):
            try:
                agent(question="find alpha")
            except Exception:  # noqa: BLE001 — a loop error still leaves partial ARC state
                pass

    with live_plane_context(arc, session=_SESSION, scope=_SCOPE):
        context = list(arc.render_working_set(_SESSION, _SCOPE))
        trace_live = list(arc.render_segments(_SESSION, _SCOPE))
        times = sorted({s.logical_time for s in trace_live})
        final_t = times[-1] if times else None
        trace_replay_final = (
            list(arc.render_segments(_SESSION, _SCOPE, as_of=final_t))
            if final_t is not None
            else []
        )
    return context, trace_live, trace_replay_final


# --------------------------------------------------------------------------- #
# gact turn capture (SSE + persistence) — the real build_app turn
# --------------------------------------------------------------------------- #


def _capture_gact_surfaces(
    config: WriterConfig, tmp_dir: Path, *, crash: bool = False
) -> tuple[list[Any], list[dict[str, Any]]]:
    """Drive a real gact turn through TestClient; return (sse_events, persisted_rows).

    ``crash`` injects a turn-time failure (caveat b) so the SSE + persistence error
    envelopes are exercised. Must run under the pytest env fixtures.
    """

    from clio_agent.gact.app import build_app

    arc = ARCMemory(
        data_dir=str(tmp_dir / "arc_gact"),
        store=_MemoryStore(),
        working_set_fold=config.working_set_fold,
    )
    agent = FakeClioAgent(answer=config.answer, raise_on_forward=crash)
    app = build_app(sessions_path=tmp_dir / "sessions.json", agent=agent, arc=arc)
    with TestClient(app) as c:
        sid = c.post("/v1/sessions", json={"title": "equiv"}).json()["id"]
        ack = c.post(f"/v1/sessions/{sid}/messages", json={"parts": [{"type": "text", "text": "hi"}]})
        assert ack.status_code == 200, ack.text
        deadline = time.monotonic() + 10.0
        status = "running"
        while time.monotonic() < deadline:
            status = c.get(f"/v1/sessions/{sid}").json()["status"]
            if status != "running":
                break
            time.sleep(0.05)
        rows = c.get(f"/v1/sessions/{sid}/messages").json()["messages"]
        events = list(app.state.bus._history.get(sid, []))
    return events, rows


def capture_surfaces(config: WriterConfig, tmp_dir: Path, *, crash: bool = False) -> SurfaceCapture:
    """Capture all four frozen surfaces from one scripted run under ``config``."""

    context, trace_live, trace_replay_final = _capture_arc_surfaces(config, tmp_dir)
    sse, persistence = _capture_gact_surfaces(config, tmp_dir, crash=crash)
    return SurfaceCapture(
        sse=sse,
        persistence=persistence,
        context=context,
        trace_live=trace_live,
        trace_replay_final=trace_replay_final,
    )


# --------------------------------------------------------------------------- #
# The A/B diff + the determinism verdict
# --------------------------------------------------------------------------- #

#: The cross-run masks the determinism verdict finds necessary. SSE ids/clock and the
#: persistence envelope ids/timestamps are the ONLY irreducibly non-deterministic
#: fields across two independent turns (each turn mints a fresh session/turn/message/
#: part id); context/trace need NO masking (their projections carry neither ids nor
#: the clock).
_SSE_XRUN_EXTRA = frozenset(
    {
        "part_id",
        "call_id",
        "message_id",
        "session_id",
        "turn_id",
        # span/trace ids — minted per run; the error path (caveat b) carries extra
        # semantic-event span ids the happy path does not (an empirical finding).
        "span_id",
        "step_span_id",
        "expert_span_id",
        "run_span_id",
        "trace_id",
        "attempt_id",
        "question_id",
    }
)


def dual_run(
    config_a: WriterConfig, config_b: WriterConfig, tmp_a: Path, tmp_b: Path, *, crash: bool = False
) -> DualRunReport:
    """Run both configs on the same scripted inputs and diff the four surfaces.

    Uses the empirically-determined cross-run masks (ids + wall-clock) so a clean
    diff means *ordering + content + type-presence agree*, which is the equivalence
    signal. The report states exactly what was masked.
    """

    cap_a = capture_surfaces(config_a, tmp_a, crash=crash)
    cap_b = capture_surfaces(config_b, tmp_b, crash=crash)

    # SSE: mask the extra cross-run id fields on top of the standard non-normative set.
    global_sse_mask = N.SSE_MASK_FIELDS | _SSE_XRUN_EXTRA
    sse_ref = [
        {"type": r["type"], "payload": N._mask(r["payload"], global_sse_mask)}
        for r in N.bus_events_to_records(cap_a.sse)
        if N._sse_included(r["type"])
    ]
    sse_cand = [
        {"type": r["type"], "payload": N._mask(r["payload"], global_sse_mask)}
        for r in N.bus_events_to_records(cap_b.sse)
        if N._sse_included(r["type"])
    ]
    sse_report = N.DiffReport(
        "sse",
        (
            N.first_divergence(sse_ref, sse_cand)
            if N.sse_present_types(cap_a.sse) == N.sse_present_types(cap_b.sse)
            else N.Divergence(
                "event_type_presence",
                N.sse_present_types(cap_a.sse),
                N.sse_present_types(cap_b.sse),
                "drop_detection",
            )
        ),
        masked_fields=sorted(global_sse_mask),
    )

    reports = {
        "sse": sse_report,
        "persistence": N.diff_persistence(
            cap_a.persistence, cap_b.persistence, mask=N.PERSISTENCE_XRUN_MASK
        ),
        "context": N.diff_context(cap_a.context, cap_b.context),
        "trace": N.diff_trace(cap_a.trace_live, cap_b.trace_live),
    }
    note = (
        "SSE masked {" + ", ".join(sorted(global_sse_mask)) + "}; "
        "persistence masked {" + ", ".join(sorted(N.PERSISTENCE_XRUN_MASK)) + "}; "
        "context + trace masked NOTHING (their projections carry neither ids nor the "
        "log clock). A clean diff after only id/wall-clock masking IS a valid "
        "equivalence signal: ordering, content, and event-type presence are unmasked."
    )
    return DualRunReport(reports=reports, masked_note=note)
