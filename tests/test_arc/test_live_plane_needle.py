"""LIVE behavioral proof that ARC memory IS the context (real ALCF inference).

Needle-in-a-haystack: inject an UNGUESSABLE fact into ARC, ask the model — it
recalls the needle ONLY while ARC holds it, and genuinely cannot after ``delete``.
Because the needle is random, the model can only produce it by READING it from the
ARC-rendered trajectory; recall vanishing on delete proves the prompt is ARC.

Also exercises a REAL provider-driven auto-compaction (real prompt_tokens via the
token_counter fallback, real ALCF-generated summary). Gated by ``CLIO_RUN_LIVE=1``;
Argonne provider only (LM Studio left free).
"""

from __future__ import annotations

import os

import dspy
import pytest

import clio_agent.gact.app as app
from clio_agent.gact import context as ctx

from .conftest import live_plane_context, make_react_agent

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


def _probe(agent, lm, arc, question: str) -> str:
    """One model call whose trajectory is rendered FROM ARC (via the override)."""
    with live_plane_context(arc, session=SID, scope=SCOPE):
        with dspy.context(lm=lm, adapter=dspy.ChatAdapter()):
            r = agent._call_with_potential_trajectory_truncation(
                agent.extract, {}, question=question
            )
    return str(getattr(r, "answer", "") or "").strip()


@pytest.mark.parametrize(
    "needle",
    ["MAGNETO-7731-CITRINE", "QUASAR-5519-OBSIDIAN", "NEBULA-8823-VERIDIAN"],
)
def test_needle_arc_is_the_context(arc, needle):
    """Inject (insert) an unguessable needle → model finds it; delete → it cannot."""
    lm = _live_lm()
    agent = make_react_agent()
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
            SID, SCOPE, 1, "observation",
            {"text": f"CONFIDENTIAL: the vault override code is {needle}."},
        )

    present = _probe(agent, lm, arc, question)
    assert needle in present, f"model failed to recall needle while in ARC: {present!r}"

    arc.delete_segments(SID, SCOPE, [needle_seg.id])  # DELETION (recorded as arc.op delete)

    deleted = _probe(agent, lm, arc, question)
    assert needle not in deleted, f"model recalled a DELETED needle (ARC is not the context): {deleted!r}"


def test_real_auto_compaction_on_alcf(arc):
    """Provider-driven auto-compaction with a REAL ALCF-generated summary. Also
    guards that _last_prompt_tokens is non-zero on ALCF (which reports
    prompt_tokens:0) via the token_counter fallback."""
    lm = _live_lm()
    agent = make_react_agent()
    trajectory = [
        ("thought", {"text": "Investigating the incident timeline."}),
        ("tool_call", {"name": "search", "args": {"q": "incident"}}),
        ("observation", {"text": "At 02:14 UTC the primary node lost quorum; failover to "
                                 "node-B took 38s; 1,204 requests queued; no data loss; root "
                                 "cause: a stale lease on node-A."}),
        ("thought", {"text": "Now checking the remediation steps applied."}),
        ("observation", {"text": "Remediation: lease TTL lowered to 5s; quorum monitor alert "
                                 "added; node-A rebooted and rejoined at 02:31 UTC."}),
    ]
    with live_plane_context(arc, session=SID, scope=SCOPE):
        for i, (kind, content) in enumerate(trajectory):
            arc.append_segment(SID, SCOPE, kind, content, step=i // 3)
    assert len(arc.render_segments(SID, SCOPE)) == 5

    with live_plane_context(arc, session=SID, scope=SCOPE):
        with dspy.track_usage(), dspy.context(lm=lm, adapter=dspy.ChatAdapter()):
            # one real call populates the usage path; then check the token readback
            agent._call_with_potential_trajectory_truncation(
                agent.extract, {}, question="Briefly, what happened?"
            )
            real_pt = app._last_prompt_tokens()
            assert real_pt > 0, "ALCF prompt_tokens readback is 0 (token_counter fallback broken)"
            # window so the real prompt lands at ~90% — over the 0.85 default threshold
            ctx.set_react_context_window(int(real_pt / 0.90))
            agent._maybe_autocompact()

    after = arc.render_segments(SID, SCOPE)
    assert len(after) == 1 and after[0].kind == "summary", "did not collapse to one summary"
    assert len(after[0].content.get("text", "")) > 20, "summary is empty/placeholder, not a real LLM summary"
    # the originals survive (tombstoned) for replay
    tombstoned = [
        s for s in arc._segments.list_segments(SID, SCOPE, include_tombstoned=True)
        if s.status == "tombstoned"
    ]
    assert len(tombstoned) == 5
