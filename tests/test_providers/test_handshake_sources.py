"""Offline tests for the context-source factory.

Every test loads the captured ``models_dev_subset.json`` fixture through the
models.dev path seam (``_load_models_dev(path=...)``) and monkeypatches the
factory's models.dev lookup so :func:`resolve_context` never touches the network.
The marketplace DB and static table are exercised purely through their built-in
data / explicit path seams. No test makes a network call.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clio_agent.providers.handshake.sources import (
    SOURCE_MARKETPLACE,
    SOURCE_MODELS_DEV,
    SOURCE_STATIC,
    resolve_context,
)
from clio_agent.providers.handshake.sources import models_dev as models_dev_mod
from clio_agent.providers.handshake.sources.marketplace import (
    load_marketplace,
    lookup_marketplace,
    resolve_db_path,
)
from clio_agent.providers.handshake.sources.models_dev import (
    _load_models_dev,
    lookup_models_dev,
)
from clio_agent.providers.handshake.sources.static import lookup_static

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "handshake"
MODELS_DEV_FIXTURE = FIXTURE_DIR / "models_dev_subset.json"


@pytest.fixture(autouse=True)
def offline_models_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force every models.dev lookup to read the captured fixture, never the net.

    Patches the module-level loader so both ``lookup_models_dev`` and the factory
    (which calls it with no path) resolve against ``models_dev_subset.json``.
    """
    catalog = json.loads(MODELS_DEV_FIXTURE.read_text(encoding="utf-8"))

    def _fixture_loader(path=None, *, ttl_s=models_dev_mod.DEFAULT_TTL_S, allow_fetch=True):  # type: ignore[no-untyped-def]
        if path is not None:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        return catalog

    monkeypatch.setattr(models_dev_mod, "_load_models_dev", _fixture_loader)


@pytest.fixture(autouse=True)
def isolated_marketplace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point the marketplace DB at a non-existent file so only the seed is active.

    Prevents a developer's real ``~/.config/clio-agent/context_db.json`` from
    leaking into assertions about provenance/fall-through.
    """
    monkeypatch.setenv("CLIO_CONTEXT_DB", str(tmp_path / "no_such_context_db.json"))


# --------------------------------------------------------------------------- #
# models.dev source (path seam)                                               #
# --------------------------------------------------------------------------- #
def test_load_models_dev_via_path_seam() -> None:
    """The path seam loads a catalog straight off disk with no network."""
    catalog = _load_models_dev(MODELS_DEV_FIXTURE)
    assert "google/gemma-4-31b-it" in catalog
    assert catalog["google/gemma-4-31b-it"]["limit"]["context"] == 262144


def test_lookup_models_dev_exact_key_via_path() -> None:
    """A full ``vendor/id`` key resolves to its ``limit.context``."""
    assert lookup_models_dev("google/gemma-4-31b-it", path=MODELS_DEV_FIXTURE) == 262144


def test_lookup_models_dev_basename_match() -> None:
    """A provider id lacking the vendor prefix still matches on the basename."""
    assert lookup_models_dev("gemma-4-31b-it", path=MODELS_DEV_FIXTURE) == 262144


def test_lookup_models_dev_case_insensitive() -> None:
    """Matching is case-insensitive."""
    assert lookup_models_dev("Google/Gemma-4-31B-IT", path=MODELS_DEV_FIXTURE) == 262144


def test_lookup_models_dev_miss_returns_none() -> None:
    """An unknown id is a clean miss."""
    assert lookup_models_dev("acme/qwopus-99x", path=MODELS_DEV_FIXTURE) is None


def test_models_dev_offline_path_never_fetches(monkeypatch: pytest.MonkeyPatch) -> None:
    """Using the path seam must not invoke the HTTP fetch helper."""

    def _boom() -> str | None:
        raise AssertionError("network fetch attempted in offline test")

    monkeypatch.setattr(models_dev_mod, "_fetch_catalog", _boom)
    assert _load_models_dev(MODELS_DEV_FIXTURE)  # loads fixture, no fetch
    assert lookup_models_dev("openai/gpt-4o", path=MODELS_DEV_FIXTURE) == 128000


# --------------------------------------------------------------------------- #
# factory: strict ordering + provenance                                       #
# --------------------------------------------------------------------------- #
def test_resolve_finds_gemma_via_models_dev() -> None:
    """The headline case: gemma resolves to 262144 with provenance ``models.dev``."""
    window, source = resolve_context("google/gemma-4-31b-it", "openai_compat")
    assert window == 262144
    assert source == SOURCE_MODELS_DEV
    assert source == "models.dev"


def test_resolve_qwopus_like_falls_through_to_miss() -> None:
    """A qwopus-like id absent from every source returns ``(None, "")``."""
    window, source = resolve_context("acme/qwopus-99x-turbo", "openai_compat")
    assert window is None
    assert source == ""


def test_resolve_models_dev_takes_priority_over_marketplace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When a model is in BOTH models.dev and the marketplace, models.dev wins.

    Proves strict ordering: a curated DB entry deliberately disagrees with
    models.dev for gemma (it sets a wrong window), yet the factory must return
    the models.dev value with provenance ``models.dev``.
    """
    db = tmp_path / "context_db.json"
    db.write_text(json.dumps({"gemma-4-31b-it": 4096}), encoding="utf-8")
    monkeypatch.setenv("CLIO_CONTEXT_DB", str(db))
    # Precondition: the marketplace now answers a (wrong) value for gemma.
    assert lookup_marketplace("gemma-4-31b-it") == 4096
    # But models.dev is consulted first and wins.
    window, source = resolve_context("google/gemma-4-31b-it", "openai_compat")
    assert window == 262144
    assert source == SOURCE_MODELS_DEV


