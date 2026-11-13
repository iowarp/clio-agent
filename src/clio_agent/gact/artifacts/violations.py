"""``policy_violation`` provenance — the #966 ``gap`` node's enforced-tier variant (#976/B2).

On the honest floor (#966/B1) an out-of-root write SUCCEEDS and is recorded as a ``gap``
version (mechanism ``none``, actor unknown). B2 activates OS write-fences, so the SAME
attempt now surfaces differently and MORE precisely:

* the fence DENIED the write → a typed ``policy_violation`` (``prevented``): attributed to the
  child/tool ``call_id``, the path, the call window and the DENYING mechanism — never silently
  a gap. The denial is read from the ONE sandbox-boundary diagnosis
  (:mod:`clio_agent.runtime.sandbox_diagnosis`, #1905) the confined seam attached to its
  result as ``boundary`` (the shell, ``run_code``), checked against the roots that seam gave
  its fence. A confined stdio MCP server cannot attach one, so for a call such a server
  served (``resolve_serving_child_id``) and that failed, the diagnosis is made here from the
  local paths its arguments name as writes (designated output args and the args its server
  declared, :mod:`clio_agent.tools.tool_path_contract`) with the recomputed fleet roots, at
  ``possible`` confidence, and published for the model's observation of that call. Its
  error text is never read: a server's prose may describe a remote host's denial. An
  in-process tool is not fenced, so its denial is never one;
* a designated out-of-root output path is absent post-call → ``prevented``; present and
  provably fresh → ``detected`` (the fence was escaped) — worse than prevented, and recorded
  as such.

The mint logic lives HERE (the artifacts owner package); :mod:`clio_agent.runtime.sandbox`
only exposes the resolved state. Violations are DURABLE-ONLY this slice — emitted as the
trace-only ``artifact.policy_violation`` event (registered in
``semantic_events.SSE_TRACE_ONLY_EVENT_TYPES``) and appended to a bounded per-app ledger.
The signal fires ONLY when a fence is active, so the floor never mints a violation (its
out-of-root write is an honest gap, as before).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from pydantic import BaseModel, ConfigDict

from clio_agent.runtime.sandbox_diagnosis import (
    KIND_WRITE_DENIED,
    BoundaryDiagnosis,
    diagnose_write_targets,
    hedged,
    nearest_existing_parent,
    publish_call_boundary,
    result_boundary,
    within_roots,
    write_denied_message,
)

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.runtime.sandbox import SandboxResult

logger = logging.getLogger(__name__)

POLICY_VIOLATION_EVENT = "artifact.policy_violation"

#: Violation kinds (owner decision #974 / #966 gap-variant).
VIOLATION_PREVENTED = "prevented"  # the fence blocked the out-of-root write
VIOLATION_DETECTED = "detected"  # the fence was escaped — a change happened anyway

#: Bounded ledger cap (a pathological session cannot grow it unboundedly).
_LEDGER_MAX = 256


class PolicyViolation(BaseModel):
    """One attributed policy violation (the enforced-tier ``gap`` variant, B2).

    Immutable value: the harness builds it from OS reality (an errno / a post-call stat),
    never the model. ``mechanism`` is the DENYING fence (``srt_bwrap`` / ``landlock`` / ...).
    """

    model_config = ConfigDict(frozen=True)

    kind: str  # VIOLATION_PREVENTED | VIOLATION_DETECTED
    mechanism: str  # the denying/escaped fence mechanism (sandbox SandboxResult.mechanism)
    path: str = ""  # the out-of-root path (best-effort; "" when unextractable)
    call_id: str = ""
    tool: str = ""
    session_id: str = ""
    turn_id: str = ""
    workspace_id: str = ""
    errno_name: str = ""  # the diagnosis' denial signature (EROFS / EACCES / ...); "" otherwise
    signal: str = (
        ""  # how it was detected: "result_errno" | "designated_path_absent" | "detected_change"
    )
    started_at: str = ""  # the call window (start / end ISO)
    ended_at: str = ""
    detail: str = ""  # a bounded evidence snippet (e.g. the stderr line)
    next_action: str = ""  # the diagnosis' message: the folder and the retry the MODEL may make

    def to_payload(self) -> dict[str, Any]:
        """The durable ``artifact.policy_violation`` payload."""
        return self.model_dump()


# --------------------------------------------------------------------------- #
# The observer seam entry: detect + mint violations for one tool call.         #
# --------------------------------------------------------------------------- #


def observe_policy_violations(
    app: "FastAPI",
    sid: str,
    *,
    tool_name: str,
    args: dict[str, Any],
    call_id: str,
    result: Any,
    workspace_id: str,
    turn_id: str = "",
    trace_id: str = "",
    started_at: Optional[float] = None,
    state: Optional["SandboxResult"] = None,
) -> list[PolicyViolation]:
    """Detect + mint policy violations for one tool call (B2). Guarded, one-line from the observer.

    A NO-OP unless an OS write-fence is ACTIVE (on the floor an out-of-root write succeeds and
    is an honest ``gap``, handled by the existing mint path — never a violation).

    PRECISION OVER RECALL (#966.10 — a false attribution is worse than a missed one). Both
    signals require the denied path to be PROVEN out-of-root before a mint:

    * the ``boundary`` diagnosis its confined seam attached (the shell, ``run_code``) names write-denied
      ``paths`` outside the territory → one ``prevented`` per path, carrying the diagnosis'
      signature and message. A diagnosis with no path (a bare "permission denied", SSH
      publickey) or for elevation is a typed debug skip (``violation_signal_unattributed``),
      never a fabricated out-of-root violation; an in-root denial has no diagnosis at all;
    * a designated output path, or a path the tool's server declared as a local write
      (:func:`local_write_targets`), OUTSIDE the territory — absent post-call → ``prevented``;
      present AND provably fresh within the call window → ``detected``. Without a call window
      (``started_at is None``) a PRESENT file is NEVER upgraded to ``detected`` (F5) — a typed
      skip, since escape cannot be proven.
    """
    resolved = state if state is not None else _current_sandbox_state()
    if resolved is None or not resolved.active:
        return []
    mechanism = str(resolved.mechanism)
    from clio_agent.gact.artifacts.designation import result_declared_paths  # noqa: PLC0415
    from clio_agent.gact.artifacts.minting import _workspace_root  # noqa: PLC0415

    boundary = result_boundary(result)
    targets = local_write_targets(tool_name, args, lambda: _workspace_root(app, workspace_id))
    if boundary is None:  # published for the model; its paths are the designated ones below
        _served_call_boundary(
            app,
            call_id=call_id,
            targets=targets,
            result=result,
            workspace_id=workspace_id,
            mechanism=mechanism,
        )
    designated = {**targets, **result_declared_paths(result)}
    if boundary is None and not designated:
        # Nothing to place against the write territory: resolving it (every root through
        # the filesystem) was most of the observer's own time on a read-only call burst.
        return []
    started_iso = _iso(started_at)
    ended_iso = _iso(time.time())
    violations: list[PolicyViolation] = []

    if boundary is not None and boundary.kind == KIND_WRITE_DENIED and boundary.paths:
        violations.extend(
            PolicyViolation(
                kind=VIOLATION_PREVENTED,
                mechanism=boundary.mechanism or mechanism,
                path=path,
                call_id=call_id,
                tool=tool_name,
                session_id=sid,
                turn_id=turn_id,
                workspace_id=workspace_id,
                errno_name=boundary.signature,
                signal="result_errno",
                started_at=started_iso,
                ended_at=ended_iso,
                next_action=boundary.message,
            )
            for path in boundary.paths
        )
    elif boundary is not None:
        # A path-less or elevation diagnosis: the fence did NOT prove an out-of-root WRITE,
        # so attributing one would be false provenance.
        logger.debug(
            "policy violation skipped reason=violation_signal_unattributed kind=%s "
            "signature=%s tool=%s",
            boundary.kind,
            boundary.signature,
            tool_name,
        )

    if designated:
        violations.extend(
            _designated_out_of_root_violations(
                app,
                sid,
                tool_name=tool_name,
                designated=designated,
                call_id=call_id,
                workspace_id=workspace_id,
                turn_id=turn_id,
                mechanism=mechanism,
                roots=_effective_roots(app, workspace_id),
                started_at=started_at,
                started_iso=started_iso,
                ended_iso=ended_iso,
            )
        )

    for violation in violations:
        _mint_policy_violation(app, sid, violation, turn_id=turn_id, trace_id=trace_id)
    return violations


def local_write_targets(
    tool_name: str, args: dict[str, Any], workspace_root: Callable[[], Optional[Path]]
) -> dict[str, str]:
    """The local paths a call's arguments name as writes, by argument name.

    Structure only: the designated output args (:func:`grounded_output_paths`) and the args
    the tool's server declared as ``local`` ``write`` paths
    (:mod:`clio_agent.tools.tool_path_contract`). A relative declared path is the server's
    own, resolved against the workspace root it was started in (``workspace_root`` is asked
    only then). A path declared ``remote`` or ``none`` is never a local target.
    """
    from clio_agent.gact.artifacts.designation import grounded_output_paths  # noqa: PLC0415
    from clio_agent.tools.tool_path_contract import (  # noqa: PLC0415
        ACCESS_WRITE,
        MACHINE_LOCAL,
        declared_args,
    )

    targets = dict(grounded_output_paths(args))
    for arg in declared_args(tool_name, machine=MACHINE_LOCAL, access=ACCESS_WRITE):
        value = args.get(arg)
        if not isinstance(value, str) or not value.strip():
            continue
        path = Path(value.strip()).expanduser()
        if not path.is_absolute():
            root = workspace_root()
            if root is None:
                continue
            path = root / path
        targets[arg] = str(path)
    return targets


def _served_call_boundary(
    app: "FastAPI",
    *,
    call_id: str,
    targets: dict[str, str],
    result: Any,
    workspace_id: str,
    mechanism: str,
) -> Optional[BoundaryDiagnosis]:
    """Diagnose a failed call a confined stdio MCP server served, and publish it for the
    model's observation of that call (#1905).

    ``None`` for a result that is not an error (its ``isError`` flag, never its text), or a
    call no confined child served (an in-process tool is not fenced). See
    :func:`_diagnose_served`.
    """
    if not (isinstance(result, dict) and (result.get("isError") or result.get("is_error"))):
        return None
    diagnosis = _diagnose_served(
        app, call_id=call_id, targets=targets, workspace_id=workspace_id, mechanism=mechanism
    )
    if diagnosis is not None:
        publish_call_boundary(call_id, diagnosis)
    return diagnosis


def raised_call_boundary(tool_name: str, args: Mapping[str, Any]) -> Optional[BoundaryDiagnosis]:
    """The diagnosis for the running call when it RAISED (FastMCP raises a server's error
    result, so the observer never sees it); the execution boundary notes its message on the
    error. ``None`` off a turn, on the floor, for a call no confined child served, or one
    whose arguments name no local write outside the territory.

    Never raises: it runs on the failing call's own error path, whose error must stand.
    """
    from clio_agent.gact.artifacts.minting import (  # noqa: PLC0415
        _session_workspace_id,
        _workspace_root,
    )
    from clio_agent.gact.artifacts.observer_bridge import observer_call_id  # noqa: PLC0415
    from clio_agent.gact.runtime.globals import _resolve_tool_session  # noqa: PLC0415

    app = _active_app()
    state = _current_sandbox_state()
    if app is None or state is None or not state.active:
        return None
    try:
        sid, _session = _resolve_tool_session(app)
        workspace_id = _session_workspace_id(app, sid) if sid else ""
        return _diagnose_served(
            app,
            call_id=observer_call_id(),
            targets=local_write_targets(
                tool_name, dict(args), lambda: _workspace_root(app, workspace_id)
            ),
            workspace_id=workspace_id,
            mechanism=str(state.mechanism),
        )
    except (OSError, RuntimeError, ValueError, LookupError, AttributeError, TypeError) as exc:
        logger.warning(
            "boundary diagnosis skipped reason=raised_call_boundary_failed error=%r", exc
        )
        return None


def _active_app() -> Optional["FastAPI"]:
    from clio_agent.gact import context as _ctx  # noqa: PLC0415

    return _ctx.active_app()


def _diagnose_served(
    app: "FastAPI",
    *,
    call_id: str,
    targets: dict[str, str],
    workspace_id: str,
    mechanism: str,
) -> Optional[BoundaryDiagnosis]:
    """A confined stdio MCP server's failed call, diagnosed at ``possible`` confidence.

    Both signals are structural: a confined child served the call
    (``resolve_serving_child_id``), and its arguments name local write ``targets`` outside
    the recomputed fleet territory (:func:`_effective_roots`, the roots its spawn was given,
    plus grants since). ``None`` otherwise, whatever the error says.
    """
    from clio_agent.gact.artifacts.ingest_edges import resolve_serving_child_id  # noqa: PLC0415

    if not targets or not resolve_serving_child_id(app, call_id):
        return None
    diagnosis = diagnose_write_targets(
        tuple(targets.values()),
        write_roots=_effective_roots(app, workspace_id),
        mechanism=mechanism,
        seam="tool",
    )
    return hedged(diagnosis) if diagnosis is not None else None


def _effective_roots(app: "FastAPI", workspace_id: str) -> tuple[Path, ...]:
    """The effective write territory for this call (the same boundary the fence enforces)."""
    from clio_agent.gact.artifacts.minting import _workspace_root  # noqa: PLC0415
    from clio_agent.runtime.sandbox import PROFILE_FLEET, effective_write_roots  # noqa: PLC0415

    root = _workspace_root(app, workspace_id)
    return effective_write_roots(
        PROFILE_FLEET, workspace_root=str(root) if root is not None else None
    )


def _designated_out_of_root_violations(
    app: "FastAPI",
    sid: str,
    *,
    tool_name: str,
    designated: dict[str, str],
    call_id: str,
    workspace_id: str,
    turn_id: str,
    mechanism: str,
    roots: tuple[Path, ...],
    started_at: Optional[float],
    started_iso: str,
    ended_iso: str,
) -> list[PolicyViolation]:
    """Post-call stat of designated output paths OUTSIDE the write territory (the fence's view).

    A tool that DECLARED an output path outside the effective write roots either had it
    blocked (absent → ``prevented``) or somehow wrote it anyway (present + provably fresh →
    ``detected``). A path INSIDE the territory is a normal deliverable (minted elsewhere), not
    a violation. Without a call window a present out-of-root file is NEVER called ``detected``
    (F5) — escape is unproven, so it is a typed skip.
    """
    out: list[PolicyViolation] = []
    for raw_path in designated.values():
        path = Path(raw_path)
        if within_roots(path, roots):
            continue  # inside the write territory — a normal deliverable, not a violation
        if not _safe_is_file(path):
            kind, signal = VIOLATION_PREVENTED, "designated_path_absent"
        elif started_at is not None and _changed_within_window(path, started_at):
            kind, signal = VIOLATION_DETECTED, "detected_change"
        else:
            # Present, but no proof it changed THIS call (no window, or a pre-existing file).
            # Never upgrade to a 'detected' escape without a real fresh-change signal (F5).
            logger.debug(
                "policy violation skipped reason=violation_signal_unattributed "
                "signal=designated_present_unproven path=%s",
                path,
            )
            continue
        out.append(
            PolicyViolation(
                kind=kind,
                mechanism=mechanism,
                path=str(path),
                call_id=call_id,
                tool=tool_name,
                session_id=sid,
                turn_id=turn_id,
                workspace_id=workspace_id,
                signal=signal,
                started_at=started_iso,
                ended_at=ended_iso,
                next_action=write_denied_message(
                    (nearest_existing_parent(str(path)),), seam="tool"
                ),
            )
        )
    return out


def _safe_is_file(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:
        return False


def _changed_within_window(path: Path, started_at: float) -> bool:
    """Whether ``path``'s mtime is at/after the call start (the change happened this call).

    Only called with a real ``started_at`` — a missing call window is handled by the caller
    (never minted as ``detected``, F5), so there is no "no-window ⇒ suspect" branch here.
    """
    try:
        return path.stat().st_mtime >= (started_at - 1.0)
    except OSError:
        return False


def _mint_policy_violation(
    app: "FastAPI",
    sid: str,
    violation: PolicyViolation,
    *,
    turn_id: str = "",
    trace_id: str = "",
) -> None:
    """Emit the trace-only ``artifact.policy_violation`` + append to the bounded app ledger.

    Guarded — a provenance emit must never break a turn. Durable-only this slice (the event
    type is registered trace-only; SSE listing waits for B5).
    """
    _append_ledger(app, violation)
    try:
        from clio_agent.gact.runtime.globals import _emit_semantic_event  # noqa: PLC0415

        _emit_semantic_event(
            app,
            sid,
            POLICY_VIOLATION_EVENT,
            turn_id=turn_id,
            trace_id=trace_id,
            status="failed",
            summary=(
                f"Out-of-root write {violation.kind} by fence {violation.mechanism} "
                f"(tool={violation.tool}, path={violation.path or '?'})."
            ),
            actor={"tool": violation.tool, "mechanism": "harness"},
            subject={"call_id": violation.call_id, "workspace_id": violation.workspace_id},
            payload=violation.to_payload(),
        )
    except Exception:  # noqa: BLE001 — a provenance emit must never break a turn
        logger.warning(
            "policy violation emit skipped reason=policy_violation_emit_failed session=%s call_id=%s",
            sid,
            violation.call_id,
        )


def _append_ledger(app: "FastAPI", violation: PolicyViolation) -> None:
    try:
        ledger = getattr(app.state, "artifact_policy_violations", None)
        if not isinstance(ledger, list):
            ledger = []
            app.state.artifact_policy_violations = ledger
        ledger.append(violation.to_payload())
        del ledger[:-_LEDGER_MAX]
    except Exception:  # noqa: BLE001 — the ledger append must never re-raise
        logger.debug(
            "policy violation ledger append skipped reason=ledger_unwritable", exc_info=True
        )


def policy_violations(app: "FastAPI") -> list[dict[str, Any]]:
    """Return the bounded policy-violation ledger for this process (empty when unset)."""
    ledger = getattr(app.state, "artifact_policy_violations", None)
    return list(ledger) if isinstance(ledger, list) else []


def _current_sandbox_state() -> Optional["SandboxResult"]:
    try:
        from clio_agent.runtime.sandbox import current_state  # noqa: PLC0415

        return current_state()
    except Exception:  # noqa: BLE001 — sandbox state read is best-effort
        return None


def _iso(epoch: Optional[float]) -> str:
    if epoch is None:
        return ""
    try:
        from clio_agent.gact.runtime.globals import _iso_from_epoch  # noqa: PLC0415

        return _iso_from_epoch(epoch)
    except Exception:  # noqa: BLE001 — timestamp projection is best-effort
        return ""


__all__ = [
    "POLICY_VIOLATION_EVENT",
    "VIOLATION_DETECTED",
    "VIOLATION_PREVENTED",
    "PolicyViolation",
    "observe_policy_violations",
    "local_write_targets",
    "policy_violations",
    "raised_call_boundary",
]
