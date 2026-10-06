"""LIVE behavioral proof that ARC memory IS the context (real ALCF inference).

Needle-in-a-haystack: inject an UNGUESSABLE fact into ARC, ask the model — it
recalls the needle ONLY while ARC holds it, and genuinely cannot after ``delete``.
Because the needle is random, the model can only produce it by READING it from the
ARC-folded context; recall vanishing on delete proves the prompt is ARC.

Also exercises a REAL provider-driven auto-compaction (real prompt_tokens via the
token_counter fallback, real ALCF-generated summary). Gated by ``CLIO_RUN_LIVE=1``;
Argonne provider only (LM Studio left free).
"""

from __future__ import annotations

import contextlib
import os
import types
from datetime import datetime, timezone
from typing import Any, Iterator

import dspy
import pytest
from dspy.lm15 import Message as LMMessage
from dspy.lm15 import Request

import clio_agent.gact.app as app
from clio_agent.gact import context as ctx
from clio_agent.gact.agents.clio_react import _call_lm
from clio_agent.gact.compaction import AutoCompactionGuard, maybe_autocompact
from clio_agent.gact.types import Message, Part, Tokens

from .conftest import live_plane_context, probe_live_context, response_text

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("CLIO_RUN_LIVE") != "1",
        reason="live ALCF run: set CLIO_RUN_LIVE=1 (+ Argonne auth + CLIO_LM_* env)",
    ),
]

SID, SCOPE = "needle-s1", "agentA"


def _live_lm():
    from clio_agent.config import create_lm, load_config_from_env  # noqa: PLC0415

    cfg = load_config_from_env()
    if str(getattr(cfg, "provider", "")) == "lmstudio":
        pytest.skip("live run must target Argonne/ALCF, not lmstudio (leave it free)")
    return create_lm(cfg)


def _probe(lm, arc, question: str) -> str:
    """One real model call whose context is folded FROM ARC."""
    return probe_live_context(lm, arc, question, session=SID, scope=SCOPE)


class _Sessions:
    """``app.state.sessions`` for compaction: ``.get`` + a no-op ``.update``."""

    def __init__(self, rows: dict[str, Any]) -> None:
        self._rows = rows

    def get(self, sid: str) -> Any:
        return self._rows.get(sid)

    def update(self, sid: str, **_kwargs: Any) -> None:
        pass


class _Bus:
    """``app.state.bus`` for compaction: records published events."""

    def __init__(self) -> None:
        self.published: list[Any] = []

    def publish(self, event: Any) -> None:
        self.published.append(event)


class _RealSummaryAgent:
    """``app.state.agent`` whose one LM seam makes a REAL model call."""

    def __init__(self, lm: Any) -> None:
        self._lm = lm

    def _run_chat_agent(self, question: str, _session_id: str) -> str:
        request = Request(model=self._lm.model, messages=(LMMessage.user(question),))
        return response_text(_call_lm(self._lm, request))


@contextlib.contextmanager
def _compaction_app(arc, lm, ledger_text: str) -> Iterator[None]:
    """The runtime + gact app state ``maybe_autocompact`` compacts through: a
    one-message ledger (the summary's source), a real-LM agent, a bus."""
    now = datetime.now(timezone.utc).isoformat()
    seed = Message(
        id="msg_seed",
        session_id=SID,
        role="user",
        created_at=now,
        updated_at=now,
        parts=[Part(id="part_seed", type="text", text=ledger_text)],
        tokens=Tokens(),
        stop_reason="end_turn",
    )
    fake_app = types.SimpleNamespace(
        state=types.SimpleNamespace(
            arc=arc,
            sessions=_Sessions({SID: types.SimpleNamespace(metadata={})}),
            messages={SID: [seed]},
            agent=_RealSummaryAgent(lm),
            bus=_Bus(),
            memory_events={},
        )
    )
    tokens = [
        ctx.set_app(fake_app),
        ctx.set_react_scope(SCOPE),
        ctx.set_react_session(SID),
        ctx.set_react_window(0),
    ]
    try:
        yield
    finally:
        for token in reversed(tokens):
            ctx.reset(token)


