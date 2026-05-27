"""Tests for the provider registry — the single source of truth for
LM provider metadata in clio-agent.

These tests guard the invariants downstream code relies on:

- Every catalog entry has a unique ``id``.
- Each ``provider_kind`` (the wire-level value that flows into
  ``LMProviderConfig.provider``) has **at most one** ``is_kind_default``
  entry. If two entries claim the kind default, the derived
  ``PROVIDER_DEFAULTS`` dict picks the first deterministically, but
  flagging two is a config bug.
- Every entry has a non-empty ``api_base``, ``label``, and
  ``provider_kind`` (so the modal renders cleanly).
- The derived legacy views (``as_provider_defaults_dict``,
  ``as_cloud_api_key_env``, ``as_lm_presets``, ``as_provider_models_dict``)
  match the shapes downstream code expects.
"""

from __future__ import annotations

import pytest

from clio_agent.providers.registry import (
    PROVIDERS,
    Provider,
    as_cloud_api_key_env,
    as_lm_presets,
    as_provider_defaults_dict,
    as_provider_models_dict,
    get_provider,
    iter_providers,
    kind_default,
)

# ---------------------------------------------------------------------
# invariants over PROVIDERS itself
# ---------------------------------------------------------------------


class TestRegistryInvariants:
    def test_at_least_one_provider(self) -> None:
        assert len(PROVIDERS) > 0

    def test_iter_providers_returns_registration_order(self) -> None:
        assert iter_providers() == PROVIDERS

    def test_ids_are_unique(self) -> None:
        ids = [p.id for p in PROVIDERS]
        duplicates = [i for i in ids if ids.count(i) > 1]
        assert not duplicates, f"duplicate provider ids: {set(duplicates)}"

    def test_every_provider_has_required_strings(self) -> None:
        for p in PROVIDERS:
            assert p.id, f"empty id: {p}"
            assert p.label, f"{p.id}: empty label"
            assert p.provider_kind, f"{p.id}: empty provider_kind"
            # api_base may be conceptually empty for future providers
            # (e.g. Codex SDK transport doesn't need a URL), but every
            # entry shipped today must have one. When the codex
            # registry entry switches to the CustomLLM path in #51, this
            # check may need a "provider_kind == 'codex' or api_base"
            # exemption.
            assert p.api_base, f"{p.id}: empty api_base"

    def test_at_most_one_kind_default_per_kind(self) -> None:
        seen: set[str] = set()
        for p in PROVIDERS:
            if not p.is_kind_default:
                continue
            assert p.provider_kind not in seen, (
                f"multiple kind defaults for {p.provider_kind}: second is {p.id}"
            )
            seen.add(p.provider_kind)

    @pytest.mark.parametrize(
        "kind",
        [
            "lm_studio",
            "ollama",
            "openai",
            "anthropic",
            "argonne",
            "codex",
            "claude_code",
        ],
    )
    def test_each_active_kind_has_a_default(self, kind: str) -> None:
        # Sanity: every wire kind currently in PROVIDER_DEFAULTS must
        # have exactly one is_kind_default entry.
        assert kind_default(kind) is not None, (
            f"no kind default for {kind}; PROVIDER_DEFAULTS will lose this row"
        )

    def test_api_key_env_only_set_when_required(self) -> None:
        # Cloud providers carry an api_key_env (e.g. OPENAI_API_KEY) so
        # __post_init__ can fill the key from the environment. Local /
        # no-auth providers should leave it None — having an env var
        # there would invite mysterious silent failures.
        for p in PROVIDERS:
            if p.auth_method == "none":
                assert p.api_key_env is None, (
                    f"{p.id}: auth_method='none' but api_key_env={p.api_key_env!r}"
                )


# ---------------------------------------------------------------------
# lookup helpers
# ---------------------------------------------------------------------


class TestLookups:
    def test_get_provider_by_id(self) -> None:
        p = get_provider("openai")
        assert p is not None
        assert p.id == "openai"
        assert p.provider_kind == "openai"

    def test_get_provider_returns_none_for_unknown(self) -> None:
        assert get_provider("does-not-exist") is None

    def test_kind_default_resolves_argonne_sophia(self) -> None:
        p = kind_default("argonne")
        assert p is not None
        assert p.id == "argonne_sophia"

    def test_kind_default_returns_none_for_unknown_kind(self) -> None:
        assert kind_default("does-not-exist") is None


