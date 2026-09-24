"""Test helper: bind a session to an agent that declares A2UI catalogs.

v15 S8 made an agent's ``a2ui_catalogs`` the complete allowlist of catalogs
it may produce against. A bare session (no active blueprint) runs the builtin
main, which declares only ``clio-workspace``. Tests that also need Basic, or
a particular declaration order, path-activate a fixture pack: the
``a2ui-builtins-pack`` fixture declares ``[clio-workspace, basic]``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

FIXTURE_PACKS = Path(__file__).resolve().parents[1] / "fixtures" / "a2ui_packs"
BUILTINS_PACK = FIXTURE_PACKS / "builtins"
BUILTINS_PACK_ID = "a2ui-builtins-pack"


def bind_session_blueprint(app: Any, session_id: str, pack_root: Path, blueprint_id: str) -> None:
    """Path-activate ``pack_root`` as ``session_id``'s active blueprint."""

    app.state.sessions.update(
        session_id,
        metadata_patch={
            "active_agent_blueprint_id": blueprint_id,
            "active_agent_blueprint_path": str(pack_root),
        },
    )


def bind_builtin_catalogs(app: Any, session_id: str) -> None:
    """Bind ``session_id`` to the fixture agent declaring ``[clio-workspace, basic]``."""

    bind_session_blueprint(app, session_id, BUILTINS_PACK, BUILTINS_PACK_ID)
