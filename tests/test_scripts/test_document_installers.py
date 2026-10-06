"""The package step belongs to installation, never the release payload builder."""

from pathlib import Path


def test_installers_prepare_packages_without_bundling_them() -> None:
    root = Path(__file__).resolve().parents[2]
    for name in ("install.ps1", "install.sh"):
        assert "clio_agent.runtime.document_install" in (root / "install" / name).read_text()
    for name in ("build-gact-runtime.ps1", "build-gact-runtime.sh"):
        text = (root / "install" / name).read_text()
        assert "clio_agent.runtime.document_install" not in text
        assert "clio_agent.runtime.document_bundle" not in text
