"""State initializers introduced by the Codex rework."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI


def initialize_a2ui_store(app: FastAPI, state_root: Path) -> None:
    """Install the transcript-projected A2UI service on application state.

    The pre-release sidecar is deliberately not imported: its records can race
    the authoritative message log and never shipped as a compatibility promise.
    If one remains locally, retain it untouched and expose a typed superseded
    notice so operators can remove it after inspecting the evidence.
    """

    from clio_agent.gact.a2ui_store import A2UIStore  # noqa: PLC0415

    legacy_path = state_root / "a2ui-surfaces.json"
    app.state.a2ui_ledger_degradation = (
        {
            "reason": "a2ui_ledger_superseded",
            "source_path": str(legacy_path),
            "replacement": "session_message_log",
        }
        if legacy_path.exists()
        else None
    )
    app.state.a2ui_store = A2UIStore(app=app, bus=app.state.bus)


def initialize_session_defaults(app: FastAPI, sessions_path: Path | None) -> None:
    """Install the persisted session-defaults store on application state."""

    from clio_agent.gact.session_defaults import SessionDefaultsStore  # noqa: PLC0415

    app.state.session_defaults = SessionDefaultsStore(
        path=(sessions_path.parent / "session-defaults.json") if sessions_path else None
    )
