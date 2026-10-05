"""Real persisted media surface for browser interaction, with no invented token map."""

import shutil
from pathlib import Path

from fastapi import FastAPI

from clio_agent.gact.a2ui_catalogs.builtin import workspace_catalog_id
from clio_agent.gact.a2ui_producer._export import export_workspace_paths


def add_media_fixture(app: FastAPI, session_id: str, root: Path) -> None:
    """Export an existing repository image and add a selectable inline data table."""
    workspace = root / "media-workspace"
    workspace.mkdir(exist_ok=True)
    app.state.workspaces.update("ws_default", root_path=str(workspace))
    shutil.copyfile(
        Path(__file__).parents[3] / "site/src/assets/brand/clio-mark.png", workspace / "owl.png"
    )
    components = [
        {"id": "root", "component": "Column", "children": ["image", "table"]},
        {
            "id": "image",
            "component": "Image",
            "url": "owl.png",
            "description": "Recorded logo reference",
            "fit": "contain",
        },
        {
            "id": "table",
            "component": "clio.data-table.v1",
            "rows": [{"id": "station-a", "value": 1}, {"id": "station-b", "value": 2}],
            "columns": [{"key": "id", "label": "Station"}, {"key": "value", "label": "Value"}],
        },
    ]
    exported = export_workspace_paths(app, session_id, components)
    if isinstance(exported, dict):
        raise RuntimeError(f"Fixture media export failed: {exported}")
    app.state.a2ui_store.apply_batch(
        session_id,
        [
            {
                "version": "v0.9.1",
                "createSurface": {
                    "surfaceId": "recorded-media",
                    "catalogId": workspace_catalog_id(),
                },
            },
            {
                "version": "v0.9.1",
                "updateComponents": {"surfaceId": "recorded-media", "components": exported[0]},
            },
        ],
        part_id="recorded-media-part",
    )
