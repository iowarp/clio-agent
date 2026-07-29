"""Save-and-reuse of approved plans (P1.6c #1068, campaign #1057).

Owner module for the REGISTER and REUSE halves of save-and-reuse; the GENERALIZE half (the pure
:func:`clio_agent.gact.planning.playbook_from_saved_plan` derive) lives in the ``planning`` owner
module. Composes with the artifacts provenance substrate (#966) — there is no new store.

**REGISTER (:func:`save_approved_plan`).** When a plan-mode session is APPROVED via ``plan_exit``,
the approved plan file (already on disk under ``plan_acl.plans_dir()``) is registered as a
provenance-tracked artifact through the ONE :func:`~clio_agent.gact.artifacts.proposals.promote_proposal`
path — the same funnel ``create_artifact`` uses (harness-hashed identity, content dedup, version
chain, producer provenance). The ``plan`` :class:`~clio_agent.gact.artifacts.records.ArtifactKind`
is RESERVED (nothing may mint it — a typed guard at the mint boundary), so a plan DOCUMENT rides the
existing ``report`` kind (a markdown deliverable). Registration is a TYPED, NON-SILENT step: success
records the artifact ref on ``session.metadata`` (:data:`SAVED_PLAN_METADATA_KEY`, no fifth store)
and emits a ``plan.saved`` semantic event; a failure records a typed DEGRADE reason on the same key
AND emits the same event with ``status=failed`` — it NEVER blocks the plan-exit resume (a degraded
save is reported, never fatal — the no-silent-fallback ground rule).

**REUSE (:func:`resolve_saved_plan_playbook` / :func:`record_plan_playbook`).** A saved plan
artifact can be turned back into a reusable operator PLAYBOOK (the P1.6b shape): its content is read
from the registry and derived into a Playbook skeleton. A skill declares its playbook BY REFERENCE
to a saved plan artifact via a top-level ``playbook_from_plan: <artifact name>`` frontmatter key
(sibling to the P1.6b inline ``playbook:`` list); :func:`record_plan_playbook` is the single seam
``skill_effects`` calls for BOTH forms — inline (delegates to :func:`planning.record_effect_playbook`
unchanged) or by-reference (resolve + record). A dangling reference is a typed
:class:`PlanReuseError` (``reason="dangling_plan_ref"``), never a silent drop.

Deterministic replay scope: the artifact IS the replayable record (content-addressed,
version-chained via #966); reuse = re-entering plan mode with the derived skeleton. This module does
NOT autonomously re-execute a plan.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from clio_agent.gact import planning
from clio_agent.gact.artifacts.minting import (
    _contained,
    _session_workspace_id,
    _workspace_root,
)
from clio_agent.gact.artifacts.proposals import Proposal, ProposalOutcome, promote_proposal
from clio_agent.gact.artifacts.registry import get_registry
from clio_agent.runtime import trace

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.planning import Playbook
    from clio_agent.gact.skills import SkillRef

#: ``session.metadata`` key carrying the saved-plan artifact ref (success) OR the typed degrade
#: record (failure). No fifth store — rides the session record like ``plan_variant`` / the playbook.
SAVED_PLAN_METADATA_KEY = "saved_plan_artifact"

#: The artifact kind a saved plan document is registered under. ``plan`` is RESERVED
#: (:data:`~clio_agent.gact.artifacts.records.RESERVED_KINDS` — the mint funnel rejects it), so a
#: plan markdown rides the existing ``report`` kind rather than un-reserving a campaign-reserved kind.
SAVED_PLAN_KIND = "report"

#: The top-level skill frontmatter key declaring a playbook BY REFERENCE to a saved plan artifact
#: (sibling to the P1.6b inline ``playbook:`` key). Its value is the saved plan artifact's name.
# Owned by ``planning`` (the parse/placement owner, P1.6c) — re-exported here for resolvers.
PLAYBOOK_FROM_PLAN_META_KEY = planning.PLAYBOOK_FROM_PLAN_META_KEY
validate_plan_ref_placement = planning.validate_plan_ref_placement

#: The quarantined model-facing annotation stamped on the registered plan artifact.
_SAVED_PLAN_ANNOTATION = "approved plan saved for reuse (P1.6c)"


class PlanReuseError(RuntimeError):
    """A saved-plan reuse operation could not be completed (typed reason).

    Carries a machine-readable ``reason`` (``missing_plan_ref`` / ``dangling_plan_ref`` /
    ``plan_content_unreadable`` / a propagated :class:`~clio_agent.gact.planning.PlaybookError`
    reason such as ``unstructured_plan``) so callers/audit branch without string-matching.
    """

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


# --------------------------------------------------------------------------- #
# REGISTER — save an approved plan as a provenance-tracked artifact.
# --------------------------------------------------------------------------- #


def _rejected_outcome(reason: str, detail: str) -> ProposalOutcome:
    """A synthetic pre-promotion rejection outcome (uniform with a real promote rejection)."""

    return ProposalOutcome(accepted=False, reason=reason, detail=detail)


def _register_plan_artifact(app: "FastAPI", sid: str, plan_path: str) -> ProposalOutcome:
    """Register the plan file through the ONE ``promote_proposal`` path (typed outcome).

    Registers by PATH when the plan file resolves inside the bound workspace root (non-destructive —
    the on-disk file is hashed/ingested in place); otherwise reads its bytes and registers via the
    inline ``content`` channel (the plans dir may live outside the workspace). Pre-promotion guards
    (missing file / unresolvable workspace) return a typed rejection outcome, never raise.
    """

    if not plan_path or not Path(plan_path).is_file():
        return _rejected_outcome("plan_file_missing", f"no plan file on disk at {plan_path!r}")
    workspace_id = _session_workspace_id(app, sid)
    if not workspace_id:
        return _rejected_outcome("workspace_unresolved", "no workspace bound to the session")
    name = Path(plan_path).name
    root = _workspace_root(app, workspace_id)
    if root is not None and _contained(Path(plan_path), root):
        proposal = Proposal(
            name=name, kind=SAVED_PLAN_KIND, path=plan_path, annotation=_SAVED_PLAN_ANNOTATION
        )
    else:
        try:
            content = Path(plan_path).read_text(encoding="utf-8")
        except OSError as exc:
            return _rejected_outcome("plan_content_unreadable", str(exc))
        proposal = Proposal(
            name=name, kind=SAVED_PLAN_KIND, content=content, annotation=_SAVED_PLAN_ANNOTATION
        )
    return promote_proposal(app, sid, proposal, workspace_id=workspace_id)


def save_approved_plan(app: "FastAPI", sid: str, *, plan_file: str) -> dict[str, Any]:
    """Register an APPROVED plan file as a provenance-tracked artifact (P1.6c #1068).

    Called from ``plan_mode.resolve_plan_exit_answer`` on every APPROVE decision. The whole
    registration is guarded: any failure (a typed rejection from ``promote_proposal`` OR an
    exception — e.g. the registry fold refusing on the event loop) degrades to a typed reason
    recorded on ``session.metadata`` and emitted on the semantic highway; it NEVER raises, so a
    degraded save cannot block the plan-exit resume (the plan still executes).

    Returns:
        The recorded ref dict (``saved=True`` + artifact identity) on success, or the typed degrade
        dict (``saved=False`` + ``reason``) on failure — the same value written to
        ``session.metadata[SAVED_PLAN_METADATA_KEY]``.
    """

    plan_path = str(plan_file or "").strip()
    try:
        outcome = _register_plan_artifact(app, sid, plan_path)
    except Exception as exc:  # noqa: BLE001 — a degraded save must never block the plan-exit resume
        return _record_degrade(
            app, sid, plan_path, reason="save_failed_exception", detail=repr(exc)
        )
    if not outcome.accepted or outcome.version is None:
        return _record_degrade(
            app, sid, plan_path, reason=(outcome.reason or "save_rejected"), detail=outcome.detail
        )
    return _record_saved(app, sid, plan_path, outcome)


def _record_saved(
    app: "FastAPI", sid: str, plan_path: str, outcome: ProposalOutcome
) -> dict[str, Any]:
    """Record a successful save: the artifact ref on ``session.metadata`` + a typed highway event."""

    ver = outcome.version
    assert ver is not None  # narrowed by the caller
    ref: dict[str, Any] = {
        "saved": True,
        "artifact_id": ver.artifact_id,
        "sha256": ver.sha256,
        "version": ver.version,
        "name": outcome.name,
        "workspace_id": outcome.workspace_id,
        "kind": ver.kind.value,
        "plan_file": plan_path,
    }
    app.state.sessions.update(sid, metadata_patch={SAVED_PLAN_METADATA_KEY: ref})
    _emit_plan_saved(
        app,
        sid,
        status="completed",
        summary=f"saved approved plan {outcome.name!r} as artifact {ver.artifact_id} v{ver.version}",
        payload=ref,
    )
    trace.event(
        "PLAN",
        "saved approved plan as artifact %s v%d (%s) for %s",
        ver.artifact_id,
        ver.version,
        outcome.name,
        sid,
    )
    return ref


def _record_degrade(
    app: "FastAPI", sid: str, plan_path: str, *, reason: str, detail: str = ""
) -> dict[str, Any]:
    """Record a DEGRADED save: a typed reason on ``session.metadata`` + a typed highway event.

    The no-silent-fallback obligation: the reason reaches BOTH the session record (queryable
    projection) and the semantic highway (the served/trace projection). Never raises.
    """

    ref: dict[str, Any] = {
        "saved": False,
        "reason": reason,
        "detail": detail,
        "plan_file": plan_path,
    }
    app.state.sessions.update(sid, metadata_patch={SAVED_PLAN_METADATA_KEY: ref})
    _emit_plan_saved(
        app, sid, status="failed", summary=f"plan save degraded: {reason}", payload=ref
    )
    trace.event("PLAN", "plan save degraded reason=%s detail=%s session=%s", reason, detail, sid)
    return ref


def _emit_plan_saved(
    app: "FastAPI", sid: str, *, status: str, summary: str, payload: dict[str, Any]
) -> None:
    """Emit the ``plan.saved`` semantic event (best-effort; capture never breaks the resume)."""

    try:
        from clio_agent.gact.runtime.globals import (  # noqa: PLC0415
            _active_semantic_trace_id,
            _active_semantic_turn_id,
            _emit_semantic_event,
        )

        _emit_semantic_event(
            app,
            sid,
            "plan.saved",
            turn_id=_active_semantic_turn_id(),
            trace_id=_active_semantic_trace_id(),
            status=status,
            summary=summary,
            actor={"role": "harness"},
            subject={
                "artifact_id": payload.get("artifact_id", ""),
                "name": payload.get("name", ""),
            },
            payload=payload,
        )
    except Exception as exc:  # noqa: BLE001 — capture never breaks a performed save
        trace.event("PLAN", "plan.saved emit failed for %s: %s", sid, exc)


# --------------------------------------------------------------------------- #
# REUSE — resolve a saved plan artifact into a reusable playbook.
# --------------------------------------------------------------------------- #


def resolve_saved_plan_playbook(
    app: "FastAPI", workspace_id: str, plan_ref: str, *, name: str = ""
) -> "Playbook":
    """Resolve a saved-plan artifact reference into a derived :class:`Playbook` (P1.6c #1068).

    Looks the artifact up in the registry by ``(workspace_id, plan_ref)``, reads its head version's
    content from disk, and derives the reusable skeleton via
    :func:`planning.playbook_from_saved_plan`. Every failure is a typed :class:`PlanReuseError`
    (never a silent ``None``): an empty ref, a missing artifact (``dangling_plan_ref``), unreadable
    content (``plan_content_unreadable``), or an unstructured plan (the propagated derive reason).
    """

    ref = str(plan_ref or "").strip()
    if not ref:
        raise PlanReuseError("no saved-plan reference given", reason="missing_plan_ref")
    record = get_registry(app).get(workspace_id, ref)
    if record is None or record.head is None:
        raise PlanReuseError(
            f"no saved plan artifact named {ref!r} in workspace {workspace_id!r}",
            reason="dangling_plan_ref",
        )
    path = str(getattr(record.head, "path", "") or "")
    if not path or not Path(path).is_file():
        raise PlanReuseError(
            f"saved plan artifact {ref!r} has no readable content on disk",
            reason="plan_content_unreadable",
        )
    try:
        content = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise PlanReuseError(
            f"saved plan artifact {ref!r} content unreadable: {exc}",
            reason="plan_content_unreadable",
        ) from exc
    try:
        return planning.playbook_from_saved_plan(content, name=name or ref)
    except planning.PlaybookError as exc:
        raise PlanReuseError(str(exc), reason=exc.reason) from exc


def record_plan_playbook(
    app: "FastAPI",
    session_id: str,
    ref: "SkillRef",
    playbook: "Playbook | None",
    *,
    default_name: str,
) -> None:
    """Record a plan-entering skill's operator playbook — inline (P1.6b) OR by reference (P1.6c).

    The single seam ``skill_effects`` calls for a plan-entering effect. An inline ``playbook``
    (already parsed, P1.6b) is recorded verbatim via :func:`planning.record_effect_playbook`
    (behaviour unchanged). Otherwise a top-level ``playbook_from_plan`` frontmatter reference is
    resolved against the session's workspace, derived into a skeleton, and recorded. When neither is
    present this is a strict no-op (a plain plan-entering skill is unchanged — the byte-identical
    guarantee). A dangling reference raises a typed :class:`PlanReuseError` (never a silent drop).
    """

    if playbook is not None:
        planning.record_effect_playbook(app, session_id, playbook, default_name=default_name)
        return
    meta = getattr(ref, "meta", None)
    plan_ref = ""
    if isinstance(meta, Mapping):
        plan_ref = str(meta.get(PLAYBOOK_FROM_PLAN_META_KEY) or "").strip()
    if not plan_ref:
        return
    workspace_id = _session_workspace_id(app, session_id)
    derived = resolve_saved_plan_playbook(app, workspace_id, plan_ref, name=plan_ref)
    planning.record_effect_playbook(app, session_id, derived, default_name=default_name)
    trace.event(
        "PLAN",
        "activated playbook from saved plan %r (%d steps) for %s",
        plan_ref,
        len(derived.steps),
        session_id,
    )


__all__ = [
    "PLAYBOOK_FROM_PLAN_META_KEY",
    "SAVED_PLAN_KIND",
    "SAVED_PLAN_METADATA_KEY",
    "PlanReuseError",
    "record_plan_playbook",
    "resolve_saved_plan_playbook",
    "save_approved_plan",
    "validate_plan_ref_placement",
]
