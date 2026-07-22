"""``policy_violation`` provenance — the #966 ``gap`` node's enforced-tier variant (#976/B2).

On the honest floor (#966/B1) an out-of-root write SUCCEEDS and is recorded as a ``gap``
version (mechanism ``none``, actor unknown). B2 activates OS write-fences (srt / Landlock),
so the SAME attempt now surfaces differently and MORE precisely:

* the fence DENIED the write (the shell/tool result carries an ``EROFS``/``EACCES`` from the
  fenced child, or a designated out-of-root path is absent post-call) → a typed
  ``policy_violation`` (``prevented``): attributed to the child/tool ``call_id``, the path,
  the call window and the DENYING mechanism — never silently a gap;
* a fenced platform still OBSERVES an out-of-root change (the fence was escaped) →
  ``policy_violation(detected)`` — worse than prevented, and recorded as such.

The mint logic lives HERE (the artifacts owner package); :mod:`clio_agent.runtime.sandbox`
only exposes the resolved state. Violations are DURABLE-ONLY this slice — emitted as the
trace-only ``artifact.policy_violation`` event (registered in
``semantic_events.SSE_TRACE_ONLY_EVENT_TYPES``) and appended to a bounded per-app ledger;
the SSE listing / lineage-node projection waits for B5's SPEC rider.

VIOLATION MAPPING (owner note #974 spike): a bwrap denial surfaces as ``EROFS``
("Read-only file system"), NOT ``PermissionError`` — so this catches errno **EROFS + EACCES**
(WinError 5 is B3's). The errno signal fires ONLY when a fence is active, so the floor never
mints a violation (its out-of-root write is an honest gap, as before).
"""

from __future__ import annotations

import errno
import logging
import os
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.runtime.sandbox import SandboxResult

logger = logging.getLogger(__name__)

POLICY_VIOLATION_EVENT = "artifact.policy_violation"

#: Violation kinds (owner decision #974 / #966 gap-variant).
VIOLATION_PREVENTED = "prevented"  # the fence blocked the out-of-root write
VIOLATION_DETECTED = "detected"  # the fence was escaped — a change happened anyway

#: The write-denial errnos a fence surfaces (owner note #974 spike). WinError 5 is B3's.
_DENIAL_ERRNOS: dict[int, str] = {errno.EROFS: "EROFS", errno.EACCES: "EACCES"}
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
    errno_name: str = ""  # EROFS / EACCES when sourced from a result errno; "" otherwise
    signal: str = (
        ""  # how it was detected: "result_errno" | "designated_path_absent" | "detected_change"
    )
    started_at: str = ""  # the call window (start / end ISO)
    ended_at: str = ""
    detail: str = ""  # a bounded evidence snippet (e.g. the stderr line)

    def to_payload(self) -> dict[str, Any]:
        """The durable ``artifact.policy_violation`` payload."""
        return self.model_dump()


# --------------------------------------------------------------------------- #
# errno signal — parse a fenced child's OS write-denial out of a tool result.  #
# --------------------------------------------------------------------------- #

_ERRNO_BRACKET = {code: re.compile(rf"\[Errno {code}\]") for code in _DENIAL_ERRNOS}
#: strerror text form (``/bin/sh`` prints this, not the ``[Errno N]`` bracket).
_ERRNO_STRERROR = {code: os.strerror(code).lower() for code in _DENIAL_ERRNOS}
#: Best-effort path extractors from a denial message, spanning the common shell/tool forms:
#: coreutils/sh ("cannot create /p:"), bash redirect ("bash: line 1: /p: Read-only file
#: system"), and Python OSError ("[Errno 30] ...: '/p'"). Ordered most-specific first.
_PATH_PATTERNS = [
    re.compile(r"cannot (?:create|touch|open|write to) (?:directory )?(\S+?):", re.IGNORECASE),
    re.compile(r"line \d+: (\S+?): (?:read-only file system|permission denied)", re.IGNORECASE),
    re.compile(r": '([^']+)'"),
    re.compile(r': "([^"]+)"'),
]


def _walk_strings(obj: Any, *, _depth: int = 0) -> list[str]:
    """Collect the string leaves of a (possibly nested) tool result, bounded in depth."""
    if _depth > 6:
        return []
    if isinstance(obj, str):
        return [obj]
    out: list[str] = []
    if isinstance(obj, dict):
        for value in obj.values():
            out.extend(_walk_strings(value, _depth=_depth + 1))
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            out.extend(_walk_strings(item, _depth=_depth + 1))
    return out


def _extract_path(text: str) -> str:
    for pattern in _PATH_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1)
    return ""


