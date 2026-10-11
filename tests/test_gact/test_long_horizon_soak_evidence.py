"""Independent qualification evidence rejects gaps, rewrites and false manifests."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest


@pytest.fixture
def soak(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Load the standalone verifier without starting any runtime or model."""
    scripts = Path(__file__).resolve().parents[2] / "scripts/live_verification"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location("soak_evidence", scripts / "long_horizon_soak.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def checkpoint(root: Path, number: int, marker: str) -> Path:
    """Write a test fixture with the exact required checkpoint fields."""
    path = root / f"batch-{number:03d}.json"
    path.write_text(
        json.dumps({"batch": number, "next": number + 1, "previous": number - 1, "marker": marker}),
        encoding="utf-8",
    )
    return path


def test_verifier_retains_hash_and_rejects_rewrite(soak: ModuleType, tmp_path: Path) -> None:
    path = checkpoint(tmp_path, 0, soak.MARKER)
    hashes = soak.verify_reports(tmp_path, {})
    assert hashes == soak.verify_reports(tmp_path, hashes)
    path.write_text(path.read_text() + "\n", encoding="utf-8")
    with pytest.raises(AssertionError, match="overwritten"):
        soak.verify_reports(tmp_path, hashes)


def test_verifier_rejects_gap(soak: ModuleType, tmp_path: Path) -> None:
    checkpoint(tmp_path, 1, soak.MARKER)
    with pytest.raises(AssertionError, match="gap"):
        soak.verify_reports(tmp_path, {})


def test_verifier_rejects_wrong_marker(soak: ModuleType, tmp_path: Path) -> None:
    checkpoint(tmp_path, 0, "forgot-original-instruction")
    with pytest.raises(AssertionError, match="contents"):
        soak.verify_reports(tmp_path, {})


def test_verifier_rejects_removed_file(soak: ModuleType, tmp_path: Path) -> None:
    with pytest.raises(AssertionError, match="removed"):
        soak.verify_reports(tmp_path, {"batch-000.json": "old-digest"})


def test_manifest_requires_every_checkpoint_in_order(soak: ModuleType, tmp_path: Path) -> None:
    path = tmp_path / "final-manifest.json"
    content = {"marker": soak.MARKER, "files": [f"batch-{n:03d}.json" for n in range(soak.COUNT)]}
    path.write_text(json.dumps(content), encoding="utf-8")
    soak.verify_manifest(tmp_path)
    content["files"] = list(reversed(content["files"]))
    path.write_text(json.dumps(content), encoding="utf-8")
    with pytest.raises(AssertionError, match="manifest"):
        soak.verify_manifest(tmp_path)
