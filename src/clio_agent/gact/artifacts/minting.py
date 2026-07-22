"""The artifact minting funnel — identity hashing + the three S1 mint seams.

One funnel (:func:`mint_artifact`) builds an immutable version, emits the durable
``artifact.created`` semantic event (the sole artifact-event emitter; as of S2
(#968) it rides the SSE UI wire at ``semantic`` detail, no longer trace-only),
folds it into the registry projection and patches the SessionStore badge index.
The three seams feed it:

* :func:`mint_tool_declared_outputs` — gact tool observer ``completed`` phase
  (mechanism ``tool-schema``, producing ``call_id``);
* the harness-write mint (mechanism ``harness``) minted by the gact-side
  ``fs_apply_edit_write`` caller off :func:`mint_artifact` directly;
* :func:`mint_pack_declared_paths` — the secondary pack ``artifact_paths`` channel.

Identity is hashed by the harness (:func:`compute_identity`), streamed so large
outputs stay bounded; over the size threshold a version is ``stat-pinned`` (typed,
permanent — never a silent hash-skip). The model is never load-bearing here.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from clio_agent import conf
from clio_agent.gact.artifacts.records import (
    RESERVED_KINDS,
    ArtifactKind,
    ArtifactVersion,
    Custody,
    IdentityEvidence,
    Mechanism,
)
from clio_agent.gact.artifacts.registry import (
    ARTIFACT_CREATED_EVENT,
    MintOutcome,
    get_registry,
    patch_session_index,
)
from clio_agent.gact.artifacts.versions import (
    emit_alias_moved,
    emit_version_added,
    reconcile_if_content_revert,
    reconcile_if_tool_drift,
    version_record_payload,
)

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

#: Default ceiling on hashing a designated output at mint. Over this, the version
#: is recorded ``stat-pinned`` (typed, permanent) rather than paying multi-GB I/O
#: on the turn thread (design resolution 5b). Config-first (#985 conventions).
_DEFAULT_HASH_MAX_FILE_BYTES = 64 * 1024 * 1024

_HASH_CHUNK_BYTES = 1024 * 1024

#: Per-session turn-scoped buffer of the artifact versions minted THIS turn — the
#: source for the one-``resource_link``-part-per-generated-artifact append at turn
#: finalize (#968 item 2). Only genuinely NEW versions land here (a W&B same-sha
#: dedup no-op mints nothing, so it contributes no part — matching "one part per
#: artifact GENERATED this turn"). ``turn_finalize`` drains + filters by turn id
#: and clears the session's list; ``settle_failed_finalize`` calls
#: :func:`clear_turn_artifacts` on the failure path so a crashed turn cannot
#: re-emit its buffered parts when the same turn is retried. Bounded per session
#: so a pathological turn cannot grow it unboundedly.
_TURN_ARTIFACT_CAP = 256
_TURN_ARTIFACT_LOCK = threading.Lock()


def _record_turn_artifact(
    app: "FastAPI",
    sid: str,
    *,
    workspace_id: str,
    name: str,
    version: "ArtifactVersion",
    turn_id: str,
) -> None:
    """Buffer a freshly-minted version for the finalize ``resource_link`` append.

    Thread-safe: the observer mint runs on a worker thread while a finalize on the
    turn thread may drain concurrently. A single module lock guards the per-session
    list so an append never races a drain-and-clear. Best-effort — a buffering
    failure must never break a live mint, so the caller wraps the whole mint.
    """
    with _TURN_ARTIFACT_LOCK:
        buffers = getattr(app.state, "turn_artifacts", None)
        if buffers is None:
            buffers = {}
            app.state.turn_artifacts = buffers
        entries = buffers.setdefault(sid, [])
        if len(entries) >= _TURN_ARTIFACT_CAP:
            logger.warning(
                "artifact turn buffer at cap reason=turn_artifact_cap session=%s cap=%d",
                sid,
                _TURN_ARTIFACT_CAP,
            )
            return
        entries.append(
            {
                "workspace_id": workspace_id,
                "name": name,
                "version": version,
                "turn_id": turn_id,
            }
        )


def drain_turn_artifacts(app: "FastAPI", sid: str, turn_id: str = "") -> list[dict[str, Any]]:
    """Pop the turn's buffered artifact versions for ``sid`` (finalize seam).

    Returns the buffered entries and CLEARS the session's list. When ``turn_id`` is
    given, only entries stamped with that turn are returned — a defensive filter so
    a stray mint from a prior (un-drained) turn never rides this turn's message;
    entries for other turns are dropped with the list (a new turn re-buffers its
    own). Empty list when nothing was minted this turn.
    """
    with _TURN_ARTIFACT_LOCK:
        buffers = getattr(app.state, "turn_artifacts", None)
        if not buffers:
            return []
        entries = buffers.pop(sid, [])
    if not turn_id:
        return entries
    return [e for e in entries if str(e.get("turn_id") or "") == turn_id]


def clear_turn_artifacts(app: "FastAPI", sid: str) -> None:
    """Drop the whole per-session turn buffer (failed-finalize seam, finding [7]).

    A finalize-region crash never reaches the finalize drain, so its buffered
    versions would linger. If the SAME turn is then retried, the retry re-buffers
    the same mints and the next successful finalize would drain BOTH the stale and
    the fresh entries — one artifact, two ``resource_link`` parts. Clearing the
    session's buffer on the failure path makes the retry emit exactly once. Called
    unconditionally from ``settle_failed_finalize``; a missing buffer is a no-op.
    """
    with _TURN_ARTIFACT_LOCK:
        buffers = getattr(app.state, "turn_artifacts", None)
        if buffers:
            buffers.pop(sid, None)


def hash_max_file_bytes() -> int:
    """Resolve the mint-time hash size threshold (bytes) from config.

    ``artifacts.hash_max_file_bytes`` (env ``CLIO_ARTIFACTS_HASH_MAX_FILE_BYTES``)
    — a designated output larger than this is stat-pinned, not hashed.
    """
    return conf.resolve(
        "artifacts.hash_max_file_bytes",
        env="CLIO_ARTIFACTS_HASH_MAX_FILE_BYTES",
        default=_DEFAULT_HASH_MAX_FILE_BYTES,
        cast=conf.as_int,
    )


@dataclass(frozen=True)
class _StatHash:
    """A designated path's stat + (optional) streamed sha256."""

    exists: bool
    size_bytes: int
    mtime: float
    sha256: Optional[str]
    over_threshold: bool


