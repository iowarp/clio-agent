"""Recover an existing Windows fence during Desktop runtime installation."""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any


def prepare_existing_windows_fence(
    *, progress: Callable[[str], None] | None = None
) -> dict[str, Any]:
    """Verify existing sandbox accounts for this installation without prompting for UAC.

    Fresh machines still use the explicit Protected execution setup action.
    An unsuccessful verification never creates an affirmative fence receipt.
    """
    if sys.platform != "win32":
        return {"status": "not_required"}
    from clio_agent.runtime.codex_desktop_access import (
        ensure_desktop_runtime_access,  # noqa: PLC0415
    )
    from clio_agent.runtime.sandbox_cli import provision_codex_windows  # noqa: PLC0415
    from clio_agent.runtime.sandbox_codex import REASON_CODEX_WINDOWS_UNPROVISIONED  # noqa: PLC0415

    result = provision_codex_windows(
        allow_elevation=False,
        grantor=lambda: ensure_desktop_runtime_access(progress=progress),
    )
    if result.reason == REASON_CODEX_WINDOWS_UNPROVISIONED:
        return {"status": "setup_required", "next_action": result.next_action}
    failed_grants = [
        item for item in result.extra.get("fleet_runtime_grants", []) if item["status"] == "failed"
    ]
    if not result.ok or failed_grants:
        from clio_agent.runtime.document_runtime import DocumentRuntimeError  # noqa: PLC0415

        detail = failed_grants if failed_grants else result.detail
        raise DocumentRuntimeError(f"Protected execution verification failed: {detail}")
    return {"status": "available", "reason": result.reason}
