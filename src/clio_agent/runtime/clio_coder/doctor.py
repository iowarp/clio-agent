"""The doctor row for Clio Coder, the coding agent CLIO delegates code changes to."""

from __future__ import annotations

from typing import TYPE_CHECKING

from clio_agent.runtime.clio_coder.launch import (
    CLIO_CODER_BIN_ENV,
    MIN_CLIO_CODER_VERSION,
    ClioCoderError,
    ClioCoderInstall,
)

if TYPE_CHECKING:
    from clio_agent.runtime.status import IntegrationStatus

NAME = "clio_coder"


def probe_clio_coder() -> IntegrationStatus:
    """Report where Clio Coder is found and whether its version is supported."""
    import os  # noqa: PLC0415

    from clio_agent.runtime.status import IntegrationState, IntegrationStatus  # noqa: PLC0415

    minimum = ".".join(map(str, MIN_CLIO_CODER_VERSION))
    source = CLIO_CODER_BIN_ENV if os.environ.get(CLIO_CODER_BIN_ENV) else "PATH"
    try:
        install = ClioCoderInstall.resolve()
    except ClioCoderError as exc:
        return IntegrationStatus(
            name=NAME,
            state=IntegrationState.SKIPPED,
            summary=f"{exc}; coding delegation (delegate_to_clio_coder) is withheld",
            config_source=source,
            next_action=f"install clio-coder {minimum}+ on PATH or set {CLIO_CODER_BIN_ENV}",
            required=False,
        )
    entry = " ".join(install.argv)
    found = install.version()
    if found is None or found < MIN_CLIO_CODER_VERSION:
        shown = f"Clio Coder {'.'.join(map(str, found))}" if found else "Its version is unreadable"
        return IntegrationStatus(
            name=NAME,
            state=IntegrationState.MISCONFIGURED if found else IntegrationState.DEGRADED,
            summary=f"{shown}; CLIO needs {minimum}+, so coding delegation is withheld",
            config_source=source,
            next_action=f"install clio-coder {minimum} or newer (check `clio-coder --version`)",
            endpoint=entry,
            details={"minimum": minimum},
            required=False,
        )
    version = ".".join(map(str, found))
    return IntegrationStatus(
        name=NAME,
        state=IntegrationState.READY,
        summary=f"Clio Coder {version} (minimum {minimum}); coding delegation available",
        config_source=source,
        next_action="none",
        endpoint=entry,
        details={"version": version, "minimum": minimum},
        required=False,
    )
