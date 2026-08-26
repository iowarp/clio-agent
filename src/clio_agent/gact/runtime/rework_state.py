"""State initializers introduced by the Codex rework."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI


def initialize_a2ui_store(app: FastAPI, state_root: Path) -> None:
    """Install the transitional A2UI store on application state."""

    from clio_agent.gact.a2ui import A2UIStore  # noqa: PLC0415

    app.state.a2ui_store = A2UIStore(path=state_root / "a2ui-surfaces.json", bus=app.state.bus)


def initialize_session_defaults(app: FastAPI, sessions_path: Path | None) -> None:
    """Install the persisted session-defaults store on application state."""

    from clio_agent.gact.session_defaults import SessionDefaultsStore  # noqa: PLC0415

    app.state.session_defaults = SessionDefaultsStore(
        path=(sessions_path.parent / "session-defaults.json") if sessions_path else None
    )
