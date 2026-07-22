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
CAS ingestion of inline bytes is S6 — this slice writes inline content as a normal
workspace file (custody ``workspace-referenced``) via the policy-checked writer.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from clio_agent import conf
from clio_agent.gact.artifacts.minting import (
    _contained,
    _workspace_root,
    artifact_name_for_path,
    compute_identity,
    mint_artifact,
)
from clio_agent.gact.artifacts.records import (
    RESERVED_KINDS,
    ArtifactKind,
    ArtifactVersion,
    Custody,
    IdentityEvidence,
    Mechanism,
)
from clio_agent.gact.artifacts.registry import get_registry

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

#: Bound on the per-turn counter map so a pathological long-lived process cannot
#: grow it unboundedly (turn ids accrete). Evicts oldest insertion first.
_COUNTER_MAP_CAP = 4096
_COUNTER_LOCK = threading.Lock()

#: The proposal event type — trace-visible, deliberately OFF the SSE UI wire
#: (parity with ``artifact.proposed`` for file diffs, #968). Every proposal
#: outcome — accepted, deduplicated, or rejected — emits one so no decision is
#: silently dropped (no-silent-fallback ground rule).
PROPOSED_ARTIFACT_EVENT = "artifact.proposed"


def proposals_per_turn() -> int:
    """Resolve the per-turn promotion cap from config (file → env → default)."""
    return conf.resolve(
        "artifacts.proposals_per_turn",
        env="CLIO_ARTIFACTS_PROPOSALS_PER_TURN",
        default=_DEFAULT_PROPOSALS_PER_TURN,
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
    WRITE_FAILED = "write_failed"  # inline content could not be written under policy


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
        return {
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


def _emit_proposal_event(
    app: "FastAPI",
    sid: str,
    *,
    turn_id: str,
    trace_id: str,
    agent_id: str,
    outcome: ProposalOutcome,
    proposal: Proposal,
    source: str,
) -> None:
    """Emit one ``artifact.proposed`` event for a proposal outcome (trace-only).

    Every outcome — accepted / deduplicated / rejected — is recorded so no
    proposal decision is silently dropped. Guarded: capture must never break a
    tool call that already ran.
    """
    try:
        from clio_agent.gact.runtime.globals import _emit_semantic_event  # noqa: PLC0415

        ver = outcome.version
        status = "completed" if outcome.accepted else "rejected"
        summary = (
            f"{agent_id or 'agent'} proposed artifact {outcome.name!r}: "
            + ("created" if outcome.created else "already registered")
            if outcome.accepted
            else f"{agent_id or 'agent'} artifact proposal {outcome.name!r} rejected: {outcome.reason}"
        )
        _emit_semantic_event(
            app,
            sid,
            PROPOSED_ARTIFACT_EVENT,
            turn_id=turn_id,
            trace_id=trace_id,
            status=status,
            summary=summary,
            actor={"agent_id": agent_id, "mechanism": Mechanism.MODEL.value},
            subject={
                "artifact_id": ver.artifact_id if ver is not None else "",
                "name": outcome.name,
                "workspace_id": outcome.workspace_id,
            },
            payload={
                "designation": "agent-proposed",
                "source": source,
                "name": outcome.name,
                "kind": proposal.kind,
                "annotation": proposal.annotation,
                "accepted": outcome.accepted,
                "created": outcome.created,
                "reason": outcome.reason,
                "detail": outcome.detail,
                "artifact_id": ver.artifact_id if ver is not None else "",
                "sha256": ver.sha256 if ver is not None else None,
                "version": ver.version if ver is not None else 0,
            },
        )
    except Exception:  # noqa: BLE001 — capture never breaks a tool call
        logger.warning(
            "artifact proposal event skipped reason=proposal_event_failed session=%s name=%s",
            sid,
            outcome.name,
        )


def _write_inline_content(
    proposal: Proposal, root: Path
) -> tuple[Optional[Path], Optional[IdentityEvidence], Optional[ProposalOutcome]]:
    """Write inline ``content`` as a workspace file; return (path, evidence, reject).

    The target is ``root/name`` (name may nest); it MUST resolve inside the
    workspace root (owner decision 10) before any write. The policy-checked writer
    (mechanism ``harness``) returns the on-disk sha256 — the harness hash, used
    directly as ``hashed-at-use`` evidence. Returns a typed rejection outcome
    (third slot) on containment or policy failure; never raises.
    """
    from clio_agent.tools.file_policy import FilePolicyError  # noqa: PLC0415
    from clio_agent.tools.fs_write import write_text_with_policy  # noqa: PLC0415

    if not proposal.name:
        return (
            None,
            None,
            _rejected(
                proposal.name, RejectionReason.MISSING_INPUT, "inline content requires a name"
            ),
        )
    target = (root / proposal.name).resolve(strict=False)
    if not _contained(target, root):
        return (
            None,
            None,
            _rejected(
                proposal.name,
                RejectionReason.ESCAPES_ROOT,
                f"{proposal.name!r} escapes the workspace",
            ),
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = write_text_with_policy(str(target), proposal.content)
    except (FilePolicyError, OSError, ValueError) as exc:
        return (
            None,
            None,
            _rejected(proposal.name, RejectionReason.WRITE_FAILED, f"write refused: {exc}"),
        )
    evidence = IdentityEvidence.hashed_at_use(
        sha256=str(result.get("sha256") or ""),
        size_bytes=int(result.get("size_bytes") or 0),
    )
    return target, evidence, None


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
    if proposal.content:
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
        name = proposal.name or artifact_name_for_path(target)
        path = target
    elif proposal.path:
        path = Path(proposal.path)
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
        name = proposal.name or artifact_name_for_path(path)
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
            evidence = compute_identity(path)
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
        outcome = ProposalOutcome(
            accepted=True,
            name=name,
            reason="already_registered",
            created=False,
            version=existing_version,
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

    version = mint_artifact(
        app,
        sid,
        name=name,
        workspace_id=workspace_id,
        evidence=evidence,
        kind=kind,
        mechanism=Mechanism.MODEL,
        producer={
            "designation": "agent-proposed",
            "session_id": sid,
            "turn_id": turn_id,
            "agent_id": agent_id,
        },
        custody=Custody.WORKSPACE_REFERENCED,
        path=str(path),
        annotation=proposal.annotation,
        turn_id=turn_id,
        trace_id=trace_id,
    )
    if version is None:
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
    _increment_proposal_count(app, sid, turn_id)
    outcome = ProposalOutcome(
        accepted=True,
        name=name,
        created=True,
        version=version,
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
    the rest — the model sees exactly which succeeded). The summary counts make the
    turn's designation footprint legible for bounded repair.
    """
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
    accepted = sum(1 for o in outcomes if o.accepted and o.created)
    deduplicated = sum(1 for o in outcomes if o.accepted and not o.created)
    rejected = sum(1 for o in outcomes if not o.accepted)
    return {
        "artifacts": [o.to_wire() for o in outcomes],
        "accepted": accepted,
        "deduplicated": deduplicated,
        "rejected": rejected,
    }


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
    import dspy  # noqa: PLC0415

    from clio_agent.gact import context as _ctx  # noqa: PLC0415

    agent_id = str(getattr(agent_def, "id", "") or "")

    def create_artifact(
        name: str = "",
        kind: str = "",
        path: str = "",
        content: str = "",
        annotation: str = "",
        artifacts: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        app = _ctx.active_app()
        sid = _ctx.active_session_id()
        if app is None or not sid:
            return {
                "artifacts": [],
                "accepted": 0,
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

    return dspy.Tool(
        func=create_artifact,
        name="create_artifact",
        desc=(
            "Designate a deliverable as a first-class artifact — YOU decide what is "
            "worth keeping (a report, a document you wrote, a generated file). "
            "Register an existing workspace file with path=<workspace path>, OR "
            "author content in this turn and pass it as content=<text> with a "
            "name=<filename> (it is saved as a workspace file). kind is one of "
            "dataset|image|report|script|config|model|ui_payload|other. Put your "
            "intent (why it matters, deliverable vs scratch) in annotation. To "
            "designate several at once pass artifacts=[{name,kind,path|content,"
            "annotation}, ...]. Returns each record on acceptance, or a typed "
            "rejection reason (path_missing, escapes_root, over_cap, invalid_kind, "
            "missing_input) you can correct and retry. Nothing is auto-registered; "
            "the artifact exists only because you called this."
        ),
        args={
            "name": {
                "type": "string",
                "description": "Artifact name (filename); required for inline content.",
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
    "proposals_per_turn",
    "validate_kind",
]
