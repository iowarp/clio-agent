"""Which LibreOffice the document tools use, and where its seeded profiles live."""

from __future__ import annotations

from pathlib import Path

from clio_agent import paths
from clio_agent.runtime.execution_environment import bundled_root


def office_runtime_root() -> Path:
    """Select the relocated bundled renderer or this user's private cache."""
    from clio_agent.runtime.document_stack.office import VERSION, executable

    bundled = bundled_root()
    if bundled is not None and executable(bundled / "documents" / "office"):
        return bundled / "documents" / "office"
    return paths.user_cache_dir() / "document-office" / VERSION


def office_profile_templates() -> Path:
    """Hold each Office build's seeded first-start profile for this user."""
    return paths.user_cache_dir() / "document-office-profiles"


def office_profile_starter(office_root: Path) -> Path | None:
    """Return the first-start profile laid out beside the bundled renderer, if any."""
    from clio_agent.runtime.document_stack.office import PROFILE_STARTER

    bundled = bundled_root()
    if bundled is None or office_root != bundled / "documents" / "office":
        return None
    starter = office_root.parent / PROFILE_STARTER
    return starter if starter.is_file() else None


def prepare_office_runtime(*, timings: dict[str, float] | None = None) -> str:
    """Use an installed renderer or provision a private verified renderer automatically.

    ``timings``, when given, receives the renderer's unpack and seed seconds.
    """
    from clio_agent.runtime.document_stack.office import ensure_office, seed_profile
    from clio_agent.runtime.document_stack.process import find_native

    managed_root = office_runtime_root()
    native = find_native("soffice")
    if native and not Path(native).resolve().is_relative_to(managed_root.resolve()):
        seed_profile(Path(native), office_profile_templates())
        return native
    # A discovered managed executable still needs its host-side access repair.
    return ensure_office(
        managed_root,
        profile_templates=office_profile_templates(),
        profile_starter=office_profile_starter(managed_root),
        timings=timings,
    )