@pytest.mark.parametrize(
    "needle",
    ["MAGNETO-7731-CITRINE", "QUASAR-5519-OBSIDIAN", "NEBULA-8823-VERIDIAN"],
)
def test_needle_arc_is_the_context(arc, needle):
    """Inject (insert) an unguessable needle → model finds it; delete → it cannot."""
    lm = _live_lm()
    question = (
        "What is the vault override code? Reply with ONLY the code. "
        "If your trajectory does not contain it, reply exactly: ABSENT"
    )

    with live_plane_context(arc, session=SID, scope=SCOPE):
        arc.append_segment(SID, SCOPE, "thought", {"text": "Reviewing the case file."}, step=0)
        arc.append_segment(
            SID, SCOPE, "observation", {"text": "Case file opened; routine metadata only."}, step=0
        )
        needle_seg = arc.insert_segment(  # INJECTION via insert (recorded as arc.op insert)
            SID,
            SCOPE,
            1,
            "observation",
            {"text": f"CONFIDENTIAL: the vault override code is {needle}."},
        )

    present = _probe(lm, arc, question)
    assert needle in present, f"model failed to recall needle while in ARC: {present!r}"

    arc.delete_segments(SID, SCOPE, [needle_seg.id])  # DELETION (recorded as arc.op delete)

    deleted = _probe(lm, arc, question)
    assert needle not in deleted, (
        f"model recalled a DELETED needle (ARC is not the context): {deleted!r}"
    )


def test_real_auto_compaction_on_alcf(arc):
    """Provider-driven auto-compaction with a REAL ALCF-generated summary. Also
    guards that _last_prompt_tokens is non-zero on ALCF (which reports
    prompt_tokens:0) via the token_counter fallback."""
    lm = _live_lm()
    trajectory = [
        ("thought", {"text": "Investigating the incident timeline."}),
        ("tool_call", {"name": "search", "args": {"q": "incident"}}),
        (
            "observation",
            {
                "text": "At 02:14 UTC the primary node lost quorum; failover to "
                "node-B took 38s; 1,204 requests queued; no data loss; root "
                "cause: a stale lease on node-A."
            },
        ),
        ("thought", {"text": "Now checking the remediation steps applied."}),
        (
            "observation",
            {
                "text": "Remediation: lease TTL lowered to 5s; quorum monitor alert "
                "added; node-A rebooted and rejoined at 02:31 UTC."
            },
        ),
    ]
    with live_plane_context(arc, session=SID, scope=SCOPE):
        for i, (kind, content) in enumerate(trajectory):
            arc.append_segment(SID, SCOPE, kind, content, step=i // 3)
    assert len(arc.render_segments(SID, SCOPE)) == 5

    ledger = "\n".join(str(content.get("text", "")) for _kind, content in trajectory)
    with _compaction_app(arc, lm, ledger):
        with dspy.track_usage(), dspy.context(lm=lm):
            # one real call populates the usage path; then check the token readback
            probe_live_context(lm, arc, "Briefly, what happened?", session=SID, scope=SCOPE)
            real_pt = app._last_prompt_tokens()
            assert real_pt > 0, "ALCF prompt_tokens readback is 0 (token_counter fallback broken)"
            # window so the real prompt lands at ~90% — over the 0.85 default threshold
            ctx.set_react_context_window(int(real_pt / 0.90))
            maybe_autocompact(AutoCompactionGuard())

    after = arc.render_segments(SID, SCOPE)
    assert len(after) == 1 and after[0].kind == "summary", "did not collapse to one summary"
    assert len(after[0].content.get("text", "")) > 20, (
        "summary is empty/placeholder, not a real LLM summary"
    )
    # the originals survive (tombstoned) for replay
    tombstoned = [
        s
        for s in arc._segments.list_segments(SID, SCOPE, include_tombstoned=True)
        if s.status == "tombstoned"
    ]
    assert len(tombstoned) == 5
