"""Nanoagent spawn primitive for Tier-2 experts.

A nanoagent is a Tier-3 ephemeral DSPy ReAct invocation a Tier-2
expert kicks off in parallel for a sub-task. Each spawn produces a
``NanoagentResult`` the caller appends to its own
``Prediction.nanoagents_spawned`` list — the GACT layer (see
``app._run_turn_in_background``) materialises the spawns as child
sessions and publishes ``subagent.started/completed`` events.

Usage from a blueprint runtime helper::

    from clio_agent.runtime.nanoagent import spawn_many

    spawns = spawn_many(
        agent_factory=lambda: validator_module,
        items=[
            {"input": {"file": "a.h5"}, "agent_id": "data_validator"},
            {"input": {"file": "b.h5"}, "agent_id": "data_validator"},
        ],
    )
    pred.nanoagents_spawned = [s.to_wire() for s in spawns]

The actual parallel execution uses ``dspy.Parallel`` for I/O
overlap when the underlying experts make external calls.
"""

from __future__ import annotations

import contextvars
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


class _ContextBoundModule:
    """Run a wrapped DSPy module inside a captured ``contextvars.Context``.

    ``dspy.Parallel``'s ``ParallelExecutor`` forwards only DSPy's
    ``thread_local_overrides`` into its worker threads — it does NOT copy the
    caller's ``contextvars.Context``. So per-turn ContextVars (``active_app()``,
    the tool-runtime hooks, and ``_ACTIVE_TOOL_WORKSPACE_ROOT``) are dropped and
    nanoagent tool calls fall back to process-globals (a sibling app's hooks in
    a multi-app process — this is #735/#813). Wrapping each worker so it runs
    inside a context captured on the spawning thread restores that inheritance.

    Each pair gets its OWN captured context copy: ``dspy.Parallel`` runs the
    pairs concurrently, and a single shared context entered by two threads at
    once raises ``RuntimeError: cannot enter context: already entered``.
    Straggler resubmit (which would re-enter one pair's context) is disabled at
    the ``dspy.Parallel`` call site via ``timeout=0``.
    """

    __slots__ = ("_module", "_context")

    def __init__(self, module: Any, context: contextvars.Context) -> None:
        self._module = module
        self._context = context

    def __call__(self, **kwargs: Any) -> Any:
        return self._context.run(lambda: self._module(**kwargs))


@dataclass
class NanoagentResult:
    """One nanoagent invocation's result, in the wire shape the
    GACT layer's ``_run_turn_in_background`` consumes."""

    agent_id: str
    input: dict[str, Any]
    answer: str = ""
    duration_ms: float = 0.0
    tokens: dict[str, int] = field(default_factory=dict)
    cost_usd: float = 0.0
    error: str = ""
    tools_called: list[dict[str, Any]] = field(default_factory=list)

    def to_wire(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "agent_id": self.agent_id,
            "input": self.input,
            "answer": self.answer,
            "duration_ms": self.duration_ms,
            "cost_usd": self.cost_usd,
        }
        if self.tokens:
            out["tokens"] = self.tokens
        if self.error:
            out["error"] = self.error
        if self.tools_called:
            out["tools_called"] = self.tools_called
        return out


def spawn_one(
    agent_factory: Callable[[], Any],
    *,
    agent_id: str,
    input: dict[str, Any],
    question_field: str = "question",
) -> NanoagentResult:
    """Run a single nanoagent synchronously and capture its result.

    ``agent_factory`` builds a fresh DSPy module per invocation so
    the Tier-3 spawn doesn't share state with its peers (or its
    parent). ``input`` is rendered into a question string by
    interpolating ``input`` values into the agent's expected
    field; for stricter agents we just pass kwargs.
    """

    t0 = time.time()
    agent = agent_factory()
    try:
        result = agent(**{question_field: _render_input(input)})
    except Exception as exc:  # noqa: BLE001
        return NanoagentResult(
            agent_id=agent_id,
            input=input,
            duration_ms=(time.time() - t0) * 1000,
            error=repr(exc),
        )
    answer = getattr(result, "answer", None) or getattr(result, "analysis", None) or str(result)
    return NanoagentResult(
        agent_id=agent_id,
        input=input,
        answer=answer,
        duration_ms=(time.time() - t0) * 1000,
    )


def spawn_many(
    agent_factory: Callable[[], Any],
    *,
    items: list[dict[str, Any]],
    question_field: str = "question",
    num_threads: int = 4,
) -> list[NanoagentResult]:
    """Run N nanoagent invocations in parallel via dspy.Parallel.

    Each item must carry at least ``input`` (passed to the agent)
    and ``agent_id`` (label used by the GACT layer).

    Returns the results in the same order as ``items``.
    """

    if not items:
        return []

    try:
        import dspy
    except Exception:  # pragma: no cover - dspy not present
        return [
            spawn_one(
                agent_factory,
                agent_id=item.get("agent_id", "nanoagent"),
                input=item.get("input", {}),
                question_field=question_field,
            )
            for item in items
        ]

    # Build (module, kwargs-dict) pairs. Each pair gets a fresh agent so they
    # don't share DSPy state, and its OWN captured ``contextvars.Context`` so
    # the spawning thread's per-turn state (active_app / tool-runtime hooks /
    # workspace root) reaches the worker — dspy.Parallel drops contextvars,
    # copying only ``thread_local_overrides`` (#735/#813, see
    # ``_ContextBoundModule``). Per-pair copies avoid concurrent re-entry of a
    # shared context; ``timeout=0`` below disables straggler resubmit (we do not
    # want hung nanoagents resubmitted, and a resubmit would re-enter a pair's
    # context concurrently).
    pairs = []
    for item in items:
        agent = agent_factory()
        kwargs = {question_field: _render_input(item.get("input", {}))}
        captured = contextvars.copy_context()
        pairs.append((_ContextBoundModule(agent, captured), kwargs))

    parallel = dspy.Parallel(num_threads=num_threads, timeout=0)
    raw_results = parallel(pairs)

    out: list[NanoagentResult] = []
    for item, result in zip(items, raw_results, strict=True):
        if result is None or isinstance(result, Exception):
            out.append(
                NanoagentResult(
                    agent_id=item.get("agent_id", "nanoagent"),
                    input=item.get("input", {}),
                    error=repr(result) if result else "no result",
                )
            )
            continue
        answer = getattr(result, "answer", None) or getattr(result, "analysis", None) or str(result)
        out.append(
            NanoagentResult(
                agent_id=item.get("agent_id", "nanoagent"),
                input=item.get("input", {}),
                answer=answer,
            )
        )
    return out


def _render_input(payload: dict[str, Any]) -> str:
    """Flatten an input dict into a one-line question string.

    The Tier-2 expert is responsible for building a richer prompt
    if needed; this is the default for spawns that just want
    "validate this file" semantics.
    """

    if not payload:
        return ""
    if "question" in payload:
        return str(payload["question"])
    return ", ".join(f"{k}={v}" for k, v in payload.items())
