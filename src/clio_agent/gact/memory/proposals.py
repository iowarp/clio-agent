"""Background memory changes are proposals (owner module).

The background pass never edits a document. The ``curator`` run calls
``propose_memory_change`` with a structured change -- add facts (creating the
document when it is new), supersede a fact, merge documents, retire a document -- and the evidence ids of the
candidates it rests on. The server stamps each new fact's provenance from those
candidates (the model never writes annotations), previews the result against the
current document, and stores a ``memory_proposals`` record. A proposal changes memory
only when it is accepted: by the user in the inbox, or by the selected auto-accept
rule. ``user_stated`` applies only when every source is ``user``; ``all`` also applies
changes based on tool and imported evidence. The source is always retained on the fact.

Accepting re-applies the change to the document as it is then (so a document edited in
between keeps its edits) and writes it through the memory service with the proposal
as the version's author. Rejecting keeps the record for 30 days, so the pass does not
propose it again.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from clio_agent.arc import memory_document as md
from clio_agent.errors import ClioError
from clio_agent.gact.memory.scope import parse_record_name
from clio_agent.gact.memory.service import Author, MemoryService
from clio_agent.gact.memory.store import PROPOSALS

Change = Literal["add", "supersede", "merge", "retire"]
Status = Literal["pending", "accepted", "rejected"]
CHANGES: tuple[str, ...] = ("add", "supersede", "merge", "retire")
REJECTED_KEPT = timedelta(days=30)
#: A source that keeps a proposal out of auto-accept.
REVIEW_SOURCES = frozenset({"tool", "imported"})


class ProposalError(ClioError):
    """A proposal could not be made or decided (typed reason)."""

    def __init__(self, reason: str, message: str, **details: Any) -> None:
        self.reason = reason
        super().__init__(message, error_type=reason, details={"reason": reason, **details})


@dataclass(frozen=True)
class Evidence:
    """Where a candidate fact came from, stamped by the server from the transcript."""

    id: str  # the candidate id the curator cites
    session: str
    messages: tuple[str, ...]
    source: md.Source
    fact: str = ""


@dataclass
class Proposal:
    """One proposed memory change."""

    id: str
    target: str
    change: Change
    facts: list[str]
    replaces: list[str]
    merge_from: list[str]
    description: str
    evidence: list[dict[str, Any]]
    sources: list[str]
    rationale: str
    origin: str  # "session:<id>" or "import:<id>"
    handle: str = ""
    status: Status = "pending"
    decided_by: str = ""
    created_at: str = ""
    decided_at: str = ""
    base_text: str = ""
    proposed_text: str = ""
    notes: list[str] = field(default_factory=list)
    applied_before_text: str = ""
    applied_text: str | None = None

    @property
    def needs_review(self) -> bool:
        """Whether any source keeps it out of auto-accept."""
        return self.status == "pending" and bool(REVIEW_SOURCES & set(self.sources))


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def stamp_source(roles: list[str]) -> md.Source:
    """The source of a fact whose evidence messages have ``roles``.

    ``user`` when every evidence message is the user's own text, ``tool`` when any is
    a tool result, otherwise ``inferred`` (it rests on the assistant's words).
    """
    if any(role == "tool" for role in roles):
        return "tool"
    if roles and all(role == "user" for role in roles):
        return "user"
    return "inferred"


def _facts_from(evidence: list[Evidence], texts: list[str]) -> list[md.Fact]:
    """New facts for ``texts``, each stamped with the evidence it rests on."""
    texts = [text for text in texts if " ".join(text.split())]
    if not texts:
        return []
    if not evidence:
        raise ProposalError("memory_proposal_no_evidence", "a fact needs the evidence it rests on")
    first = evidence[0]
    sources = {item.source for item in evidence}
    source: md.Source = (
        "tool"
        if "tool" in sources
        else "imported"
        if "imported" in sources
        else "inferred"
        if "inferred" in sources
        else "user"
    )
    message = first.messages[0] if first.messages else ""
    return [
        md.Fact(
            id="f000000",  # assigned when applied, against the document's ids
            text=" ".join(text.split()),
            source=source,
            session=first.session,
            message=message if md._TOKEN.match(message or "-") and message else "",  # noqa: SLF001
            at=md.today(),
        )
        for text in texts
        if " ".join(text.split())
    ]


def _with_ids(facts: list[md.Fact], taken: set[str]) -> list[md.Fact]:
    out = []
    for fact in facts:
        fresh = md.new_fact_id(taken)
        taken.add(fresh)
        out.append(replace(fact, id=fresh))
    return out


def apply_change(
    service: MemoryService, proposal: Proposal, evidence: list[Evidence]
) -> tuple[md.MemoryDocument | None, str]:
    """The target document after ``proposal`` (``None`` for a retirement) and its base sha.

    Raises:
        ProposalError: The change no longer applies (a superseded fact is gone, a
            merge source is missing).
    """
    address = parse_record_name(proposal.target)
    current = service.get(address)
    taken = {
        fact.id for stored in service.documents((address.scope,)) for fact in stored.document.facts
    }
    taken |= set(service.forgotten(address.scope))
    new_facts = _with_ids(_facts_from(evidence, proposal.facts), taken)
    stamp = md.now_stamp()
    if proposal.change == "retire":
        if current is None:
            raise ProposalError("memory_proposal_stale", f"{proposal.target} no longer exists")
        return None, current.sha
    if proposal.change == "add" and current is None:
        if address.category in md.SINGLETON_CATEGORIES:
            current = service.ensure_singleton(address.scope, address.category)
        else:
            document = md.MemoryDocument(
                name=address.slug,
                description=" ".join((proposal.description or address.slug).split()),
                category=address.category,  # type: ignore[arg-type]
                updated=stamp,
                facts=tuple(new_facts),
            )
            return document, ""
    if current is None:
        raise ProposalError("memory_proposal_stale", f"{proposal.target} does not exist")
    document = current.document
    if proposal.change == "supersede":
        if len(proposal.replaces) != 1 or len(new_facts) != 1:
            raise ProposalError("memory_proposal_invalid", "supersede replaces one fact with one")
        old = document.fact(proposal.replaces[0])
        if old is None or not old.live:
            raise ProposalError("memory_proposal_stale", f"fact {proposal.replaces[0]} is gone")
        return md.superseding(document, old.id, new_facts[0], updated=stamp), current.sha
    if proposal.change == "merge":
        merged = list(document.facts)
        for name in proposal.merge_from:
            other = service.get(name)
            if other is None:
                raise ProposalError("memory_proposal_stale", f"merge source {name} is gone")
            merged.extend(fact for fact in other.document.live_facts if not document.fact(fact.id))
        merged.extend(new_facts)
        return replace(document, facts=tuple(merged), updated=stamp), current.sha
    existing = {fact.text.casefold() for fact in document.live_facts}
    additions = tuple(fact for fact in new_facts if fact.text.casefold() not in existing)
    return replace(document, facts=(*document.facts, *additions), updated=stamp), current.sha


class ProposalStore:
    """Proposals on the memory store, and their decisions."""

    def __init__(self, service: MemoryService) -> None:
        self._service = service
        self._store = service.store

    def get(self, proposal_id: str) -> Proposal | None:
        """The proposal ``proposal_id``, or ``None``."""
        raw = self._store.get(PROPOSALS, proposal_id)
        return Proposal(**json.loads(raw)) if raw else None

    def _put(self, proposal: Proposal) -> None:
        self._store.put(PROPOSALS, proposal.id, json.dumps(asdict(proposal)).encode("utf-8"))

    def all(self) -> list[Proposal]:
        """Every kept proposal, newest first; rejected ones past 30 days are dropped."""
        cutoff = (datetime.now(UTC) - REJECTED_KEPT).strftime("%Y-%m-%dT%H:%M:%SZ")
        out: list[Proposal] = []
        for name in list(self._store.scan_names(PROPOSALS)):
            proposal = self.get(name)
            if proposal is None:
                continue
            if proposal.status == "rejected" and proposal.decided_at < cutoff:
                self._store.delete(PROPOSALS, name)
                continue
            out.append(proposal)
        return sorted(out, key=lambda p: p.created_at, reverse=True)

    def pending(self) -> list[Proposal]:
        """Proposals waiting for review."""
        return [proposal for proposal in self.all() if proposal.status == "pending"]

    def propose(
        self,
        *,
        target: str,
        change: str,
        facts: list[str],
        replaces: list[str],
        merge_from: list[str],
        description: str,
        evidence: list[Evidence],
        rationale: str,
        origin: str,
        handle: str,
        auto_accept: str,
    ) -> Proposal:
        """Record a proposal (idempotent per run, target and change) and auto-apply it
        when the rule allows.

        Raises:
            ProposalError: The change is malformed or does not apply.
        """
        if change not in CHANGES:
            raise ProposalError("memory_proposal_invalid", f"change must be one of {CHANGES}")
        parse_record_name(target)
        body = json.dumps(
            [handle or origin, target, change, facts, replaces, merge_from], sort_keys=True
        )
        proposal_id = "mp_" + hashlib.sha256(body.encode()).hexdigest()[:20]
        existing = self.get(proposal_id)
        if existing is not None:
            return existing  # a resumed run proposing again: no duplicate
        proposal = Proposal(
            id=proposal_id,
            target=target,
            change=change,  # type: ignore[arg-type]
            facts=list(facts),
            replaces=list(replaces),
            merge_from=list(merge_from),
            description=description,
            evidence=[asdict(item) for item in evidence],
            sources=sorted({item.source for item in evidence}),
            rationale=rationale,
            origin=origin,
            handle=handle,
            created_at=_now(),
        )
        document, _base = apply_change(self._service, proposal, evidence)
        current = self._service.get(target)
        proposal.base_text = current.text if current else ""
        proposal.proposed_text = md.serialize(document) if document is not None else ""
        self._put(proposal)
        if auto_accept == "all" or (auto_accept == "user_stated" and proposal.sources == ["user"]):
            return self.accept(proposal.id, decided_by="auto_accept")
        return proposal

    def accept(
        self, proposal_id: str, *, decided_by: str, edited_facts: list[str] | None = None
    ) -> Proposal:
        """Apply a pending proposal through the memory service (authored by it).

        Raises:
            ProposalError: No such pending proposal, or its change no longer applies.
        """
        proposal = self.get(proposal_id)
        if proposal is None or proposal.status != "pending":
            raise ProposalError("memory_proposal_not_pending", f"no pending proposal {proposal_id}")
        if edited_facts is not None:
            proposal.facts = list(edited_facts)
        # Facts the user rewrote while reviewing are theirs (as a correction on the memory
        # page is); the stored evidence keeps where the proposal came from.
        stamp = {"source": "user"} if edited_facts is not None else {}
        evidence = [
            Evidence(**{**item, "messages": tuple(item["messages"]), **stamp})
            for item in proposal.evidence
        ]
        author = Author("proposal", proposal.id, session=proposal.origin)
        document, base_sha = apply_change(self._service, proposal, evidence)
        before = self._service.get(proposal.target)
        if (before.sha if before else "") != base_sha:
            from clio_agent.gact.memory.service import MemoryConflict  # noqa: PLC0415

            raise MemoryConflict(
                proposal.target, expected=base_sha, current=before.sha if before else ""
            )
        if document is None:
            self._service.remove(proposal.target, expected_sha=base_sha, author=author)
            applied_text = ""
        else:
            written = self._service.write(
                proposal.target, document, expected_sha=base_sha or None, author=author
            )
            applied_text = written.text
        for name in proposal.merge_from if proposal.change == "merge" else []:
            other = self._service.get(name)
            if other is not None:
                self._service.remove(name, expected_sha=other.sha, author=author)
        proposal.status = "accepted"
        proposal.applied_before_text = before.text if before else ""
        proposal.applied_text = applied_text
        proposal.decided_by = decided_by
        proposal.decided_at = _now()
        self._put(proposal)
        return proposal

    def reject(self, proposal_id: str, *, decided_by: str) -> Proposal:
        """Mark a pending proposal rejected (kept 30 days to suppress repeats)."""
        proposal = self.get(proposal_id)
        if proposal is None or proposal.status != "pending":
            raise ProposalError("memory_proposal_not_pending", f"no pending proposal {proposal_id}")
        proposal.status = "rejected"
        proposal.decided_by = decided_by
        proposal.decided_at = _now()
        self._put(proposal)
        return proposal


def propose_from_curator(**change: Any) -> dict[str, Any]:
    """The ``propose_memory_change`` tool body (curator sessions only)."""
    from clio_agent.gact.memory.curator import curator_proposal  # noqa: PLC0415

    return curator_proposal(**change)


__all__ = [
    "CHANGES",
    "Evidence",
    "Proposal",
    "ProposalError",
    "ProposalStore",
    "apply_change",
    "propose_from_curator",
    "stamp_source",
]
