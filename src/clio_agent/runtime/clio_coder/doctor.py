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
            state=(
                IntegrationState.MISCONFIGURED
                if source == CLIO_CODER_BIN_ENV
                else IntegrationState.SKIPPED
            ),
            summary=f"{exc}; coding delegation (run_clio_coder) is withheld",
            config_source=source,
            next_action=(
                f"Correct or unset {CLIO_CODER_BIN_ENV}; then run clio-coder --version"
                if source == CLIO_CODER_BIN_ENV
                else f"Install clio-coder {minimum}+ on PATH and restart CLIO"
            ),
            details={"reason": exc.error_type, "minimum": minimum},
            required=False,
        )
    entry = " ".join(install.argv)
    try:
        found = install.require_supported()
    except ClioCoderError as exc:
        return IntegrationStatus(
            name=NAME,
            state=(
                IntegrationState.MISCONFIGURED
                if exc.error_type == "clio_coder_too_old"
                else IntegrationState.DEGRADED
            ),
            summary=f"{exc}; coding delegation is withheld",
            config_source=source,
            next_action=f"Check `{entry} --version`; install Clio Coder {minimum}+ and restart CLIO",
            endpoint=entry,
            details={"minimum": minimum, "reason": exc.error_type},
            required=False,
        )
    version = ".".join(map(str, found))
    return IntegrationStatus(
        name=NAME,
        state=IntegrationState.READY,
        summary=f"Clio Coder {version}; binary ready for coding delegation",
        config_source=source,
        next_action="No extra setup: delegation inherits the conversation's model and autonomy",
        endpoint=entry,
        details={"version": version, "minimum": minimum},
        required=False,
    )
