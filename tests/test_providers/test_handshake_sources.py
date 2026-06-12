"""Offline tests for the context-source factory + the local model-limits DB.

The cascade is provider-live -> models.dev -> local DB. models.dev is forced to a
captured fixture (no network); the DB is pointed at a tmp copy of the repo seed so
lookups work and write-backs land in tmp, never the repo. No test hits the network.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from clio_agent.providers.handshake.sources import (
    SOURCE_DB,
    SOURCE_MODELS_DEV,
    resolve_context,
    resolve_output_limit,
)
from clio_agent.providers.handshake.sources import (
    db as db_mod,
)
from clio_agent.providers.handshake.sources import models_dev as models_dev_mod
from clio_agent.providers.handshake.sources.models_dev import lookup_models_dev

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "handshake"
MODELS_DEV_FIXTURE = FIXTURE_DIR / "models_dev_subset.json"
SEED_DB = Path(db_mod.__file__).resolve().parent / "data" / "model_limits.json"


@pytest.fixture(autouse=True)
def offline_models_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force every models.dev lookup to read the captured fixture, never the net."""
    catalog = json.loads(MODELS_DEV_FIXTURE.read_text(encoding="utf-8"))

    def _loader(path=None, *, ttl_s=models_dev_mod.DEFAULT_TTL_S, allow_fetch=True):  # type: ignore[no-untyped-def]
        if path is not None:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        return catalog

    monkeypatch.setattr(models_dev_mod, "_load_models_dev", _loader)


@pytest.fixture(autouse=True)
def isolated_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the DB at a tmp copy of the repo seed (lookups work; writes stay in tmp)."""
    db_file = tmp_path / "model_limits.json"
    shutil.copyfile(SEED_DB, db_file)
    monkeypatch.setenv("CLIO_MODEL_DB", str(db_file))
    return db_file


# ---- models.dev source (path seam) ----
def test_lookup_models_dev_exact_and_basename() -> None:
    assert lookup_models_dev("google/gemma-4-31b-it", path=MODELS_DEV_FIXTURE) == 262144
    assert lookup_models_dev("gemma-4-31b-it", path=MODELS_DEV_FIXTURE) == 262144


def test_models_dev_offline_path_never_fetches(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom() -> str | None:
        raise AssertionError("network fetch attempted in offline test")

    monkeypatch.setattr(models_dev_mod, "_fetch_catalog", _boom)
    assert lookup_models_dev("openai/gpt-4o", path=MODELS_DEV_FIXTURE) == 128000


# ---- factory cascade: models.dev -> db ----
def test_resolve_finds_gemma_via_models_dev() -> None:
    window, source = resolve_context("google/gemma-4-31b-it", "openai_compat")
    assert window == 262144
    assert source == SOURCE_MODELS_DEV == "models.dev"


def test_resolve_falls_through_to_db_seed() -> None:
    # granite + mistral are in the DB seed, not the models.dev subset
    assert lookup_models_dev("granite-4-h-tiny", path=MODELS_DEV_FIXTURE) is None
    window, source = resolve_context("ibm/granite-4-h-tiny", "openai_compat")
    assert window == 1048576
    assert source == SOURCE_DB == "db"
    window, source = resolve_context("mistral-7b", "openai_compat")
    assert window == 32768
    assert source == "db"


def test_resolve_models_dev_takes_priority_over_db(isolated_db: Path) -> None:
    data = json.loads(isolated_db.read_text(encoding="utf-8"))
    # Use the fully-qualified key: the seed already carries
    # ``google/gemma-4-31b-it`` (262144), whose basename ``gemma-4-31b-it`` is
    # registered in the lookup index — so a bare key would be shadowed. Overwrite
    # the qualified entry to plant the deliberately-wrong DB value.
    data["google/gemma-4-31b-it"] = {"context": 4096}  # deliberately wrong DB value
    isolated_db.write_text(json.dumps(data), encoding="utf-8")
    assert db_mod.lookup_context("google/gemma-4-31b-it") == 4096
    window, source = resolve_context("google/gemma-4-31b-it", "openai_compat")
    assert window == 262144  # models.dev wins
    assert source == SOURCE_MODELS_DEV


def test_resolve_miss_and_empty() -> None:
    assert resolve_context("acme/qwopus-99x-turbo", "openai_compat") == (None, "")
    assert resolve_context("", "openai_compat") == (None, "")


def test_resolve_output_limit_from_models_dev() -> None:
    assert resolve_output_limit("google/gemma-4-31b-it", "openai_compat") == 32768


# ---- the local DB: read / write-back / mismatch / record_report ----
def test_db_lookup_basename() -> None:
    assert db_mod.lookup_context("openai/gpt-4o") == 128000
    assert db_mod.lookup_context("gpt-4o") == 128000


def test_db_record_writes_back() -> None:
    assert db_mod.lookup_context("acme/new-model-x") is None
    db_mod.record(
        "acme/new-model-x", context=50000, output=8000, source="live", provider="lm_studio"
    )
    assert db_mod.lookup_context("acme/new-model-x") == 50000
    assert db_mod.lookup_output("acme/new-model-x") == 8000


def test_db_record_logs_mismatch(isolated_db: Path) -> None:
    db_mod.record("acme/m", context=1000)
    db_mod.record("acme/m", context=2000)  # disagreement -> mismatch
    assert db_mod.lookup_context("acme/m") == 2000  # latest wins in the store
    mfile = isolated_db.with_suffix(".mismatches.jsonl")
    assert mfile.exists()
    rows = [
        json.loads(line) for line in mfile.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    assert any(r["stored"] == 1000 and r["discovered"] == 2000 for r in rows)


def test_db_record_report_records_only_live() -> None:
    from clio_agent.providers.handshake.model import (
        AuthState,
        ConnectivityState,
        HandshakeReport,
        ModelProfile,
    )

    rep = HandshakeReport(
        provider_id="p",
        provider_kind="argonne",
        connectivity=ConnectivityState.OK,
        auth=AuthState.OK,
        models=(
            ModelProfile(id="vendor/live-model", context_window=99999, context_source="live"),
            ModelProfile(id="vendor/dev-model", context_window=11111, context_source="models.dev"),
        ),
    )
    db_mod.record_report(rep)
    assert db_mod.lookup_context("vendor/live-model") == 99999  # live recorded
    assert db_mod.lookup_context("vendor/dev-model") is None  # non-live NOT recorded