def write_denial_from_result(result: Any) -> Optional[dict[str, Any]]:
    """Return a write-denial ``{errno, errno_name, path, detail}`` from a tool result, or ``None``.

    Scans the result's string leaves for a fenced child's ``EROFS``/``EACCES`` signal — the
    Python ``[Errno N]`` bracket form OR the OS ``strerror`` text a shell prints (e.g.
    "Read-only file system"). Reality-surfacing (an OS errno in a tool result), NOT a
    prose/routing heuristic — the caller only calls this when a fence is active, so a normal
    permission error on the floor never becomes a violation.
    """
    for text in _walk_strings(result):
        lowered = text.lower()
        for code, name in _DENIAL_ERRNOS.items():
            if _ERRNO_BRACKET[code].search(text) or _ERRNO_STRERROR[code] in lowered:
                return {
                    "errno": code,
                    "errno_name": name,
                    "path": _extract_path(text),
                    "detail": text.strip()[:400],
                }
    return None


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

    * the result carries a fenced child's ``EROFS``/``EACCES`` AND a non-empty extracted path
      that is OUTSIDE the effective write roots → ``prevented``. An in-root denial (srt's
      mandatory ``.git/hooks`` protection), a read EACCES on an in-root file, or a bare
      "permission denied" phrase with NO extractable path (SSH publickey) is a typed debug
      skip (``violation_signal_unattributed``), never a fabricated out-of-root violation;
    * a designated output path OUTSIDE the territory — absent post-call → ``prevented``;
      present AND provably fresh within the call window → ``detected``. Without a call window
      (``started_at is None``) a PRESENT file is NEVER upgraded to ``detected`` (F5) — a typed
      skip, since escape cannot be proven.
    """
    resolved = state if state is not None else _current_sandbox_state()
    if resolved is None or not resolved.active:
        return []
    mechanism = str(resolved.mechanism)
    started_iso = _iso(started_at)
    ended_iso = _iso(time.time())
    roots = _effective_roots(app, workspace_id)
    violations: list[PolicyViolation] = []

    denial = write_denial_from_result(result)
    if denial is not None:
        dpath = str(denial.get("path") or "")
        if dpath and not _within_roots(Path(dpath), roots):
            violations.append(
                PolicyViolation(
                    kind=VIOLATION_PREVENTED,
                    mechanism=mechanism,
                    path=dpath,
                    call_id=call_id,
                    tool=tool_name,
                    session_id=sid,
                    turn_id=turn_id,
                    workspace_id=workspace_id,
                    errno_name=str(denial.get("errno_name") or ""),
                    signal="result_errno",
                    started_at=started_iso,
                    ended_at=ended_iso,
                    detail=str(denial.get("detail") or ""),
                )
            )
        else:
            # In-root (mandatory protection / DAC read) or path-less denial: the fence did NOT
            # prove an out-of-root WRITE, so attributing one would be false provenance.
            logger.debug(
                "policy violation skipped reason=violation_signal_unattributed errno=%s "
                "path=%r in_root=%s tool=%s",
                denial.get("errno_name"),
                dpath,
                bool(dpath and _within_roots(Path(dpath), roots)),
                tool_name,
            )

    violations.extend(
        _designated_out_of_root_violations(
            app,
            sid,
            tool_name=tool_name,
            args=args,
            result=result,
            call_id=call_id,
            workspace_id=workspace_id,
            turn_id=turn_id,
            mechanism=mechanism,
            roots=roots,
            started_at=started_at,
            started_iso=started_iso,
            ended_iso=ended_iso,
        )
    )

    for violation in violations:
        _mint_policy_violation(app, sid, violation, turn_id=turn_id, trace_id=trace_id)
    return violations


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
    args: dict[str, Any],
    result: Any,
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
    from clio_agent.gact.artifacts.designation import (  # noqa: PLC0415
        grounded_output_paths,
        result_declared_paths,
    )

    designated = {**grounded_output_paths(args), **result_declared_paths(result)}
    out: list[PolicyViolation] = []
    for raw_path in designated.values():
        path = Path(raw_path)
        if _within_roots(path, roots):
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
            )
        )
    return out


def _within_roots(path: Path, roots: tuple[Path, ...]) -> bool:
    try:
        resolved = path.expanduser().resolve(strict=False)
    except OSError:
        resolved = path
    for root in roots:
        try:
            resolved.relative_to(root.resolve(strict=False))
            return True
        except (ValueError, OSError):
            continue
    return False


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
    "policy_violations",
    "write_denial_from_result",
]
