"""The artifact minting funnel — identity hashing + the three S1 mint seams.

One funnel (:func:`mint_artifact`) builds an immutable version, emits the durable
``artifact.created`` semantic event (trace-only this slice — the sole
artifact-event emitter), folds it into the registry projection and patches the
SessionStore badge index. The three seams feed it:

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
    get_registry,
    patch_session_index,
)

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

#: Default ceiling on hashing a designated output at mint. Over this, the version
#: is recorded ``stat-pinned`` (typed, permanent) rather than paying multi-GB I/O
#: on the turn thread (design resolution 5b). Config-first (#985 conventions).
_DEFAULT_HASH_MAX_FILE_BYTES = 64 * 1024 * 1024

_HASH_CHUNK_BYTES = 1024 * 1024


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


def artifact_name_for_path(path: str | Path) -> str:
    """The logical artifact name for a designated output path (its basename).

    Basename keys the logical chain so a tool overwriting the same deliverable
    (``timeseries.png`` re-rendered) folds into a version chain, matching how the
    harness/grader collects deliverables by filename. A later slice may key on the
    workspace-relative path when directory disambiguation is needed.
    """
    return Path(str(path)).name


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
) -> Optional[ArtifactVersion]:
    """Mint one artifact version: emit ``artifact.created`` (trace-only), then fold.

    The single mint funnel for all three S1 seams. Computes the next version
    number against the registry's existing chain for ``(workspace_id, name)``,
    emits the durable semantic event via ``_emit_semantic_event`` (the sole
    artifact-event emitter), then folds the event back into the registry and
    patches the SessionStore badge index. Returns the minted version, or the
    already-known version when the fold was a replay/conflict.

    ``plan`` kind is RESERVED — minting it raises ``ValueError`` (a reserved
    capability leaked; typed, not silently downgraded).
    """
    if kind in RESERVED_KINDS:
        raise ValueError(
            f"artifact kind {kind.value!r} is reserved and cannot be minted this campaign"
        )

    registry = get_registry(app)
    existing = registry.get(workspace_id, name)
    version_number = existing.next_version_number() if existing is not None else 1

    version = ArtifactVersion(
        version=version_number,
        kind=kind,
        custody=custody,
        mechanism=mechanism,
        evidence=evidence,
        producer=dict(producer or {}),
        path=path,
        created_at=_now_iso(),
        annotation=annotation,
    )

    from clio_agent.gact.semantic_events import _event_id  # noqa: PLC0415

    event_id = _event_id()
    payload = _created_payload(event_id, workspace_id, name, version)

    from clio_agent.gact.runtime.globals import _emit_semantic_event  # noqa: PLC0415

    _emit_semantic_event(
        app,
        sid,
        ARTIFACT_CREATED_EVENT,
        turn_id=turn_id,
        trace_id=trace_id,
        status="completed",
        summary=f"Artifact {name} v{version_number} created.",
        actor={"mechanism": mechanism.value},
        subject={"artifact_id": version.artifact_id, "name": name, "workspace_id": workspace_id},
        payload=payload,
        # Trace-only this slice: keep it off the SSE detail lane (S2 adds the wire).
        detail_level="off",
    )

    result = registry.fold_payload(payload)
    patch_session_index(app, sid, registry, workspace_id)
    if not result.applied:
        logger.info(
            "artifact mint fold non-applied reason=%s ws=%s name=%s version=%d",
            result.reason,
            workspace_id,
            name,
            version_number,
        )
        return result.version
    return version


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
) -> list[ArtifactVersion]:
    """Mint one artifact per grounded, existing tool-declared output path (seam a).

    Called from the gact tool observer's ``completed`` phase for a successful
    call. For each designated output arg (:func:`grounded_output_paths`) that
    resolves to an existing file, stat + stream sha256 (over the threshold →
    typed ``stat-pinned``) and mint an ``artifact.created`` with mechanism
    ``tool-schema`` carrying the producing ``call_id``. A designated-but-absent
    path (tool declared an out but wrote nothing) is skipped with a typed reason,
    never an error — mint failures must never break a turn.
    """
    from clio_agent.gact.artifacts.designation import (  # noqa: PLC0415
        grounded_output_paths,
        kind_for_path,
    )

    minted: list[ArtifactVersion] = []
    for arg_name, raw_path in grounded_output_paths(effective_args).items():
        path = Path(raw_path)
        if not path.is_file():
            logger.info(
                "artifact mint skipped reason=designated_path_absent tool=%s arg=%s path=%s",
                tool_name,
                arg_name,
                raw_path,
            )
            continue
        try:
            evidence = compute_identity(path)
        except OSError:
            logger.warning(
                "artifact mint skipped reason=stat_hash_failed tool=%s arg=%s path=%s",
                tool_name,
                arg_name,
                raw_path,
            )
            continue
        version = mint_artifact(
            app,
            sid,
            name=artifact_name_for_path(path),
            workspace_id=workspace_id,
            evidence=evidence,
            kind=kind_for_path(path),
            mechanism=Mechanism.TOOL_SCHEMA,
            producer={
                "call_id": call_id,
                "tool": tool_name,
                "session_id": sid,
                "turn_id": turn_id,
                "arg": arg_name,
            },
            custody=Custody.WORKSPACE_REFERENCED,
            path=str(path),
            turn_id=turn_id,
            trace_id=trace_id,
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
    finalize, hash any declared path that exists on disk and mint it (mechanism
    ``harness`` — the harness hashes in-hand — with a ``designation=pack-declared``
    producer note making the weaker basis visible). Never load-bearing: a path
    already minted by seam (a) folds as a same-sha replay (no-op). Best-effort.
    """
    from clio_agent.gact.artifacts.designation import (  # noqa: PLC0415
        kind_for_path,
        pack_declared_paths,
    )

    minted: list[ArtifactVersion] = []
    for raw_path in pack_declared_paths(workflow_state, path_specs):
        path = Path(raw_path)
        try:
            if not path.is_file():
                continue
            evidence = compute_identity(path)
        except OSError:
            logger.warning(
                "artifact mint skipped reason=pack_declared_stat_failed path=%s", raw_path
            )
            continue
        version = mint_artifact(
            app,
            sid,
            name=artifact_name_for_path(path),
            workspace_id=workspace_id,
            evidence=evidence,
            kind=kind_for_path(path),
            mechanism=Mechanism.HARNESS,
            producer={
                "designation": "pack-declared",
                "session_id": sid,
                "turn_id": turn_id,
            },
            custody=Custody.WORKSPACE_REFERENCED,
            path=str(path),
            turn_id=turn_id,
            trace_id=trace_id,
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
) -> None:
    """Seam (a) entry point: mint tool-declared outputs for a completed call.

    Fully self-contained + guarded so the gact tool observer calls it in one line:
    resolves the workspace id + turn/trace ids, mints, and swallows any failure
    with a typed reason. A live artifact mint must never break a turn.
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
        )
    except Exception:  # noqa: BLE001 — a live artifact mint must never break a turn
        logger.warning(
            "artifact mint skipped reason=observer_mint_failed session=%s tool=%s call_id=%s",
            sid,
            tool_name,
            call_id,
        )


def mint_harness_write(app: "FastAPI", session: Any, target: str, write_result: dict[str, Any]) -> None:
    """Seam (b) entry point: mint an ``artifact.created`` for a user-approved write.

    The bytes flowed through the harness (``write_text_with_policy``), so the write
    itself is the evidence: mechanism ``harness``, ``hashed-at-use`` from the
    ``sha256`` the writer returned in-hand. Trace-only this slice. Fully guarded so
    the gact-side ``fs_apply_edit_write`` caller invokes it in one line — an
    artifact mint must never break the approved write.
    """
    try:
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
            },
            custody=Custody.WORKSPACE_REFERENCED,
            path=target,
        )
    except Exception:  # noqa: BLE001 — an artifact mint must never break an approved write
        logger.warning("artifact mint skipped reason=harness_mint_failed path=%s", target)


def _created_payload(
    event_id: str, workspace_id: str, name: str, version: ArtifactVersion
) -> dict[str, Any]:
    """Build the durable ``artifact.created`` payload (the fold-source of truth)."""
    return {
        "event_id": event_id,
        "artifact_id": version.artifact_id,
        "workspace_id": workspace_id,
        "name": name,
        "version": version.version,
        "kind": version.kind.value,
        "custody": version.custody.value,
        "mechanism": version.mechanism.value,
        "sha256": version.sha256,
        "size_bytes": version.size_bytes,
        "path": version.path,
        "created_at": version.created_at,
        "annotation": version.annotation,
        "producer": dict(version.producer),
        "evidence": {
            "evidence_class": version.evidence.evidence_class.value,
            "authority": version.evidence.authority,
            "mtime": version.evidence.mtime,
        },
    }


__all__ = [
    "artifact_name_for_path",
    "compute_identity",
    "hash_max_file_bytes",
    "mint_artifact",
    "mint_harness_write",
    "mint_pack_declared_paths",
    "mint_tool_declared_outputs",
    "observe_tool_completion",
]
