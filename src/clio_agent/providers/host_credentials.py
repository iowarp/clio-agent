"""Host credential chains for providers that never take an API key.

Google Vertex AI and AWS Bedrock sign requests with the credentials already on
the computer: Google's Application Default Credentials, and the AWS credential
chain. A catalog :class:`~clio_agent.providers.catalog_types.Provider` names its
chain in ``host_credentials``; the handshake asks :func:`present` instead of
checking an API key, and reports :func:`missing_reason` when the chain is empty,
so the failure names the real missing thing instead of an API key the provider
does not use.

The checks are local and cheap: they never open a network connection or start a
process. Google: ``GOOGLE_APPLICATION_CREDENTIALS`` pointing at a file, or the
``gcloud auth application-default login`` file under the gcloud configuration
directory. AWS: botocore's offline credential providers (environment keys,
shared credentials and config files, SSO, web identity and ``aws login``
profiles), plus local configuration that names a credential source without
resolving it: a profile with ``role_arn`` or ``credential_process``, or the
container credential variables. botocore's container and instance-metadata
providers are never consulted, since they connect to an HTTP endpoint (for
EC2, the link-local ``169.254.169.254``, which macOS treats as a Local Network
access). An identity that exists only on a cloud VM's metadata server is
therefore not detected, for either chain. LiteLLM still resolves the full AWS
chain when a Bedrock request is actually made.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

__all__ = ["HOST_CREDENTIALS_MISSING", "chain_for", "missing_reason", "present"]

logger = logging.getLogger(__name__)

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


#: botocore credential providers (by ``METHOD``) whose ``load()`` reads only local
#: state. SSO and web identity return deferred credentials that contact AWS only
#: when used; ``aws login`` reads its token cache. Anything else, including
#: botocore's network-backed ``container-role`` and ``iam-role`` (IMDS) providers,
#: ``assume-role`` (which may resolve its source from those endpoints),
#: ``custom-process`` (which runs a command) and any provider added in a future
#: botocore, is excluded.
_AWS_OFFLINE_PROVIDERS = frozenset(
    {
        "env",
        "assume-role-with-web-identity",
        "sso",
        "shared-credentials-file",
        "login",
        "config-file",
        "ec2-credentials-file",
        "boto-config",
    }
)

#: Profile keys that configure a credential source whose resolution could reach the
#: network or run a command (an assumed role's ``credential_source``, a
#: ``credential_process``). Their presence counts without resolving them.
_AWS_PROFILE_SOURCE_KEYS = ("role_arn", "credential_process")

#: Container credential variables (ECS, EKS Pod Identity). Their presence counts
#: without fetching from the endpoint they name.
_AWS_CONTAINER_ENV = (
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
)


def _aws_present() -> bool:
    """Whether AWS credentials are configured on this computer, without any network call."""

    if any(os.environ.get(name, "").strip() for name in _AWS_CONTAINER_ENV):
        return True

    import botocore.exceptions  # noqa: PLC0415 - heavy import, only for Bedrock checks
    import botocore.session  # noqa: PLC0415

    session = botocore.session.Session()
    try:
        profile = session.get_scoped_config()
    except botocore.exceptions.ProfileNotFound as exc:
        logger.info("AWS credentials not detected: %s", exc)
        return False
    if any(profile.get(key) for key in _AWS_PROFILE_SOURCE_KEYS):
        return True

    resolver = session.get_component("credential_provider")
    resolver.providers = [p for p in resolver.providers if p.METHOD in _AWS_OFFLINE_PROVIDERS]
    try:
        return resolver.load_credentials() is not None
    except botocore.exceptions.BotoCoreError as exc:
        logger.warning("AWS credential configuration is incomplete: %s", exc)
        return False


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
