"""The ONLY user-updatable components of a CLIO runtime.

Everything a CLIO runtime ships is installed from ``uv.lock`` and checked
against it (``scripts/check_bundle_matches_lock.py``). The provider components
below are the one deliberate exception: the providers gate new models by client
version, so a runtime that cannot move these forward between releases loses
models the user's own account already serves. They may therefore be updated in
place, to a version NEWER than the lock, from the provider panel. For Codex the
component is the Codex runtime (``openai-codex-cli-bin``), whose version is the
``client_version`` the direct transport's model list presents to the backend.

``scripts/check_bundle_matches_lock.py`` carries the same set as
``USER_UPDATABLE_COMPONENTS`` (it is standard-library-only and cannot import
this package); ``tests/test_scripts/test_check_bundle_matches_lock.py`` fails
when the two drift.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderComponents:
    """The user-updatable distributions of one provider.

    Attributes:
        provider_kind: The provider kind (``codex``, ``claude_code``).
        distributions: The PyPI distributions, updated together; each moves to
            its own newest installable release.
        modules: The top-level import names, in distribution order.
        release_notes_url: Where the provider publishes release notes.
    """

    provider_kind: str
    distributions: tuple[str, ...]
    modules: tuple[str, ...]
    release_notes_url: str


PROVIDER_COMPONENTS: dict[str, ProviderComponents] = {
    "codex": ProviderComponents(
        provider_kind="codex",
        distributions=("openai-codex-cli-bin",),
        modules=("codex_cli_bin",),
        release_notes_url="https://github.com/openai/codex/releases",
    ),
    "claude_code": ProviderComponents(
        provider_kind="claude_code",
        distributions=("claude-agent-sdk",),
        modules=("claude_agent_sdk",),
        release_notes_url="https://github.com/anthropics/claude-agent-sdk-python/releases",
    ),
}

#: Every user-updatable distribution name (PEP 503 normalized).
USER_UPDATABLE_COMPONENTS: frozenset[str] = frozenset(
    name for spec in PROVIDER_COMPONENTS.values() for name in spec.distributions
)


def components_for(provider_kind: str) -> ProviderComponents | None:
    """The component group of a provider kind, or ``None`` when it has none."""
    return PROVIDER_COMPONENTS.get(provider_kind)


__all__ = [
    "PROVIDER_COMPONENTS",
    "USER_UPDATABLE_COMPONENTS",
    "ProviderComponents",
    "components_for",
]
