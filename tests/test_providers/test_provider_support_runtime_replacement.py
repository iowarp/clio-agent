"""Integration: provider support installed into a runtime survives that runtime's replacement.

A real ``uv`` environment stands in for the desktop's bundled runtime. A
stand-in provider support (``clio-fake-sdk``, a locally built wheel) is
installed into it through the provider installer seam, which records it in
the (isolated) user ``config.yaml``. The runtime directory is then deleted and
recreated empty -- exactly what a CLIO Desktop update does to
``bundled-runtime/gact-runtime`` -- and the startup restore must plan the
reinstall from the record and bring the package back with a real ``uv pip
install``. ``UV_OFFLINE=1`` keeps uv off the network.
"""

from __future__ import annotations

import base64
import hashlib
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from clio_agent.providers import dependencies, support_record, support_restore


def _wheel(directory: Path) -> Path:
    dist_info = "clio_fake_sdk-1.0.0.dist-info"
    files = {
        "clio_fake_sdk/__init__.py": b'__version__ = "1.0.0"\n',
        f"{dist_info}/METADATA": b"Metadata-Version: 2.1\nName: clio-fake-sdk\nVersion: 1.0.0\n",
        f"{dist_info}/WHEEL": b"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    path = directory / "clio_fake_sdk-1.0.0-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as archive:
        record = []
        for name, data in files.items():
            archive.writestr(name, data)
            digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
            record.append(f"{name},sha256={digest},{len(data)}")
        archive.writestr(f"{dist_info}/RECORD", "\n".join([*record, f"{dist_info}/RECORD,,"]))
    return path


class _Runtime:
    """A replaceable runtime directory holding a real uv environment."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.python = str(root / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))

    def create(self) -> None:
        subprocess.run(  # noqa: S607 - test-local uv
            ["uv", "venv", str(self.root), "--python", sys.executable, "--quiet"], check=True
        )

    def replace(self) -> None:
        """What a desktop update does: the whole runtime directory is swapped for a fresh one."""
        shutil.rmtree(self.root)
        self.create()

    def has_fake_sdk(self) -> bool:
        probe = subprocess.run(
            [self.python, "-c", "import clio_fake_sdk"], capture_output=True, check=False
        )
        return probe.returncode == 0


@pytest.fixture(autouse=True)
def _clean_restorer() -> None:
    support_restore.RESTORER.reset()
    yield
    support_restore.RESTORER.reset()


@pytest.fixture
def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[_Runtime, Path]:
    monkeypatch.setenv("UV_OFFLINE", "1")
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    wheel = _wheel(wheels)
    rt = _Runtime(tmp_path / "bundled-runtime" / "gact-runtime")
    rt.create()

    def _install_fake(*, python_executable: str | None = None) -> bool:
        if rt.has_fake_sdk():
            return False
        if not wheel.is_file():
            raise dependencies.ProviderDependencyInstallError("clio-fake-sdk is unreachable")
        subprocess.run(  # noqa: S607 - test-local uv, into the scratch runtime only
            ["uv", "pip", "install", "--python", rt.python, "--quiet", str(wheel)], check=True
        )
        return dependencies._recorded("fake_sdk", True)

    monkeypatch.setitem(
        dependencies.PROVIDER_SUPPORT,  # type: ignore[arg-type]
        "fake_sdk",
        dependencies.ProviderSupport("fake_sdk", "clio_fake_sdk", ("clio-fake-sdk",), "Fake SDK"),
    )
    monkeypatch.setitem(dependencies._PROVIDER_INSTALLERS, "fake_sdk", _install_fake)  # type: ignore[arg-type]
    return rt, wheel


def _restore(rt: _Runtime) -> support_restore.RestoreJob | None:
    steps = support_restore.plan_boot_restore(
        installed=lambda kind: kind != "fake_sdk" or rt.has_fake_sdk(),
        version_of=lambda _name: "",
    )
    jobs = support_restore.RESTORER.claim(steps)
    support_restore.RESTORER.execute(
        jobs, install=dependencies.ensure_provider_support, update=lambda _k: None
    )
    return support_restore.RESTORER.job("fake_sdk")


def test_an_installed_support_comes_back_after_the_runtime_is_replaced(
    runtime: tuple[_Runtime, Path],
) -> None:
    rt, _wheel_path = runtime
    assert dependencies.ensure_provider_support("fake_sdk") is True
    assert rt.has_fake_sdk()
    assert "fake_sdk" in support_record.read_recorded_support().entries

    rt.replace()
    assert not rt.has_fake_sdk()  # the update removed it, as in the field

    job = _restore(rt)

    assert job is not None and (job.action, job.state) == ("install", "restored")
    assert rt.has_fake_sdk()
    assert _restore(rt) is job  # nothing left to restore: no second job


def test_a_restore_that_cannot_install_fails_plainly_and_a_retry_succeeds(
    runtime: tuple[_Runtime, Path], tmp_path: Path
) -> None:
    rt, wheel = runtime
    dependencies.ensure_provider_support("fake_sdk")
    rt.replace()
    hidden = tmp_path / "hidden.whl"
    wheel.replace(hidden)  # the "network" is down during the first boot

    failed = _restore(rt)
    assert failed is not None and failed.state == "failed"
    assert failed.error_code == support_restore.PROVIDER_SUPPORT_RESTORE_FAILED
    assert "unreachable" in failed.error
    assert support_restore.missing_support_status("fake_sdk")[0] == "install_required"

    hidden.replace(wheel)
    retried = _restore(rt)
    assert retried is not None and retried.state == "restored"
    assert rt.has_fake_sdk()
