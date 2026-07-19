"""Shared turn-execution harness for the GACT test suite (#948 S4b).

Background
----------
Before S4b the GACT turn engine had a fall-through ``else`` branch: a
default/``main`` session with no resolvable Agent Blueprint ran
``app.state.agent.forward(question, session_id)`` directly — the legacy Tier-1
``ClioAgent`` planner. Dozens of turn-engine tests exploited that seam by handing
``build_app(agent=<fake with a .forward>)`` a canned-``Prediction`` fake and
asserting the turn produced that prediction.

S4b deleted the legacy planner and its ``else`` branch. Every default/``main``
session now resolves the default-registry Agent Blueprint's react ``main`` root
(the ONE blueprint branch) and runs it through
``_build_blueprint_dspy_module(app.state.agent, dynamic_agent)`` +
``_try_streamed_forward_compat`` / ``_run_blueprint_dspy_agent``. That module is a
real DSPy react program that would call an LM — which unit tests do not have.

The seam
--------
:func:`install_host_agent_executor` monkeypatches the ONE builder seam the turn
engine resolves at call time (``clio_agent.gact.app._build_blueprint_dspy_module``,
re-imported inside ``forward_turn`` and ``_run_blueprint_dspy_agent`` on every
turn) so the "built module" simply DELEGATES to the host agent's own
``forward(...)``. The fake host agent stays the executor, so a test still
expresses "the turn produces prediction X" by handing ``build_app`` a fake whose
``forward`` returns X — no per-test rewrite of the fake.

The delegating module is deliberately NOT a ``dspy.Module``: the streamed path
(``_try_streamed_forward``) rejects a non-module ``agent_override`` with the typed
``agent_not_streamable`` fallback and drops to the synchronous
``_run_blueprint_dspy_agent`` path, which rebuilds through the same seam and calls
the fake — matching the pre-S4b batch-delivery shape a plain ``.forward`` fake
always produced.

A host fake that is ITSELF a ``dspy.Module`` (the streaming tests' ``_DspyAgent``)
is returned unchanged, so the streamed path streamifies it exactly as the pre-S4b
``app.state.agent`` was — preserving the ``streamify``-monkeypatching fallback
scenarios (``stream_completed_without_chunks`` / ``stream_failed_before_output`` /
mid-stream failure).

Tests that instead monkeypatch ``clio_agent.gact.app._try_streamed_forward``
directly are unaffected: that patch short-circuits the streamed path AFTER the
delegating module is built but before it is consulted, and the delegating module
is never LM-bound, so its construction cannot fail the turn. (The genuine
"agent not configured" ingress paths return their structured 503 before any
module is built, so they are untouched.)
"""

from __future__ import annotations

import inspect
from typing import Any


class _HostAgentBlueprintModule:
    """A stand-in for the compiled blueprint DSPy module that delegates to the
    host agent's ``forward``.

    Intentionally NOT a ``dspy.Module`` so the streamed path classifies it as
    ``agent_not_streamable`` and falls through to the synchronous runner (see the
    module docstring).
    """

    def __init__(self, base_agent: Any, agent_def: Any) -> None:
        self._base_agent = base_agent
        self.agent_def = agent_def

    def __call__(
        self,
        question: str,
        session_id: str,
        cancel_requested: Any | None = None,
        **kwargs: Any,
    ) -> Any:
        return self.forward(
            question=question,
            session_id=session_id,
            cancel_requested=cancel_requested,
            **kwargs,
        )

    def forward(
        self,
        question: str,
        session_id: str,
        cancel_requested: Any | None = None,
        **kwargs: Any,
    ) -> Any:
        forward = self._base_agent.forward
        call_kwargs: dict[str, Any] = {"question": question, "session_id": session_id}
        optional = {
            "cancel_requested": cancel_requested,
            "session_mode": kwargs.get("session_mode", "chat"),
            "session_edit_mode": kwargs.get("session_edit_mode", "diff"),
        }
        try:
            params = inspect.signature(forward).parameters
        except (TypeError, ValueError):
            params = {}
        accepts_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
        for name, value in optional.items():
            if accepts_var_kw or name in params:
                call_kwargs[name] = value
        return forward(**call_kwargs)


def install_host_agent_executor(monkeypatch: Any) -> None:
    """Route the ONE blueprint-runtime build seam to the host agent's ``forward``.

    Monkeypatches ``clio_agent.gact.app._build_blueprint_dspy_module`` so a default
    session's react ``main`` executes the ``build_app(agent=...)`` host fake
    instead of compiling a real (LM-bound) DSPy react program. The delegating
    module is returned unconditionally so the real, LM-bound builder never runs
    under this fixture — tests that monkeypatch ``_try_streamed_forward`` (whose
    host ``agent`` is never actually invoked) therefore cannot fail on module
    construction, and tests that DO rely on the host fake get its ``forward``.
    """

    import dspy

    from clio_agent.gact import app as gact_app

    def _delegating_builder(base_agent: Any, agent_def: Any) -> Any:
        # A dspy.Module host is already a streamable executor: hand it straight to
        # the streamed path so streamify runs on it (streaming-fallback tests).
        if isinstance(base_agent, dspy.Module):
            return base_agent
        return _HostAgentBlueprintModule(base_agent, agent_def)

    monkeypatch.setattr(gact_app, "_build_blueprint_dspy_module", _delegating_builder)
