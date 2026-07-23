"""The sandbox conformance sweep (#980, B6) — the zero-untyped-degrade guarantee.

The campaign-done guarantee (gate 4): EVERY child-spawn seam, on EVERY platform tier
(srt bwrap/seatbelt/windows, Landlock, the honest floor), carries a TYPED mechanism + reason
— never an ``unknown``, never a silent passthrough. This owner module holds the sweep logic
(kept out of the ladder owner + the doctor-row sibling so both stay under their ratchets); the
doctor row (:func:`probe_sandbox_conformance`) and the live campaign-done suite are thin over
it.

Two seam classes (owner decision #974.5):

* **wrapped** — the three agent-driven spawn seams routed through
  :func:`clio_agent.runtime.sandbox.wrap_confined`: ``transport_for`` /
  ``transport_from_spec`` (the MCP fleet) and ``shell`` (the per-invocation shell). Each is
  governed by the resolved backend, so it inherits the state's ``mechanism`` + ``active`` +
  typed ``reason``.
* **excluded** — the three seams VERIFIABLY never wrapped (#974.5): the shared clio-core CTE
  daemon (breakaway is load-bearing), the provider LLM CLI links (need the network + their own
  territory), and ``serve.py`` (the confiner itself). Each carries a typed EXCLUSION reason so
  the exclusion is visible policy, not a silent hole — corroborated live by the process
  census ``confinement`` column (#975).

:func:`sweep_conformance` is a PURE function over an injected :class:`SandboxResult`, so the
whole (seam × tier) matrix is unit-pinnable without a real fence — the live suite injects one
synthetic state per tier and asserts zero untyped degrades on each.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from clio_agent.runtime import sandbox as sb
from clio_agent.runtime.status import IntegrationState, IntegrationStatus

if TYPE_CHECKING:
    from clio_agent.runtime.sandbox import SandboxResult

logger = logging.getLogger(__name__)

# The three wrapped agent-driven spawn seams (routed through wrap_confined).
SEAM_TRANSPORT_FOR = "transport_for"
SEAM_TRANSPORT_FROM_SPEC = "transport_from_spec"
SEAM_SHELL = "shell"
WRAPPED_SEAMS: tuple[str, ...] = (SEAM_TRANSPORT_FOR, SEAM_TRANSPORT_FROM_SPEC, SEAM_SHELL)

# The three verifiably-excluded seams (#974.5) + their typed exclusion reasons.
SEAM_CTE_DAEMON = "cte_daemon"
SEAM_PROVIDERS = "providers"
SEAM_SERVE = "serve"
EXCLUDED_SEAMS: tuple[str, ...] = (SEAM_CTE_DAEMON, SEAM_PROVIDERS, SEAM_SERVE)
EXCLUSION_REASONS: dict[str, str] = {
    SEAM_CTE_DAEMON: "excluded_cte_breakaway_load_bearing",
    SEAM_PROVIDERS: "excluded_provider_needs_network",
    SEAM_SERVE: "excluded_serve_is_the_confiner",
}

CONFINEMENT_WRAPPED = sb.CONFINEMENT_WRAPPED
CONFINEMENT_EXCLUDED = sb.CONFINEMENT_EXCLUDED
#: The mechanism label an excluded seam reports (never a fence, never ``unknown``).
MECHANISM_EXCLUDED = "excluded"
#: The forbidden label the sweep exists to prove absent anywhere.
MECHANISM_UNKNOWN = "unknown"


@dataclass(frozen=True)
class SeamConformance:
    """One seam's conformance verdict on the current tier (typed mechanism + reason)."""

    seam: str
    confinement: str  # CONFINEMENT_WRAPPED | CONFINEMENT_EXCLUDED
    mechanism: str  # a KNOWN_MECHANISMS label (wrapped) or MECHANISM_EXCLUDED
    active: bool
    reason: str  # typed, never blank
    typed: bool  # passes the zero-untyped-degrade check

    def to_dict(self) -> dict[str, object]:
        return {
            "seam": self.seam,
            "confinement": self.confinement,
            "mechanism": self.mechanism,
            "active": self.active,
            "reason": self.reason,
            "typed": self.typed,
        }


@dataclass(frozen=True)
class ConformanceReport:
    """The full sweep over every (seam × current tier): the zero-untyped-degrade guarantee."""

    tier: str  # the resolved mechanism governing the wrapped seams (or "none" on the floor)
    resolved: bool  # whether a backend was resolved (a server booted)
    seams: tuple[SeamConformance, ...] = field(default_factory=tuple)
    untyped: tuple[str, ...] = field(default_factory=tuple)  # seam names failing the typed check

    @property
    def ok(self) -> bool:
        """The guarantee holds: a backend resolved AND every seam is typed (no untyped degrade)."""
        return self.resolved and not self.untyped

    def to_dict(self) -> dict[str, object]:
        return {
            "tier": self.tier,
            "resolved": self.resolved,
            "ok": self.ok,
            "untyped": list(self.untyped),
            "seams": [s.to_dict() for s in self.seams],
        }


def _is_typed_mechanism(mechanism: str, reason: str) -> bool:
    """A seam is TYPED iff its mechanism is a known label (never ``unknown``) + reason non-blank."""
    if not reason.strip():
        return False
    if mechanism == MECHANISM_UNKNOWN:
        return False
    return mechanism in sb.KNOWN_MECHANISMS or mechanism == MECHANISM_EXCLUDED


