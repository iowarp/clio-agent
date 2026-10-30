"""Workspace rows as stores written before one-workspace-per-folder (#1771) hold them."""

from __future__ import annotations

import dataclasses
from typing import Any

from clio_agent.gact.workspaces import Workspace


def legacy_duplicate(app: Any, original: Workspace, name: str) -> Workspace:
    """A second workspace on ``original``'s folder, inserted as an old store holds it.

    ``create`` refuses a taken folder, but such rows still load, so code that serves
    a shared folder still has to behave.
    """

    store = app.state.workspaces
    duplicate = dataclasses.replace(
        original, id=f"ws_{name}", name=name, display_name=name, config=dict(original.config)
    )
    with store._lock:
        store._workspaces[duplicate.id] = duplicate
        store._flush()
    return duplicate
