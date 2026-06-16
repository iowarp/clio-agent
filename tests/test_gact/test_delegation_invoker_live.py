"""LIVE detached-seam proof (epic #667, #671): a REAL ALCF child expert's
prediction crosses the loopback boundary and the parent recovers its answer.

Gated by ``CLIO_RUN_LIVE=1``. ALCF only (no local GPU). This is the end-to-end
validation that the transport-abstracted boundary survives real inference: the
child runs a real ALCF completion, its prediction is serialized to wire form, sent
across, and rebuilt on the parent side — exactly the path a cluster takes with
clio-core transport swapped in for the loopback (#659).
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import dspy
import pytest

from clio_agent.config import create_lm, load_config_from_env
from clio_agent.gact.delegation_invoker import run_child_via_boundary

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("CLIO_RUN_LIVE") != "1",
        reason="live ALCF run: set CLIO_RUN_LIVE=1 (and Argonne auth + CLIO_LM_* env)",
    ),
]


async def test_loopback_carries_a_real_alcf_child_answer():
    cfg = load_config_from_env()
    if str(getattr(cfg, "provider", "")) in {"lmstudio", "lm_studio"}:
        pytest.skip("live run must target Argonne/ALCF, not LM Studio (leave it free)")
    lm = create_lm(cfg)

    async def run_child(agent_def, prompt: str):
        # a REAL ALCF completion produces the child's prediction
        with dspy.context(lm=lm, adapter=dspy.ChatAdapter()):
            return dspy.Predict("question -> answer")(question=prompt)

    # in-process (parity) vs loopback (crosses the JSON wire) — same real child
    in_proc = await run_child_via_boundary(
        SimpleNamespace(id="data"),
        "What is 2+2? Answer with only the number.",
        run_child=run_child,
        mode="",
    )
    loop = await run_child_via_boundary(
        SimpleNamespace(id="data"),
        "What is 2+2? Answer with only the number.",
        run_child=run_child,
        session_id="live-s1",
        mode="loopback",
    )

    assert str(getattr(in_proc, "answer", "") or "").strip(), "in-process child gave no answer"
    answer = str(getattr(loop, "answer", "") or "")
    assert answer.strip(), "loopback child answer was lost crossing the wire"
    assert "4" in answer  # the real answer survived serialization round-trip
