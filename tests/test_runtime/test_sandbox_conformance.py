"""B6 (#980): the sandbox conformance sweep — the zero-untyped-degrade guarantee.

The campaign-done gate 4: every child-spawn seam, on every platform tier, carries a TYPED
mechanism + reason — no ``unknown``, no silent passthrough. Two test layers:

* UNIT (runs everywhere) — :func:`sweep_conformance` is pure over an INJECTED
  :class:`SandboxResult`, so the whole (seam × tier) matrix is pinned without a real fence:
  every tier yields six typed seams and zero untyped; an injected ``unknown`` / blank-reason
  seam is CAUGHT as untyped (the guarantee has teeth); the doctor row maps the verdicts.
* LIVE (``@pytest.mark.sandbox_conformance``) — asserts the guarantee against the backend
  actually resolved in this process. Skips cleanly when no server boot resolved a state; in
  the coordinated live gate (WSL Linux fence + the owner's Windows) it runs against the real
  ladder and asserts zero untyped degrades on the live tier.

Each key lock carries a sabotage note.
"""

from __future__ import annotations

import pytest

from clio_agent.runtime import sandbox as sb
from clio_agent.runtime import sandbox_conformance as sc
from clio_agent.runtime.status import IntegrationState

# Every mechanism the ladder can resolve to (the wrapped seams must be typed on each).
_ALL_TIERS = [
    sb.MECHANISM_CODEX,
    sb.MECHANISM_LANDLOCK,
    sb.MECHANISM_NONE,
]


def _state(mechanism: str, *, active: bool, reason: str) -> sb.SandboxResult:
    return sb.SandboxResult(mechanism=mechanism, active=active, reason=reason)


# --------------------------------------------------------------------------- #
# 1. The sweep is typed on every tier — the (seam × tier) matrix.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("mechanism", _ALL_TIERS)
def test_sweep_is_fully_typed_on_every_tier(mechanism: str) -> None:
    active = mechanism != sb.MECHANISM_NONE
    reason = sb.REASON_FENCE_ACTIVE if active else sb.REASON_NOT_INSTALLED
    report = sc.sweep_conformance(_state(mechanism, active=active, reason=reason))
    # Six seams: 3 wrapped + 3 excluded, all typed, zero untyped degrades.
    assert len(report.seams) == len(sc.WRAPPED_SEAMS) + len(sc.EXCLUDED_SEAMS)
    assert report.untyped == ()
    assert report.ok is True
    assert report.tier == mechanism
    # Sabotage: mark an excluded seam mechanism "unknown" in the module → untyped non-empty.
    for seam in report.seams:
        assert seam.typed is True
        assert seam.reason.strip() != ""
        assert seam.mechanism != sc.MECHANISM_UNKNOWN


@pytest.mark.parametrize("mechanism", _ALL_TIERS)
def test_wrapped_seams_inherit_the_tier_excluded_are_labeled(mechanism: str) -> None:
    active = mechanism != sb.MECHANISM_NONE
    report = sc.sweep_conformance(_state(mechanism, active=active, reason=sb.REASON_FENCE_ACTIVE))
    wrapped = {s.seam: s for s in report.seams if s.confinement == sc.CONFINEMENT_WRAPPED}
    excluded = {s.seam: s for s in report.seams if s.confinement == sc.CONFINEMENT_EXCLUDED}
    assert set(wrapped) == set(sc.WRAPPED_SEAMS)
    assert set(excluded) == set(sc.EXCLUDED_SEAMS)
    for s in wrapped.values():
        assert s.mechanism == mechanism  # governed by the resolved backend
        assert s.active is active
    for name, s in excluded.items():
        assert s.mechanism == sc.MECHANISM_EXCLUDED
        assert s.active is False
        assert s.reason == sc.EXCLUSION_REASONS[name]


# --------------------------------------------------------------------------- #
# 2. The guarantee has teeth — an untyped seam is CAUGHT.
# --------------------------------------------------------------------------- #