# ---------------------------------------------------------------------
# derived legacy views
# ---------------------------------------------------------------------


class TestDerivedViews:
    def test_provider_defaults_keys_match_kinds(self) -> None:
        defaults = as_provider_defaults_dict()
        assert set(defaults.keys()) == {
            "lm_studio",
            "ollama",
            "openai",
            "anthropic",
            "argonne",
            "codex",
            "claude_code",
        }

    def test_provider_defaults_have_required_keys(self) -> None:
        for kind, row in as_provider_defaults_dict().items():
            for key in ("api_base", "model", "api_key"):
                assert key in row, f"{kind}: missing {key}"

    def test_provider_defaults_argonne_overrides(self) -> None:
        row = as_provider_defaults_dict()["argonne"]
        assert row["model"] == "openai/gpt-oss-120b"
        assert row["max_tokens"] == 4096
        assert row["strip_openai_prefix"] is False

    def test_argonne_catalog_prefers_modern_models_before_legacy_llama31(self) -> None:
        models = as_provider_models_dict()["argonne_sophia"]
        ids = [row["id"] for row in models]
        assert ids[:3] == ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "gpt-oss-120b"]
        assert "meta-llama/Meta-Llama-3.1-8B-Instruct" in ids
        assert ids.index("openai/gpt-oss-120b") < ids.index(
            "meta-llama/Meta-Llama-3.1-8B-Instruct"
        )

    def test_cloud_api_key_env_only_cloud_kinds(self) -> None:
        env_map = as_cloud_api_key_env()
        assert env_map == {
            "openai": "OPENAI_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
        }

    def test_lm_presets_includes_every_provider(self) -> None:
        presets = as_lm_presets()
        ids = {p.id for p in presets}
        registry_ids = {p.id for p in PROVIDERS}
        assert ids == registry_ids

    def test_provider_models_dict_keys_cover_ids_and_kind_argonne(self) -> None:
        models = as_provider_models_dict()
        # Every preset id should have an entry.
        for p in PROVIDERS:
            assert p.id in models, f"{p.id} missing from _PROVIDER_MODELS"
        # Argonne preset ids don't match the kind, so a bare-kind key
        # ("argonne") must also be present for legacy callers that look
        # up by wire kind.
        assert "argonne" in models

    def test_codex_catalog_uses_user_facing_model_ids(self) -> None:
        models = as_provider_models_dict()["codex"]
        ids = {row["id"] for row in models}
        assert {"gpt-5.5", "gpt-5.5-codex", "gpt-5.1"} <= ids
        assert all(not model_id.startswith("cdx-") for model_id in ids)

    def test_claude_code_catalog_uses_user_facing_model_ids(self) -> None:
        models = as_provider_models_dict()["claude_code"]
        ids = {row["id"] for row in models}
        assert {"sonnet", "opus", "haiku"} <= ids
        assert all(not model_id.startswith("cc-") for model_id in ids)

    def test_local_vllm_is_not_labeled_as_alcf_provider(self) -> None:
        provider = get_provider("argonne_local_vllm")
        assert provider is not None
        assert provider.label == "vLLM (localhost)"
        assert "ALCF" not in provider.label


# ---------------------------------------------------------------------
# provider frozenness — guards against accidental mutation
# ---------------------------------------------------------------------


class TestImmutability:
    def test_provider_is_frozen(self) -> None:
        from dataclasses import FrozenInstanceError

        p = get_provider("openai")
        assert p is not None
        with pytest.raises(FrozenInstanceError):
            p.label = "Hijacked"  # type: ignore[misc]

    def test_providers_tuple_not_list(self) -> None:
        # Tuple, not list — accidental .append() would silently mutate
        # the source of truth.
        assert isinstance(PROVIDERS, tuple)


# ---------------------------------------------------------------------
# dataclass smoke
# ---------------------------------------------------------------------


def test_provider_dataclass_round_trip() -> None:
    p = Provider(
        id="test",
        label="Test",
        description="",
        provider_kind="openai",
        api_base="http://localhost",
        suggested_model="x",
    )
    assert p.requires_api_key is True  # default
    assert p.auth_method == "api_key"
    assert p.max_tokens_default == 32000
    assert p.strip_openai_prefix is True
    assert p.is_kind_default is False
    assert p.model_catalog == ()
