"""Side-effecting helpers behind ``create_artifact`` promotion (owner module).

Split out of :mod:`clio_agent.gact.artifacts.proposals` (no-accretion ground rule)
so the pure validation/orchestration in ``promote_proposal`` stays legible. This
module owns the two external effects a promotion performs and the guards around
them:

* :func:`_emit_proposal_event` — the durable ``artifact.proposed`` trace event
  emitted for EVERY outcome (accepted / deduplicated / rejected) so no proposal
  decision is silently dropped;
* :func:`_write_inline_content` — writing model-authored inline bytes as a
  workspace file through the policy-checked writer, and
* :func:`_gate_content_write` — the unified write discipline that a content write
  must clear first (mode contract, overwrite guard, permission policy + audit),
  so the native tool honors the same contract every other write path does
  (finding [2]).

Direction of dependency: this module imports only leaf helpers at load time
(registry / minting / records); the ``proposals``-owned types (``RejectionReason``,
``ProposalOutcome``, ``_rejected``) are imported lazily inside the functions so the
two modules do not form an import cycle.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from clio_agent.gact.artifacts.minting import _contained
from clio_agent.gact.artifacts.records import IdentityEvidence, Mechanism
from clio_agent.gact.artifacts.registry import get_registry

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.artifacts.proposals import Proposal, ProposalOutcome

logger = logging.getLogger(__name__)

#: The proposal event type — trace-visible, deliberately OFF the SSE UI wire
#: (parity with ``artifact.proposed`` for file diffs, #968). Every proposal
#: outcome — accepted, deduplicated, or rejected — emits one so no decision is
#: silently dropped (no-silent-fallback ground rule).
PROPOSED_ARTIFACT_EVENT = "artifact.proposed"


def _emit_proposal_event(
    app: "FastAPI",
    sid: str,
    *,
    turn_id: str,
    trace_id: str,
    agent_id: str,
    outcome: "ProposalOutcome",
    proposal: "Proposal",
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
                # ``stage`` discriminates this designation event from the S2
                # file-diff ``artifact.proposed`` event, which shares the type
                # string but carries a disjoint payload (path/unified_diff/...).
                # Finding [3]: a type-filtering consumer keys on ``stage`` (or the
                # file-diff shape's own keys) so the two producers coexist; the S2
                # byte-parity contract is untouched (that payload gains nothing).
                "stage": "designation",
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


def _session_for(app: "FastAPI", sid: str) -> Any:
    """Resolve the driving session object for ``sid`` (``None`` when unresolved)."""
    store = getattr(app.state, "sessions", None) or getattr(app.state, "session_store", None)
    if store is None:
        return None
    try:
        return store.get(sid)
    except Exception:  # noqa: BLE001 — an unresolvable session gates fail-safe
        return None


def _own_registered_target(app: "FastAPI", workspace_id: str, name: str, target: Path) -> bool:
    """Whether ``target`` is the current on-disk file of a registered artifact.

    ``True`` only when a registered ``(workspace_id, name)`` artifact's head version
    points at the SAME resolved path — re-versioning your own artifact is a
    legitimate overwrite (mint dedup/version semantics apply, owner decision [2a]).
    Any OTHER existing file at ``target`` (unregistered user data) is never clobbered.
    """
    record = get_registry(app).get(workspace_id, name)
    head = record.head if record is not None else None
    if head is None or not head.path:
        return False
    try:
        return Path(head.path).expanduser().resolve(strict=False) == target
    except OSError:
        return False


def _gate_content_write(
    app: "FastAPI",
    sid: str,
    proposal: "Proposal",
    root: Path,
    *,
    workspace_id: str,
) -> Optional["ProposalOutcome"]:
    """Unify the write discipline for the inline-content channel (finding [2]).

    Inline content is a real workspace write, so it honors the SAME contract every
    other write path does — instead of the ungated ``write_text`` it previously ran:

    * (a) OVERWRITE: an existing file at the target is never clobbered UNLESS it is
      the current on-disk file of an already-registered artifact of the same
      ``(workspace, name)`` (re-versioning your own artifact); otherwise typed
      ``would_overwrite`` (the model can pick another name — bounded repair).
    * (c) POLICY + MODE: consult the same resolver the diffs/apply path and the live
      tool gate use (``_policy_action_for_tool``, passing the session ``mode``) and
      land a resolved permission audit row — so allow/deny/ask policies AND the
      built-in plan_acl rules apply to this native tool exactly as to bridge tools.
      P1.1 #1063: the read-only mode contract for plan/architect is no longer a
      private ``session.mode`` predicate here; it rides that ONE resolver (a content
      write is denied typed ``policy_denied`` in plan/architect via the @40 rule).

    Returns a typed rejection outcome to short-circuit the write, or ``None`` to
    proceed. Path-only proposals (registering an EXISTING file) never reach here —
    they stay non-destructive.
    """
    from clio_agent.gact.artifacts.proposals import RejectionReason, _rejected  # noqa: PLC0415
    from clio_agent.gact.permission_gate import (  # noqa: PLC0415
        _policy_action_for_tool,
        _record_resolved_permission,
    )

    session = _session_for(app, sid)

    # (a) Overwrite guard — never silently clobber a non-owned existing file.
    if proposal.name:
        target = (root / proposal.name).resolve(strict=False)
        if target.exists() and not _own_registered_target(app, workspace_id, proposal.name, target):
            return _rejected(
                proposal.name,
                RejectionReason.WOULD_OVERWRITE,
                f"{proposal.name!r} already exists and is not a registered artifact you "
                "can re-version; choose another name",
            )

    # (c) Policy + mode consult + audit row (the same resolver the diffs/apply write and the live
    # tool gate use). Passing ``mode`` folds the built-in plan_acl rules in, so plan/architect deny
    # a content write here through the SAME path — no separate mode predicate.
    args = {"name": proposal.name, "content_bytes": len(proposal.content)}
    action = _policy_action_for_tool(
        app,
        session_id=sid,
        session=session,
        tool_name="create_artifact",
        args=args,
        mode=str(getattr(session, "mode", "") or ""),
    )
    if action == "deny":
        _record_resolved_permission(
            app,
            session_id=sid,
            tool_name="create_artifact",
            args=args,
            status="auto_denied",
            action="deny",
            summary=f"create_artifact content write {proposal.name!r} blocked by permission policy",
            reason="policy_deny",
        )
        return _rejected(
            proposal.name,
            RejectionReason.POLICY_DENIED,
            "a permission policy denied create_artifact",
        )
    if action == "ask":
        _record_resolved_permission(
            app,
            session_id=sid,
            tool_name="create_artifact",
            args=args,
            status="auto_denied",
            action="ask",
            summary=f"create_artifact content write {proposal.name!r} requires approval",
            reason="policy_ask",
        )
        return _rejected(
            proposal.name,
            RejectionReason.PERMISSION_REQUIRED,
            "create_artifact content write requires approval (policy=ask); "
            "no inline approver on the native tool path",
        )
    _record_resolved_permission(
        app,
        session_id=sid,
        tool_name="create_artifact",
        args=args,
        status="auto_approved",
        action="allow",
        summary=f"create_artifact: write {len(proposal.content)} bytes as {proposal.name!r}",
        reason=f"policy_{action}" if action else "content_write",
    )
    return None


def _write_inline_content(
    proposal: "Proposal", root: Path
) -> tuple[Optional[Path], Optional[IdentityEvidence], Optional["ProposalOutcome"]]:
    """Write inline ``content`` as a workspace file; return (path, evidence, reject).

    The target is ``root/name`` (name may nest); it MUST resolve inside the
    workspace root (owner decision 10) before any write. The policy-checked writer
    (mechanism ``harness``) returns the on-disk sha256 — the harness hash, used
    directly as ``hashed-at-use`` evidence. Returns a typed rejection outcome
    (third slot) on containment or policy failure; never raises.
    """
    from clio_agent.gact.artifacts.proposals import RejectionReason, _rejected  # noqa: PLC0415
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


__all__ = [
    "PROPOSED_ARTIFACT_EVENT",
    "_emit_proposal_event",
    "_gate_content_write",
    "_own_registered_target",
    "_session_for",
    "_write_inline_content",
]
