"""Invalidate discovery projections and announce one connected CLIO revision."""

from __future__ import annotations

import uuid
from typing import Any

from clio_agent.gact.events import Event
from clio_agent.gact.runtime.app_state import per_app_dict


def blueprint_revision_changed(app: Any) -> str:
    """Notify every connected client after a committed marketplace mutation."""
    catalogs = getattr(app.state, "a2ui_catalogs", None)
    if catalogs is not None:
        catalogs.invalidate()
    per_app_dict("workflow_state_schemas", app=app).clear()
    revision = uuid.uuid4().hex
    app.state.blueprint_revision = revision
    app.state.bus.publish(
        Event(type="blueprint.revision.changed", session_id="", payload={"revision": revision})
    )
    return revision
