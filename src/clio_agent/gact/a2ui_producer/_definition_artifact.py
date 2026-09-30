"""Mint a surface's own component definition as a linked artifact (#1533 S4).

"Every surface keeps its definition": whichever way a producer call supplied
components (inline, or via ``components_path``), the FINAL, exported/
schema-validated JSON array is written to the session's workspace and minted
through the SAME harness funnel :mod:`clio_agent.gact.a2ui_producer._export`
uses for exported media (content-addressed: an unchanged definition dedups
onto its existing version, never a fresh one). This makes the surface's own
JSON independently fetchable/reproducible as a normal artifact, not only as
live transcript state that only this session's store carries.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from clio_agent.gact.a2ui_producer._refusal import refusal

if TYPE_CHECKING:
    from fastapi import FastAPI

#: The producer designation stamped on a minted surface-definition artifact —
#: shares the export boundary's designation (both are harness-side, S4 records
#: of what a producer tool did, never a model decision).
DEFINITION_ARTIFACT_DESIGNATION = "a2ui-surface-definition"


def mint_surface_definition_artifact(
    app: "FastAPI", session_id: str, surface_id: str, components: list[dict[str, Any]]
) -> dict[str, Any]:
    """Write + register ``components`` as ``.clio/a2ui/<surface_id>.json``.

    Returns ``{"definition_artifact_id": ..., "definition_artifact_uri": ...}``
    on success, or a typed ``a2ui_definition_artifact_failed`` refusal.
    """

    from clio_agent.gact.artifacts.designation import kind_for_path  # noqa: PLC0415
    from clio_agent.gact.artifacts.minting import (  # noqa: PLC0415
        _session_workspace_id,
        _workspace_root,
        artifact_name_for_path,
        mint_artifact,
    )
    from clio_agent.gact.artifacts.model_identity import artifact_id_uri  # noqa: PLC0415
    from clio_agent.gact.artifacts.records import Mechanism  # noqa: PLC0415
    from clio_agent.gact.artifacts.storage import ingest_artifact_identity  # noqa: PLC0415

    workspace_id = _session_workspace_id(app, session_id)
    root = _workspace_root(app, workspace_id)
    if root is None:
        return refusal(
            "a2ui_definition_artifact_failed",
            detail=(
                "this session's workspace root is unresolvable; the surface "
                "definition cannot be stored as an artifact"
            ),
        )
    target = root / ".clio" / "a2ui" / f"{surface_id}.json"
    payload = json.dumps(components, indent=2, sort_keys=False)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(payload, encoding="utf-8")
    except OSError as exc:
        return refusal(
            "a2ui_definition_artifact_failed",
            detail=f"the surface definition could not be written: {exc}",
        )
    try:
        ingested = ingest_artifact_identity(app, target, workspace_root=root)
        version = mint_artifact(
            app,
            session_id,
            name=artifact_name_for_path(target),
            workspace_id=workspace_id,
            evidence=ingested.evidence,
            kind=kind_for_path(target),
            mechanism=Mechanism.HARNESS,
            producer={
                "designation": DEFINITION_ARTIFACT_DESIGNATION,
                "session_id": session_id,
                "surface_id": surface_id,
            },
            custody=ingested.custody,
            path=str(target),
            ingested=ingested,
            not_ingested_size=ingested.not_ingested_size,
        )
    except (OSError, ValueError) as exc:
        return refusal(
            "a2ui_definition_artifact_failed",
            detail=f"the surface definition could not be registered as an artifact: {exc}",
        )
    if version is None:
        return refusal(
            "a2ui_definition_artifact_failed",
            detail="the surface definition could not be registered as an artifact",
        )
    return {
        "definition_artifact_id": version.artifact_id,
        "definition_artifact_uri": artifact_id_uri(version.artifact_id),
    }


__all__ = ["DEFINITION_ARTIFACT_DESIGNATION", "mint_surface_definition_artifact"]
