"""Provenance edge detection for TransformRecords (S5 #971) — used + authority.

Split out of :mod:`clio_agent.gact.artifacts.transforms` (no-accretion — the record
model + recording orchestration own that file; edge DISCOVERY owns this one). Two
detectors, both **precision over recall** (owner decision #966.10):

* :func:`detect_used_edges` — walk the call args for strings that resolve to an
  existing file inside the workspace root and match a registered artifact by path,
  then re-hash under the threshold (hash equal → ``schema-arg`` + ``hash-pair``;
  hash differs → mint a GAP version FIRST and point the edge at it; over threshold
  → ``stat-pinned`` labeled). An existing contained file NOT in the registry → an
  ``external:<path>`` edge; anything else → no edge.
* :func:`detect_authority_edges` — a SPECIFIC catalog input (``stage_resource`` /
  ``get_dataset_details``) registers its catalog resource ``url`` / ``id`` as an
  ``authority``-asserted input. NDP results carry no checksum/ETag/DOI (verified
  against the real server), so the catalog URL/UUID IS the authority. A broad
  ``search_datasets`` / ``list_organizations`` is DISCOVERY — its hits were listed,
  not consumed (finding [2]) — so it edges nothing and records a typed
  ``catalog_hits_not_consumed`` note; absent any recognized NDP shape → a typed skip.

Both detectors return an :class:`EdgeScan` (edges + typed notes) so a deliberate
non-edge (precision over recall) stays DETECTABLE on the record, never silent.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, Optional

from clio_agent.gact.artifacts.designation import (
    ARTIFACT_SUFFIXES,
    OUTPUT_PATH_ARG_NAMES,
    kind_for_path,
)
from clio_agent.gact.artifacts.records import ArtifactKind, ArtifactVersion, Mechanism
from clio_agent.gact.artifacts.transform_types import EdgeEvidence, EdgeRole, ProvEdge

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)


class EdgeScan(NamedTuple):
    """One detector pass: the edges it minted plus typed notes (detectable misses).

    ``notes`` are the honest, queryable residue of precision-over-recall (owner
    decision #966.10): a candidate the detector deliberately did NOT edge — a
    freshly-written output under a non-designation arg name
    (``unminted_output_candidate``), a path-looking arg that never resolved to a
    workspace file (``unresolved_path_arg``), or a broad catalog search whose hits
    were listed, not consumed (``catalog_hits_not_consumed``). The record carries
    them so the miss is DETECTABLE in the trace, never a silent drop.
    """

    edges: list[ProvEdge]
    notes: list[dict[str, Any]]


#: The NDP MCP tool that names a SPECIFIC catalog resource the agent asked for →
#: its ``url`` / ``id`` is an ``authority``-asserted USED input.
_NDP_DETAILS_TOOLS: frozenset[str] = frozenset({"get_dataset_details"})
#: NDP DISCOVERY tools — a broad ``search_datasets`` / ``list_organizations``
#: returns RESULTS the agent listed, NOT inputs it consumed (finding [2]). Their
#: hits never become ``used`` authority edges; the record instead notes
#: ``catalog_hits_not_consumed`` so the discovery is honestly recorded.
_NDP_SEARCH_TOOLS: frozenset[str] = frozenset({"search_datasets", "list_organizations"})
_NDP_STAGE_TOOL = "stage_resource"
# #1200: clio-agent's own bounded relay transfer. Named here for the same reason
# ``stage_resource`` is -- the tool's result carries a REMOTE origin the generic
# args-based scan cannot see, and that origin is the used-edge authority for the
# local file it wrote. Both the gateway-mounted name and the bare mount name are
# listed because the short-name match sees whichever the dispatch used.
_RELAY_FETCH_TOOLS: frozenset[str] = frozenset({"relay_fetch_artifact", "fetch_artifact"})

#: Bound on how many authority-asserted catalog resources one result contributes,
#: so a large search result cannot grow a transform record unboundedly.
_MAX_AUTHORITY_EDGES = 32
#: Bound on recursion + fan-out when walking call args for candidate paths.
_MAX_ARG_STRINGS = 64
_MAX_ARG_DEPTH = 4
#: A path arg is never megabytes — a large inline value (``content`` / ``cmd``
#: heredoc) can exceed the OS path limit and is NEVER a candidate path, so it is
#: skipped before any ``Path()``/``resolve()`` (which raises on an over-long path).
_MAX_CANDIDATE_STRLEN = 4096


# --------------------------------------------------------------------------- #
# Used-edge detection (item 3) — precision over recall (owner decision #966.10).
# --------------------------------------------------------------------------- #


#: Declared-channel arg names EXCLUDED from the generic heuristic scan — each has
#: its OWN dedicated resolver, so letting the generic path-guesser ALSO walk it
#: would produce a redundant/duplicate edge for the same input (#1191): ``used``
#: is create_artifact's own explicit input-refs field, resolved by
#: :mod:`clio_agent.gact.artifacts.declared_used_edges`.
_DECLARED_CHANNEL_ARG_NAMES: frozenset[str] = frozenset({"used"})


def _candidate_arg_strings(args: Any) -> list[tuple[str, str]]:
    """Walk call args for candidate path strings as ``(arg_name, value)`` pairs.

    Output-path args (:data:`OUTPUT_PATH_ARG_NAMES`) are EXCLUDED — those are the
    generated side, not used inputs. :data:`_DECLARED_CHANNEL_ARG_NAMES` (``used``)
    is ALSO excluded — a dedicated resolver already owns it, so the generic
    heuristic must not double-edge the same ref. Nested dicts/lists are walked to a
    bounded depth and fan-out so a pathological arg blob cannot explode the scan
    (precision over recall — a missed deep path is a lost edge, never a false one).
    """
    out: list[tuple[str, str]] = []

    def _excluded(key: str) -> bool:
        return key in OUTPUT_PATH_ARG_NAMES or key in _DECLARED_CHANNEL_ARG_NAMES

    def walk(name: str, value: Any, depth: int) -> None:
        if len(out) >= _MAX_ARG_STRINGS or depth > _MAX_ARG_DEPTH:
            return
        if isinstance(value, str):
            if value.strip() and len(value) <= _MAX_CANDIDATE_STRLEN:
                out.append((name, value))
            return
        if isinstance(value, dict):
            for key, sub in value.items():
                if _excluded(str(key)):
                    continue
                walk(str(key), sub, depth + 1)
        elif isinstance(value, (list, tuple)):
            for sub in value:
                walk(name, sub, depth + 1)

    if isinstance(args, dict):
        for key, value in args.items():
            if _excluded(str(key)):
                continue
            walk(str(key), value, 0)
    return out


def _looks_like_path(raw: str) -> bool:
    """Whether a bare arg string plausibly NAMES a file (separator/suffix heuristic).

    Gates the ``unresolved_path_arg`` note (finding [4]) so ordinary non-path string
    args (queries, city names, formats like ``"png"``) never emit a spurious miss —
    only a value that carries a path separator or a recognized artifact suffix is
    treated as a would-be input whose non-resolution is worth recording.
    """
    if "/" in raw or "\\" in raw:
        return True
    return Path(raw).suffix.lower() in ARTIFACT_SUFFIXES


def contributing_workspace_ids(app: "FastAPI", workspace_id: str) -> Optional[set[str]]:
    """The cross-job contributing set for ``workspace_id`` (P3.1 #1038), or ``None``.

    Every workspace whose ``Workspace.root_path`` resolves to the SAME absolute path
    as the current workspace's — the "same job filesystem, separate top-level job"
    boundary (a re-run in the same directory registers under a different
    workspace_id). Computed at the CALLER (which holds ``app``) and threaded into
    :func:`detect_used_edges` so that detector keeps its acyclic position.

    Returns ``None`` (→ same-workspace-only resolution) when the workspace registry
    is unavailable or the current workspace has no resolvable root — a typed,
    non-widening skip (a missing registry NEVER silently widens the bind set). The
    returned set always includes ``workspace_id`` itself when the root resolves.
    """
    store = getattr(app.state, "workspaces", None)
    if store is None or not workspace_id:
        return None
    try:
        current = store.get(workspace_id)
    except Exception:  # noqa: BLE001 — an unresolvable store degrades to same-workspace-only
        logger.info("cross-job bind set skipped reason=workspace_store_error ws=%s", workspace_id)
        return None
    current_root = str(getattr(current, "root_path", "") or "").strip() if current else ""
    if not current_root:
        logger.info(
            "cross-job bind set skipped reason=no_current_root_path ws=%s "
            "(narrowed to same-workspace-only)",
            workspace_id,
        )
        return None
    try:
        target_root = Path(current_root).expanduser().resolve(strict=False)
    except (OSError, ValueError):
        logger.info(
            "cross-job bind set skipped reason=current_root_unresolvable ws=%s root=%s "
            "(narrowed to same-workspace-only)",
            workspace_id,
            current_root,
        )
        return None
    try:
        rows = list(store.list())
    except Exception:  # noqa: BLE001 — a store without list() degrades to same-workspace-only
        logger.info(
            "cross-job bind set skipped reason=workspace_list_unavailable ws=%s", workspace_id
        )
        return None
    ids: set[str] = set()
    for ws in rows:
        raw = str(getattr(ws, "root_path", "") or "").strip()
        if not raw:
            continue
        try:
            if Path(raw).expanduser().resolve(strict=False) == target_root:
                ids.add(str(getattr(ws, "id", "") or ""))
        except (OSError, ValueError):
            continue
    ids.discard("")
    return ids or None


def detect_used_edges(
    app: "FastAPI",
    sid: str,
    *,
    args: dict[str, Any],
    workspace_id: str,
    turn_id: str,
    trace_id: str,
    call_started_at: Optional[float] = None,
    allowed_workspace_ids: Optional[set[str]] = None,
    call_id: str = "",
    tool_name: str = "",
) -> EdgeScan:
    """Detect ``used`` edges from call args (item 3 — precision over recall).

    For each candidate arg string: a RELATIVE/bare value is resolved against the
    WORKSPACE ROOT (the base the gateway gives the tool subprocess — finding [4]),
    NOT the server CWD; it must then resolve to an existing file INSIDE the root
    (else NO edge, and a path-looking miss records ``unresolved_path_arg``). A file
    whose ``mtime`` is at/after ``call_started_at`` was plausibly WRITTEN by this
    call, so it is NEVER a used input (finding [1] — the mint side's freshness
    guard, mirrored here); the miss records ``unminted_output_candidate`` so it is
    detectable (designation stays the only mint path). A registry match re-hashes
    under the threshold — hash equal → ``hash-pair``; hash differs → mint a GAP
    version FIRST and point the edge at it (the ``note`` carries the ACTUAL
    reconcile class — finding [3]); over threshold → ``stat-pinned`` labeled. An
    existing contained file NOT in the registry → an ``external:<path>`` edge.

    ``allowed_workspace_ids`` (P3.1 #1038 — cross-job lineage bind) is the CROSS-JOB
    contributing set the CALLER computed (every workspace sharing this job's
    ``root_path`` — see :func:`contributing_workspace_ids`). ``None`` keeps the
    same-workspace-only resolution (drop-in). When provided, a producer registered
    under a DIFFERENT workspace_id sharing the root becomes visible: the bound edge
    REUSES the foreign version's ``artifact_id`` (never a minted local id) and carries
    ``cross_workspace_bind=True``. Threaded IN (never reached via ``app`` here) so
    this detector keeps its acyclic position.

    ``call_id`` / ``tool_name`` (A8, #1176) identify the CONSUMING call whose args
    are being scanned — the caller (:func:`~clio_agent.gact.artifacts.transforms.record_transform`)
    always has both. They stamp the producer of a designate-on-USE script mint
    (below) so that version is joinable by session/call exactly like every other
    tool-schema mint; omitted (module default ``""``) only by a caller — a direct
    unit-test invocation — that genuinely has no call to attribute, never invented.
    """
    from clio_agent.gact.artifacts.minting import (  # noqa: PLC0415
        _contained,
        _workspace_root,
        compute_identity,
        mint_artifact_outcome,
    )
    from clio_agent.gact.artifacts.registry import get_registry  # noqa: PLC0415

    root = _workspace_root(app, workspace_id)
    if root is None:
        return EdgeScan([], [])
    registry = get_registry(app)
    edges: list[ProvEdge] = []
    notes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for arg_name, raw in _candidate_arg_strings(args):
        try:
            candidate = Path(raw)
            # Resolve a relative/bare value against the WORKSPACE ROOT, not the server
            # CWD (finding [4] — the tool subprocess reads it inside the workspace).
            if not candidate.is_absolute():
                candidate = root / raw
            contained = _contained(candidate, root)
        except (TypeError, ValueError, OSError):
            # An over-long / malformed value is never a workspace path (defensive).
            continue
        if not contained:
            continue
        try:
            is_file = candidate.is_file()
        except (OSError, ValueError):
            is_file = False
        if not is_file:
            # A path-looking arg that never resolved to a workspace file is a
            # DETECTABLE miss (finding [4]); a bare query string is not.
            if _looks_like_path(raw):
                notes.append({"reason": "unresolved_path_arg", "arg": arg_name, "value": raw})
            continue
        resolved = str(candidate.expanduser().resolve(strict=False))
        if resolved in seen:
            continue
        seen.add(resolved)
        # Freshness guard (finding [1]): a file whose mtime is at/after the call's
        # start was plausibly WRITTEN by this call — never a used input. Record the
        # miss so it is detectable; designation remains the only mint path.
        try:
            mtime = candidate.stat().st_mtime
        except OSError:
            continue
        if call_started_at is not None and mtime >= call_started_at:
            notes.append({"reason": "unminted_output_candidate", "arg": arg_name, "path": resolved})
            continue
        try:
            evidence = compute_identity(resolved)
        except OSError:
            continue
        match = registry.find_version_by_path(
            workspace_id, resolved, allowed_workspace_ids=allowed_workspace_ids
        )
        if match is None:
            # Designate-on-USE for a CONSUMED script (P3.2 #1039). A used `.py`/`.sh`
            # is minted as its OWN SCRIPT version so a transform whose instrument is a
            # script pins that script as a first-class dependency (its hash +
            # artifact_id ride the used edge, which ``_script_instrument`` reads). The
            # mint fires ONLY here — inside ``match is None`` — so a script already
            # registered (locally OR by a sibling job under the shared root, P3.1
            # #1038) reuses the existing/foreign id and never forks a local v1. It is
            # minted under ``workspace_id`` (the LOCAL/consuming job — there is no
            # foreign producer, that is why the match is None), with ``producing=False``
            # (an observed input, not a generated output) and ``TOOL_SCHEMA`` (a known
            # basis → renders as an ``artifact`` node, not a ``gap``). Only a script
            # with a REAL hash mints; a stat-pinned (oversized) script has no sha256 →
            # it stays the external leaf below (matching the arg channel's semantics,
            # and ``_script_instrument`` skips a hashless edge anyway). A non-SCRIPT
            # suffix (plain data/report input) also falls through unchanged.
            if kind_for_path(resolved) is ArtifactKind.SCRIPT and evidence.sha256:
                outcome = mint_artifact_outcome(
                    app,
                    sid,
                    name=Path(resolved).name,
                    workspace_id=workspace_id,
                    evidence=evidence,
                    kind=ArtifactKind.SCRIPT,
                    mechanism=Mechanism.TOOL_SCHEMA,
                    producing=False,
                    path=resolved,
                    turn_id=turn_id,
                    trace_id=trace_id,
                    # A8 (#1176): this mint seam was dropping ``producer`` entirely
                    # (no kwarg → ``mint_artifact_outcome`` defaults it to ``{}``),
                    # so a consumed script's OWN version carried no session/call
                    # identity — unlike every other tool-schema mint. That breaks
                    # the session-scoped artifacts route (it joins on
                    # ``producer.session_id``) for exactly this record. Stamp the
                    # SAME shape the other tool-schema seams use: the consuming
                    # call's session/tool/call_id, plus a designation note so the
                    # weaker "observed as a used input, not a designated output"
                    # basis stays visible (never invented — arrives as "" only
                    # when the caller genuinely has no call to attribute).
                    producer={
                        "designation": "used-script",
                        "session_id": sid,
                        "tool": tool_name,
                        "call_id": call_id,
                        "turn_id": turn_id,
                    },
                )
                if outcome is not None:
                    minted_version = outcome.version
                    edges.append(
                        ProvEdge(
                            role=EdgeRole.USED,
                            evidence=EdgeEvidence.HASH_PAIR,
                            artifact_id=minted_version.artifact_id,
                            sha256=evidence.sha256,
                            name=Path(resolved).name,
                            version=minted_version.version,
                            path=resolved,
                            arg=arg_name,
                        )
                    )
                    continue
                # A typed mint skip (never silent) → fall through to the external leaf
                # so the consumed script still leaves a detectable used edge.
                logger.info(
                    "used script mint skipped reason=mint_returned_none session=%s path=%s",
                    sid,
                    resolved,
                )
            # Existing contained file, not registered → an external:path edge.
            edges.append(
                ProvEdge(
                    role=EdgeRole.USED,
                    evidence=EdgeEvidence.SCHEMA_ARG,
                    external_ref=f"external:{resolved}",
                    sha256=evidence.sha256,
                    path=resolved,
                    arg=arg_name,
                    note=("" if evidence.sha256 else "stat_pinned"),
                )
            )
            continue
        record, version = match
        # Cross-job bind (P3.1 #1038): the producer is registered under a DIFFERENT
        # workspace_id (a separate top-level job sharing this root). Reconcile /
        # attach against the PRODUCING record's workspace so an edited input revises
        # the FOREIGN chain (reusing its identity) instead of forking a local v1.
        cross_bind = record.workspace_id != workspace_id
        edges.append(
            _matched_used_edge(
                app,
                sid,
                record_name=record.name,
                workspace_id=record.workspace_id,
                version=version,
                on_disk=evidence,
                path=resolved,
                arg=arg_name,
                turn_id=turn_id,
                trace_id=trace_id,
                cross_workspace_bind=cross_bind,
            )
        )
    return EdgeScan(edges, notes)


def _matched_used_edge(
    app: "FastAPI",
    sid: str,
    *,
    record_name: str,
    workspace_id: str,
    version: ArtifactVersion,
    on_disk: Any,
    path: str,
    arg: str,
    turn_id: str,
    trace_id: str,
    cross_workspace_bind: bool = False,
) -> ProvEdge:
    """Build the used edge for a registry-matched path (re-hash under threshold).

    ``cross_workspace_bind`` (P3.1 #1038) is stamped on EVERY branch when the match
    came from a foreign workspace — the clean hash-pair reuses the foreign
    ``artifact_id``, the changed-input reconcile revises the foreign chain, and
    ``workspace_id`` here is the PRODUCING record's workspace (so the reconcile
    attaches there, never forking a local id).
    """
    disk_sha = on_disk.sha256
    if disk_sha is None:
        # Over the hash threshold → stat-pinned, labeled (never a silent hash-skip).
        return ProvEdge(
            role=EdgeRole.USED,
            evidence=EdgeEvidence.SCHEMA_ARG,
            artifact_id=version.artifact_id,
            name=record_name,
            version=version.version,
            path=path,
            arg=arg,
            note="over_threshold",
            cross_workspace_bind=cross_workspace_bind,
        )
    if disk_sha == version.sha256:
        # Content unchanged since registration → schema-arg + hash-pair.
        return ProvEdge(
            role=EdgeRole.USED,
            evidence=EdgeEvidence.HASH_PAIR,
            artifact_id=version.artifact_id,
            sha256=disk_sha,
            name=record_name,
            version=version.version,
            path=path,
            arg=arg,
            cross_workspace_bind=cross_workspace_bind,
        )
    # Content DIFFERS — never silently pin the stale registered version. Mint a GAP
    # version FIRST (S4 reconcile machinery) and point the edge at it. The note
    # carries the ACTUAL reconcile class (finding [3]) — never an unconditional
    # ``gap_first`` that mislabels a clean auto-revision / re-link / stale fallback.
    outcome = _mint_gap_for_changed_input(
        app,
        sid,
        name=record_name,
        workspace_id=workspace_id,
        path=path,
        turn_id=turn_id,
        trace_id=trace_id,
    )
    if outcome is None:
        # Reconcile skipped → the edge falls back to the STALE registered version;
        # say so honestly rather than claiming a gap was minted.
        return ProvEdge(
            role=EdgeRole.USED,
            evidence=EdgeEvidence.HASH_PAIR,
            artifact_id=version.artifact_id,
            sha256=version.sha256 or disk_sha,
            name=record_name,
            version=version.version,
            path=path,
            arg=arg,
            note="stale_fallback",
            cross_workspace_bind=cross_workspace_bind,
        )
    target = outcome.version
    return ProvEdge(
        role=EdgeRole.USED,
        evidence=EdgeEvidence.HASH_PAIR,
        artifact_id=target.artifact_id,
        sha256=target.sha256 or disk_sha,
        name=record_name,
        version=target.version,
        path=path,
        arg=arg,
        note=_reconcile_note(str(getattr(outcome, "action", "") or "")),
        cross_workspace_bind=cross_workspace_bind,
    )


#: Map the ONE-decision-point action (``VersionAction``) recorded by the reconcile
#: onto the edge's typed ``note`` (finding [3]). A hash-mismatch used edge is no
#: longer unconditionally ``gap_first``: a dirty-lease GAP is ``gap``, a provably-
#: clean single-writer auto-mint is ``auto_revision``, a revert-to-known-version is
#: ``relink`` (the vocabulary ``ProvEdge.note`` already declared but never emitted).
_RECONCILE_NOTE: dict[str, str] = {
    "gap": "gap",
    "new_version": "auto_revision",
    "relink": "relink",
    "dedup": "unchanged",
}


def _reconcile_note(action: str) -> str:
    """The edge note for a reconcile outcome's ``VersionAction`` (default ``gap``)."""
    return _RECONCILE_NOTE.get(action, "gap")


def _mint_gap_for_changed_input(
    app: "FastAPI",
    sid: str,
    *,
    name: str,
    workspace_id: str,
    path: str,
    turn_id: str,
    trace_id: str,
) -> Any:
    """Mint a GAP/new version for an input that changed since registration (S4).

    Routes through :func:`reconcile_designated_path` with ``producing=False``: the
    content is an undesignated overwrite observed at use, so it becomes a GAP
    version (dirty lease) / auto revision (clean lease) / re-link (known non-head)
    — never a false producing mint, and the old version is never mutated. Returns
    the full :class:`MintOutcome` (so the caller reads the reconcile CLASS via
    ``outcome.action`` — finding [3]), or ``None`` on a typed skip.
    """
    from clio_agent.gact.artifacts.versions import reconcile_designated_path  # noqa: PLC0415

    outcome = reconcile_designated_path(
        app,
        sid,
        name=name,
        workspace_id=workspace_id,
        path=path,
        mechanism=Mechanism.NONE,
        turn_id=turn_id,
        trace_id=trace_id,
        session_id=sid,
    )
    if outcome is None:
        logger.info(
            "used-edge gap mint skipped reason=reconcile_skipped session=%s name=%s", sid, name
        )
        return None
    return outcome


# --------------------------------------------------------------------------- #
# Authority-asserted identity (item 4) — NDP catalog inputs.
# --------------------------------------------------------------------------- #


def _structured_result(result: Any) -> Optional[dict[str, Any]]:
    """Extract the structured-content dict from a raw tool result, or ``None``."""
    if isinstance(result, dict):
        sc = result.get("structuredContent")
        if isinstance(sc, dict):
            return sc
        return result
    return None


def detect_authority_edges(
    app: "FastAPI",
    *,
    tool_name: str,
    result: Any,
    workspace_id: str,
) -> EdgeScan:
    """Register NDP catalog inputs as ``authority-asserted`` used edges (item 4).

    NDP tool results carry NO checksum/ETag/DOI (verified against the real
    server); the catalog resource ``url`` / ``id`` IS the authority. A
    ``stage_resource`` result names a downloaded ``local_path`` (hashed when it
    lands in the workspace) plus its source ``url``; ``get_dataset_details`` names
    the SPECIFIC catalog resource the agent asked for by ``url`` + ``id`` → an
    authority USED edge. A broad ``search_datasets`` / ``list_organizations`` is
    DISCOVERY — its hits are RESULTS the agent listed, not inputs it consumed
    (finding [2]) — so it edges NOTHING and instead records a typed
    ``catalog_hits_not_consumed`` note. Absent any recognized NDP shape → a typed
    skip (no edges, no notes).
    """
    structured = _structured_result(result)
    if structured is None:
        return EdgeScan([], [])
    short = tool_name.rsplit(".", 1)[-1] if "." in tool_name else tool_name
    if short == _NDP_STAGE_TOOL:
        return EdgeScan(_stage_resource_edges(app, structured, workspace_id), [])
    if short in _RELAY_FETCH_TOOLS:
        return EdgeScan(_relay_fetch_edges(structured), [])
    if short in _NDP_DETAILS_TOOLS:
        return EdgeScan(_catalog_resource_edges(structured), [])
    if short in _NDP_SEARCH_TOOLS:
        hits = _count_catalog_hits(structured)
        notes = (
            [{"reason": "catalog_hits_not_consumed", "tool": short, "hits": hits}] if hits else []
        )
        return EdgeScan([], notes)
    return EdgeScan([], [])


def _count_catalog_hits(structured: dict[str, Any]) -> int:
    """Count catalog resources a discovery result LISTED (never consumed)."""
    datasets: list[Any] = []
    if isinstance(structured.get("datasets"), list):
        datasets = list(structured["datasets"])
    elif isinstance(structured.get("dataset"), dict):
        datasets = [structured["dataset"]]
    total = 0
    for dataset in datasets:
        if isinstance(dataset, dict):
            total += sum(1 for r in (dataset.get("resources") or ()) if isinstance(r, dict))
    if isinstance(structured.get("organizations"), list):
        total += len(structured["organizations"])
    return total


def _stage_resource_edges(
    app: "FastAPI", structured: dict[str, Any], workspace_id: str
) -> list[ProvEdge]:
    """Authority edge for a ``stage_resource`` download (url is the authority)."""
    url = str(structured.get("url") or "").strip()
    if not url:
        return []
    local_path = str(structured.get("local_path") or "").strip()
    sha: Optional[str] = None
    if local_path:
        sha = _hash_if_contained(app, local_path, workspace_id)
    return [
        ProvEdge(
            role=EdgeRole.USED,
            evidence=EdgeEvidence.AUTHORITY,
            authority=url,
            external_ref=f"external:{url}",
            sha256=sha,
            path=local_path,
            note="ndp_stage_resource",
        )
    ]


def _relay_fetch_edges(structured: dict[str, Any]) -> list[ProvEdge]:
    """Authority edge recording where a fetched artifact's bytes were produced.

    The transfer is the transform: the local file exists because
    ``relay_fetch_artifact`` moved custody of a remote artifact into this
    workspace. The remote reference (cluster + relay job + artifact id) is the
    authority for that file, so it rides a used-edge and the provenance graph
    shows the local artifact standing on its cluster-side origin rather than
    appearing from nowhere. The digest is relay's own recorded ``sha256``,
    already verified against the transferred bytes by the tool -- so this edge
    pins the REMOTE identity, not a re-hash of the local copy.
    """
    origin = structured.get("origin")
    if not isinstance(origin, Mapping):
        return []
    artifact_id = str(origin.get("artifact_id") or "").strip()
    job_id = str(origin.get("job_id") or "").strip()
    if not artifact_id or not job_id:
        return []
    cluster = str(origin.get("cluster") or "").strip() or "unknown-cluster"
    authority = str(origin.get("uri") or "").strip() or f"relay://{cluster}/{job_id}/{artifact_id}"
    remote_sha = origin.get("remote_sha256")
    return [
        ProvEdge(
            role=EdgeRole.USED,
            evidence=EdgeEvidence.AUTHORITY,
            authority=authority,
            external_ref=f"external:relay://{cluster}/{job_id}/{artifact_id}",
            sha256=remote_sha if isinstance(remote_sha, str) and remote_sha else None,
            path=str(structured.get("local_path") or "").strip(),
            note="relay_fetch_artifact",
        )
    ]


def _catalog_resource_edges(structured: dict[str, Any]) -> list[ProvEdge]:
    """Authority edges for catalog resources named in a search/details result."""
    datasets: list[Any] = []
    if isinstance(structured.get("datasets"), list):
        datasets = list(structured["datasets"])
    elif isinstance(structured.get("dataset"), dict):
        datasets = [structured["dataset"]]
    edges: list[ProvEdge] = []
    seen: set[str] = set()
    for dataset in datasets:
        if not isinstance(dataset, dict):
            continue
        for resource in dataset.get("resources") or ():
            if not isinstance(resource, dict) or len(edges) >= _MAX_AUTHORITY_EDGES:
                continue
            authority = str(resource.get("url") or resource.get("id") or "").strip()
            if not authority or authority in seen:
                continue
            seen.add(authority)
            edges.append(
                ProvEdge(
                    role=EdgeRole.USED,
                    evidence=EdgeEvidence.AUTHORITY,
                    authority=authority,
                    external_ref=f"external:{authority}",
                    name=str(resource.get("name") or ""),
                    note="ndp_catalog_resource",
                )
            )
    return edges


def _hash_if_contained(app: "FastAPI", raw_path: str, workspace_id: str) -> Optional[str]:
    """Hash a staged file when it exists inside the workspace root, else ``None``."""
    from clio_agent.gact.artifacts.minting import (  # noqa: PLC0415
        _contained,
        _workspace_root,
        compute_identity,
    )

    root = _workspace_root(app, workspace_id)
    if root is None:
        return None
    path = Path(raw_path)
    if not _contained(path, root):
        return None
    try:
        if not path.is_file():
            return None
        return compute_identity(path).sha256
    except OSError:
        return None


__all__ = [
    "EdgeScan",
    "contributing_workspace_ids",
    "detect_authority_edges",
    "detect_used_edges",
]