def test_unknown_mechanism_is_flagged_untyped() -> None:
    # A wrapped seam that resolved to an unknown mechanism is the campaign-forbidden state.
    report = sc.sweep_conformance(_state("unknown", active=False, reason="some_reason"))
    assert set(report.untyped) == set(sc.WRAPPED_SEAMS)
    assert report.ok is False


def test_blank_reason_is_flagged_untyped() -> None:
    # A resolved fence with NO typed reason is a silent degrade — caught.
    # Sabotage: make _is_typed_mechanism ignore a blank reason → ok True → red.
    report = sc.sweep_conformance(_state(sb.MECHANISM_LANDLOCK, active=True, reason="   "))
    assert set(report.untyped) == set(sc.WRAPPED_SEAMS)
    assert report.ok is False


def test_none_state_is_unresolved_not_untyped() -> None:
    report = sc.sweep_conformance(None)
    assert report.resolved is False
    assert report.ok is False
    # Not resolved is distinct from untyped: the seams still carry the typed floor reason.
    assert report.untyped == ()
    assert all(
        s.reason == sb.REASON_NOT_INSTALLED
        for s in report.seams
        if s.confinement == sc.CONFINEMENT_WRAPPED
    )


# --------------------------------------------------------------------------- #
# 3. The doctor row maps the verdicts.
# --------------------------------------------------------------------------- #


def test_probe_ready_when_fenced_and_typed() -> None:
    row = sc.probe_sandbox_conformance(
        state=_state(sb.MECHANISM_CODEX, active=True, reason=sb.REASON_FENCE_ACTIVE)
    )
    assert row.state is IntegrationState.READY
    assert "zero-untyped-degrade" in row.capabilities


def test_probe_ready_on_typed_floor() -> None:
    # The floor SATISFIES conformance (a typed `none` reason) — the *presence* of a fence is
    # the separate `sandbox` row's question. Sabotage: DEGRADE the floor here → red.
    row = sc.probe_sandbox_conformance(
        state=_state(sb.MECHANISM_NONE, active=False, reason=sb.REASON_NOT_INSTALLED)
    )
    assert row.state is IntegrationState.READY


def test_probe_degraded_on_untyped_degrade() -> None:
    row = sc.probe_sandbox_conformance(state=_state("unknown", active=False, reason=""))
    assert row.state is IntegrationState.DEGRADED
    assert row.fallback == "untyped-degrade-detected"


def test_probe_skipped_when_unresolved(monkeypatch) -> None:
    # Force the truly-unresolved path (an earlier test's install_sandbox() can leave a cached
    # module state, so `state=None` must be pinned to the no-boot case explicitly).
    monkeypatch.setattr(sb, "current_state", lambda: None)
    row = sc.probe_sandbox_conformance(state=None)
    assert row.state is IntegrationState.SKIPPED


def test_conformance_row_wired_into_doctor_collect(monkeypatch) -> None:
    # The row appears in the runtime doctor report (thin over the owner module).
    from clio_agent.runtime.status import RuntimeProbe

    checker = RuntimeProbe(env={})
    report = checker.collect(include_process_census=False)
    names = {r.name for r in report.integrations}
    assert "sandbox_conformance" in names


# --------------------------------------------------------------------------- #
# 4. LIVE campaign-done gate (needs a resolved backend; skips otherwise).
# --------------------------------------------------------------------------- #


@pytest.mark.sandbox_conformance
def test_live_zero_untyped_degrade_on_the_resolved_tier() -> None:
    """Gate 4 (live): the tier resolved in THIS process reports zero untyped degrades.

    Runs against the real backend when a server boot resolved one (the coordinated live gate);
    skips cleanly otherwise so the unit suite stays host-agnostic. Never asserts a *fence is
    active* (the floor is a legal tier) — only that every seam is TYPED.
    """
    state = sb.current_state()
    if state is None:
        pytest.skip("no confinement backend resolved in this process (no server boot)")
    report = sc.sweep_conformance(state)
    assert report.resolved is True
    assert report.untyped == (), f"untyped degrades on tier {report.tier}: {report.untyped}"
    assert report.ok is True
