"""What a deploy's claim step found, as the question the person answers (owner module).

The claim step never stops, starts or upgrades an agent on its own. When it finds one
(running on the port, or installed in the target's root but stopped) the deploy ends
with a typed ``clio_deploy_version_conflict`` and this detail, and the person chooses:
connect to it (starting it when stopped), update it and connect, or start a new one.
"""

from __future__ import annotations

from clio_agent.gact.infrastructure.clio_agent_deploy import ClaimResult
from clio_agent.gact.infrastructure.models import VersionConflictDetail


def conflict_question(
    claim: ClaimResult, *, target_version: str, port: int
) -> tuple[str, VersionConflictDetail] | None:
    """The progress line and conflict detail to ask about, or None when nothing was found."""

    if claim.result == "installed":
        progress = (
            f"CLIO {claim.installed_version or '(unknown version)'} is installed on this host "
            "but not running."
        )
        health = "stopped"
    elif claim.result == "found":
        health = claim.health or "unknown"
        progress = (
            f"CLIO {claim.installed_version or '(unknown version)'} "
            f"is already running on this host (pid {claim.pid or 'unknown'})."
            if health == "healthy"
            else (
                f"A CLIO-looking process is already on this host's port "
                f"(pid {claim.pid or 'unknown'}), but it isn't answering."
            )
        )
    else:
        return None
    return progress, VersionConflictDetail(
        installed_version=claim.installed_version or "unknown",
        pid=claim.pid or "",
        health=health,  # type: ignore[arg-type]
        target_version=target_version,
        owner=claim.owner or "",
        port=port,
    )
