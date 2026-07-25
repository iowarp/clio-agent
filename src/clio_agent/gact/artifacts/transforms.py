"""TransformRecords — ``b = transform(a)`` made real (owner decision #966.6 / S5).

One coarse :class:`TransformRecord` per producing tool call, keyed by the tool
observer's ``call_id`` (NO second id namespace — the activity id IS the call id).
It carries: the session/turn/workspace, the time span, the status (``success``
AND ``failed`` — a failed run that wrote outputs is real provenance), the agent
(executing vs annotating), the instrument (tool + args, or ``{cmd, script_hash}``
— a generated script is itself a ``script``-kind artifact and its own hashed dep,
DVC's move), the tiered environment (:mod:`environment`), and the ``used[]`` /
``generated[]`` edges, each carrying its own evidence
(``schema-arg | hash-pair | lease-window | authority | assertion``).

Used-edge detection is **precision over recall** (owner decision #966.10): at
observer completion we walk the call args for strings that (1) resolve to an
existing file, (2) sit inside the workspace root, (3) match a registered artifact
by path — then re-hash under the threshold (hash equal → ``schema-arg`` +
``hash-pair``; hash differs → mint a GAP version FIRST and point the edge at the
gap, never silently pin stale; over threshold → ``stat-pinned`` labeled).
Existing-file args NOT in the registry become ``external:path`` objects; anything
else yields NO edge. NDP tool results carrying catalog resource urls/ids register
those inputs ``authority-asserted`` (item 4 — NDP results carry no checksum/ETag/
DOI, so the catalog URL/UUID IS the authority).

The record is emitted as ``artifact.transform.recorded`` — TRACE-ONLY (NOT on the
SSE UI wire, per the S2 split) — via ``_emit_semantic_event`` and folded into the
registry projection (idempotent by ``event_id`` + ``call_id``).
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from clio_agent import conf
from clio_agent.gact.artifacts.environment import (
    EnvironmentRecord,
    EnvironmentTier,
    environment_from_payload,
    tier_at_least,
)
from clio_agent.gact.artifacts.records import ArtifactVersion
from clio_agent.gact.artifacts.transform_edges import (
    contributing_workspace_ids,
    detect_authority_edges,
    detect_used_edges,
)
from clio_agent.gact.artifacts.transform_types import (
    AgentRole,
    EdgeEvidence,
    EdgeRole,
    Instrument,
    ProvEdge,
    ReplayContract,
    TransformKind,
    TransformStatus,
    bound_instrument_args,
)

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

ARTIFACT_TRANSFORM_RECORDED_EVENT = "artifact.transform.recorded"
#: Trace-only event a wiring failure emits so the swallow is DETECTABLE (finding [11]).
ARTIFACT_TRANSFORM_FAILED_EVENT = "artifact.transform.failed"

#: Re-exported so ``tests`` + callers can reach the detector via ``transforms``.
_detect_used_edges = detect_used_edges

#: Per-arg bound on the instrument args stored in the process-lifetime registry +
#: the durable trace event (finding [7]). An arg over this is elided to its content
#: digest (identity kept, bytes dropped). Config-first (#985 conventions).
_DEFAULT_INSTRUMENT_ARG_MAX_BYTES = 2 * 1024
#: Whole-instrument ceiling: over this the args collapse to one digest (finding [7]).
_DEFAULT_INSTRUMENT_TOTAL_MAX_BYTES = 16 * 1024


def instrument_arg_max_bytes() -> int:
    """Resolve the per-arg instrument bound (bytes) from config (finding [7])."""
    return conf.resolve(
        "artifacts.instrument_arg_max_bytes",
        env="CLIO_ARTIFACTS_INSTRUMENT_ARG_MAX_BYTES",
        default=_DEFAULT_INSTRUMENT_ARG_MAX_BYTES,
        cast=conf.as_int,
    )


def instrument_total_max_bytes() -> int:
    """Resolve the whole-instrument args bound (bytes) from config (finding [7])."""
    return conf.resolve(
        "artifacts.instrument_total_max_bytes",
        env="CLIO_ARTIFACTS_INSTRUMENT_TOTAL_MAX_BYTES",
        default=_DEFAULT_INSTRUMENT_TOTAL_MAX_BYTES,
        cast=conf.as_int,
    )


class TransformRecord(BaseModel):
    """One coarse transform keyed by the observer ``call_id`` (owner decision #966.6).

    Immutable value: the harness builds it; the model is never load-bearing (its
    intent is quarantined in ``annotation``). ``environment`` stamps the tiered
    identity; ``replay`` stamps the permanent guarantee derived from the tier and
    the used-edge pinning.
    """

    model_config = ConfigDict(frozen=True)

    #: THE key — the tool observer's ``call_id`` (the activity id).
    call_id: str
    event_id: str = ""
    session_id: str = ""
    turn_id: str = ""
    workspace_id: str = ""
    status: TransformStatus = TransformStatus.SUCCESS
    kind: TransformKind = TransformKind.ORDINARY
    agent_role: AgentRole = AgentRole.EXECUTING
    agent_id: str = ""
    instrument: Instrument = Field(default_factory=Instrument)
    environment: EnvironmentRecord = Field(default_factory=EnvironmentRecord)
    replay: ReplayContract = ReplayContract.RE_RUNNABLE
    replay_reason: str = ""
    used: list[ProvEdge] = Field(default_factory=list)
    generated: list[ProvEdge] = Field(default_factory=list)
    started_at: str = ""
    ended_at: str = ""
    #: Model-provided intent (untrusted, quarantined — never merged into evidence).
    annotation: str = ""
    #: The contended candidate set (other active session ids on the workspace).
    candidates: list[str] = Field(default_factory=list)
    #: Typed notes for DETECTABLE non-edges (precision over recall, #966.10): a
    #: freshly-written output under a non-designation arg (``unminted_output_candidate``,
    #: finding [1]), a path-looking arg that never resolved (``unresolved_path_arg``,
    #: finding [4]), a discovery search whose hits were listed not consumed
    #: (``catalog_hits_not_consumed``, finding [2]). Each ``{reason, ...}``.
    notes: list[dict[str, Any]] = Field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        """The durable ``artifact.transform.recorded`` payload (fold source of truth)."""
        return {
            "event_id": self.event_id,
            "call_id": self.call_id,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "workspace_id": self.workspace_id,
            "status": self.status.value,
            "kind": self.kind.value,
            "agent_role": self.agent_role.value,
            "agent_id": self.agent_id,
            "instrument": self.instrument.model_dump(),
            "environment": self.environment.model_dump(),
            "replay": self.replay.value,
            "replay_reason": self.replay_reason,
            "used": [e.model_dump() for e in self.used],
            "generated": [e.model_dump() for e in self.generated],
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "annotation": self.annotation,
            "candidates": list(self.candidates),
            "notes": [dict(n) for n in self.notes],
        }

    def to_relay_provenance(self) -> dict[str, Any]:
        """The extras block that rides relay ``ArtifactRef.metadata['clio.provenance.v1']``.

        Relay's ``ArtifactUse`` is frozen + ``extra='forbid'`` with no metadata
        field, so our mechanism/evidence/environment extras cannot ride the edge
        itself — they ride the producing artifact's ``ArtifactRef.metadata`` under
        this versioned key until relay's schema converges (the S5 convergence
        issue). ``used_artifact_refs`` is the list of relay ``ArtifactUse`` dicts
        for the hash-pinned used edges (the shape relay lands unchanged).
        """
        return {
            "activity_id": self.call_id,
            "instrument": self.instrument.model_dump(),
            "environment": self.environment.model_dump(),
            "replay": self.replay.value,
            "used_evidence": [
                {
                    "artifact_id": e.artifact_id,
                    "external_ref": e.external_ref,
                    "authority": e.authority,
                    "evidence": e.evidence.value,
                    "note": e.note,
                }
                for e in self.used
            ],
            "used_artifact_refs": [
                use for e in self.used if (use := e.to_artifact_use()) is not None
            ],
        }


def transform_from_payload(payload: dict[str, Any]) -> Optional[TransformRecord]:
    """Rebuild a :class:`TransformRecord` from a folded payload, or ``None`` if malformed.

    A payload with no ``call_id`` cannot be keyed — dropped from the fold with a
    typed reason by the caller, never a crash.
    """
    call_id = str(payload.get("call_id") or "")
    if not call_id:
        return None

    def _edges(raw: Any) -> list[ProvEdge]:
        out: list[ProvEdge] = []
        for item in raw if isinstance(raw, list) else ():
            if not isinstance(item, dict):
                continue
            try:
                out.append(ProvEdge.model_validate(item))
            except Exception:  # noqa: BLE001 — a malformed edge is dropped, never a crash
                continue
        return out

    try:
        status = TransformStatus(str(payload.get("status") or TransformStatus.SUCCESS.value))
    except ValueError:
        status = TransformStatus.SUCCESS
    try:
        kind = TransformKind(str(payload.get("kind") or TransformKind.ORDINARY.value))
    except ValueError:
        kind = TransformKind.ORDINARY
    try:
        agent_role = AgentRole(str(payload.get("agent_role") or AgentRole.EXECUTING.value))
    except ValueError:
        agent_role = AgentRole.EXECUTING
    try:
        replay = ReplayContract(str(payload.get("replay") or ReplayContract.RE_RUNNABLE.value))
    except ValueError:
        replay = ReplayContract.RE_RUNNABLE
    raw_instrument = payload.get("instrument")
    instrument = (
        Instrument.model_validate(raw_instrument)
        if isinstance(raw_instrument, dict)
        else Instrument()
    )
    return TransformRecord(
        call_id=call_id,
        event_id=str(payload.get("event_id") or ""),
        session_id=str(payload.get("session_id") or ""),
        turn_id=str(payload.get("turn_id") or ""),
        workspace_id=str(payload.get("workspace_id") or ""),
        status=status,
        kind=kind,
        agent_role=agent_role,
        agent_id=str(payload.get("agent_id") or ""),
        instrument=instrument,
        environment=environment_from_payload(payload.get("environment")),
        replay=replay,
        replay_reason=str(payload.get("replay_reason") or ""),
        used=_edges(payload.get("used")),
        generated=_edges(payload.get("generated")),
        started_at=str(payload.get("started_at") or ""),
        ended_at=str(payload.get("ended_at") or ""),
        annotation=str(payload.get("annotation") or ""),
        candidates=[str(c) for c in (payload.get("candidates") or []) if c],
        notes=[dict(n) for n in (payload.get("notes") or []) if isinstance(n, dict)],
    )


# --------------------------------------------------------------------------- #
# Replay contract (owner decision #966.6) — permanent, honest, never upgraded.
# --------------------------------------------------------------------------- #


def _edge_pin_class(edge: ProvEdge) -> str:
    """Classify a used edge for the replay contract (finding [5] — honest, never false).

    * ``pinned`` — a content sha proves the exact BITS: a ``hash-pair`` edge, OR an
      ``authority`` edge that ALSO carries a real sha (a staged download hashed
      in-workspace). A bit-identical replay is guaranteed for this input.
    * ``authority`` — an ``authority`` edge WITHOUT a sha: a mutable catalog URL /
      registry id / DOI pins the input's IDENTITY but not its bytes (NDP carries no
      checksum/ETag/DOI). Re-runnable, never reproducible — the remote resource can
      be re-published at the same locator.
    * ``unpinned`` — a ``stat-pinned`` / bare ``schema-arg`` / external edge with no
      sha and no authority. Neither bits nor identity are pinned.
    """
    if edge.evidence is EdgeEvidence.HASH_PAIR and edge.sha256:
        return "pinned"
    if edge.evidence is EdgeEvidence.AUTHORITY and edge.authority:
        return "pinned" if edge.sha256 else "authority"
    return "unpinned"


def compute_replay_contract(
    environment: EnvironmentRecord, used: list[ProvEdge]
) -> tuple[ReplayContract, str]:
    """Derive the permanent replay contract (owner decision #966.6, finding [5]).

    ``reproducible`` iff the environment tier is at least ``lockfile-hash`` AND
    every used input carries hash-pair (or authority-with-hash) evidence — i.e. the
    exact bits are pinned. Otherwise ``re-runnable`` with a typed reason, never a
    false bit-identical guarantee:

    * ``env_below_lockfile_hash`` — the environment tier is too weak;
    * ``inputs_unpinned:<n>`` — ``n`` inputs pin neither bits nor identity;
    * ``inputs_authority_asserted:<n>`` — ``n`` inputs are authority-asserted
      (identity pinned by a mutable catalog locator, NOT the bytes).

    No silent upgrade — an empty used set with a pinned environment IS reproducible.
    Unpinned dominates authority-asserted in the reason (the weaker basis wins).
    """
    if not tier_at_least(environment.tier, EnvironmentTier.LOCKFILE_HASH):
        return ReplayContract.RE_RUNNABLE, "env_below_lockfile_hash"
    classes = [_edge_pin_class(e) for e in used]
    unpinned = sum(1 for c in classes if c == "unpinned")
    if unpinned:
        return ReplayContract.RE_RUNNABLE, f"inputs_unpinned:{unpinned}"
    authority = sum(1 for c in classes if c == "authority")
    if authority:
        return ReplayContract.RE_RUNNABLE, f"inputs_authority_asserted:{authority}"
    return ReplayContract.REPRODUCIBLE, ""


# --------------------------------------------------------------------------- #
# Contended candidate set (owner decision #966.10).
# --------------------------------------------------------------------------- #


def _contended_candidates(app: "FastAPI", workspace_id: str, session_id: str) -> list[str]:
    """Return other active session ids that could be writing the same workspace.

    Reuses the honest single-writer proof (:func:`workspace_lease_clean`'s peer
    scan): when the lease is clean the set is empty (``ordinary`` record); when it
    is dirty the candidate sessions are surfaced (``contended`` record) rather than
    a false single-writer certainty. Best-effort; an unreadable registry yields no
    candidates (the lease already went dirty separately).
    """
    from clio_agent.gact.artifacts.versions import _session_workspace  # noqa: PLC0415

    out: list[str] = []
    in_flight = getattr(app.state, "in_flight_turns", None)
    if in_flight:
        # A ``RuntimeError`` means the in-flight map mutated mid-enumeration — a transient race,
        # NOT "no peers". Swallowing it into an empty result would mis-classify a possibly-
        # contended record as clean/``ordinary``. Retry the snapshot (races settle in a tick);
        # a persistent race is backstopped by the separate lease-dirty guard (docstring).
        others: list[str] = []
        for _attempt in range(3):
            try:
                others = [s for s in list(in_flight.keys()) if s and s != session_id]
                break
            except RuntimeError:
                continue
        for other in others:
            if _session_workspace(app, other) == workspace_id:
                out.append(other)
    return out


# --------------------------------------------------------------------------- #
# Recording orchestration (the observer seam).
# --------------------------------------------------------------------------- #


def _now_iso() -> str:
    from clio_agent.gact.runtime.globals import _iso_from_epoch  # noqa: PLC0415

    return _iso_from_epoch(time.time())


def _generated_edges(
    minted: list[ArtifactVersion], *, fence_proven: bool = False
) -> list[ProvEdge]:
    """Project the versions minted this call to ``generated`` edges.

    ``fence_proven`` (B6 #980) stamps the per-edge lease-window → fence_proven upgrade on
    every generated edge when an active OS fence proved this call's output territory exclusive
    by construction (``transform_exclusivity.generated_fence_proven``). Identity evidence
    (``hash-pair`` / ``schema-arg``) is unchanged — the marker is a separate attribution axis.
    """
    edges: list[ProvEdge] = []
    for version in minted:
        edges.append(
            ProvEdge(
                role=EdgeRole.GENERATED,
                evidence=(EdgeEvidence.HASH_PAIR if version.sha256 else EdgeEvidence.SCHEMA_ARG),
                artifact_id=version.artifact_id,
                sha256=version.sha256,
                name="",
                version=version.version,
                path=version.path,
                note=("" if version.sha256 else "stat_pinned"),
                fence_proven=fence_proven,
            )
        )
    return edges


def _script_instrument(tool_name: str, args: dict[str, Any], used: list[ProvEdge]) -> Instrument:
    """Build the instrument, promoting a used script to ``{cmd, script_hash}`` (DVC).

    When a used edge is a ``script`` artifact (a generated ``.py``/``.sh`` the tool
    executed), its hash becomes the instrument's own ``script_hash`` +
    ``script_artifact_id`` so the script is pinned as its own dependency.
    """
    script_hash = ""
    script_artifact_id = ""
    cmd = ""
    for edge in used:
        suffix = Path(edge.path or edge.name or "").suffix.lower()
        if suffix in {".py", ".sh"} and edge.sha256:
            script_hash = edge.sha256
            script_artifact_id = edge.artifact_id
            cmd = f"{tool_name} {edge.path or edge.name}".strip()
            break
    return Instrument(
        tool=tool_name,
        args=bound_instrument_args(
            dict(args),
            arg_max_bytes=instrument_arg_max_bytes(),
            total_max_bytes=instrument_total_max_bytes(),
        ),
        cmd=cmd,
        script_hash=script_hash,
        script_artifact_id=script_artifact_id,
    )


def record_transform(
    app: "FastAPI",
    sid: str,
    *,
    tool_name: str,
    args: dict[str, Any],
    call_id: str,
    ok: bool,
    result: Any,
    minted: list[ArtifactVersion],
    workspace_id: str,
    turn_id: str = "",
    trace_id: str = "",
    started_at: Optional[float] = None,
    agent_id: str = "",
    serving_child_id: str = "",
) -> Optional[TransformRecord]:
    """Build, emit (trace-only), and fold one :class:`TransformRecord` (owner #966.6).

    ``minted`` are the generated versions the mint seam produced this call (for
    ``generated`` edges). Used edges are detected from ``args``; authority edges
    from ``result``. The record is emitted as ``artifact.transform.recorded``
    (TRACE-ONLY — NOT on the SSE wire, per the S2 split) and folded into the
    registry projection. Returns the record, or ``None`` when it cannot be keyed.
    """
    if not call_id:
        logger.info(
            "transform record skipped reason=missing_call_id session=%s tool=%s", sid, tool_name
        )
        return None
    from clio_agent.gact.artifacts.environment import capture_environment  # noqa: PLC0415
    from clio_agent.gact.artifacts.registry import get_registry  # noqa: PLC0415
    from clio_agent.gact.semantic_events import _event_id  # noqa: PLC0415

    # P3.1 (#1038): the CROSS-JOB contributing set — every workspace sharing this
    # job's root_path — computed here (the caller HAS ``app``) and threaded into the
    # detector so it keeps its acyclic position. ``None`` → same-workspace-only.
    allowed_workspace_ids = contributing_workspace_ids(app, workspace_id)
    used_scan = _detect_used_edges(
        app,
        sid,
        args=args,
        workspace_id=workspace_id,
        turn_id=turn_id,
        trace_id=trace_id,
        call_started_at=started_at,
        allowed_workspace_ids=allowed_workspace_ids,
    )
    authority_scan = detect_authority_edges(
        app, tool_name=tool_name, result=result, workspace_id=workspace_id
    )
    used = [*used_scan.edges, *authority_scan.edges]
    notes = [*used_scan.notes, *authority_scan.notes]
    # B4 (#978): join in-window ``net.egress`` records onto the used edges as
    # ``used web:<domain>@<time>`` — enriching a staged-download/catalog URL edge whose host
    # the chokepoint observed (step 1, one edge two evidence bases), or minting one fresh web
    # edge ONLY when the producing call's SERVING confined child is known and its egress is a
    # single unambiguous domain (step 2, child-keyed). Precision over recall (#966.10): an
    # unattributable egress (unknown serving child / multi-domain / unbounded window) stays a
    # bare ``net.egress`` record — a sibling child's egress is never minted onto this
    # transform. Guarded — a provenance join must never break a turn.
    try:
        from clio_agent.gact.artifacts.ingest_edges import attach_ingest_edges  # noqa: PLC0415

        used = attach_ingest_edges(
            app,
            used,
            workspace_id=workspace_id,
            tool_name=tool_name,
            started_at=started_at,
            serving_child_id=serving_child_id,
        )
    except Exception:  # noqa: BLE001 — the ingest join is best-effort, never fatal
        logger.debug("ingest edge join skipped reason=ingest_join_failed", exc_info=True)
    environment = capture_environment(app)
    replay, replay_reason = compute_replay_contract(environment, used)
    candidates = _contended_candidates(app, workspace_id, sid)
    kind = TransformKind.CONTENDED if candidates else TransformKind.ORDINARY
    # B6 (#980): the per-edge lease-window → fence_proven upgrade on the generated (written)
    # side — proven only when an active fence made this call's output territory exclusive by
    # construction (ordinary record + set-math). Never on the floor, never when contended.
    from clio_agent.gact.artifacts.transform_exclusivity import (  # noqa: PLC0415
        generated_fence_proven,
    )

    generated = _generated_edges(
        minted, fence_proven=generated_fence_proven(app, workspace_id, sid, kind=kind)
    )
    started_iso = _iso_from_epoch_opt(started_at)
    record = TransformRecord(
        call_id=call_id,
        event_id=_event_id(),
        session_id=sid,
        turn_id=turn_id,
        workspace_id=workspace_id,
        status=TransformStatus.SUCCESS if ok else TransformStatus.FAILED,
        kind=kind,
        agent_role=AgentRole.EXECUTING,
        agent_id=agent_id,
        instrument=_script_instrument(tool_name, args, used),
        environment=environment,
        replay=replay,
        replay_reason=replay_reason,
        used=used,
        generated=generated,
        started_at=started_iso,
        ended_at=_now_iso(),
        candidates=candidates,
        notes=notes,
    )
    _emit_transform_recorded(app, sid, record, turn_id=turn_id, trace_id=trace_id)
    get_registry(app).record_transform(record)
    return record


def _iso_from_epoch_opt(started_at: Optional[float]) -> str:
    if started_at is None:
        return ""
    from clio_agent.gact.runtime.globals import _iso_from_epoch  # noqa: PLC0415

    return _iso_from_epoch(started_at)


def _emit_transform_recorded(
    app: "FastAPI",
    sid: str,
    record: TransformRecord,
    *,
    turn_id: str = "",
    trace_id: str = "",
) -> None:
    """Emit ``artifact.transform.recorded`` — TRACE-ONLY (never on the SSE wire).

    Guarded — a provenance emit must never break a turn. The event type is
    deliberately absent from ``SSE_UI_EVENT_TYPES`` (the S2 split): it is captured
    FULL on the durable trace + ARC and folded at boot, but never served to the UI.
    """
    try:
        from clio_agent.gact.runtime.globals import _emit_semantic_event  # noqa: PLC0415

        _emit_semantic_event(
            app,
            sid,
            ARTIFACT_TRANSFORM_RECORDED_EVENT,
            turn_id=turn_id,
            trace_id=trace_id,
            status="completed" if record.status is TransformStatus.SUCCESS else "failed",
            summary=(
                f"Transform {record.instrument.tool} recorded "
                f"({len(record.used)} used, {len(record.generated)} generated)."
            ),
            actor={"tool": record.instrument.tool, "mechanism": "harness"},
            subject={"call_id": record.call_id, "workspace_id": record.workspace_id},
            payload=record.to_payload(),
        )
    except Exception:  # noqa: BLE001 — a provenance emit must never break a turn
        logger.warning(
            "transform record emit skipped reason=transform_emit_failed session=%s call_id=%s",
            sid,
            record.call_id,
        )


def observe_tool_transform(
    app: "FastAPI",
    sid: str,
    tool_name: str,
    effective_args: dict[str, Any],
    call_id: str,
    ok: bool,
    result: Any = None,
    call_started_at: Optional[float] = None,
) -> None:
    """Observer seam entry: mint generated outputs + record the transform (S5).

    Fully self-contained + guarded so the gact tool observer calls it in one line.
    Mints tool-declared generated outputs (for BOTH success and failure — a failed
    run that wrote outputs is real provenance), then records one TransformRecord
    keyed by ``call_id`` carrying the used/generated edges, environment tier, and
    replay contract. A live provenance record must never break a turn.
    """
    try:
        from clio_agent.gact import context as _ctx  # noqa: PLC0415
        from clio_agent.gact.artifacts.minting import (  # noqa: PLC0415
            _observer_call_started_at,
            _session_workspace_id,
            mint_tool_declared_outputs,
        )

        workspace_id = _session_workspace_id(app, sid)
        turn_id = _ctx.active_turn_id()
        trace_id = _ctx.active_trace_id()
        started = call_started_at if call_started_at is not None else _observer_call_started_at()
        minted = mint_tool_declared_outputs(
            app,
            sid,
            tool_name=tool_name,
            effective_args=effective_args,
            call_id=call_id,
            workspace_id=workspace_id,
            turn_id=turn_id,
            trace_id=trace_id,
            call_started_at=started,
            result=result,
        )
        # B4 (#978): resolve the confined child that SERVED this call so the ingest join can
        # attribute egress deterministically (``egress → child → call-window → transform``).
        # ``""`` when no child link is recorded (the floor / unattributed) — the step-2 mint
        # then abstains rather than guess.
        from clio_agent.gact.artifacts.ingest_edges import (  # noqa: PLC0415
            resolve_serving_child_id,
        )

        record_transform(
            app,
            sid,
            tool_name=tool_name,
            args=effective_args,
            call_id=call_id,
            ok=ok,
            result=result,
            minted=minted,
            workspace_id=workspace_id,
            turn_id=turn_id,
            trace_id=trace_id,
            started_at=started,
            agent_id=_ctx.active_react_scope() or "",
            serving_child_id=resolve_serving_child_id(app, call_id),
        )
        # B2 (#976): on a FENCED platform, a denied (or fence-escaping) out-of-root write is a
        # typed ``policy_violation`` — the enforced-tier variant of #966's ``gap`` node. No-op
        # on the floor (the write succeeds → honest gap). Guarded above with the record.
        from clio_agent.gact.artifacts.violations import (  # noqa: PLC0415
            observe_policy_violations,
        )

        observe_policy_violations(
            app,
            sid,
            tool_name=tool_name,
            args=effective_args,
            call_id=call_id,
            result=result,
            workspace_id=workspace_id,
            turn_id=turn_id,
            trace_id=trace_id,
            started_at=started,
        )
    except Exception as exc:  # noqa: BLE001 — a live provenance record must never break a turn
        # Finding [11]: the wiring's ONLY production path must not SWALLOW its own
        # failure silently — record a TYPED failure (queryable ledger + trace event)
        # so "all provenance silently empty" is detectable, the turn unharmed.
        _record_transform_failure(
            app,
            sid,
            tool_name=tool_name,
            call_id=call_id,
            reason=type(exc).__name__,
            detail=str(exc),
        )


def _record_transform_failure(
    app: "FastAPI",
    sid: str,
    *,
    tool_name: str,
    call_id: str,
    reason: str,
    detail: str,
) -> None:
    """Record a TYPED ``transform_record_failed`` (finding [11]) — never a bare swallow.

    Appends to a bounded per-app ledger (``app.state.artifact_transform_failures``)
    so a regression that empties all provenance is DETECTABLE after the fact, emits
    a trace-only ``artifact.transform.failed`` event, and logs. Every step is itself
    guarded so the failure recorder can never re-raise into the turn.
    """
    logger.warning(
        "transform record failed reason=transform_record_failed session=%s tool=%s "
        "call_id=%s cause=%s detail=%s",
        sid,
        tool_name,
        call_id,
        reason,
        detail,
    )
    entry = {
        "reason": "transform_record_failed",
        "session_id": sid,
        "tool": tool_name,
        "call_id": call_id,
        "cause": reason,
        "detail": detail[:512],
    }
    try:
        ledger = getattr(app.state, "artifact_transform_failures", None)
        if not isinstance(ledger, list):
            ledger = []
            app.state.artifact_transform_failures = ledger
        ledger.append(entry)
        del ledger[:-256]  # bounded — a pathological session cannot grow it unboundedly
    except Exception:  # noqa: BLE001 — the failure recorder must never re-raise
        logger.debug(
            "transform failure ledger append skipped reason=ledger_unwritable", exc_info=True
        )
    try:
        from clio_agent.gact import context as _ctx  # noqa: PLC0415
        from clio_agent.gact.runtime.globals import _emit_semantic_event  # noqa: PLC0415

        _emit_semantic_event(
            app,
            sid,
            ARTIFACT_TRANSFORM_FAILED_EVENT,
            turn_id=_ctx.active_turn_id(),
            trace_id=_ctx.active_trace_id(),
            status="failed",
            summary=f"Transform record for {tool_name} failed ({reason}).",
            actor={"tool": tool_name, "mechanism": "harness"},
            subject={"call_id": call_id},
            payload=entry,
        )
    except Exception:  # noqa: BLE001 — a failure emit must never break the turn either
        logger.debug("transform failure event emit skipped reason=emit_unavailable", exc_info=True)


def transform_record_failures(app: "FastAPI") -> list[dict[str, Any]]:
    """Return the bounded typed-failure ledger (finding [11]), empty when unset."""
    ledger = getattr(app.state, "artifact_transform_failures", None)
    return list(ledger) if isinstance(ledger, list) else []


__all__ = [
    "ARTIFACT_TRANSFORM_FAILED_EVENT",
    "ARTIFACT_TRANSFORM_RECORDED_EVENT",
    "AgentRole",
    "EdgeEvidence",
    "EdgeRole",
    "Instrument",
    "ProvEdge",
    "ReplayContract",
    "TransformKind",
    "TransformRecord",
    "TransformStatus",
    "compute_replay_contract",
    "observe_tool_transform",
    "record_transform",
    "transform_from_payload",
    "transform_record_failures",
]
