"""BestOfN and Refine around the REAL ClioReAct on real clio-core (Phase 9).

The earlier variant tests wrapped a stub inner (``dspy.Predict`` over a dummy LM), so they
never saw that DSPy's Refine advice cannot reach ``ClioReAct``: it delivers advice as a
``hint_`` input through an adapter wrapper, keyed by named predictors, and the loop has
neither (each step is one ``lm(Request)``). These drive the real loop through one scripted
engine that records every request.
"""

from __future__ import annotations

import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import dspy
import pytest

from clio_agent.arc.memory import ARCMemory
from clio_agent.gact import context as ctx
from clio_agent.gact.agents import module_variants
from clio_agent.gact.agents.clio_react import ClioReAct
from clio_agent.gact.agents.clio_react_record import read_steps
from tests._scripted_engine import Reply, scripted_lm, wire

SESSION, SCOPE = "sess-variant", "writer"


@pytest.fixture
def arc(tmp_path: Path) -> Iterator[ARCMemory]:
    arc_memory = ARCMemory(data_dir=str(tmp_path / "arc"))
    app = types.SimpleNamespace(state=types.SimpleNamespace(arc=arc_memory))
    tokens = [ctx.set_app(app), ctx.set_react_scope(SCOPE), ctx.set_react_session(SESSION)]
    try:
        yield arc_memory
    finally:
        for token in reversed(tokens):
            ctx.reset(token)


def good_only(kwargs: dict, pred: Any) -> float:
    """Score 1.0 for an answer that says GOOD, else 0.0 (a real def: Refine getsource's it)."""
    return 1.0 if "GOOD" in str(getattr(pred, "answer", "")) else 0.0


def _wrap(cls: type, n: int = 2) -> Any:
    keyed = module_variants._RunKeyedModule(
        ClioReAct("question -> answer", tools=[]), agent_id=SCOPE, variant=cls.__name__
    )
    wrapped = cls(module=keyed, N=n, reward_fn=good_only, threshold=1.0)
    wrapped._clio_variant = "refine" if "Refine" in cls.__name__ else "best_of_n"
    wrapped._clio_agent_id = SCOPE
    return wrapped


def _texts(request: Any) -> list[str]:
    return [text for _role, parts in wire(request) for _kind, text, *_ in parts]


def test_best_of_n_returns_the_best_try_and_its_line_continues(arc: ARCMemory) -> None:
    lm, engine = scripted_lm([Reply(text="BAD draft"), Reply(text="GOOD draft")])
    with dspy.context(lm=lm):
        pred = _wrap(module_variants._RunScopedBestOfN)(question="write the email")

    assert pred.answer == "GOOD draft"
    assert pred.variant_selection["winning_index"] == 1
    base = read_steps(arc, SESSION, SCOPE)
    assert any("GOOD draft" in p.text for m in base for p in m.parts if hasattr(p, "text"))
    assert not any("BAD draft" in p.text for m in base for p in m.parts if hasattr(p, "text"))
    assert len(engine.requests) == 2


def test_refine_tells_the_next_try_the_advice(arc: ARCMemory) -> None:
    """Red before Phase 9: the second try's request carried no advice at all."""
    lm, engine = scripted_lm(
        [
            Reply(text="BAD draft"),
            Reply(text="Say it plainly and sign it GOOD."),  # the advice call
            Reply(text="GOOD draft"),
        ]
    )
    with dspy.context(lm=lm):
        pred = _wrap(module_variants._RunScopedRefine)(question="write the email")

    assert pred.answer == "GOOD draft"
    advice_request, second_try = engine.requests[1], engine.requests[2]
    assert any("BAD draft" in t for t in _texts(advice_request)), "advice reads try 0"
    assert "[clio: variant_advice]\nSay it plainly and sign it GOOD." in _texts(second_try)
    # the advice is recorded on the second try's own clio-core scope, as a CLIO addition
    second_scope = read_steps(arc, SESSION, f"{SCOPE}#run1")
    assert any("Say it plainly" in getattr(p, "text", "") for m in second_scope for p in m.parts)