def test_resolve_falls_through_to_marketplace() -> None:
    """An id missing from models.dev but present in the marketplace seed resolves there.

    ``granite-4-h-tiny`` is a real LM Studio checkpoint absent from the models.dev
    subset but carried in the built-in marketplace seed.
    """
    assert lookup_models_dev("granite-4-h-tiny", path=MODELS_DEV_FIXTURE) is None
    assert "granite-4-h-tiny" in load_marketplace()
    window, source = resolve_context("ibm/granite-4-h-tiny", "openai_compat")
    assert window == 1048576
    assert source == SOURCE_MARKETPLACE


def test_resolve_falls_through_to_static() -> None:
    """An id only the static table knows resolves with provenance ``static``."""
    window, source = resolve_context("mistral-7b", "openai_compat")
    assert window == 32768
    assert source == SOURCE_STATIC


def test_resolve_empty_id_is_miss() -> None:
    """A blank id is a clean miss, never a crash."""
    assert resolve_context("", "openai_compat") == (None, "")
    assert resolve_context("   ", "openai_compat") == (None, "")


# --------------------------------------------------------------------------- #
# marketplace source                                                          #
# --------------------------------------------------------------------------- #
def test_marketplace_seed_lookup() -> None:
    """The built-in seed is consulted when no DB file is present."""
    assert lookup_marketplace("granite-4-h-tiny") == 1048576


def test_marketplace_db_overrides_seed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An on-disk DB entry overrides the built-in seed for the same id."""
    db = tmp_path / "context_db.json"
    db.write_text(json.dumps({"granite-4-h-tiny": 99999}), encoding="utf-8")
    monkeypatch.setenv("CLIO_CONTEXT_DB", str(db))
    assert lookup_marketplace("granite-4-h-tiny") == 99999


def test_marketplace_db_models_wrapper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A DB file may wrap the mapping under a top-level ``models`` key."""
    db = tmp_path / "context_db.json"
    db.write_text(
        json.dumps({"version": 1, "models": {"my-local-model": 16384}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("CLIO_CONTEXT_DB", str(db))
    assert lookup_marketplace("my-local-model") == 16384


def test_marketplace_malformed_db_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A corrupt DB file degrades to the seed rather than raising."""
    db = tmp_path / "context_db.json"
    db.write_text("{ this is not json", encoding="utf-8")
    monkeypatch.setenv("CLIO_CONTEXT_DB", str(db))
    # Seed still works; the bad file is silently skipped.
    assert lookup_marketplace("granite-4-h-tiny") == 1048576


def test_marketplace_db_path_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit path arg beats env, which beats the config-dir default."""
    explicit = tmp_path / "explicit.json"
    monkeypatch.setenv("CLIO_CONTEXT_DB", str(tmp_path / "env.json"))
    assert resolve_db_path(explicit) == explicit
    assert resolve_db_path() == tmp_path / "env.json"


# --------------------------------------------------------------------------- #
# static source                                                               #
# --------------------------------------------------------------------------- #
def test_static_lookup_basename() -> None:
    """The static table matches on a vendor-stripped basename."""
    assert lookup_static("openai/gpt-4o") == 128000
    assert lookup_static("gpt-4o") == 128000


def test_static_miss_returns_none() -> None:
    """An id unknown to the static table is a clean miss."""
    assert lookup_static("acme/qwopus-99x") is None
