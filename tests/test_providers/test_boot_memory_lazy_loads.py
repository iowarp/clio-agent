"""Boot-time memory: work nothing at startup needs is deferred to first use (D16).

Measured on a booted server (no LM calls), develop vs the 2026-09-03 gate commit:
two owned startup costs that no startup consumer uses --

- the Codex SDK (``openai_codex``, ~35 MB private in isolation) imported by the
  startup SDK probe, which runs for every user because the Codex preset always exists;
- the LiteLLM community cost map (~10 MB in isolation) parsed and memoised by the
  startup catalog refresh, whose job is only to make the disk cache fresh.

Both are now deferred without changing what the consumer eventually sees.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from clio_agent.providers.fetched_catalog import FetchedCatalog
from clio_agent.providers.model_discovery.overlay import ProviderDiscoveryResult

# --------------------------------------------------------------------------- #
# LiteLLM cost map: the startup refresh does not keep the parsed map resident
# --------------------------------------------------------------------------- #


def _catalog(tmp_path: Path) -> FetchedCatalog[dict[str, Any]]:
    return FetchedCatalog(
        "widgets",
        "https://example/catalog.json",
        parse=lambda payload: json.loads(payload.decode("utf-8")),
        ttl_s=3600.0,
        timeout_s=1.0,
        cache_path=tmp_path / "widgets.json",
    )


def _response(payload: str) -> httpx.Response:
    return httpx.Response(
        200, text=payload, request=httpx.Request("GET", "https://example/catalog.json")
    )


def test_refresh_disk_cache_writes_the_cache_but_memoises_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    catalog = _catalog(tmp_path)
    monkeypatch.setattr(
        "clio_agent.providers.fetched_catalog.httpx.get", lambda *_a, **_kw: _response('{"a": 1}')
    )
    result = catalog.refresh_disk_cache()
    assert result.source == "network"
    assert (tmp_path / "widgets.json").is_file()
    assert catalog._fresh is None  # noqa: SLF001 - the point: nothing resident

    def _no_network(*_a: object, **_kw: object) -> httpx.Response:
        raise AssertionError("the first lookup must read the fresh disk cache")

    monkeypatch.setattr("clio_agent.providers.fetched_catalog.httpx.get", _no_network)
    first = catalog.get()
    assert (first.source, first.data, first.stale_reason) == ("disk_cache", {"a": 1}, "")
    assert catalog._fresh is not None  # noqa: SLF001 - memoised on first use


def test_refresh_disk_cache_keeps_a_memo_a_lookup_already_made(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    catalog = _catalog(tmp_path)
    monkeypatch.setattr(
        "clio_agent.providers.fetched_catalog.httpx.get", lambda *_a, **_kw: _response('{"a": 1}')
    )
    catalog.get()
    catalog.refresh_disk_cache()
    assert catalog._fresh is not None  # noqa: SLF001 - never evicts a real consumer's memo


def test_the_startup_refresh_uses_the_disk_only_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    from clio_agent.providers.handshake.sources import db as model_limits_db
    from clio_agent.providers.handshake.sources import litellm_catalog
    from clio_agent.providers.model_discovery import model_overlay_catalog, refresh

    calls: list[str] = []

    class _Catalog:
        def refresh_disk_cache(self) -> None:
            calls.append("refresh_disk_cache")

        def get(self, **_kw: object) -> None:
            calls.append("get")

    monkeypatch.setattr(litellm_catalog, "_catalog", lambda: _Catalog())
    monkeypatch.setattr(model_limits_db, "refresh_model_limits_seed", lambda: "")
    monkeypatch.setattr(model_overlay_catalog, "load_model_overlay_catalog", lambda: None)
    assert refresh.refresh_online_catalogs() == {}
    assert calls == ["refresh_disk_cache"]


# --------------------------------------------------------------------------- #
# Codex SDK: the startup probe runs in a child unless the SDK is already loaded
# --------------------------------------------------------------------------- #


def test_the_probe_runs_in_a_child_when_the_sdk_is_not_loaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from clio_agent.providers.codex import sdk_discovery

    monkeypatch.setattr(sdk_discovery, "AsyncCodex", None)
    monkeypatch.delitem(sys.modules, "openai_codex", raising=False)
    expected = ProviderDiscoveryResult(provider="codex_sdk", discovered=[], source="x")
    seen: list[float] = []

    def _child(timeout: float) -> ProviderDiscoveryResult:
        seen.append(timeout)
        return expected

    async def _in_process(_timeout: float) -> ProviderDiscoveryResult:
        raise AssertionError("must not import the SDK into this process")

    monkeypatch.setattr(sdk_discovery, "_probe_in_child", _child)
    monkeypatch.setattr(sdk_discovery, "_probe", _in_process)
    assert asyncio.run(sdk_discovery.discover_codex_sdk_async(timeout=7.0)) is expected
    assert seen == [7.0]


def test_the_probe_stays_in_process_once_the_sdk_is_loaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from clio_agent.providers.codex import sdk_discovery

    monkeypatch.setattr(sdk_discovery, "AsyncCodex", None)
    monkeypatch.setitem(sys.modules, "openai_codex", sys.modules.get("openai_codex") or object())
    expected = ProviderDiscoveryResult(provider="codex_sdk", discovered=[], source="y")

    async def _in_process(_timeout: float) -> ProviderDiscoveryResult:
        return expected

    def _child(_timeout: float) -> ProviderDiscoveryResult:
        raise AssertionError("an already-loaded SDK needs no child")

    monkeypatch.setattr(sdk_discovery, "_probe", _in_process)
    monkeypatch.setattr(sdk_discovery, "_probe_in_child", _child)
    assert asyncio.run(sdk_discovery.discover_codex_sdk_async()) is expected


def _completed(stdout: str, stderr: str = "", code: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=code, stdout=stdout, stderr=stderr)


def test_the_child_result_round_trips(monkeypatch: pytest.MonkeyPatch) -> None:
    from clio_agent.providers.codex import sdk_discovery

    original = ProviderDiscoveryResult(
        provider="codex_sdk",
        discovered=[{"id": "gpt-x", "capabilities": ["image"]}],
        source="codex_sdk_model_list",
        default_model="gpt-x",
    )
    line = sdk_discovery._CHILD_RESULT_PREFIX + json.dumps(dataclasses.asdict(original))
    monkeypatch.setattr(
        sdk_discovery.subprocess, "run", lambda *_a, **_kw: _completed(f"noise\n{line}\n")
    )
    assert sdk_discovery._probe_in_child(5.0) == original


@pytest.mark.parametrize(
    ("outcome", "detail"),
    [
        (_completed("", "Traceback: boom", code=1), "exited 1 without a result"),
        (_completed("CLIO_CODEX_SDK_PROBE_RESULT {bad"), "unreadable"),
    ],
)
def test_a_child_without_a_result_is_a_typed_probe_failure(
    monkeypatch: pytest.MonkeyPatch, outcome: subprocess.CompletedProcess[str], detail: str
) -> None:
    from clio_agent.providers.codex import sdk_discovery

    monkeypatch.setattr(sdk_discovery.subprocess, "run", lambda *_a, **_kw: outcome)
    result = sdk_discovery._probe_in_child(5.0)
    assert result.discovered == []
    assert result.failed_reason is not None
    assert result.failed_reason.startswith("codex_sdk_probe_failed")
    assert detail in result.failed_reason


def test_a_child_that_times_out_is_a_typed_probe_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from clio_agent.providers.codex import sdk_discovery

    def _timeout(*_a: object, **_kw: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd="python", timeout=1.0)

    monkeypatch.setattr(sdk_discovery.subprocess, "run", _timeout)
    result = sdk_discovery._probe_in_child(1.0)
    assert result.failed_reason is not None
    assert result.failed_reason.startswith("codex_sdk_probe_failed")
