"""Regression tests for the signature-driven forward-compat shims (#772).

The old shims sniffed ``TypeError`` *messages* to decide whether an agent /
runner accepted new optional kwargs, retrying the call up to four times and
then re-calling with the legacy signature. That double-run corrupted any
callee whose body legitimately raised ``TypeError`` (it ran twice, once per
"attempt") and mis-attributed internal errors to signature mismatches.

These tests pin the new contract: inspect the signature BEFORE the call, build
the single kwargs dict the callee accepts, and call exactly ONCE. An internal
``TypeError`` must propagate on the first (only) invocation; legacy fakes must
still work. (The turn's own module run, ``turn_forward._run_module``, is pinned
to one call in ``test_streaming.py``.)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from clio_agent.gact.agent_invocation import _run_dynamic_agent_compat


@dataclass
class _Pred:
    answer: str = "ok"


def _modern_runner(
    base_agent: Any,
    dynamic_agent: Any,
    question: str,
    sid: str,
    cancel_requested: Any | None = None,
) -> _Pred:
    return _Pred(answer="modern-runner")


def test_run_dynamic_agent_compat_internal_typeerror_calls_once() -> None:
    """A runner whose body raises TypeError must not be re-invoked."""

    calls = {"n": 0}

    def runner(
        base_agent: Any,
        dynamic_agent: Any,
        question: str,
        sid: str,
        cancel_requested: Any | None = None,
    ) -> _Pred:
        calls["n"] += 1
        raise TypeError("positional argument boom")

    with pytest.raises(TypeError, match="positional argument boom"):
        _run_dynamic_agent_compat(runner, object(), object(), "q", "sid", lambda: False)
    assert calls["n"] == 1


def test_run_dynamic_agent_compat_modern_runner_receives_cancel() -> None:
    """A 5-arg runner receives the cancel callback."""

    seen: dict[str, Any] = {}

    def runner(
        base_agent: Any,
        dynamic_agent: Any,
        question: str,
        sid: str,
        cancel_requested: Any | None = None,
    ) -> _Pred:
        seen["cancel_requested"] = cancel_requested
        return _Pred(answer="modern-runner")

    cancel = lambda: True  # noqa: E731
    pred = _run_dynamic_agent_compat(runner, object(), object(), "q", "sid", cancel)
    assert pred.answer == "modern-runner"
    assert seen["cancel_requested"] is cancel


def test_run_dynamic_agent_compat_legacy_runner_drops_cancel() -> None:
    """A 4-arg legacy runner is called once without the cancel arg."""

    calls = {"n": 0}

    def legacy_runner(
        base_agent: Any,
        dynamic_agent: Any,
        question: str,
        sid: str,
    ) -> _Pred:
        calls["n"] += 1
        return _Pred(answer="legacy-runner")

    pred = _run_dynamic_agent_compat(legacy_runner, object(), object(), "q", "sid", lambda: False)
    assert pred.answer == "legacy-runner"
    assert calls["n"] == 1
