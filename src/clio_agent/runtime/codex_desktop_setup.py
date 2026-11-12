"""Qualify native MXC or recover the elevated Windows fence during installation."""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


def prepare_existing_windows_fence(
    *, progress: Callable[[str], None] | None = None, allow_elevation: bool = False
) -> dict[str, Any]:
    """Verify the fence; only an explicit installation request may create accounts.

    Normal startup never prompts for UAC. Windows installation can explicitly
    request the one-time setup, with real administrator approval and verification.
    An unsuccessful verification never creates an affirmative fence receipt.
    """
    if sys.platform != "win32":
        return {"status": "not_required"}
    from clio_agent.runtime.sandbox_cli import provision_codex_windows  # noqa: PLC0415
    from clio_agent.runtime.sandbox_codex import REASON_CODEX_WINDOWS_UNPROVISIONED  # noqa: PLC0415

    result = provision_codex_windows(
        allow_elevation=allow_elevation,
        progress=progress,
    )
    if result.reason == REASON_CODEX_WINDOWS_UNPROVISIONED:
        return {"status": "setup_required", "next_action": result.next_action}
    grants = result.extra.get("fleet_runtime_grants", [])
    failed_grants = [item for item in grants if item["status"] == "failed"]
    if not result.ok or failed_grants:
        from clio_agent.runtime.document_runtime import DocumentRuntimeError  # noqa: PLC0415
        from clio_agent.runtime.sandbox_cli import _runtime_access_failure  # noqa: PLC0415

        if failed_grants:
            # The per-grant records are diagnostics; people read the plain sentence.
            logger.warning("protected execution runtime grants failed: %s", failed_grants)
            result = _runtime_access_failure(grants, elevated=result.elevated) or result
        raise DocumentRuntimeError(f"{result.detail} {result.next_action}")
    return {
        "status": "available",
        "reason": result.reason,
        "implementation": result.extra.get("implementation", "elevated"),
    }
