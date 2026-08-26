"""Agent-proposed artifact designation — the ``create_artifact`` tool floor.

Owner decisions #966.2 + #966.5 (campaign slice S3, issue #969): the model can
DECIDE to designate an artifact — its report, an in-context document, a generated
script — through ONE path, the ``create_artifact`` tool. Nothing auto-registers an
answer (⚑ clio provides capability, never deterministic registration). The
legacy-shaped inert ``artifacts`` structured-output field is DELETED, not made
real, in the same slice.

The model is never load-bearing in the chain of custody (#966.5): a proposal only
NAMES a deliverable (a workspace path, or inline bytes to write) and an intent
(``kind`` + ``annotation``). The HARNESS computes every sha256 here — any
model-supplied hash/claim in the tool args is ignored. Model intent is quarantined
in ``annotation`` and never merged into evidence. Accepted proposals mint through
the S1 atomic :func:`mint_artifact` funnel with mechanism ``model`` and a
``designation=agent-proposed`` producer note; rejections are TYPED so the model can
react (bounded repair), never silently dropped.

Validation reuses the S1 containment helpers (``minting._workspace_root`` /
``minting._contained``) so the tool boundary and the mint seams share one rule: a
path must exist and resolve inside the bound workspace root before it is hashed.
Inline content is written through the policy-checked writer and then handed to the
same selected artifact store as an existing-path proposal.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from clio_agent import conf
from clio_agent.gact.artifacts.cas import IngestedIdentity
from clio_agent.gact.artifacts.minting import (
    _contained,
    _workspace_root,
    artifact_name_for_path,
    mint_artifact_outcome,
)
from clio_agent.gact.artifacts.proposal_effects import (
    PROPOSED_ARTIFACT_EVENT,
    _dedup_enrich,
    _emit_proposal_event,
    _gate_content_write,
    _mint_producer,
    _write_inline_content,
)
from clio_agent.gact.artifacts.records import (
    RESERVED_KINDS,
    ArtifactKind,
    ArtifactVersion,
    Custody,
    Mechanism,
)
from clio_agent.gact.artifacts.registry import get_registry
from clio_agent.gact.artifacts.storage import (
    harness_write_artifact_identity,
    ingest_artifact_identity,
)
from clio_agent.gact.artifacts.wire import declare_create_artifact_structured_content

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.types import AgentDef

logger = logging.getLogger(__name__)

#: Default ceiling on artifact promotions ONE turn may make through the tool
#: (owner risk: agent over-designation). Config-first (#985 conventions):
#: ``artifacts.proposals_per_turn`` / ``CLIO_ARTIFACTS_PROPOSALS_PER_TURN``. Only
#: genuinely NEW versions count; a duplicate (already-registered) proposal is a
#: no-op that never consumes budget, so the cap never blocks bounded repair on a
#: re-designation of the same bytes.
_DEFAULT_PROPOSALS_PER_TURN = 8

#: Default ceiling on the number of proposals ONE ``create_artifact`` call may carry
#: (finding [1]: the batch list is model-controlled and each item durably emits an
#: ``artifact.proposed`` event regardless of outcome — the per-turn promotion cap
#: gates only genuinely-new mints, not the event/ARC-store growth an oversized batch
#: causes). A batch over this bound is rejected with ONE typed ``over_batch`` event
#: instead of fanning out N durable records. Config-first (#985 conventions):
#: ``artifacts.proposals_batch_max`` / ``CLIO_ARTIFACTS_PROPOSALS_BATCH_MAX``.
_DEFAULT_PROPOSALS_BATCH_MAX = 32

#: Bound on the per-turn counter map so a pathological long-lived process cannot
#: grow it unboundedly (turn ids accrete). Evicts oldest insertion first.
_COUNTER_MAP_CAP = 4096
_COUNTER_LOCK = threading.Lock()

#: ``PROPOSED_ARTIFACT_EVENT`` and the ``_emit_proposal_event`` / ``_gate_content_write``
#: / ``_write_inline_content`` helpers now live in the ``proposal_effects`` owner
#: module (no-accretion); they are imported above so this module + its tests still
#: reference them by their original names.


def proposals_per_turn() -> int:
    """Resolve the per-turn promotion cap from config (file → env → default)."""
    return conf.resolve(
        "artifacts.proposals_per_turn",
        env="CLIO_ARTIFACTS_PROPOSALS_PER_TURN",
        default=_DEFAULT_PROPOSALS_PER_TURN,
        cast=conf.as_int,
    )


def proposals_batch_max() -> int:
    """Resolve the max proposals per ``create_artifact`` call (file → env → default)."""
    return conf.resolve(
        "artifacts.proposals_batch_max",
        env="CLIO_ARTIFACTS_PROPOSALS_BATCH_MAX",
        default=_DEFAULT_PROPOSALS_BATCH_MAX,
        cast=conf.as_int,
    )


class RejectionReason(str, Enum):
    """Why a proposal was declined — a bounded, model-reactable vocabulary.

    Every value is a TYPED reason the model can read from the tool observation
    and repair against (owner decision #966.2). ``already_registered`` is NOT a
    rejection — a byte-identical re-designation returns the existing record with
    ``created=false`` (bounded-repair-friendly); it lives on :class:`ProposalOutcome`.
    """

    MISSING_INPUT = "missing_input"  # neither a path nor inline content (+name) given
    INVALID_KIND = "invalid_kind"  # kind not in the enum, or a RESERVED kind (plan)
    PATH_MISSING = "path_missing"  # a named workspace path does not exist on disk
    ESCAPES_ROOT = "escapes_root"  # path resolves OUTSIDE the bound workspace root
    CONTAINMENT_UNRESOLVED = "containment_unresolved"  # workspace root unresolvable
    OVER_CAP = "over_cap"  # this turn already hit the per-turn promotion cap
    OVER_BATCH = "over_batch"  # this call carried more proposals than the batch max
    WRITE_FAILED = "write_failed"  # inline content could not be written under policy
    # P1.1 #1063: the former MODE_READ_ONLY reason is gone with the copy-pasted plan/architect
    # lock. A content write in plan/architect is now denied through the ONE resolver (the built-in
    # plan_acl @40 rule), surfaced as the typed POLICY_DENIED below — no separate mode predicate.
    WOULD_OVERWRITE = "would_overwrite"  # inline content would clobber a non-owned file
    POLICY_DENIED = "policy_denied"  # a permission policy denied the content write
    PERMISSION_REQUIRED = "permission_required"  # policy=ask, no inline approver


@dataclass(frozen=True)
class Proposal:
    """One normalized artifact proposal parsed from the tool args.

    Exactly one of ``path`` (register an existing workspace file) or ``content``
    (write inline bytes as a workspace file, then register) is the primary source.
    ``kind`` is the model's declared kind (validated against the enum); ``annotation``
    is quarantined model intent. Any ``sha256``/hash the model supplied is NOT
    represented here — the harness computes identity.
    """

    name: str = ""
    kind: str = ""
    path: str = ""
    content: str = ""
    annotation: str = ""

    @classmethod
    def from_mapping(cls, raw: Any) -> "Proposal":
        """Build a proposal from a model-supplied mapping, ignoring unknown keys.

        Notably ignores any ``sha256`` / ``hash`` / ``digest`` key: the harness is
        the sole hasher (#966.5). Non-mapping input yields an empty proposal that
        validation rejects as ``missing_input``.
        """
        if not isinstance(raw, dict):
            return cls()
        content = raw.get("content")
        return cls(
            name=str(raw.get("name") or "").strip(),
            kind=str(raw.get("kind") or "").strip(),
            path=str(raw.get("path") or "").strip(),
            content=content if isinstance(content, str) else "",
            annotation=str(raw.get("annotation") or "").strip(),
        )


@dataclass(frozen=True)
class ProposalOutcome:
    """The typed result of one proposal — accepted (created or deduped) or rejected.

    ``accepted`` is ``True`` for both a fresh mint (``created=True``) and a
    byte-identical re-designation (``created=False``, ``reason=already_registered``).
    A rejection carries ``accepted=False`` and a :class:`RejectionReason` value in
    ``reason``. ``to_wire`` projects it to the JSON the tool returns to the model.
    """

    accepted: bool
    name: str = ""
    reason: str = ""
    detail: str = ""
    created: bool = False
    version: Optional[ArtifactVersion] = None
    workspace_id: str = ""
    #: A9 (#1176): typed dedup-enrichment outcome (see ``proposal_effects._dedup_enrich``).
    #: ``""`` (nothing to merge) omits the key from ``to_wire`` — a plain dedup's wire
    #: shape stays byte-identical to before this field existed.
    enrichment: str = ""

    def to_wire(self) -> dict[str, Any]:
        """Project to the model-facing observation dict (bounded-repair-friendly)."""
        if not self.accepted:
            return {
                "accepted": False,
                "name": self.name,
                "reason": self.reason,
                "detail": self.detail,
            }
        ver = self.version
        wire = {
            "accepted": True,
            "created": self.created,
            "name": self.name,
            "artifact_id": ver.artifact_id if ver is not None else "",
            "version": ver.version if ver is not None else 0,
            "sha256": (ver.sha256 if ver is not None else None),
            "kind": ver.kind.value if ver is not None else "",
            "custody": ver.custody.value if ver is not None else "",
            "mechanism": ver.mechanism.value if ver is not None else "",
            "reason": self.reason,
        }
        if self.enrichment:
            wire["enrichment"] = self.enrichment
        return wire


def _rejected(name: str, reason: RejectionReason, detail: str = "") -> ProposalOutcome:
    return ProposalOutcome(accepted=False, name=name, reason=reason.value, detail=detail)


def validate_kind(raw: str) -> ArtifactKind:
    """Resolve a model-supplied kind string to an :class:`ArtifactKind`.

    An empty kind defaults to ``other`` (a missing intent must not block a real
    deliverable). A non-enum value or a RESERVED kind (``plan``, #966.4) raises
    :class:`ValueError` — the caller turns that into a typed ``invalid_kind``
    rejection the model can repair.
    """
    token = (raw or "").strip().lower()
    if not token:
        return ArtifactKind.OTHER
    try:
        kind = ArtifactKind(token)
    except ValueError:
        valid = ", ".join(sorted(k.value for k in ArtifactKind if k not in RESERVED_KINDS))
        raise ValueError(f"unknown kind {raw!r}; choose one of: {valid}") from None
    if kind in RESERVED_KINDS:
        raise ValueError(f"kind {kind.value!r} is reserved and cannot be designated")
    return kind


# --------------------------------------------------------------------------- #
# Per-turn promotion cap (counts genuinely-new promotions only).
# --------------------------------------------------------------------------- #


def _counter_key(sid: str, turn_id: str) -> str:
    return f"{sid}:{turn_id}"


def _counter_map(app: "FastAPI") -> dict[str, int]:
    counts = getattr(app.state, "artifact_proposal_counts", None)
    if counts is None:
        counts = {}
        app.state.artifact_proposal_counts = counts
    return counts


def proposal_count(app: "FastAPI", sid: str, turn_id: str) -> int:
    """Return how many NEW promotions this ``(session, turn)`` has already made."""
    with _COUNTER_LOCK:
        return _counter_map(app).get(_counter_key(sid, turn_id), 0)


def _increment_proposal_count(app: "FastAPI", sid: str, turn_id: str) -> None:
    """Record one accepted NEW promotion, evicting the oldest key past the bound."""
    key = _counter_key(sid, turn_id)
    with _COUNTER_LOCK:
        counts = _counter_map(app)
        if key not in counts and len(counts) >= _COUNTER_MAP_CAP:
            # Bound the map: drop the oldest-inserted key (dict preserves order).
            oldest = next(iter(counts), None)
            if oldest is not None:
                counts.pop(oldest, None)
        counts[key] = counts.get(key, 0) + 1


# --------------------------------------------------------------------------- #
# Promotion — the validation + mint core (testable without a tool loop).
# --------------------------------------------------------------------------- #


def promote_proposal(
    app: "FastAPI",
    sid: str,
    proposal: Proposal,
    *,
    workspace_id: str,
    turn_id: str = "",
    trace_id: str = "",
    agent_id: str = "",
) -> ProposalOutcome:
    """Validate + (on acceptance) mint one agent-proposed artifact.

    The single promotion point behind the ``create_artifact`` tool. Order: resolve
    kind (reserved/invalid → typed reject), resolve the byte source (existing path
    OR inline content written to the workspace), enforce containment BEFORE hashing
    (owner decision 10), harness-hash identity, then — for a genuinely new version —
    check the per-turn cap and mint through :func:`mint_artifact` with mechanism
    ``model`` + ``designation=agent-proposed``. A byte-identical re-designation
    returns the existing record with ``created=False`` (``already_registered``) and
    consumes no cap budget. Every outcome emits an ``artifact.proposed`` trace event.
    """
    try:
        kind = validate_kind(proposal.kind)
    except ValueError as exc:
        outcome = _rejected(proposal.name, RejectionReason.INVALID_KIND, str(exc))
        _emit_proposal_event(
            app,
            sid,
            turn_id=turn_id,
            trace_id=trace_id,
            agent_id=agent_id,
            outcome=outcome,
            proposal=proposal,
            source="none",
        )
        return outcome

    root = _workspace_root(app, workspace_id)
    if root is None:
        outcome = _rejected(
            proposal.name, RejectionReason.CONTAINMENT_UNRESOLVED, "workspace root unresolvable"
        )
        _emit_proposal_event(
            app,
            sid,
            turn_id=turn_id,
            trace_id=trace_id,
            agent_id=agent_id,
            outcome=outcome,
            proposal=proposal,
            source="none",
        )
        return outcome

    # Resolve the byte source: inline content (write it) or an existing path.
    source = "inline" if proposal.content else "path"
    # The selected store's ingestion outcome for both path and inline channels.
    path_ingest: Optional[IngestedIdentity] = None
    if proposal.content:
        # A content write is destructive — gate it (mode/overwrite/policy) BEFORE
        # touching disk, so the native tool honors the same write discipline every
        # other write path does (finding [2]).
        gate_reject = _gate_content_write(app, sid, proposal, root, workspace_id=workspace_id)
        if gate_reject is not None:
            _emit_proposal_event(
                app,
                sid,
                turn_id=turn_id,
                trace_id=trace_id,
                agent_id=agent_id,
                outcome=gate_reject,
                proposal=proposal,
                source=source,
            )
            return gate_reject
        target, evidence, reject = _write_inline_content(proposal, root)
        if reject is not None:
            _emit_proposal_event(
                app,
                sid,
                turn_id=turn_id,
                trace_id=trace_id,
                agent_id=agent_id,
                outcome=reject,
                proposal=proposal,
                source=source,
            )
            return reject
        assert target is not None and evidence is not None  # narrowed by reject is None
        # proposal.name is the WRITE TARGET (may be a full path); the record
        # identity is always the basename so every seam (tool-declared,
        # harness-write, model-designated) folds one deliverable into ONE
        # version chain instead of splitting on the name's spelling.
        name = (
            artifact_name_for_path(proposal.name)
            if proposal.name
            else artifact_name_for_path(target)
        )
        path = target
        path_ingest = harness_write_artifact_identity(
            app,
            path,
            workspace_root=root,
            in_hand_sha=evidence.sha256 or "",
            in_hand_size=int(evidence.size_bytes or 0),
        )
        evidence = path_ingest.evidence
    elif proposal.path:
        # Ground BOTH channels against the workspace root (finding [4/5/9]): a
        # relative path is root-relative (symmetric with inline content, which
        # targets root/name) and an absolute path keeps today's containment check.
        # Resolve ONCE and hash the SAME resolved object — closing the
        # check-here/hash-there TOCTOU seam (a symlink is followed exactly once).
        raw = Path(proposal.path)
        path = (raw if raw.is_absolute() else (root / raw)).resolve(strict=False)
        if not _contained(path, root):
            outcome = _rejected(
                proposal.name or artifact_name_for_path(path),
                RejectionReason.ESCAPES_ROOT,
                f"{proposal.path!r} resolves outside the workspace root",
            )
            _emit_proposal_event(
                app,
                sid,
                turn_id=turn_id,
                trace_id=trace_id,
                agent_id=agent_id,
                outcome=outcome,
                proposal=proposal,
                source=source,
            )
            return outcome
        name = (
            artifact_name_for_path(proposal.name) if proposal.name else artifact_name_for_path(path)
        )
        if not path.is_file():
            outcome = _rejected(
                name, RejectionReason.PATH_MISSING, f"{proposal.path!r} does not exist"
            )
            _emit_proposal_event(
                app,
                sid,
                turn_id=turn_id,
                trace_id=trace_id,
                agent_id=agent_id,
                outcome=outcome,
                proposal=proposal,
                source=source,
            )
            return outcome
        try:
            # A model-designated deliverable uses the selected provider's store;
            # native file storage retains its existing CAS/threshold semantics.
            path_ingest = ingest_artifact_identity(app, path, workspace_root=root)
            evidence = path_ingest.evidence
        except OSError as exc:
            outcome = _rejected(name, RejectionReason.PATH_MISSING, f"stat/hash failed: {exc}")
            _emit_proposal_event(
                app,
                sid,
                turn_id=turn_id,
                trace_id=trace_id,
                agent_id=agent_id,
                outcome=outcome,
                proposal=proposal,
                source=source,
            )
            return outcome
    else:
        outcome = _rejected(
            proposal.name, RejectionReason.MISSING_INPUT, "provide a path or inline content"
        )
        _emit_proposal_event(
            app,
            sid,
            turn_id=turn_id,
            trace_id=trace_id,
            agent_id=agent_id,
            outcome=outcome,
            proposal=proposal,
            source="none",
        )
        return outcome

    # W&B same-name+same-sha dedup: return the existing record, created=False, and
    # consume no cap budget — a re-designation of identical bytes is not a new
    # promotion (bounded-repair-friendly).
    registry = get_registry(app)
    existing = registry.get(workspace_id, name)
    existing_version = existing.version_for_sha(evidence.sha256) if existing is not None else None
    if existing_version is not None:
        # A9 (#1176): a dedup is not a no-op for the caller's OWN declared enrichment.
        enrichment = _dedup_enrich(
            app,
            sid,
            proposal,
            workspace_id=workspace_id,
            name=name,
            version=existing_version,
            turn_id=turn_id,
            trace_id=trace_id,
        )
        outcome = ProposalOutcome(
            accepted=True,
            name=name,
            reason="already_registered",
            created=False,
            version=existing_version,
            workspace_id=workspace_id,
            enrichment=enrichment,
        )
        _emit_proposal_event(
            app,
            sid,
            turn_id=turn_id,
            trace_id=trace_id,
            agent_id=agent_id,
            outcome=outcome,
            proposal=proposal,
            source=source,
        )
        return outcome

    # New promotion — enforce the per-turn cap BEFORE minting.
    if proposal_count(app, sid, turn_id) >= proposals_per_turn():
        outcome = _rejected(
            name,
            RejectionReason.OVER_CAP,
            f"per-turn promotion cap ({proposals_per_turn()}) reached",
        )
        _emit_proposal_event(
            app,
            sid,
            turn_id=turn_id,
            trace_id=trace_id,
            agent_id=agent_id,
            outcome=outcome,
            proposal=proposal,
            source=source,
        )
        return outcome

    mint = mint_artifact_outcome(
        app,
        sid,
        name=name,
        workspace_id=workspace_id,
        evidence=evidence,
        kind=kind,
        mechanism=Mechanism.MODEL,
        producer=_mint_producer(sid, turn_id, agent_id),
        custody=path_ingest.custody if path_ingest is not None else Custody.WORKSPACE_REFERENCED,
        path=str(path),
        ingested=path_ingest,
        annotation=proposal.annotation,
        turn_id=turn_id,
        trace_id=trace_id,
        not_ingested_size=(path_ingest.not_ingested_size if path_ingest is not None else None),
    )
    if mint is None or mint.version is None:
        outcome = _rejected(name, RejectionReason.WRITE_FAILED, "mint returned no version")
        _emit_proposal_event(
            app,
            sid,
            turn_id=turn_id,
            trace_id=trace_id,
            agent_id=agent_id,
            outcome=outcome,
            proposal=proposal,
            source=source,
        )
        return outcome
    if not mint.created:
        # Concurrent dedup (finding [7]): the pre-mint check above passed, but a
        # parallel promote (fan-out child / observer worker) minted these exact
        # bytes first. Report created=False and consume NO cap budget — a
        # re-designation of identical bytes is never a new promotion, even under a
        # race — instead of the stale created=True + cap consumption.
        # A9 (#1176): the SAME enrichment merge as the pre-check dedup above (idempotent).
        enrichment = _dedup_enrich(
            app,
            sid,
            proposal,
            workspace_id=workspace_id,
            name=name,
            version=mint.version,
            turn_id=turn_id,
            trace_id=trace_id,
        )
        outcome = ProposalOutcome(
            accepted=True,
            name=name,
            reason="already_registered",
            created=False,
            version=mint.version,
            workspace_id=workspace_id,
            enrichment=enrichment,
        )
        _emit_proposal_event(
            app,
            sid,
            turn_id=turn_id,
            trace_id=trace_id,
            agent_id=agent_id,
            outcome=outcome,
            proposal=proposal,
            source=source,
        )
        return outcome
    _increment_proposal_count(app, sid, turn_id)
    outcome = ProposalOutcome(
        accepted=True,
        name=name,
        created=True,
        version=mint.version,
        workspace_id=workspace_id,
    )
    _emit_proposal_event(
        app,
        sid,
        turn_id=turn_id,
        trace_id=trace_id,
        agent_id=agent_id,
        outcome=outcome,
        proposal=proposal,
        source=source,
    )
    return outcome


def promote_proposals(
    app: "FastAPI",
    sid: str,
    proposals: list[Proposal],
    *,
    workspace_id: str,
    turn_id: str = "",
    trace_id: str = "",
    agent_id: str = "",
) -> dict[str, Any]:
    """Promote a batch of proposals; return the model-facing summary + per-item wire.

    Each item is validated + minted independently (one over-cap item does not abort
    the rest — the model sees exactly which succeeded): ``created``/``deduplicated``/
    ``rejected`` — no top-level ``accepted`` (it collided with the per-item boolean).

    A batch carrying more than ``proposals_batch_max()`` items is rejected WHOLE
    with ONE typed ``over_batch`` event (finding [1]): otherwise every item — even
    empty/dedup/reject ones that never consume the per-turn promotion cap — durably
    emits an ``artifact.proposed`` record, so a model-controlled batch length is an
    unbounded ARC/event write the promotion cap does not gate. The bound closes that
    without fanning out N durable records.
    """
    batch_max = proposals_batch_max()
    if len(proposals) > batch_max:
        outcome = _rejected(
            "",
            RejectionReason.OVER_BATCH,
            f"batch of {len(proposals)} proposals exceeds the max {batch_max} per call",
        )
        _emit_proposal_event(
            app,
            sid,
            turn_id=turn_id,
            trace_id=trace_id,
            agent_id=agent_id,
            outcome=outcome,
            proposal=Proposal(),
            source="none",
        )
        result = {
            "artifacts": [outcome.to_wire()],
            "created": 0,
            "deduplicated": 0,
            "rejected": 1,
        }
        declare_create_artifact_structured_content([outcome], result)
        return result
    outcomes = [
        promote_proposal(
            app,
            sid,
            p,
            workspace_id=workspace_id,
            turn_id=turn_id,
            trace_id=trace_id,
            agent_id=agent_id,
        )
        for p in proposals
    ]
    created = sum(1 for o in outcomes if o.accepted and o.created)
    deduplicated = sum(1 for o in outcomes if o.accepted and not o.created)
    rejected = sum(1 for o in outcomes if not o.accepted)
    result = {
        "artifacts": [o.to_wire() for o in outcomes],
        "created": created,
        "deduplicated": deduplicated,
        "rejected": rejected,
    }
    declare_create_artifact_structured_content(outcomes, result)
    return result


def parse_proposals(
    *,
    name: str,
    kind: str,
    path: str,
    content: str,
    annotation: str,
    artifacts: Any,
) -> list[Proposal]:
    """Normalize the tool's single-item OR batch args into a proposal list.

    When ``artifacts`` is a non-empty list it is the batch source (each element a
    mapping); otherwise the top-level fields form ONE proposal. An empty call
    yields a single empty proposal that validation rejects as ``missing_input`` —
    the model gets a typed reason rather than a silent no-op.
    """
    if isinstance(artifacts, list) and artifacts:
        return [Proposal.from_mapping(item) for item in artifacts]
    return [
        Proposal(
            name=(name or "").strip(),
            kind=(kind or "").strip(),
            path=(path or "").strip(),
            content=content if isinstance(content, str) else "",
            annotation=(annotation or "").strip(),
        )
    ]


def build_create_artifact_tool(agent_def: "AgentDef") -> Any:
    """The ``create_artifact`` DSPy tool (auto-attached runtime infrastructure).

    Attached to EVERY react expert alongside ``load_skill`` (NOT children-gated,
    NOT part of the 5-7 curated domain-tool budget). Register an existing workspace
    file by ``path``, or author a deliverable in-context and pass it as ``content``
    (it lands as a workspace file + record). Batch via ``artifacts=[{...}, ...]``.
    Returns the typed record on acceptance or a typed rejection the model can react
    to. The harness computes every hash — any ``sha256`` in the args is ignored.
    """
    from clio_agent.gact import context as _ctx  # noqa: PLC0415
    from clio_agent.gact.agents.tool_instrumentation import native_tool  # noqa: PLC0415

    agent_id = str(getattr(agent_def, "id", "") or "")

    def create_artifact(
        name: str = "",
        kind: str = "",
        path: str = "",
        content: str = "",
        annotation: str = "",
        artifacts: Optional[list[dict[str, Any]]] = None,
        used: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        """Register a session output as an artifact (model contract lives in the dspy ``desc``).

        ``path`` registers an EXISTING file; ``content`` authors a NEW file WRITTEN AT
        ``name`` — the target path (workspace-relative or absolute), not a display label.

        ``used`` (#1191, OPTIONAL): cites the inputs this deliverable was DERIVED
        FROM (paths and/or artifact ids). NOT threaded into the promotion below —
        the mint decision is unaffected. The tool-observer transform seam
        (``declared_used_edges.detect_declared_used_edges``, fired AFTER this call
        returns) reads it from this call's own args and records real ``used`` PROV
        edges on the producing activity; an unresolvable ref is typed, never
        fabricated; omitted/blank leaves the mint exactly as it is today.
        """
        app = _ctx.active_app()
        sid = _ctx.active_session_id()
        if app is None or not sid:
            return {
                "artifacts": [],
                "created": 0,
                "deduplicated": 0,
                "rejected": 0,
                "error": "create_artifact called outside an active session",
            }
        from clio_agent.gact.artifacts.minting import _session_workspace_id  # noqa: PLC0415

        workspace_id = _session_workspace_id(app, sid)
        proposals = parse_proposals(
            name=name,
            kind=kind,
            path=path,
            content=content,
            annotation=annotation,
            artifacts=artifacts,
        )
        return promote_proposals(
            app,
            sid,
            proposals,
            workspace_id=workspace_id,
            turn_id=_ctx.active_turn_id(),
            trace_id=_ctx.active_trace_id(),
            agent_id=agent_id,
        )

    # Declared "chip": normal tool_call/tool_result parts PLUS its resource_link
    # chip, appended at turn finalize — adornment, never a call-row replacement.
    return native_tool(
        create_artifact,
        name="create_artifact",
        title="Create Artifact",
        representation="chip",
        desc=(
            "Designate a deliverable as a first-class artifact — YOU decide what is "
            "worth keeping (a report, a document you wrote, a generated file). "
            "Register an existing workspace file with path=<workspace path>, OR "
            "author content in this turn and pass it as content=<text> with "
            "name=<target path> — the file is WRITTEN AT name (workspace-relative "
            "or absolute; directories kept, e.g. '.clio/plans/my-plan.md'), so when "
            "a specific destination is required, name must be that full path, not a "
            "bare filename. kind is one of "
            "dataset|image|report|script|config|model|ui_payload|other. Put your "
            "intent (why it matters, deliverable vs scratch) in annotation. To "
            "designate several at once pass artifacts=[{name,kind,path|content,"
            "annotation}, ...]. OPTIONAL: cite what this deliverable was DERIVED "
            "FROM via used=[...] (paths and/or artifact ids) so its lineage graph "
            "shows its real inputs. Returns each record on acceptance, or a typed rejection reason "
            "(path_missing, escapes_root, over_cap, invalid_kind, missing_input) you "
            "can correct and retry. Nothing is auto-registered; the artifact exists "
            "only because you called this."
        ),
        args={
            "name": {
                "type": "string",
                "description": (
                    "Target path the inline content is written to (workspace-relative "
                    "or absolute; directories kept); required for inline content."
                ),
            },
            "kind": {
                "type": "string",
                "description": "One of dataset|image|report|script|config|model|ui_payload|other.",
            },
            "path": {
                "type": "string",
                "description": "Existing workspace file to register (mutually exclusive with content).",
            },
            "content": {
                "type": "string",
                "description": "Inline content to write as a workspace file, then register.",
            },
            "annotation": {
                "type": "string",
                "description": "Your intent/why (quarantined; never trusted as evidence).",
            },
            "artifacts": {
                "type": "array",
                "description": "Batch: a list of {name,kind,path|content,annotation} proposals.",
            },
            "used": {
                "type": "array",
                "description": (
                    "OPTIONAL: workspace paths and/or artifact ids this deliverable was "
                    "derived from. Recorded as real provenance edges on this mint; an "
                    "unresolvable ref is typed on the trace, never silently dropped."
                ),
            },
        },
    )


__all__ = [
    "PROPOSED_ARTIFACT_EVENT",
    "Proposal",
    "ProposalOutcome",
    "RejectionReason",
    "build_create_artifact_tool",
    "parse_proposals",
    "promote_proposal",
    "promote_proposals",
    "proposal_count",
    "proposals_batch_max",
    "proposals_per_turn",
    "validate_kind",
]
