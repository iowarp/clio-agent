"""Tests for keyed, read-only credential resolution (``providers.credentials``).

Covers the four ref backends (default cloud env var, argonne token, named
per-account source, missing ref), the read-only contract (no ``os.environ``
writes), concurrency safety (N accounts resolve independently with no
cross-talk), and a backward-compat regression proving ``LMProviderConfig.
__post_init__`` still resolves the identical ``api_key`` for the default
provider.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

import pytest

import clio_agent.config as config
from clio_agent.config import LMProviderConfig
from clio_agent.providers import credentials
from clio_agent.providers.credentials import CredentialResolver

_CLOUD_ENV_VARS = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENROUTER_API_KEY",
    "CLIO_LM_API_KEY",
)
_ARGONNE_ENV_VARS = ("CLIO_ARGONNE_TOKEN", "ALCF_INFERENCE_TOKEN", "access_token")


@pytest.fixture(autouse=True)
def _clean_credential_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip inherited provider/account credentials so tests are hermetic."""
    for var in (*_CLOUD_ENV_VARS, *_ARGONNE_ENV_VARS):
        monkeypatch.delenv(var, raising=False)
    # Drop any pre-existing named per-account credentials from the ambient env.
    for key in list(os.environ):
        if key.startswith(credentials._NAMED_CRED_ENV_PREFIX):
            monkeypatch.delenv(key, raising=False)


class TestDefaultCloudRef:
    """The default ref reads the same well-known env var, read-only."""

    def test_default_ref_reads_cloud_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
        assert credentials.resolve("openai", "") == "sk-openai"

    def test_explicit_default_account_is_the_default_ref(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
        # ``<provider>:default`` denotes the default credential, same as "".
        assert credentials.resolve("anthropic", "anthropic:default") == "sk-ant"

    def test_default_ref_missing_env_returns_empty(self) -> None:
        assert credentials.resolve("openai", "") == ""

    def test_non_cloud_provider_default_ref_returns_empty(self) -> None:
        # lm_studio has no cloud env var; its local placeholder is a provider
        # default handled by __post_init__, not a credential.
        assert credentials.resolve("lm_studio", "") == ""

    def test_resolve_does_not_write_environ(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
        before = dict(os.environ)
        credentials.resolve("openai", "")
        credentials.resolve("openai", "openai:acctA")
        credentials.resolve("argonne", "")
        assert dict(os.environ) == before


class TestArgonneRef:
    """The argonne ref returns the override token via the config seam."""

    def test_argonne_ref_returns_override_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLIO_ARGONNE_TOKEN", "globus-override")
        assert credentials.resolve("argonne", "") == "globus-override"

    def test_argonne_ref_reads_alcf_ecosystem_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ALCF_INFERENCE_TOKEN", "alcf-tok")
        assert credentials.resolve_argonne_token() == "alcf-tok"

    def test_argonne_ref_routes_through_config_seam(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The runtime-refresh path + existing tests monkeypatch this seam; the
        # resolver must observe the patch.
        monkeypatch.setattr(config, "_resolve_argonne_api_key", lambda: "seam-token")
        assert credentials.resolve("argonne", "") == "seam-token"

    def test_argonne_missing_token_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("clio_agent.providers.argonne_auth.tokens_exist", lambda: False)
        assert credentials.resolve("argonne", "") == ""

    def test_argonne_named_ref_does_not_return_default_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A NAMED (non-default) argonne ref must NOT silently return the node default token.

        Finding #5: ``resolve`` ignored ``credential_ref`` for argonne and returned
        the node default Globus token for *any* ref, authenticating the expert under
        the wrong identity. A named ref with no per-account backend must surface ""
        so the LM call raises an actionable auth error (no silent default identity).
        """
        monkeypatch.setattr(config, "_resolve_argonne_api_key", lambda: "default-globus-token")
        # The default ref still returns the node default token (unchanged).
        assert credentials.resolve("argonne", "") == "default-globus-token"
        assert credentials.resolve("argonne", "argonne:default") == "default-globus-token"
        # A NAMED ref does not borrow the default identity.
        assert credentials.resolve("argonne", "argonne:acctB") == ""
        assert credentials.resolve("argonne", "acctB") == ""


class TestNamedAccountRef:
    """A named ref reads a distinct per-account source (two accounts, two keys)."""

    def test_two_accounts_two_keys(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLIO_CRED_OPENAI_ACCTA", "key-A")
        monkeypatch.setenv("CLIO_CRED_OPENAI_ACCTB", "key-B")
        assert credentials.resolve("openai", "openai:acctA") == "key-A"
        assert credentials.resolve("openai", "openai:acctB") == "key-B"

    def test_named_ref_does_not_fall_back_to_default_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A missing named credential returns "" — NO silent fallback to the
        # default provider env var (design §3.2).
        monkeypatch.setenv("OPENAI_API_KEY", "sk-default")
        assert credentials.resolve("openai", "openai:acctZ") == ""

    def test_bare_account_label_without_colon(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLIO_CRED_OPENAI_TEAM", "key-team")
        assert credentials.resolve("openai", "team") == "key-team"


class TestMissingRef:
    """Unknown / missing ref returns '' (LM call surfaces the auth error)."""

    def test_unknown_provider_returns_empty(self) -> None:
        assert credentials.resolve("no_such_provider", "") == ""

    def test_missing_named_account_returns_empty(self) -> None:
        assert credentials.resolve("openai", "openai:ghost") == ""


class TestCredentialResolverClass:
    """The injectable ``CredentialResolver`` delegates to the module function."""

    def test_resolver_matches_module_function(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
        resolver = CredentialResolver()
        assert resolver.resolve("openai", "") == credentials.resolve("openai", "")


class TestConcurrency:
    """N accounts resolve independently with zero cross-talk (design §7.4)."""

    def test_concurrent_named_accounts_no_cross_talk(self, monkeypatch: pytest.MonkeyPatch) -> None:
        accounts = {f"acct{i}": f"key-{i}" for i in range(24)}
        for account, key in accounts.items():
            monkeypatch.setenv(credentials._named_account_env_var("openai", account), key)

        def _resolve(item: tuple[str, str]) -> tuple[str, bool]:
            account, expected = item
            got = credentials.resolve("openai", f"openai:{account}")
            return account, got == expected

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(_resolve, list(accounts.items()) * 4))

        assert all(ok for _, ok in results)


class TestBackwardCompatPostInit:
    """__post_init__ resolves the identical api_key it did before this module."""

    def test_default_provider_cloud_key_unchanged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-live")
        cfg = LMProviderConfig(provider="openai")
        assert cfg.api_key == "sk-live"
        assert cfg.api_key == credentials.resolve("openai", "")

    def test_local_provider_placeholder_preserved(self) -> None:
        # lm_studio's "lm-studio" placeholder is a provider default that
        # __post_init__ must still apply on the empty-resolution path.
        assert LMProviderConfig(provider="lm_studio").api_key == "lm-studio"
        assert LMProviderConfig(provider="ollama").api_key == "ollama"

    def test_cloud_provider_missing_key_stays_empty(self) -> None:
        assert LMProviderConfig(provider="openai").api_key == ""

    def test_argonne_post_init_uses_seam(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "_resolve_argonne_api_key", lambda: "token")
        assert LMProviderConfig(provider="argonne").api_key == "token"