def _stat_and_hash(path: Path, max_bytes: int) -> _StatHash:
    """Stat ``path`` and stream its sha256 unless it exceeds ``max_bytes``.

    Streaming keeps memory bounded on large scientific outputs. Over the
    threshold, the hash is skipped and ``over_threshold`` is set so the caller
    records a ``stat-pinned`` evidence class (typed, never a silent hash-skip).
    """
    stat = path.stat()
    size = int(stat.st_size)
    mtime = float(stat.st_mtime)
    if size > max_bytes:
        return _StatHash(True, size, mtime, None, True)
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(_HASH_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    return _StatHash(True, size, mtime, digest.hexdigest(), False)


def compute_identity(path: str | Path, *, max_bytes: int | None = None) -> IdentityEvidence:
    """Build :class:`IdentityEvidence` for a designated output path.

    Hashes when the file is at or under the threshold (``hashed-at-use``); over
    it, records ``stat-pinned`` with size+mtime. The path must exist — a caller
    minting for a non-existent designated path is a designation error the caller
    handles (this raises ``FileNotFoundError``), never a silent skip.
    """
    resolved = Path(str(path))
    ceiling = hash_max_file_bytes() if max_bytes is None else max_bytes
    sh = _stat_and_hash(resolved, ceiling)
    if sh.over_threshold or sh.sha256 is None:
        return IdentityEvidence.stat_pinned(size_bytes=sh.size_bytes, mtime=sh.mtime)
    return IdentityEvidence.hashed_at_use(
        sha256=sh.sha256, size_bytes=sh.size_bytes, mtime=sh.mtime
    )


def _now_iso() -> str:
    from clio_agent.gact.runtime.globals import _iso_from_epoch  # noqa: PLC0415

    return _iso_from_epoch(time.time())


def _observer_call_started_at() -> float | None:
    """The current tool call's start epoch from the observer thread-local, if set.

    Seam (a) runs synchronously in the observer worker thread, where the observer
    stamped ``_OBSERVER_CALL_T0`` at the ``started`` phase — so the pre-existing
    untouched skip (finding [8]) reads it without threading it through the call.
    """
    try:
        from clio_agent.gact.tool_observer import _OBSERVER_CALL_T0  # noqa: PLC0415

        value = getattr(_OBSERVER_CALL_T0, "value", None)
        return float(value) if value is not None else None
    except Exception:  # noqa: BLE001 — the skip is an optimization, never load-bearing
        return None


def _workspace_root(app: "FastAPI", workspace_id: str) -> Optional[Path]:
    """Resolve the bound workspace's root path, or ``None`` when unresolvable.

    A ``None`` root means containment cannot be verified — the seams then skip the
    mint (typed ``containment_unresolved``) rather than read an unbounded path
    (precision over recall — owner decision 10).
    """
    store = getattr(app.state, "workspaces", None)
    if store is None or not workspace_id:
        return None
    try:
        ws = store.get(workspace_id)
    except Exception:  # noqa: BLE001 — an unresolvable workspace is a skip, never a crash
        return None
    root = str(getattr(ws, "root_path", "") or "") if ws is not None else ""
    if not root:
        return None
    return Path(root).expanduser().resolve(strict=False)


def _contained(path: Path, root: Path) -> bool:
    """Return whether ``path`` resolves inside the workspace ``root``.

    Reuses the file-policy containment helper (``tools/file_policy._is_relative_to``)
    so the mint seams and the tool boundary share one containment rule. Resolves
    ``..`` traversal and symlink targets before the check (owner decision 10 — a
    model-authored path must never read/hash a file outside the workspace).
    """
    from clio_agent.tools.file_policy import _is_relative_to  # noqa: PLC0415

    try:
        resolved = path.expanduser().resolve(strict=False)
    except OSError:
        return False
    return _is_relative_to(resolved, root)


def artifact_name_for_path(path: str | Path) -> str:
    """The logical artifact name for a designated output path (its basename).

    Basename keys the logical chain so a tool overwriting the same deliverable
    (``timeseries.png`` re-rendered) folds into a version chain, matching how the
    harness/grader collects deliverables by filename. A later slice may key on the
    workspace-relative path when directory disambiguation is needed.
    """
    return Path(str(path)).name


def mint_artifact_outcome(
    app: "FastAPI",
    sid: str,
    *,
    name: str,
    workspace_id: str,
    evidence: IdentityEvidence,
    kind: ArtifactKind,
    mechanism: Mechanism,
    producer: dict[str, Any] | None = None,
    custody: Custody = Custody.WORKSPACE_REFERENCED,
    path: str = "",
    annotation: str = "",
    turn_id: str = "",
    trace_id: str = "",
    producing: bool = True,
    lease_clean: bool = False,
    not_ingested_size: int | None = None,
) -> Optional[MintOutcome]:
    """Mint one artifact version: atomic decide-and-append, then emit + index.

    The single mint funnel for all three S1 seams. Delegates the version decision
    to :meth:`ArtifactRegistry.mint`, which — under ONE lock — dedups a
    byte-identical re-designation onto the existing version (W&B same-sha dedup,
    findings [1/6] + [3/10]) or assigns the next version and folds it. On a genuine
    new version it emits the durable ``artifact.created`` event via
    ``_emit_semantic_event`` (the sole artifact-event emitter; the pre-registered
    ``event_id`` makes boot replay a no-op) and patches the SessionStore badge
    index; on a dedup no-op it emits NOTHING and returns the existing version.

    Returns the full :class:`MintOutcome` so a caller can read ``created`` — finding
    [7]: ``promote_proposal`` must NOT report ``created=True`` or consume a per-turn
    cap slot when the mint deduped under a concurrent race. ``mint_artifact`` is the
    back-compat projection returning just the version. ``plan`` kind is RESERVED —
    minting it raises ``ValueError`` (typed, not silently downgraded).
    """
    if kind in RESERVED_KINDS:
        raise ValueError(
            f"artifact kind {kind.value!r} is reserved and cannot be minted this campaign"
        )

    registry = get_registry(app)

    from clio_agent.gact.semantic_events import _event_id  # noqa: PLC0415

    event_id = _event_id()
    # Atomic decide-and-append under ONE registry lock (findings [1/6] + [3/10]); the
    # version decision itself is the single decision point (versions.decide_version).
    outcome = registry.mint(
        workspace_id=workspace_id,
        name=name,
        event_id=event_id,
        kind=kind,
        custody=custody,
        mechanism=mechanism,
        evidence=evidence,
        producer=dict(producer or {}),
        path=path,
        created_at=_now_iso(),
        annotation=annotation,
        producing=producing,
        lease_clean=lease_clean,
        not_ingested_size=not_ingested_size,
    )
    version = outcome.version
    if not outcome.created:
        # W&B same-sha dedup: content already versioned — emit NOTHING (the no-op is
        # at the mint, not merely the fold). Keep the badge index fresh (idempotent).
        logger.info(
            "artifact mint dedup no-op reason=%s ws=%s name=%s sha=%s existing_version=%d",
            outcome.reason,
            workspace_id,
            name,
            version.sha256,
            version.version,
        )
        patch_session_index(app, sid, registry, workspace_id)
        return outcome

    # v1 emits ``artifact.created``; v2+ revisions (incl. re-link / gap) emit
    # ``artifact.version.added`` carrying the ``wasRevisionOf`` edge, and move the
    # ``latest`` alias with ``artifact.alias.moved`` (S4 #970) — all on the S2 SSE
    # allow-list at ``semantic`` detail; capture stays FULL on the trace + ARC.
    is_v1 = version.version == 1 and version.prior_version is None
    if is_v1:
        payload = _created_payload(event_id, workspace_id, name, version)

        from clio_agent.gact.runtime.globals import _emit_semantic_event  # noqa: PLC0415

        _emit_semantic_event(
            app,
            sid,
            ARTIFACT_CREATED_EVENT,
            turn_id=turn_id,
            trace_id=trace_id,
            status="completed",
            summary=f"Artifact {name} v{version.version} created.",
            actor={"mechanism": version.mechanism.value},
            subject={
                "artifact_id": version.artifact_id,
                "name": name,
                "workspace_id": workspace_id,
            },
            payload=payload,
            detail_level="semantic",
        )
    else:
        emit_version_added(
            app,
            sid,
            workspace_id=workspace_id,
            name=name,
            version=version,
            turn_id=turn_id,
            trace_id=trace_id,
        )
        emit_alias_moved(
            app,
            sid,
            workspace_id=workspace_id,
            name=name,
            alias="latest",
            from_version=version.prior_version,
            to_version=version.version,
            at=version.created_at,
            turn_id=turn_id,
            trace_id=trace_id,
        )

    patch_session_index(app, sid, registry, workspace_id)
    # Buffer the new version for the finalize ``resource_link`` part append (item 2).
    _record_turn_artifact(
        app, sid, workspace_id=workspace_id, name=name, version=version, turn_id=turn_id
    )
    return outcome


def mint_artifact(
    app: "FastAPI",
    sid: str,
    *,
    name: str,
    workspace_id: str,
    evidence: IdentityEvidence,
    kind: ArtifactKind,
    mechanism: Mechanism,
    producer: dict[str, Any] | None = None,
    custody: Custody = Custody.WORKSPACE_REFERENCED,
    path: str = "",
    annotation: str = "",
    turn_id: str = "",
    trace_id: str = "",
    not_ingested_size: int | None = None,
) -> Optional[ArtifactVersion]:
    """Back-compat projection over :func:`mint_artifact_outcome`.

    Returns the operative :class:`ArtifactVersion` (the freshly-minted one, or the
    byte-identical version deduped onto) or ``None``. Callers that need to know
    whether a NEW version was assigned (finding [7]) call
    :func:`mint_artifact_outcome` and read ``outcome.created``.
    """
    outcome = mint_artifact_outcome(
        app,
        sid,
        name=name,
        workspace_id=workspace_id,
        evidence=evidence,
        kind=kind,
        mechanism=mechanism,
        producer=producer,
        custody=custody,
        path=path,
        annotation=annotation,
        turn_id=turn_id,
        trace_id=trace_id,
        not_ingested_size=not_ingested_size,
    )
    return outcome.version if outcome is not None else None


def mint_tool_declared_outputs(
    app: "FastAPI",
    sid: str,
    *,
    tool_name: str,
    effective_args: dict[str, Any],
    call_id: str,
    workspace_id: str,
    turn_id: str = "",
    trace_id: str = "",
    call_started_at: float | None = None,
    result: Any = None,
) -> list[ArtifactVersion]:
    """Mint one artifact per grounded, existing tool-declared output path (seam a).

    Called from the gact tool observer's ``completed`` phase. Two declaration
    channels, identical containment + existence + freshness rules: the **arg**
    channel (:func:`grounded_output_paths` — output-path ARG values) and, when no
    arg carries the path, the **result** channel (:func:`result_declared_paths` —
    GAP A, S5 #971): a tool taking a destination dir + derived filename
    (``ndp_stage_resource`` → ``local_path``) writes an intermediate the arg channel
    can't see, so reading the declared result path mints it and the downstream call
    pins it as a hash-pair edge (not external-with-sha). Arg wins a dedup tie.

    For each, stat + stream sha256 (over the threshold → ``stat-pinned``) and mint an
    ``artifact.created`` (mechanism ``tool-schema``, producing ``call_id``). Guards
    are typed skips (a mint failure must never break a turn): absent path, path
    outside the workspace root (owner decision 10), or a pre-existing untouched file
    routed to the honest drift reconcile (finding [8]). Content CHANGED outside the
    call still mints — designation is designation.
    """
    from clio_agent.gact.artifacts.cas import ingest_identity  # noqa: PLC0415
    from clio_agent.gact.artifacts.designation import (  # noqa: PLC0415
        grounded_output_paths,
        kind_for_path,
        result_declared_paths,
    )

    root = _workspace_root(app, workspace_id)
    minted: list[ArtifactVersion] = []
    seen: set[str] = set()
    # (channel, label, path) — arg channel FIRST so it wins a dedup tie over a
    # result that merely echoes the same arg path.
    designated = [("arg", k, v) for k, v in grounded_output_paths(effective_args).items()]
    designated += [("result", k, v) for k, v in result_declared_paths(result).items()]
    for channel, label, raw_path in designated:
        path = Path(raw_path)
        if root is None:
            logger.warning(
                "artifact mint skipped reason=containment_unresolved tool=%s %s=%s path=%s",
                tool_name,
                channel,
                label,
                raw_path,
            )
            continue
        if not _contained(path, root):
            logger.warning(
                "artifact mint skipped reason=containment_rejected tool=%s %s=%s path=%s root=%s",
                tool_name,
                channel,
                label,
                raw_path,
                root,
            )
            continue
        try:
            resolved = str(path.expanduser().resolve(strict=False))
        except OSError:
            resolved = str(path)
        if resolved in seen:
            continue
        if not path.is_file():
            logger.info(
                "artifact mint skipped reason=designated_path_absent tool=%s %s=%s path=%s",
                tool_name,
                channel,
                label,
                raw_path,
            )
            continue
        try:
            # S6 (#972): stream the identity hash ONCE and tee small bytes into CAS
            # (custody ``cas``); over threshold → referenced + typed not_ingested_size.
            ingested = ingest_identity(path, workspace_root=root)
        except OSError:
            logger.warning(
                "artifact mint skipped reason=stat_hash_failed tool=%s %s=%s path=%s",
                tool_name,
                channel,
                label,
                raw_path,
            )
            continue
        evidence = ingested.evidence
        seen.add(resolved)
        name = artifact_name_for_path(path)
        # A declared output the tool provably did NOT write this call (mtime predates
        # the call) is a DRIFT re-observation → the honest reconcile (producing=False),
        # never a false producing tool-schema mint (finding [2/6], #966.10).
        handled, outcome = reconcile_if_tool_drift(
            app,
            sid,
            name=name,
            workspace_id=workspace_id,
            path=str(path),
            evidence=evidence,
            call_started_at=call_started_at,
            mechanism=Mechanism.TOOL_SCHEMA,
            turn_id=turn_id,
            trace_id=trace_id,
        )
        if handled:
            if outcome is not None and outcome.created:
                minted.append(outcome.version)
            continue
        # The designation basis rides the producer: ``arg`` names the output-path arg;
        # ``result_key`` names the structured-result key (GAP A, designation-by-result).
        producer: dict[str, Any] = {
            "call_id": call_id,
            "tool": tool_name,
            "session_id": sid,
            "turn_id": turn_id,
            "designation": f"tool-{channel}",
            ("result_key" if channel == "result" else "arg"): label,
        }
        version = mint_artifact(
            app,
            sid,
            name=name,
            workspace_id=workspace_id,
            evidence=evidence,
            kind=kind_for_path(path),
            mechanism=Mechanism.TOOL_SCHEMA,
            producer=producer,
            custody=ingested.custody,
            path=str(path),
            turn_id=turn_id,
            trace_id=trace_id,
            not_ingested_size=ingested.not_ingested_size,
        )
        if version is not None:
            minted.append(version)
    return minted


def mint_pack_declared_paths(
    app: "FastAPI",
    sid: str,
    *,
    workflow_state: dict[str, Any],
    path_specs: Any,
    workspace_id: str,
    turn_id: str = "",
    trace_id: str = "",
) -> list[ArtifactVersion]:
    """Mint artifacts for pack-declared ``workflow_state.artifact_paths`` (seam c).

    The secondary/optional designation channel (owner decision #966.1): at turn
    finalize, hash any declared path that exists on disk INSIDE the bound workspace
    and mint it (mechanism ``harness`` — the harness hashes in-hand — with a
    ``designation=pack-declared`` producer note making the weaker basis visible).
    This is the model-influenced channel, so containment is enforced BEFORE any
    stat/hash (owner decision 10): a declared path outside the workspace root is
    skipped with a typed ``containment_rejected`` reason — no read, no mint. Never
    load-bearing: a path already minted by seam (a) with identical content
    deduplicates at the mint (no new version). Best-effort.
    """
    from clio_agent.gact.artifacts.cas import ingest_identity  # noqa: PLC0415
    from clio_agent.gact.artifacts.designation import (  # noqa: PLC0415
        kind_for_path,
        pack_declared_paths,
    )

    root = _workspace_root(app, workspace_id)
    minted: list[ArtifactVersion] = []
    for raw_path in pack_declared_paths(workflow_state, path_specs):
        path = Path(raw_path)
        if root is None:
            logger.warning("artifact mint skipped reason=containment_unresolved path=%s", raw_path)
            continue
        if not _contained(path, root):
            logger.warning(
                "artifact mint skipped reason=containment_rejected path=%s root=%s",
                raw_path,
                root,
            )
            continue
        try:
            if not path.is_file():
                continue
            ingested = ingest_identity(path, workspace_root=root)
        except OSError:
            logger.warning(
                "artifact mint skipped reason=pack_declared_stat_failed path=%s", raw_path
            )
            continue
        evidence = ingested.evidence
        name = artifact_name_for_path(path)
        # A declared file reverted to a KNOWN NON-HEAD version's bytes would DEDUP onto
        # that old version and silently heal the gap under a producing mint → route it
        # through the reconcile RE-LINK instead (finding [2/6], wired like seam a).
        handled, outcome = reconcile_if_content_revert(
            app,
            sid,
            name=name,
            workspace_id=workspace_id,
            path=str(path),
            evidence=evidence,
            mechanism=Mechanism.HARNESS,
            turn_id=turn_id,
            trace_id=trace_id,
        )
        if handled:
            if outcome is not None and outcome.created:
                minted.append(outcome.version)
            continue
        version = mint_artifact(
            app,
            sid,
            name=name,
            workspace_id=workspace_id,
            evidence=evidence,
            kind=kind_for_path(path),
            mechanism=Mechanism.HARNESS,
            producer={
                "designation": "pack-declared",
                "session_id": sid,
                "turn_id": turn_id,
            },
            custody=ingested.custody,
            path=str(path),
            turn_id=turn_id,
            trace_id=trace_id,
            not_ingested_size=ingested.not_ingested_size,
        )
        if version is not None:
            minted.append(version)
    return minted


def _session_workspace_id(app: "FastAPI", sid: str) -> str:
    """Resolve the workspace id for a session (``""`` when unresolved)."""
    store = getattr(app.state, "sessions", None) or getattr(app.state, "session_store", None)
    if store is None:
        return ""
    session = store.get(sid)
    return str(getattr(session, "workspace_id", "") or "") if session is not None else ""


def observe_tool_completion(
    app: "FastAPI",
    sid: str,
    *,
    tool_name: str,
    effective_args: dict[str, Any],
    call_id: str,
    call_started_at: float | None = None,
) -> None:
    """Seam (a) entry point: mint tool-declared outputs for a completed call.

    Fully self-contained + guarded so the gact tool observer calls it in one line:
    resolves the workspace id + turn/trace ids, mints, and swallows any failure
    with a typed reason. A live artifact mint must never break a turn.
    The observer's tool-start epoch (its ``_OBSERVER_CALL_T0`` thread-local — seam
    (a) runs in the same observer worker thread) lets seam (a) skip a pre-existing
    untouched designated file (finding [8]); ``call_started_at`` overrides it.
    """
    try:
        from clio_agent.gact import context as _ctx  # noqa: PLC0415

        mint_tool_declared_outputs(
            app,
            sid,
            tool_name=tool_name,
            effective_args=effective_args,
            call_id=call_id,
            workspace_id=_session_workspace_id(app, sid),
            turn_id=_ctx.active_turn_id(),
            trace_id=_ctx.active_trace_id(),
            call_started_at=call_started_at
            if call_started_at is not None
            else _observer_call_started_at(),
        )
    except Exception:  # noqa: BLE001 — a live artifact mint must never break a turn
        logger.warning(
            "artifact mint skipped reason=observer_mint_failed session=%s tool=%s call_id=%s",
            sid,
            tool_name,
            call_id,
        )


def mint_harness_write(
    app: "FastAPI", session: Any, target: str, write_result: dict[str, Any]
) -> None:
    """Seam (b) entry point: mint an ``artifact.created`` for a user-approved write.

    The bytes flowed through the harness (``write_text_with_policy``), so the write
    itself is the evidence: mechanism ``harness``, ``hashed-at-use`` from the
    ``sha256`` the writer returned in-hand. The mint rides the SSE wire like every
    other (S2 #968). The ACTIVE turn id is threaded from the turn-identity
    contextvar (:func:`context.active_turn_id`) so that when this write happens
    DURING a turn its version is buffered under that turn and drains to a
    ``resource_link`` part at finalize — parity with seams (a)/(c); an out-of-turn
    apply leaves it empty and simply buffers nothing that drains. Fully guarded so
    the gact-side ``fs_apply_edit_write`` caller invokes it in one line — an
    artifact mint must never break the approved write.
    """
    try:
        from clio_agent.gact import context as _ctx  # noqa: PLC0415
        from clio_agent.gact.artifacts.designation import kind_for_path  # noqa: PLC0415

        sha256 = str(write_result.get("sha256") or "")
        if not sha256:
            logger.warning(
                "artifact mint skipped reason=harness_write_missing_sha256 path=%s", target
            )
            return
        evidence = IdentityEvidence.hashed_at_use(
            sha256=sha256, size_bytes=int(write_result.get("size_bytes") or 0)
        )
        mint_artifact(
            app,
            str(getattr(session, "id", "") or ""),
            name=artifact_name_for_path(target),
            workspace_id=str(getattr(session, "workspace_id", "") or ""),
            evidence=evidence,
            kind=kind_for_path(target),
            mechanism=Mechanism.HARNESS,
            producer={
                "session_id": str(getattr(session, "id", "") or ""),
                "tool": "fs_apply_edit_write",
                "turn_id": _ctx.active_turn_id(),
            },
            custody=Custody.WORKSPACE_REFERENCED,
            path=target,
            turn_id=_ctx.active_turn_id(),
            trace_id=_ctx.active_trace_id(),
        )
    except Exception:  # noqa: BLE001 — an artifact mint must never break an approved write
        logger.warning("artifact mint skipped reason=harness_mint_failed path=%s", target)


def _created_payload(
    event_id: str, workspace_id: str, name: str, version: ArtifactVersion
) -> dict[str, Any]:
    """Build the durable ``artifact.created`` payload (the v1 fold-source of truth).

    Delegates to the single version-record payload builder with the revision edge
    OFF, so the v1 ``created`` payload stays byte-identical to S1/S2 (SPEC §7.6) and
    the v2+ ``version.added`` payload — which adds the four edge/marker fields —
    shares the exact same base shape.
    """
    return version_record_payload(event_id, workspace_id, name, version, revision_edge=False)


__all__ = [
    "artifact_name_for_path",
    "clear_turn_artifacts",
    "compute_identity",
    "drain_turn_artifacts",
    "hash_max_file_bytes",
    "mint_artifact",
    "mint_artifact_outcome",
    "mint_harness_write",
    "mint_pack_declared_paths",
    "mint_tool_declared_outputs",
    "observe_tool_completion",
]
