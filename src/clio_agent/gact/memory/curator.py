"""The ``curator`` run's side of the background pass: its one write, a proposal.

A curator session is spawned by the background pass (:mod:`.background`) with its
inputs pinned in session metadata: the extracted candidates (each with the evidence
the server stamped), the scopes it may touch, and where the candidates came from.
``propose_memory_change`` resolves the candidate ids it cites to that evidence, so
the model chooses *what* to propose and the server decides *where it came from*.
"""

from __future__ import annotations

from typing import Any

from clio_agent.gact.memory.policy import active_policy, setting
from clio_agent.gact.memory.proposals import Evidence, ProposalError, ProposalStore

#: Curator session metadata keys.
CANDIDATES = "memory_candidates"
SCOPES = "memory_scopes"
ORIGIN = "memory_origin"


def curator_proposal(
    *,
    target: str,
    change: str,
    evidence: list[str],
    rationale: str,
    facts: list[str],
    replaces: list[str],
    merge_from: list[str],
    description: str,
) -> dict[str, Any]:
    """Record one proposal from the active curator session.

    Raises:
        ProposalError: Not a curator session, an unknown candidate id, a target
            outside the run's scopes, or a change that does not apply.
    """
    from clio_agent.gact import context  # noqa: PLC0415
    from clio_agent.gact.memory.runtime import memory_service  # noqa: PLC0415

    active = active_policy()
    if active is None or not active[2].curator:
        raise ProposalError("memory_proposals_curator_only", "only the curator proposes")
    app, session, _policy = active
    metadata = session.metadata or {}
    candidates = {item["id"]: item for item in metadata.get(CANDIDATES, [])}
    unknown = [ref for ref in evidence if ref not in candidates]
    if unknown or not evidence:
        raise ProposalError(
            "memory_proposal_evidence_unknown",
            f"cite candidate ids from your input; unknown: {unknown or 'none given'}",
        )
    scopes = tuple(metadata.get(SCOPES, ()))
    for name in (target, *merge_from):
        if not any(name.startswith(scope + "/") for scope in scopes):
            raise ProposalError(
                "memory_out_of_scope", f"{name} is outside this run's memory ({', '.join(scopes)})"
            )
    cited = [
        Evidence(
            id=ref,
            session=str(candidates[ref]["session"]),
            messages=tuple(candidates[ref].get("messages", ())),
            source=candidates[ref]["source"],
            fact=str(candidates[ref].get("fact", "")),
        )
        for ref in evidence
    ]
    store = ProposalStore(memory_service(app))
    proposal = store.propose(
        target=target,
        change=change,
        facts=facts,
        replaces=replaces,
        merge_from=merge_from,
        description=description,
        evidence=cited,
        rationale=rationale,
        origin=str(metadata.get(ORIGIN, "")),
        handle=context.active_assignment_handle() or session.id,
        auto_accept=str(setting("memory.auto_accept", "user_stated")),
    )
    return {
        "tool": "propose_memory_change",
        "proposal": proposal.id,
        "target": proposal.target,
        "change": proposal.change,
        "status": proposal.status,
        "sources": proposal.sources,
    }


__all__ = ["CANDIDATES", "ORIGIN", "SCOPES", "curator_proposal"]
