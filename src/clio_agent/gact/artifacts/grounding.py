"""Answer grounding re-sourced from the artifact registry (S7 #973, deletion item 4).

Replaces the pre-S7 ``evidence.py`` heuristics
(``_ground_fabricated_local_artifact_paths`` + ``_verified_local_artifact_paths_by_ext``
+ ``_is_remote_artifact_ref``) that disk-scanned ``workflow_state.artifact_paths``.
Grounding now validates and rewrites a final answer's fabricated local artifact
path citations against the session's **registered artifacts** — the designation
truth (ids + content hashes) the registry holds — reached with the same
``include_children`` union the artifact routes use so a parent orchestrator's
answer grounds against its delegates' outputs too.

What re-sourcing buys (precision over the old field-scan): the registry's
per-version **producer** distinguishes a locally-PRODUCED deliverable from a
**staged input** the run consumed. A staging tool's output (``ndp_stage_resource``
and any ``stage_*`` family — the NDP metadata catalog is downloaded this way) is an
input, never the deliverable the answer cites, so it is excluded from the
substitution candidate set. Every OTHER producer — a processing tool's declared
output (``tool-schema``), an agent proposal, a user pin, a harness write — is a
deliverable REGARDLESS of its evidence class: a stat-pinned (>64MB) produced output
is still a real deliverable, so it is a candidate too. The discriminator is the
version's producer, not its evidence class (S7 review): keying on
``evidence_class == hashed-at-use`` both EXCLUDED genuine stat-pinned deliverables
(the server would then falsely author "no artifact was produced") AND relied on a
staged-input class (``authority-asserted``) production never actually mints — so the
old precision claim was synthetic. This is the exact ambiguity the old
``artifact_paths`` field restriction guarded against, now grounded in the producing
provenance rather than a declared path list.

The pack schema's ``artifact_extensions`` still scopes WHICH fabricated-path
*types* to check (a pack that declares no deliverable types grounds nothing —
same no-op default as before). It is a cheap type vocabulary, never a path scan:
the registry remains the sole source of truth for which artifacts exist and where
their verified bytes live. This keeps the honest neutralize behaviour (a
data-blocked run that produced no image still rewrites a fabricated image-file
citation to an explicit not-produced note) that a purely registry-derived
vocabulary could not provide when the registry is empty.

Pure read over ``app.state`` (the registry + session/workspace stores) plus the
filesystem existence check; no mutation, no I/O beyond ``Path.is_file``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from clio_agent.gact.artifacts.registry import get_registry

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.workflow_state.schema import WorkflowStateSchema


@lru_cache(maxsize=None)
def _artifact_path_token_re(extensions: tuple[str, ...]) -> re.Pattern[str]:
    """Compile the fabricated-artifact token matcher for the declared extensions."""

    ext_alternation = "|".join(re.escape(ext) for ext in extensions)
    return re.compile(rf"[A-Za-z0-9_./~+-]+\.(?:{ext_alternation})", re.IGNORECASE)


@lru_cache(maxsize=None)
def _artifact_path_missing_framing_re(extensions: tuple[str, ...]) -> re.Pattern[str]:
    """Compile the honest-not-produced framing matcher for the declared extensions."""

    ext_alternation = "|".join(re.escape(ext) for ext in extensions)
    return re.compile(
        r"(not\s+(?:been\s+)?(?:staged|downloaded|available|present|found|created|generated|produced)|"
        rf"no\s+(?:{ext_alternation}|plot|figure|file|artifact|local)\b|"
        r"does\s+not\s+exist|doesn'?t\s+exist|not\s+yet|is\s+blocked|blocked\s+because|"
        r"cannot\s+be|could\s+not\s+be|no\s+such\s+file|would\s+(?:need|be)|will\s+be|"
        r"written\s+to|saved\s+to|expected\s+(?:location|at)|placeholder|hypothetical|"
        r"once\s+(?:the|a)\b|to\s+be\s+(?:created|generated|written))",
        re.IGNORECASE,
    )


def _is_remote_ref(value: str) -> bool:
    """Whether a path string is a remote/URL reference (never a local artifact)."""

    value = str(value or "")
    return value.startswith(("http://", "https://", "ftp://", "//")) or "://" in value


def _is_staging_tool(tool: str) -> bool:
    """Whether a producing tool name is a STAGING tool (produces a consumed input).

    Segment-exact on the ``stage`` verb — never a substring, so ``backstage`` /
    ``multistaged`` / ``get_stage_status`` do NOT match — covering the ``stage_*``
    family and the canonical ``ndp_stage_resource``. A staging tool downloads/stages
    a remote input the run then consumes (the NDP metadata catalog), so its output is
    never the deliverable an answer cites.
    """
    segments = [s for s in re.split(r"[^a-z0-9]+", (tool or "").strip().lower()) if s]
    return "stage" in segments


def _produced_deliverable(producer: Mapping[str, Any]) -> bool:
    """Whether a registered version is a locally-produced deliverable, not a staged input.

    The discriminator is the version's PRODUCER, not its evidence class (S7 review).
    A staging tool's output (:func:`_is_staging_tool`) is an input the run consumed —
    excluded from the substitution candidate set. Every other producer — a processing
    tool's declared output (``tool-schema``), an agent proposal, a user pin, a harness
    write — is a deliverable REGARDLESS of its evidence class, so a stat-pinned (>64MB)
    produced output is a candidate too (precision over recall, owner decision #966.10).
    """
    return not _is_staging_tool(str((producer or {}).get("tool") or ""))


def _session_workspace_id(app: "FastAPI", sid: str) -> str:
    """The workspace id bound to a session, or ``""`` when the session is unknown."""
    store = getattr(app.state, "sessions", None) or getattr(app.state, "session_store", None)
    if store is None or not sid:
        return ""
    session = store.get(sid)
    if session is None:
        return ""
    return str(getattr(session, "workspace_id", "") or "")


def _grounding_workspaces(app: "FastAPI", sid: str, *, include_children: bool) -> list[str]:
    """The workspace ids whose registered artifacts ground this session's answer.

    The session's own workspace, plus — with ``include_children`` — the descendant
    child sessions' workspaces (the same union ``GET /v1/sessions/{sid}/artifacts?
    include_children=true`` builds), so a parent orchestrator's answer grounds
    against its delegates' produced artifacts. De-duplicated, own workspace first.
    """
    workspace_id = _session_workspace_id(app, sid)
    if not workspace_id:
        return []
    workspaces = [workspace_id]
    if include_children:
        from clio_agent.gact.agent_tasks import descendant_session_ids  # noqa: PLC0415

        for child in descendant_session_ids(app, sid):
            child_ws = _session_workspace_id(app, child)
            if child_ws and child_ws not in workspaces:
                workspaces.append(child_ws)
    return workspaces


def registered_deliverable_paths_by_ext(
    app: "FastAPI",
    sid: str,
    *,
    extensions: tuple[str, ...],
    include_children: bool = True,
) -> dict[str, list[str]]:
    """Collect the session's registered, on-disk deliverable paths bucketed by extension.

    Sourced from the artifact registry (designation truth), scoped to the declared
    deliverable ``extensions``: for every registered version in the grounding
    workspace union that is a locally-produced deliverable
    (:func:`_produced_deliverable`), references a path ending in a declared
    extension, and whose bytes still exist on disk, the path is bucketed by its
    lowercase extension. De-duplicated per bucket. A path that no longer exists (a
    scratch file cleaned up) or a staged remote input is never a candidate.
    """
    found: dict[str, list[str]] = {ext: [] for ext in extensions}
    if not extensions:
        return found
    registry = get_registry(app)
    ext_set = {ext.lower() for ext in extensions}
    for workspace_id in _grounding_workspaces(app, sid, include_children=include_children):
        for record in registry.list_for_workspace(workspace_id):
            for version in record.versions:
                path = str(version.path or "").strip()
                if not path or _is_remote_ref(path):
                    continue
                if not _produced_deliverable(version.producer):
                    continue
                lowered = path.lower()
                for ext in ext_set:
                    if not lowered.endswith("." + ext):
                        continue
                    try:
                        on_disk = Path(path).is_file()
                    except OSError:
                        on_disk = False
                    if on_disk and path not in found[ext]:
                        found[ext].append(path)
                    break
    return found


def produced_deliverable_extensions(
    app: "FastAPI",
    sid: str,
    *,
    extensions: tuple[str, ...],
    include_children: bool = True,
) -> set[str]:
    """The declared extensions with a PRODUCED deliverable registered — on disk or not.

    Same producer discriminator as :func:`registered_deliverable_paths_by_ext`, but
    WITHOUT the on-disk filter. It answers "was a deliverable of this type produced
    this run?" independent of whether its bytes still resolve on disk (a scratch file
    may have been cleaned up). :func:`ground_answer_artifacts` uses it to guard the
    neutralize path: it must NEVER author a "no local <ext> artifact was produced"
    note while a produced deliverable of that ext exists in the registry — that would
    be an affirmatively false statement authored into the answer (S7 review).
    """
    found: set[str] = set()
    if not extensions:
        return found
    registry = get_registry(app)
    ext_set = {ext.lower() for ext in extensions}
    for workspace_id in _grounding_workspaces(app, sid, include_children=include_children):
        for record in registry.list_for_workspace(workspace_id):
            for version in record.versions:
                path = str(version.path or "").strip()
                if not path or _is_remote_ref(path):
                    continue
                if not _produced_deliverable(version.producer):
                    continue
                lowered = path.lower()
                for ext in ext_set:
                    if lowered.endswith("." + ext):
                        found.add(ext)
                        break
    return found


def ground_answer_artifacts(
    app: "FastAPI",
    sid: str,
    answer: str,
    *,
    schema: "WorkflowStateSchema",
    include_children: bool = True,
) -> str:
    """Rewrite a final answer's fabricated local artifact citations from the registry.

    Registry-sourced re-implementation of the deleted ``evidence.py`` heuristic
    (deletion item 4, #973). The synthesis model sometimes derives a
    plausible-but-wrong local artifact filename (an invented plot path, an
    extension swap) instead of copying the tool-returned path, and on a
    data-blocked run it can cite a deliverable that was never produced. Such a path
    does not exist on disk and misrepresents the deliverable. This pass — driven by
    the registry's produced-deliverable set (per declared extension) and the
    filesystem — corrects a non-existent local artifact citation to the single
    registered artifact of that type when exactly one exists, otherwise (nothing
    real to point at) neutralizes it with an explicit not-produced note. Remote
    source URLs and paths the answer honestly frames as missing/not-yet-created are
    left untouched. A schema declaring no artifact extensions grounds nothing and
    returns the answer unchanged.
    """
    if not answer or not schema.artifact_extensions:
        return answer
    # An unbound / unknown session has no registered-artifact basis — never
    # neutralize a citation we cannot verify (the neutralize path is for a bound
    # session that produced nothing, e.g. a data-blocked run).
    if not _session_workspace_id(app, sid):
        return answer
    verified = registered_deliverable_paths_by_ext(
        app, sid, extensions=schema.artifact_extensions, include_children=include_children
    )
    produced_exts = produced_deliverable_extensions(
        app, sid, extensions=schema.artifact_extensions, include_children=include_children
    )
    token_re = _artifact_path_token_re(schema.artifact_extensions)
    framing_re = _artifact_path_missing_framing_re(schema.artifact_extensions)

    result = answer
    for match in list(token_re.finditer(answer)):
        token = match.group(0)
        if _is_remote_ref(token):
            continue
        try:
            if Path(token).is_file():
                continue
        except OSError:
            continue
        ext = token.rsplit(".", 1)[-1].lower()
        candidates = verified.get(ext) or []
        # Path-doubling / prefix-mangling: if the non-existent token EMBEDS exactly
        # one verified artifact path as a substring (e.g. a real staged path with a
        # duplicated directory prefix), collapse to that verified path. Generic;
        # runs before the ambiguity check so it corrects even when several exist.
        embedded = [c for c in candidates if c and c in token and c != token]
        if len(embedded) == 1:
            result = result.replace(token, embedded[0])
            continue
        if len(candidates) > 1:
            # Ambiguous which verified artifact was meant; leave the text unchanged.
            continue
        # Respect honest "not produced / would be at <path>" framing.
        lo = max(0, match.start() - 160)
        hi = min(len(answer), match.end() + 160)
        if framing_re.search(answer[lo:hi]):
            continue
        if len(candidates) == 1:
            # Exactly one verified deliverable of this type: correct the citation.
            result = result.replace(token, candidates[0])
        elif ext in produced_exts:
            # A produced deliverable of this type IS registered but its bytes do not
            # resolve on disk right now (a scratch file cleaned up). Authoring a
            # "no artifact was produced" note here would be an affirmatively false
            # statement — leave the citation unchanged (ambiguity, typed note).
            continue
        else:
            # No local deliverable of this type was produced this run: drop the
            # fabricated path rather than present an unproduced file as real.
            result = result.replace(token, f"[no local {ext} artifact was produced this run]")
    return result


__all__ = [
    "ground_answer_artifacts",
    "produced_deliverable_extensions",
    "registered_deliverable_paths_by_ext",
]
