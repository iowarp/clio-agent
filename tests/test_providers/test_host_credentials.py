"""Providers that authenticate with the host's credentials never ask for an API key.

Google Vertex AI and AWS Bedrock sign requests with the credentials already on
the computer (Application Default Credentials, the AWS credential chain). A
missing credential must read as exactly that -- "Google Cloud credentials not
found on this computer" -- never "no API key provided", which sent people
looking for a key those providers do not use.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from clio_agent.providers import host_credentials
from clio_agent.providers.catalog import get_provider
from clio_agent.providers.handshake.base import HandshakeContext
from clio_agent.providers.handshake.model import AuthState, ConnectivityState
from clio_agent.providers.handshake.openai_compat import OpenAICompatHandshake

_GOOGLE_ENV = ("GOOGLE_APPLICATION_CREDENTIALS", "CLOUDSDK_CONFIG")
_AWS_ENV = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_PROFILE",
    "AWS_DEFAULT_PROFILE",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
)


@pytest.fixture
def bare_host(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A computer with no Google Cloud and no AWS credentials anywhere."""

    for name in (*_GOOGLE_ENV, *_AWS_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CLOUDSDK_CONFIG", str(tmp_path / "gcloud"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "aws" / "credentials"))
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "aws" / "config"))
    # Never wait on a cloud instance-metadata endpoint from a unit test.
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    return tmp_path


class _NoNetworkClient:
    """Fails the test if the handshake reaches the network."""

    calls: list[str]

    def __init__(self) -> None:
        self.calls = []

    async def get(self, url: str, headers: dict[str, str] | None = None) -> object:
        self.calls.append(url)
        raise AssertionError(f"unexpected network call to {url}")


def _ctx(provider_id: str, api_key: str) -> HandshakeContext:
    preset = get_provider(provider_id)
    assert preset is not None
    return HandshakeContext(
        provider_id=preset.id,
        provider_kind=preset.provider_kind,
        api_base=preset.api_base,
        api_key=api_key,
        allow_external_sources=False,
    )


def test_catalog_names_the_host_credential_chain() -> None:
    vertex = get_provider("vertex_ai")
    bedrock = get_provider("bedrock")
    openai = get_provider("openai")
    assert vertex is not None and vertex.host_credentials == "google_cloud"
    assert bedrock is not None and bedrock.host_credentials == "aws"
    assert openai is not None and openai.host_credentials == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_id", "expected"),
    [
        ("vertex_ai", "Google Cloud credentials not found on this computer"),
        ("bedrock", "AWS credentials not found on this computer"),
    ],
)
@pytest.mark.parametrize("api_key", ["", "EMPTY"])
async def test_missing_host_credentials_is_named_not_an_api_key(
    bare_host: Path, provider_id: str, expected: str, api_key: str
) -> None:
    preset = get_provider(provider_id)
    client = _NoNetworkClient()

    conn = await OpenAICompatHandshake(provider=preset).check_connectivity(
        client, _ctx(provider_id, api_key)
    )

    assert conn.connectivity is ConnectivityState.SKIPPED
    assert conn.auth is AuthState.MISSING
    assert conn.error_code == "host_credentials_missing"
    assert conn.error == f"host_credentials_missing: {expected}"
    assert "API key" not in (conn.error or "")
    assert client.calls == []


def test_google_application_credentials_file_counts(
    bare_host: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_file = bare_host / "sa.json"
    key_file.write_text("{}", encoding="utf-8")
    assert not host_credentials.present("google_cloud")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(key_file))
    assert host_credentials.present("google_cloud")


def test_gcloud_application_default_login_counts(bare_host: Path) -> None:
    adc = bare_host / "gcloud" / "application_default_credentials.json"
    adc.parent.mkdir(parents=True)
    adc.write_text("{}", encoding="utf-8")
    assert host_credentials.present("google_cloud")


def test_aws_environment_keys_count(bare_host: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert not host_credentials.present("aws")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    assert host_credentials.present("aws")


def test_aws_shared_credentials_file_counts(bare_host: Path) -> None:
    creds = bare_host / "aws" / "credentials"
    creds.parent.mkdir(parents=True)
    creds.write_text(
        "[default]\naws_access_key_id = AKIAEXAMPLE\naws_secret_access_key = secret\n",
        encoding="utf-8",
    )
    assert host_credentials.present("aws")


def test_unknown_chain_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown host credential chain"):
        host_credentials.present("azure")


def test_refresh_skips_host_credential_providers_without_credentials(bare_host: Path) -> None:
    from clio_agent.providers.model_discovery.refresh import is_provider_configured

    vertex = get_provider("vertex_ai")
    assert vertex is not None
    assert not is_provider_configured(vertex)
    adc = bare_host / "gcloud" / "application_default_credentials.json"
    adc.parent.mkdir(parents=True)
    adc.write_text("{}", encoding="utf-8")
    assert is_provider_configured(vertex)


@pytest.mark.asyncio
async def test_the_catalog_probe_shape_gets_the_same_reason(bare_host: Path) -> None:
    """The provider catalog hands the handshake a gact preset without the field.

    The chain is resolved from the catalog by provider id, so that shape (here any
    object with no ``host_credentials``) reports the same typed reason.
    """

    conn = await OpenAICompatHandshake(provider=object()).check_connectivity(
        _NoNetworkClient(), _ctx("vertex_ai", "EMPTY")
    )

    assert conn.error == (
        "host_credentials_missing: Google Cloud credentials not found on this computer"
    )
