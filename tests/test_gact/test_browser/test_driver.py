"""Playwright's Node.js is fetched on first browser use when the bundle leaves it out."""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import tarfile
from pathlib import Path
from typing import Any

import pytest

from clio_agent.gact.browser import driver
from clio_agent.gact.browser.settings import BrowserError

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the fake node is a shell script")

_NODE = b"#!/bin/sh\necho v9.9.9\n"


@pytest.fixture
def bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A stripped driver folder whose manifest points at a local release archive."""
    monkeypatch.setenv(driver.ENV, "unset-by-the-fixture")
    monkeypatch.delenv(driver.ENV)  # restored afterwards, even though ensure_node sets it
    folder = tmp_path / "playwright" / "driver"
    folder.mkdir(parents=True)
    member = "node-v9.9.9-test/bin/node"
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w:xz") as archive:
        info = tarfile.TarInfo(member)
        info.size = len(_NODE)
        info.mode = 0o755
        archive.addfile(info, io.BytesIO(_NODE))
    archive_path = tmp_path / "node-v9.9.9-test.tar.xz"
    archive_path.write_bytes(payload.getvalue())
    record = {
        "schema": 1,
        "node_version": "9.9.9",
        "url": archive_path.as_uri(),
        "sha256": hashlib.sha256(payload.getvalue()).hexdigest(),
        "size": len(payload.getvalue()),
        "member": member,
        "node_sha256": hashlib.sha256(_NODE).hexdigest(),
        "node_size": len(_NODE),
        "executable": "node",
    }
    (folder / driver.MANIFEST).write_text(json.dumps(record), encoding="utf-8")
    cache = tmp_path / "cache"
    monkeypatch.setattr(driver, "driver_dir", lambda: folder)
    monkeypatch.setattr(driver.paths, "user_cache_dir", lambda: cache)
    monkeypatch.setattr(driver, "_RETRY_PAUSE_S", (0.0, 0.0))
    return {"folder": folder, "record": record, "cache": cache}


def _write_manifest(bundle: dict[str, Any], **changes: Any) -> None:
    record = {**bundle["record"], **changes}
    (bundle["folder"] / driver.MANIFEST).write_text(json.dumps(record), encoding="utf-8")


def _leftovers(cache: Path) -> list[str]:
    browser = cache / "browser"
    return sorted(p.name for p in browser.iterdir() if p.name.endswith(".part"))


def test_the_first_use_fetches_checks_and_points_playwright_at_it(
    bundle: dict[str, Any],
) -> None:
    assert driver.node_path() is None
    node = driver.ensure_node()
    assert node is not None and node.is_file()
    assert (node.parent / "INSTALLATION_COMPLETE").is_file()
    assert os.environ[driver.ENV] == str(node)
    assert _leftovers(bundle["cache"]) == []
    assert driver.fetch_state() == {}


def test_a_bad_download_is_retried_then_reported_and_leaves_nothing_installed(
    bundle: dict[str, Any],
) -> None:
    _write_manifest(bundle, sha256="0" * 64)
    with pytest.raises(BrowserError) as failed:
        driver.ensure_node()
    assert failed.value.reason == driver.REASON_INSTALL_FAILED
    assert "after 3 attempts" in failed.value.message
    assert driver.node_path() is None
    assert driver.ENV not in os.environ
    assert _leftovers(bundle["cache"]) == []

    _write_manifest(bundle)  # the next use tries again
    assert driver.ensure_node() is not None


def test_a_binary_that_is_not_the_wheels_is_refused(bundle: dict[str, Any]) -> None:
    _write_manifest(bundle, node_sha256="1" * 64)
    with pytest.raises(BrowserError) as failed:
        driver.ensure_node()
    assert "not the binary this Playwright release ships" in failed.value.message


def test_a_bundled_node_is_used_without_fetching(bundle: dict[str, Any]) -> None:
    bundled = bundle["folder"] / "node"
    bundled.write_bytes(_NODE)
    assert driver.ensure_node() == bundled
    assert not (bundle["cache"] / "browser").exists()


def test_a_slow_fetch_answers_preparing_with_progress(
    bundle: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    release = threading.Event()
    real = driver._attempt

    def slow(record: dict[str, Any]) -> Path:
        release.wait(10)
        return real(record)

    monkeypatch.setattr(driver, "_attempt", slow)
    try:
        with pytest.raises(BrowserError) as first:
            driver.driver_for_action(0.05)
        assert first.value.reason == driver.REASON_PREPARING
        assert "first use only" in first.value.message
        assert first.value.detail["progress"]["step"] == "browser_driver"
    finally:
        release.set()
    driver.driver_for_action(5)
    assert driver.node_path() is not None
