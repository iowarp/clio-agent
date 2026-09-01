"""Artifact identity in the MODEL lane — the id the wire lane already carries.

Designation mints an artifact at tool completion (:mod:`minting`, seam a) and the
**wire** lane immediately gets its identity: an ``artifact.created`` semantic event
plus a ``resource_link`` part carrying ``artifact_id``. The **model** lane got
nothing. The structured tool result the agent reads back names only the written
path (``ndp_stage_resource`` → ``{"ok": true, "local_path": "...csv", ...}``), so an
agent instructed to reference ``artifact://<artifact-id>`` — the grammar the trusted
A2UI ``clio.time-series.v1`` ``dataUri`` requires, and the one
``GET /v1/artifacts/{id}/table-preview`` resolves — has no id to cite, honestly
refuses to invent one, and the artifact-backed chart can never be built.

This module carries the SAME registry truth into the two model-facing places:

* **the tool result the model reads back** — :func:`record_call_artifacts` runs on
  the observer thread right after the mint seam and publishes this call's resolved
  identities; :func:`merge_call_artifact_identity` is consumed at the execution
  boundary's model-observation assembly
  (:func:`clio_agent.tools.tool_hooks.assemble_model_observation`) and merges them
  into the structured result as an ``artifacts`` list;
* **the turn's produced ``workflow_state``** — :func:`annotate_workflow_state_artifacts`
  stamps ``artifact_id`` / ``artifact_uri`` beside the path a section already
  carries, so the next turn (which reads the prior state through
  ``clio_prior_workflow_state``) can reuse the registered artifact instead of
  re-staging it.

Raw truth only. Every entry is a registry-resolved :class:`ArtifactVersion` reached
by an EXACT path/basename join — never a synthesized id, never a fuzzy match — and
the result merge is format-only (one new key on a JSON object; a tool that already
declares its own ``artifacts`` list, e.g. ``create_artifact``, is left untouched).
Nothing here decides, routes, or rewrites model prose.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Optional

from clio_agent.gact.artifacts.designation import (
    grounded_output_paths,
    result_declared_paths,
)
from clio_agent.gact.artifacts.records import ArtifactVersion

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

#: The key the identity list rides under in the model-visible structured result.
#: Matches ``create_artifact``'s OWN result field (``artifacts[].artifact_id``), so
#: a model sees ONE vocabulary for "the artifacts this call produced" regardless of
#: which designation channel minted them.
ARTIFACTS_RESULT_KEY = "artifacts"

#: Model-lane bound: at most this many identities ride one tool result. The model
#: lane is a context budget, so a pathological call that designates hundreds of
#: outputs contributes a bounded, typed-truncated list rather than unbounded text.
MAX_MODEL_ARTIFACTS = 16

#: Section fields a produced ``workflow_state`` may carry a written-output path in,
#: in resolution order. The head is the designation vocabulary
#: (:data:`~clio_agent.gact.artifacts.designation.RESULT_PATH_KEYS` /
#: ``OUTPUT_PATH_ARG_NAMES``); ``data_path`` / bare ``path`` are the pack-declared
#: fallbacks (``WorkflowSectionReadiness.path_fields`` is ``("local_path", "path")``
#: for the EarthScope acquisition rule). Including the bare fallbacks is safe here
#: because the join is EXACT — a section is annotated only when its path names a
#: registered artifact, so a path that merely echoes an input resolves to nothing.
WORKFLOW_STATE_PATH_FIELDS: tuple[str, ...] = (
    "local_path",
    "output_path",
    "written_path",
    "saved_path",
    "saved_to",
    "result_path",
    "out_path",
    "output_file",
    "data_path",
    "path",
)

#: Per-thread publish slot for the CURRENT tool call's identities. A tool call runs
#: synchronously on one thread — the boundary notifies the observer (which mints and
#: publishes here) and then assembles the model observation on that SAME thread, the
#: identical reason ``tool_observer._OBSERVER_CALL_IDS`` / ``_OBSERVER_CALL_T0`` are
#: thread-locals. Entries are stamped with the producing ``call_id`` so a consumer
#: can never inherit an earlier, unrelated call's publication.
_CALL_ARTIFACTS = threading.local()


def artifact_id_uri(artifact_id: str) -> str:
    """The id-addressed artifact URI (``artifact://artifact_<hex>``).

    This is the grammar the trusted A2UI catalog validates for a
    ``clio.time-series.v1`` ``dataUri`` / ``clio.artifact.v1`` ``uri`` and the one
    the bounded table-preview route resolves. It is deliberately NOT the logical
    version URI (``artifact://<ws>/<name>@vN``) the ``resource_link`` part carries:
    that one addresses a version chain, this one addresses the immutable version.
    """

    return f"artifact://{artifact_id}"


def identity_entry(version: ArtifactVersion) -> dict[str, str]:
    """Project one registered version to its model-lane identity (three tiny fields)."""

    return {
        "artifact_id": version.artifact_id,
        "uri": artifact_id_uri(version.artifact_id),
        "path": str(version.path or ""),
    }


def resolve_registered_version(
    app: "FastAPI", workspace_id: str, path: str
) -> Optional[ArtifactVersion]:
    """Resolve a written path to its registered HEAD version, or ``None``.

    Delegates to the registry's own absolute-path matcher
    (:meth:`~clio_agent.gact.artifacts.registry.ArtifactRegistry.find_version_by_path`
    — the SAME resolver the S5 used-edge detector binds), so the model lane and the
    provenance graph can never disagree about which version a path denotes. Exact
    join, HEAD wins; an unregistered path resolves to ``None``, never to a plausible
    neighbour, and an unreadable registry degrades to ``None`` with a typed reason.
    """

    from clio_agent.gact.artifacts.registry import get_registry  # noqa: PLC0415

    if not workspace_id or not path:
        return None
    try:
        match = get_registry(app).find_version_by_path(workspace_id, path)
    except Exception:  # noqa: BLE001 — an unreadable registry resolves to nothing, never a crash
        logger.debug("artifact identity resolve skipped reason=registry_unreadable", exc_info=True)
        return None
    return match[1] if match is not None else None


def designated_result_paths(effective_args: Mapping[str, Any], result: Any) -> list[str]:
    """Every output path THIS call designated, arg channel first (the mint's order).

    The two channels the tool-declared mint reads
    (:func:`~clio_agent.gact.artifacts.designation.grounded_output_paths` over the
    output-path ARGS, and
    :func:`~clio_agent.gact.artifacts.designation.result_declared_paths` over the
    structured RESULT keys), de-duplicated with the arg channel winning a tie —
    byte-for-byte the same enumeration ``mint_tool_declared_outputs`` walks, so the
    model lane and the mint can never disagree about which paths were designated.
    """

    paths: list[str] = []
    for value in list(grounded_output_paths(effective_args).values()) + list(
        result_declared_paths(result).values()
    ):
        if value and value not in paths:
            paths.append(value)
    return paths


def _identities_for_call(
    app: "FastAPI",
    *,
    workspace_id: str,
    effective_args: Mapping[str, Any],
    result: Any,
    minted: list[ArtifactVersion],
) -> list[dict[str, str]]:
    """Build this call's identity list: designated paths first, then any extra mint.

    Path-resolved FIRST so the list covers every designated output that is now a
    registered artifact — including the outcomes ``minted`` deliberately omits (a
    drift/revert reconcile that re-linked an existing version rather than creating
    one). ``minted`` versions whose path did not resolve are then appended, so a
    freshly-minted version is never lost to a path-normalisation mismatch.
    """

    entries: list[dict[str, str]] = []
    seen: set[str] = set()
    for path in designated_result_paths(effective_args, result):
        version = resolve_registered_version(app, workspace_id, path)
        if version is not None and version.artifact_id not in seen:
            seen.add(version.artifact_id)
            entries.append(identity_entry(version))
    for version in minted:
        if version.artifact_id and version.artifact_id not in seen:
            seen.add(version.artifact_id)
            entries.append(identity_entry(version))
    if len(entries) > MAX_MODEL_ARTIFACTS:
        logger.info(
            "artifact identity truncated reason=model_artifacts_cap count=%d cap=%d",
            len(entries),
            MAX_MODEL_ARTIFACTS,
        )
        return entries[:MAX_MODEL_ARTIFACTS]
    return entries


def record_call_artifacts(
    app: "FastAPI",
    *,
    call_id: str,
    workspace_id: str,
    effective_args: Mapping[str, Any],
    result: Any,
    minted: list[ArtifactVersion],
) -> list[dict[str, str]]:
    """Publish the CURRENT tool call's artifact identities for the model lane.

    Called from the observer's mint seam (:func:`observe_tool_transform`) on the
    same thread the execution boundary will assemble the model observation on.
    Always writes the slot (even with an empty list) so a later call on this thread
    can never read a stale publication. Fully guarded — a model-lane enrichment must
    never break a turn; a failure publishes nothing and logs a typed reason.
    """

    entries: list[dict[str, str]] = []
    try:
        entries = _identities_for_call(
            app,
            workspace_id=workspace_id,
            effective_args=effective_args,
            result=result,
            minted=list(minted or []),
        )
    except Exception:  # noqa: BLE001 — a model-lane enrichment must never break a turn
        logger.warning(
            "artifact identity publish skipped reason=model_identity_publish_failed call_id=%s",
            call_id,
        )
        entries = []
    _CALL_ARTIFACTS.value = (str(call_id or ""), entries)
    return entries


def take_call_artifacts(call_id: str) -> list[dict[str, str]]:
    """Read and CLEAR the identities published for ``call_id`` (consume-once).

    The slot is cleared unconditionally, so a publication whose call never reached
    the consumer (a raw MCP-Apps call, a cancelled tool) is dropped here rather than
    leaking onto the next call. A mismatched (or blank) ``call_id`` yields ``[]`` —
    an identity is never attributed to a call that did not produce it.
    """

    published = getattr(_CALL_ARTIFACTS, "value", None)
    _CALL_ARTIFACTS.value = None
    if not published or not call_id:
        return []
    published_call_id, entries = published
    return list(entries) if published_call_id == str(call_id) else []


def merge_artifact_identity(model_text: str, entries: list[dict[str, str]]) -> str:
    """Merge ``entries`` into a model-visible tool result (format-only).

    The result the model reads is the JSON serialization of the tool's structured
    content (``mcp_executor._result_to_text``). When it parses back to a JSON
    OBJECT the identities are added under :data:`ARTIFACTS_RESULT_KEY` and the
    object is re-serialized — no value the tool returned is altered, reordered, or
    dropped. A result that already carries its own ``artifacts`` list
    (``create_artifact``) is returned untouched: the tool's own declaration wins.
    Any other shape (a bare string, a list, unparseable text) keeps its bytes and
    gains one trailing ``[artifacts]`` note — the same visible-annotation idiom the
    boundary's ``[path-repair]`` note already uses, so the fact is never invisible.
    """

    if not entries or not isinstance(model_text, str):
        return model_text
    try:
        parsed = json.loads(model_text)
    except (TypeError, ValueError):
        parsed = None
    if isinstance(parsed, dict):
        if ARTIFACTS_RESULT_KEY in parsed:
            return model_text
        parsed[ARTIFACTS_RESULT_KEY] = entries
        try:
            return json.dumps(parsed, allow_nan=False, default=str)
        except (TypeError, ValueError, RecursionError, OverflowError):
            logger.warning(
                "artifact identity merge degraded reason=model_result_unserializable count=%d",
                len(entries),
            )
    note = json.dumps({ARTIFACTS_RESULT_KEY: entries}, default=str)
    return f"{model_text}\n[artifacts] {note}"


def merge_call_artifact_identity(model_text: str) -> str:
    """Merge the CURRENT tool call's published identities into its model result.

    The consumer half of :func:`record_call_artifacts`: resolves this call's id from
    the observer thread-local (``observer_bridge.observer_call_id``), consumes the
    matching publication, and merges it. A call outside an observed scope, or one
    that designated no output, returns the text unchanged.
    """

    from clio_agent.gact.artifacts.observer_bridge import observer_call_id  # noqa: PLC0415

    entries = take_call_artifacts(observer_call_id())
    return merge_artifact_identity(model_text, entries)


def annotate_workflow_state_artifacts(app: Any, sid: str, state: dict[str, Any]) -> dict[str, Any]:
    """Stamp ``artifact_id``/``artifact_uri`` beside a registered path in ``state``.

    A registry JOIN over the turn's produced ``workflow_state``: for every top-level
    section that is a mapping, the first :data:`WORKFLOW_STATE_PATH_FIELDS` value
    that resolves to a registered artifact version in the session's grounding
    workspaces (its own plus its delegates') contributes that version's id. Only
    ADDS — an ``artifact_id`` the model already wrote is never overwritten, and a
    section whose path resolves to nothing is left byte-identical.

    This is the lane the pack-declared designation channel
    (:func:`~clio_agent.gact.artifacts.minting.mint_pack_declared_paths`, which mints
    from ``workflow_state`` at turn finalize) had no way back through: it read paths
    OUT of the state and never wrote the resulting identity back in.

    Fully guarded — the produced state is returned unchanged on any failure.
    """

    if app is None or not sid or not isinstance(state, dict) or not state:
        return state
    try:
        from clio_agent.gact.artifacts.grounding import _grounding_workspaces  # noqa: PLC0415

        workspaces = _grounding_workspaces(app, sid, include_children=True)
        if not workspaces:
            return state
        annotated = dict(state)
        for name, section in state.items():
            if not isinstance(section, Mapping) or section.get("artifact_id"):
                continue
            version = _section_version(app, workspaces, section)
            if version is None:
                continue
            updated = dict(section)
            updated["artifact_id"] = version.artifact_id
            updated["artifact_uri"] = artifact_id_uri(version.artifact_id)
            annotated[name] = updated
        return annotated
    except Exception:  # noqa: BLE001 — a model-lane enrichment must never break a turn
        logger.warning(
            "workflow_state artifact annotation skipped "
            "reason=workflow_state_annotation_failed session=%s",
            sid,
        )
        return state


def _section_version(
    app: Any, workspaces: list[str], section: Mapping[str, Any]
) -> Optional[ArtifactVersion]:
    """The registered version one ``workflow_state`` section's path names, if any."""

    for field_name in WORKFLOW_STATE_PATH_FIELDS:
        raw = section.get(field_name)
        if not isinstance(raw, str) or not raw.strip():
            continue
        for workspace_id in workspaces:
            version = resolve_registered_version(app, workspace_id, raw.strip())
            if version is not None:
                return version
    return None


__all__ = [
    "ARTIFACTS_RESULT_KEY",
    "MAX_MODEL_ARTIFACTS",
    "WORKFLOW_STATE_PATH_FIELDS",
    "annotate_workflow_state_artifacts",
    "artifact_id_uri",
    "designated_result_paths",
    "identity_entry",
    "merge_artifact_identity",
    "merge_call_artifact_identity",
    "record_call_artifacts",
    "resolve_registered_version",
    "take_call_artifacts",
]
