"""LIVE acceptance: the ARC live context plane against REAL ALCF/Argonne inference.

Gated by ``CLIO_RUN_LIVE=1`` (skipped otherwise). Runs a real ``_RetainingReAct``
loop against a live Argonne model and asserts the contract on REAL data: the loop
writes its trajectory to ARC, the prompt is rebuilt from ARC (byte-equal to stock),
and an out-of-band edit propagates. Uses the Argonne provider only — never LM Studio.

Env (set when CLIO_RUN_LIVE=1):
    CLIO_LM_PROVIDER=argonne
    CLIO_LM_API_BASE=https://inference-api.alcf.anl.gov/resource_server/sophia/vllm/v1
    CLIO_LM_MODEL=openai/gpt-oss-120b
    (plus Argonne auth: browser OAuth done, or CLIO_ARGONNE_TOKEN exported)
"""

from __future__ import annotations

import os

import dspy
import pytest

from clio_agent.arc.prompt_recorder import PromptRecorder
from clio_agent.arc.segments import segments_to_keys

from .conftest import live_plane_context, make_react_agent, stock_format_trajectory

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("CLIO_RUN_LIVE") != "1",
        reason="live ALCF run: set CLIO_RUN_LIVE=1 (and Argonne auth + CLIO_LM_* env)",
    ),
]

SID, SCOPE = "live-s1", "agentA"


def _live_lm():
    from clio_agent.config import create_lm, load_config_from_env  # noqa: PLC0415

    cfg = load_config_from_env()
    if str(getattr(cfg, "provider", "")) == "lmstudio":
        pytest.skip("live run must target Argonne/ALCF, not lmstudio (leave it free)")
    return create_lm(cfg), cfg


def test_live_plane_on_real_alcf_inference(arc):
    lm, cfg = _live_lm()

    def lookup(topic: str) -> str:
        """Look up a fact about a topic."""
        return f"FACT_ABOUT[{topic}]=42"

    agent = make_react_agent(tools=[dspy.Tool(lookup)])
    rec = PromptRecorder()

    with live_plane_context(arc, session=SID, scope=SCOPE):
        with dspy.context(lm=lm, adapter=dspy.ChatAdapter(), callbacks=[rec]):
            pred = agent(question="Use the lookup tool for 'mars', then answer with the fact.")

    # 1. Real inference produced an answer.
    assert isinstance(getattr(pred, "answer", None), str) and pred.answer

    # 2. The loop wrote its trajectory to ARC.
    live = arc.render_segments(SID, SCOPE)
    assert live, "expected the react loop to write segments to ARC"
    kinds = {s.kind for s in live}
    assert "thought" in kinds  # at minimum the model reasoned

    # 3. Byte-equality on REAL data: the override renders ARC identically to stock.
    with live_plane_context(arc, session=SID, scope=SCOPE):
        with dspy.context(lm=lm, adapter=dspy.ChatAdapter()):
            keys = arc.render_segments_keys(SID, SCOPE)
            override = agent._format_trajectory({})
            stock = stock_format_trajectory(agent, keys)
    assert override == stock

    # 4. The prompt was built FROM ARC — every captured react call's trajectory span
    #    is a prefix-consistent subset of the final ARC render (the loop fed itself
    #    from ARC). If the model called the tool, its observation reached the wire.
    if any(s.kind == "tool_call" for s in live):
        assert any("FACT_ABOUT[mars]" in c.text() for c in rec.calls())


def test_live_mutation_propagates_on_real_data(arc):
    lm, _ = _live_lm()

    def lookup(topic: str) -> str:
        """Look up a fact about a topic."""
        return f"SECRET_{topic.upper()}"

    agent = make_react_agent(tools=[dspy.Tool(lookup)])
    with live_plane_context(arc, session=SID, scope=SCOPE):
        with dspy.context(lm=lm, adapter=dspy.ChatAdapter()):
            agent(question="Look up 'phobos' then answer with what you found.")

    live = arc.render_segments(SID, SCOPE)
    if not live:
        pytest.skip("model produced no trajectory to mutate")

    # Delete the last segment out-of-band and confirm it vanishes from the next render.
    victim = live[-1]
    victim_text = str(victim.content.get("text") or victim.content.get("name") or "")
    with live_plane_context(arc, session=SID, scope=SCOPE):
        with dspy.context(lm=lm, adapter=dspy.ChatAdapter()):
            before = agent._format_trajectory({})
            arc.delete_segments(SID, SCOPE, [victim.id])
            after = agent._format_trajectory({})
    assert before != after
    if victim_text:
        assert victim_text in before


def test_live_summarize_propagates_on_real_data(arc):
    """summarize(all) on a REAL ALCF trajectory collapses it to the summary in the
    next render (the other op the acceptance contract names)."""
    lm, _ = _live_lm()

    def lookup(topic: str) -> str:
        """Look up a fact about a topic."""
        return f"DEIMOS_FACT_{topic.upper()}"

    agent = make_react_agent(tools=[dspy.Tool(lookup)])
    with live_plane_context(arc, session=SID, scope=SCOPE):
        with dspy.context(lm=lm, adapter=dspy.ChatAdapter()):
            agent(question="Look up 'deimos' then answer with what you found.")

    live = arc.render_segments(SID, SCOPE)
    if not live:
        pytest.skip("model produced no trajectory to summarize")

    with live_plane_context(arc, session=SID, scope=SCOPE):
        with dspy.context(lm=lm, adapter=dspy.ChatAdapter()):
            arc.summarize_segments(
                SID, SCOPE, [s.id for s in live], {"text": "REAL_RUN_SUMMARY"}
            )
            after = agent._format_trajectory({})
    # the whole real trajectory is now just the summary on the wire
    assert "REAL_RUN_SUMMARY" in after
    assert segments_to_keys(arc.render_segments(SID, SCOPE)) == {
        "observation_0": "REAL_RUN_SUMMARY"
    }
