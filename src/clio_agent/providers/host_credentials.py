"""Host credential chains for providers that never take an API key.

Google Vertex AI and AWS Bedrock sign requests with the credentials already on
the computer: Google's Application Default Credentials, and the AWS credential
chain. A catalog :class:`~clio_agent.providers.catalog_types.Provider` names its
chain in ``host_credentials``; the handshake asks :func:`present` instead of
checking an API key, and reports :func:`missing_reason` when the chain is empty,
so the failure names the real missing thing instead of an API key the provider
does not use.

The checks are local and cheap. Google: ``GOOGLE_APPLICATION_CREDENTIALS``
pointing at a file, or the ``gcloud auth application-default login`` file under
the gcloud configuration directory. An identity that exists only on a cloud VM's
metadata server is not detected, since detecting it would mean a network probe.
AWS: botocore's own credential resolver (environment, shared files, profiles,
SSO, container and instance roles), which is what LiteLLM uses at call time.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

__all__ = ["HOST_CREDENTIALS_MISSING", "chain_for", "missing_reason", "present"]

#: The typed reason code for an empty host credential chain.
HOST_CREDENTIALS_MISSING = "host_credentials_missing"


def _gcloud_config_dir() -> Path:
    """The gcloud configuration directory, as the gcloud CLI resolves it."""

    override = os.environ.get("CLOUDSDK_CONFIG", "").strip()
    if override:
        return Path(override)
    if os.name == "nt":
        appdata = os.environ.get("APPDATA", "").strip()
        if appdata:
            return Path(appdata) / "gcloud"
    return Path.home() / ".config" / "gcloud"


def _google_cloud_present() -> bool:
    """Whether Google Application Default Credentials exist on this computer."""

    explicit = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    if explicit:
        return Path(explicit).is_file()
    return (_gcloud_config_dir() / "application_default_credentials.json").is_file()


def _aws_present() -> bool:
    """Whether the AWS credential chain resolves a credential on this computer."""

    import botocore.session  # noqa: PLC0415 - heavy import, only for Bedrock checks

    return botocore.session.Session().get_credentials() is not None


@dataclass(frozen=True)
class _Chain:
    label: str
    present: Callable[[], bool]


_CHAINS: dict[str, _Chain] = {
    "google_cloud": _Chain("Google Cloud credentials", _google_cloud_present),
    "aws": _Chain("AWS credentials", _aws_present),
}


def _chain(name: str) -> _Chain:
    chain = _CHAINS.get(name)
    if chain is None:
        raise ValueError(f"unknown host credential chain: {name!r}")
    return chain


def chain_for(provider_id: str) -> str:
    """The host credential chain the catalog provider ``provider_id`` signs with, or ``""``.

    Keyed by provider identity, never by kind (Vertex AI and Bedrock share the
    ``openai`` kind with keyed providers), and read from the catalog so every
    caller -- whatever preset shape it holds -- gets the same answer.
    """

    from clio_agent.providers.catalog import get_provider  # noqa: PLC0415 - catalog imports heavy

    preset = get_provider(provider_id) if provider_id else None
    return preset.host_credentials if preset is not None else ""


def present(name: str) -> bool:
    """Whether the named host credential chain has a credential on this computer.

    Args:
        name: A chain name from a catalog provider's ``host_credentials``.

    Returns:
        True when a credential is found.

    Raises:
        ValueError: ``name`` is not a known chain.
    """

    return _chain(name).present()


def missing_reason(name: str) -> str:
    """The typed reason for an empty chain, e.g. ``"host_credentials_missing: ..."``.

    Args:
        name: A chain name from a catalog provider's ``host_credentials``.

    Returns:
        ``"<code>: <label> not found on this computer"``.
    """

    return f"{HOST_CREDENTIALS_MISSING}: {_chain(name).label} not found on this computer"
