"""A sealed desktop runtime installs into a package overlay, never into itself."""

from __future__ import annotations

import importlib.metadata
import os
import subprocess
import sys
from pathlib import Path

import pytest

from clio_agent.runtime import package_overlay


def _runtime(root: Path, *, manifest: str = '{"schema": 1}') -> Path:
    """A fake bundled runtime: ``runtime.json`` above a python executable."""
    root.mkdir(parents=True)
    (root / "runtime.json").write_text(manifest, encoding="utf-8")
    python = root / "python" / "bin" / "python3.14"
    python.parent.mkdir(parents=True)
    python.touch()
    return python


@pytest.fixture(autouse=True)
def _no_switch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv(package_overlay.OVERLAY_ENV, raising=False)
    monkeypatch.setenv("CLIO_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(sys, "path", list(sys.path))


def test_a_runtime_inside_a_macos_app_bundle_uses_the_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    python = _runtime(tmp_path / "CLIO Desktop.app" / "Contents" / "Resources" / "gact-runtime")
    monkeypatch.setattr(sys, "executable", str(python))
    platform = sys.platform
    monkeypatch.setattr(sys, "platform", "darwin")
    assert package_overlay.overlay_in_use() is True
    # The data dir must resolve on the host: a faked darwin makes Windows paths relative.
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(package_overlay, "overlay_in_use", lambda: True)
    overlay = package_overlay.overlay_for(str(python))
    assert overlay is not None
    assert overlay.is_relative_to(tmp_path / "data")
    assert not overlay.is_relative_to(tmp_path / "CLIO Desktop.app")


def test_a_writable_runtime_outside_a_bundle_installs_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    python = _runtime(tmp_path / "gact-runtime")
    monkeypatch.setattr(sys, "executable", str(python))
    monkeypatch.setattr(package_overlay, "_site_packages", lambda: tmp_path)
    assert package_overlay.overlay_in_use() is False
    assert package_overlay.overlay_for(str(python)) is None


def test_a_read_only_runtime_uses_the_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    python = _runtime(tmp_path / "usr" / "lib" / "gact-runtime")
    monkeypatch.setattr(sys, "executable", str(python))
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(package_overlay, "_site_packages", lambda: tmp_path / "ro")
    monkeypatch.setattr(os, "access", lambda _path, _mode: False)
    assert package_overlay.overlay_in_use() is True


def test_a_writable_runtime_with_zipped_packages_uses_the_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An installer cannot see the zipped distributions, so it must not run in place."""
    python = _runtime(tmp_path / "CLIO Desktop" / "data" / "bundled-runtime" / "gact-runtime")
    site = tmp_path / "site-packages"
    site.mkdir()
    (site / package_overlay.ZIPPED_SITE_PACKAGES).write_bytes(b"PK\x05\x06" + b"\0" * 18)
    monkeypatch.setattr(sys, "executable", str(python))
    monkeypatch.setattr(package_overlay, "_site_packages", lambda: site)
    assert os.access(site, os.W_OK)
    assert package_overlay.overlay_in_use() is True
    overlay = package_overlay.overlay_for(str(python))
    assert overlay is not None
    assert overlay.is_relative_to(tmp_path / "data")


def test_a_dev_environment_is_never_redirected(monkeypatch: pytest.MonkeyPatch) -> None:
    """No runtime.json above this interpreter: installs stay in the venv."""
    monkeypatch.setattr(package_overlay, "_runtime_root", lambda executable=None: None)
    assert package_overlay.overlay_in_use() is False


def test_the_switch_forces_or_disables_the_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    python = _runtime(tmp_path / "X.app" / "Contents" / "gact-runtime")
    monkeypatch.setattr(sys, "executable", str(python))
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv(package_overlay.OVERLAY_ENV, "0")
    assert package_overlay.overlay_in_use() is False
    monkeypatch.setenv(package_overlay.OVERLAY_ENV, "1")
    monkeypatch.setattr(package_overlay, "_runtime_root", lambda executable=None: None)
    assert package_overlay.overlay_in_use() is True


def test_another_interpreter_is_never_redirected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(package_overlay.OVERLAY_ENV, "1")
    other = tmp_path / "service-venv-python"
    other.touch()
    assert package_overlay.overlay_for(str(other)) is None
    assert package_overlay.overlay_for(sys.executable) is not None


def test_an_updated_runtime_gets_a_fresh_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "CLIO Desktop.app" / "Contents" / "gact-runtime"
    python = _runtime(root, manifest='{"source": {"commit": "a"}}')
    monkeypatch.setattr(sys, "executable", str(python))
    first = package_overlay.overlay_dir()
    (root / "runtime.json").write_text('{"source": {"commit": "b"}}', encoding="utf-8")
    second = package_overlay.overlay_dir()
    assert first != second and first.parent == second.parent
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    assert package_overlay.prune_stale(second) == [first]
    assert not first.exists() and second.is_dir()


def test_prepend_puts_the_overlay_first_and_runs_its_pth_files(tmp_path: Path) -> None:
    overlay = tmp_path / "overlay"
    extra = tmp_path / "extra"
    (overlay / "clio_overlay_probe").mkdir(parents=True)
    (overlay / "clio_overlay_probe" / "__init__.py").write_text("VALUE = 7\n", encoding="utf-8")
    extra.mkdir()
    (overlay / "probe.pth").write_text(str(extra) + "\n", encoding="utf-8")

    package_overlay.prepend(overlay)
    package_overlay.prepend(overlay)  # idempotent

    assert sys.path[0] == str(overlay)
    assert sys.path.count(str(overlay)) == 1
    assert str(extra) in sys.path
    import clio_overlay_probe  # noqa: PLC0415

    assert clio_overlay_probe.VALUE == 7
    sys.modules.pop("clio_overlay_probe", None)


def test_the_child_prelude_activates_the_overlay(tmp_path: Path) -> None:
    overlay = tmp_path / "overlay"
    (overlay / "clio_overlay_probe").mkdir(parents=True)
    (overlay / "clio_overlay_probe" / "__main__.py").write_text(
        "import sys; print('ran', sys.argv[1:])\n", encoding="utf-8"
    )
    (overlay / "clio_overlay_probe" / "__init__.py").touch()
    command = package_overlay.module_command(
        sys.executable, overlay, "clio_overlay_probe", "download-runtime"
    )
    result = subprocess.run(command, capture_output=True, text=True, check=True)  # noqa: S603
    assert result.stdout.strip() == "ran ['download-runtime']"


def _fake_dist(directory: Path, name: str, version: str, module: str) -> None:
    info = directory / f"{name.replace('-', '_')}-{version}.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n", encoding="utf-8"
    )
    package = directory / module
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "__pycache__").mkdir()
    (package / "__pycache__" / "__init__.cpython-314.pyc").write_bytes(b"x")
    (info / "RECORD").write_text(
        f"{module}/__init__.py,,\n{info.name}/METADATA,,\n{info.name}/RECORD,,\n",
        encoding="utf-8",
    )


def test_duplicates_of_the_runtime_are_pruned_and_new_packages_stay(tmp_path: Path) -> None:
    overlay = tmp_path / "overlay"
    pytest_version = importlib.metadata.version("pytest")
    _fake_dist(overlay, "pytest", pytest_version, "pytest_copy")
    _fake_dist(overlay, "clio-overlay-new", "1.0", "clio_overlay_new")

    assert package_overlay.prune_duplicates(overlay) == ["pytest"]

    assert not (overlay / "pytest_copy").exists()
    assert not list(overlay.glob("pytest-*.dist-info"))
    assert (overlay / "clio_overlay_new" / "__init__.py").is_file()
    assert [d.metadata["Name"] for d in importlib.metadata.distributions(path=[str(overlay)])] == [
        "clio-overlay-new"
    ]


def test_publish_moves_a_finished_install_into_the_overlay(tmp_path: Path) -> None:
    overlay = tmp_path / "runtime-packages" / "key"
    _fake_dist(overlay, "clio-overlay-old", "1.0", "clio_overlay_old")
    _fake_dist(overlay, "clio-overlay-kept", "1.0", "clio_overlay_kept")
    staging = package_overlay.staging_dir(overlay)
    assert staging.parent == overlay.parent and not any(staging.iterdir())
    _fake_dist(staging, "pytest", importlib.metadata.version("pytest"), "pytest_copy")
    _fake_dist(staging, "clio-overlay-old", "2.0", "clio_overlay_old")
    _fake_dist(staging, "clio-overlay-new", "1.0", "clio_overlay_new")

    assert package_overlay.publish(staging, overlay) == ["clio-overlay-new", "clio-overlay-old"]

    assert not staging.exists()
    assert not (overlay / "pytest_copy").exists()  # a runtime duplicate never lands
    versions = {
        d.metadata["Name"]: d.version for d in importlib.metadata.distributions(path=[str(overlay)])
    }
    assert versions == {
        "clio-overlay-old": "2.0",
        "clio-overlay-kept": "1.0",
        "clio-overlay-new": "1.0",
    }


def test_publishing_the_runtimes_version_drops_the_overlay_copy(tmp_path: Path) -> None:
    """A rollback to the runtime's version leaves the runtime's copy showing through."""
    overlay = tmp_path / "runtime-packages" / "key"
    _fake_dist(overlay, "pytest", "0.0.1", "pytest_copy")
    staging = package_overlay.staging_dir(overlay)
    _fake_dist(staging, "pytest", importlib.metadata.version("pytest"), "pytest_copy")

    assert package_overlay.publish(staging, overlay) == []

    assert not (overlay / "pytest_copy").exists()
    assert not list(overlay.glob("pytest-*.dist-info"))


def test_prune_stale_spares_an_install_still_staging(tmp_path: Path) -> None:
    import os  # noqa: PLC0415

    overlay = tmp_path / "runtime-packages" / "key"
    overlay.mkdir(parents=True)
    active = package_overlay.staging_dir(overlay)
    abandoned = package_overlay.staging_dir(overlay)
    old = abandoned.stat().st_mtime - 7 * 3600
    os.utime(abandoned, (old, old))

    assert package_overlay.prune_stale(overlay) == [abandoned]
    assert active.is_dir() and not abandoned.exists()


def test_runtime_constraints_pin_what_the_runtime_ships(tmp_path: Path) -> None:
    pins = package_overlay.runtime_constraints(tmp_path / "overlay")
    assert f"pytest=={importlib.metadata.version('pytest')}" in pins


def test_an_unusable_overlay_is_logged_and_never_stops_the_import(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def _no_home() -> Path:
        raise RuntimeError("no home for this service user")

    monkeypatch.setattr(package_overlay, "overlay_in_use", lambda: True)
    monkeypatch.setattr(package_overlay, "overlay_dir", _no_home)
    before = list(sys.path)
    with caplog.at_level("WARNING", logger=package_overlay.__name__):
        package_overlay.activate()
    assert sys.path == before
    assert "overlay_activation_failed" in caplog.text
