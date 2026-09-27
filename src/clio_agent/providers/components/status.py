"""``update_available`` per provider: the installed component versions against PyPI.

A lockstep group (Codex: ``openai-codex`` X pins ``openai-codex-cli-bin==X``)
targets the newest version EVERY member has an installable wheel for; a single
distribution targets its own newest installable release. The installed version
is read from the distribution metadata on disk each time (never a module
attribute), so a status read right after an update sees the new files.
"""

from __future__ import annotations

import importlib
import importlib.metadata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from packaging.version import Version

from clio_agent.providers.components.pypi import (
    RELEASES,
    PyPILookupError,
    ReleaseIndex,
    ReleaseLookup,
)
from clio_agent.providers.components.registry import ProviderComponents, components_for


@dataclass(frozen=True)
class ComponentVersion:
    """One distribution's installed and target versions."""

    distribution: str
    installed_version: str
    latest_version: str

    @property
    def update_available(self) -> bool:
        """Whether the target is newer than what is installed."""
        if not self.installed_version or not self.latest_version:
            return False
        return Version(self.latest_version) > Version(self.installed_version)


@dataclass(frozen=True)
class ProviderComponentStatus:
    """The update answer for one provider's components."""

    spec: ProviderComponents
    components: tuple[ComponentVersion, ...] = ()
    checked_at: str = ""
    error_code: str = ""
    error: str = ""
    indexes: dict[str, ReleaseIndex] = field(default_factory=dict)

    @property
    def installed(self) -> bool:
        """Every distribution of the group is installed."""
        return bool(self.components) and all(c.installed_version for c in self.components)

    @property
    def update_available(self) -> bool:
        """Any distribution of an installed group has a newer target."""
        return self.installed and any(c.update_available for c in self.components)

    @property
    def target_version(self) -> str:
        """The group's headline target (the first distribution's)."""
        return self.components[0].latest_version if self.components else ""

    def targets(self) -> dict[str, str]:
        """``{distribution: target version}`` for every distribution."""
        return {c.distribution: c.latest_version for c in self.components}

    def installed_versions(self) -> dict[str, str]:
        """``{distribution: installed version}`` for every distribution."""
        return {c.distribution: c.installed_version for c in self.components}

    def to_wire(self) -> dict[str, Any]:
        """JSON shape served by ``GET /v1/providers/{id}/components``."""
        return {
            "provider_kind": self.spec.provider_kind,
            "installed": self.installed,
            "update_available": self.update_available,
            "target_version": self.target_version,
            "release_notes_url": self.spec.release_notes_url,
            "checked_at": self.checked_at,
            "components": [
                {
                    "distribution": c.distribution,
                    "installed_version": c.installed_version,
                    "latest_version": c.latest_version,
                    "update_available": c.update_available,
                }
                for c in self.components
            ],
            "error": {"code": self.error_code, "message": self.error} if self.error_code else None,
        }


def installed_version(distribution: str) -> str:
    """The distribution's version as recorded on disk now, or ``""`` when absent."""
    importlib.invalidate_caches()
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return ""


def group_targets(spec: ProviderComponents, indexes: dict[str, ReleaseIndex]) -> dict[str, str]:
    """The version each distribution of ``spec`` should move to ("" when none is installable)."""
    if spec.lockstep:
        common: set[str] | None = None
        for name in spec.distributions:
            versions = set(indexes[name].installable)
            common = versions if common is None else common & versions
        best = max(common or set(), key=Version, default="")
        return dict.fromkeys(spec.distributions, best)
    return {name: indexes[name].latest for name in spec.distributions}


def provider_component_status(
    provider_kind: str, *, refresh: bool = False, lookup: ReleaseLookup | None = None
) -> ProviderComponentStatus | None:
    """Installed vs latest for ``provider_kind``'s components (``None``: no components)."""
    spec = components_for(provider_kind)
    if spec is None:
        return None
    releases = lookup or RELEASES
    checked_at = datetime.now(timezone.utc).isoformat()
    installed = {name: installed_version(name) for name in spec.distributions}
    try:
        indexes = {name: releases.releases(name, refresh=refresh) for name in spec.distributions}
    except PyPILookupError as exc:
        return ProviderComponentStatus(
            spec=spec,
            components=tuple(ComponentVersion(n, installed[n], "") for n in spec.distributions),
            checked_at=checked_at,
            error_code=exc.code,
            error=str(exc),
        )
    targets = group_targets(spec, indexes)
    components = tuple(ComponentVersion(n, installed[n], targets[n]) for n in spec.distributions)
    missing = [n for n in spec.distributions if not targets[n]]
    return ProviderComponentStatus(
        spec=spec,
        components=components,
        checked_at=checked_at,
        error_code="component_no_installable_release" if missing else "",
        error=(
            f"no release installable on this computer for {', '.join(missing)}" if missing else ""
        ),
        indexes=indexes,
    )


__all__ = [
    "ComponentVersion",
    "ProviderComponentStatus",
    "group_targets",
    "installed_version",
    "provider_component_status",
]