def sweep_conformance(state: "SandboxResult | None") -> ConformanceReport:
    """Walk every (seam × tier) and record a typed mechanism/reason per seam (PURE, B6 #980).

    Wrapped seams inherit the resolved backend's ``mechanism`` / ``active`` / ``reason`` (the
    fence that governs them). Excluded seams report :data:`MECHANISM_EXCLUDED` with their typed
    :data:`EXCLUSION_REASONS` entry. A seam whose mechanism is ``unknown`` or whose reason is
    blank is an UNTYPED degrade — the exact thing this guarantee forbids — and is collected in
    ``untyped``. ``state is None`` (no server boot) yields an unresolved report: the wrapped
    seams carry the typed :data:`clio_agent.runtime.sandbox.REASON_NOT_INSTALLED` floor so the
    sweep never fabricates an ``unknown``, but ``resolved`` is ``False``.
    """
    if state is None:
        tier = sb.MECHANISM_NONE
        mechanism, active, reason, resolved = (
            sb.MECHANISM_NONE,
            False,
            sb.REASON_NOT_INSTALLED,
            False,
        )
    else:
        tier = state.mechanism
        mechanism, active, reason, resolved = state.mechanism, state.active, state.reason, True

    seams: list[SeamConformance] = []
    for seam in WRAPPED_SEAMS:
        typed = _is_typed_mechanism(mechanism, reason)
        seams.append(
            SeamConformance(
                seam=seam,
                confinement=CONFINEMENT_WRAPPED,
                mechanism=mechanism,
                active=active,
                reason=reason,
                typed=typed,
            )
        )
    for seam in EXCLUDED_SEAMS:
        ex_reason = EXCLUSION_REASONS[seam]
        typed = _is_typed_mechanism(MECHANISM_EXCLUDED, ex_reason)
        seams.append(
            SeamConformance(
                seam=seam,
                confinement=CONFINEMENT_EXCLUDED,
                mechanism=MECHANISM_EXCLUDED,
                active=False,
                reason=ex_reason,
                typed=typed,
            )
        )
    untyped = tuple(s.seam for s in seams if not s.typed)
    return ConformanceReport(tier=tier, resolved=resolved, seams=tuple(seams), untyped=untyped)


def probe_sandbox_conformance(*, state: Optional["SandboxResult"] = None) -> IntegrationStatus:
    """Doctor row: every spawn seam carries a typed mechanism/reason (the guarantee, B6 #980).

    SKIPPED when no backend resolved (no server boot). READY when a backend resolved and every
    seam is typed — the zero-untyped-degrade guarantee HOLDS, including on the honest floor
    (a typed ``none`` reason satisfies conformance; the *presence* of a fence is the separate
    ``sandbox`` row's question). DEGRADED — loud — only when the sweep finds an UNTYPED seam
    (a mechanism ``unknown`` / blank reason), the campaign-forbidden silent passthrough.
    """
    resolved = state if state is not None else sb.current_state()
    report = sweep_conformance(resolved)
    details = report.to_dict()
    if not report.resolved:
        return IntegrationStatus(
            name="sandbox_conformance",
            state=IntegrationState.SKIPPED,
            summary="Confinement backend not resolved in this process (no server boot).",
            config_source="runtime:sandbox_conformance",
            next_action="Start the gact server to resolve + sweep the confinement seams.",
            details=details,
            required=False,
        )
    seam_count = len(report.seams)
    if report.untyped:
        return IntegrationStatus(
            name="sandbox_conformance",
            state=IntegrationState.DEGRADED,
            summary=(
                f"{len(report.untyped)}/{seam_count} spawn seam(s) report an UNTYPED "
                f"confinement label (unknown/blank): {', '.join(report.untyped)}. The "
                "zero-untyped-degrade guarantee is violated."
            ),
            config_source="runtime:sandbox_conformance",
            next_action=(
                "A spawn seam resolved to an untyped mechanism/reason — every degrade must "
                "carry a typed reason. Inspect runtime/sandbox.py's ladder for the seam."
            ),
            fallback="untyped-degrade-detected",
            details=details,
            required=False,
        )
    return IntegrationStatus(
        name="sandbox_conformance",
        state=IntegrationState.READY,
        summary=(
            f"All {seam_count} spawn seams carry a typed confinement label on tier "
            f"'{report.tier}' ({len(WRAPPED_SEAMS)} wrapped, {len(EXCLUDED_SEAMS)} excluded); "
            "zero untyped degrades."
        ),
        config_source="runtime:sandbox_conformance",
        next_action="No action required.",
        capabilities=["zero-untyped-degrade"],
        details=details,
        required=False,
    )


__all__ = [
    "CONFINEMENT_EXCLUDED",
    "CONFINEMENT_WRAPPED",
    "EXCLUDED_SEAMS",
    "EXCLUSION_REASONS",
    "MECHANISM_EXCLUDED",
    "MECHANISM_UNKNOWN",
    "SEAM_CTE_DAEMON",
    "SEAM_PROVIDERS",
    "SEAM_SERVE",
    "SEAM_SHELL",
    "SEAM_TRANSPORT_FOR",
    "SEAM_TRANSPORT_FROM_SPEC",
    "WRAPPED_SEAMS",
    "ConformanceReport",
    "SeamConformance",
    "probe_sandbox_conformance",
    "sweep_conformance",
]
