"""The staged component update, driven for real against a SCRATCH venv.

Every test creates its own venv (``uv venv``), installs a locally built wheel
of a stand-in component (``clio-fake-sdk``), and lets the real updater run the
real installer (``uv pip install``), the real bytecode recompile and the real
fresh-interpreter verification against it. Only the component group and the
release index are test-provided (``file://`` wheels with real sha256 digests),
and ``UV_OFFLINE=1`` keeps uv off the network. The provider's own check is the
one injected callable (a scratch venv has no CLIO to run it).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from clio_agent.providers.components import updater
from clio_agent.providers.components.pypi import ReleaseIndex, ReleaseLookup, WheelFile
from clio_agent.providers.components.registry import ProviderComponents

SPEC = ProviderComponents(
    provider_kind="fake",
    distributions=("clio-fake-sdk",),
    modules=("clio_fake_sdk",),
    release_notes_url="https://example.invalid/notes",
)


def _record_line(name: str, data: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
    return f"{name},sha256={digest},{len(data)}"


def build_wheel(
    directory: Path, version: str, *, body: str | None = None, requires: tuple[str, ...] = ()
) -> Path:
    """A minimal, valid ``clio-fake-sdk`` wheel (py3-none-any) with a RECORD."""
    dist_info = f"clio_fake_sdk-{version}.dist-info"
    files = {
        "clio_fake_sdk/__init__.py": (
            body if body is not None else f'__version__ = "{version}"\n'
        ).encode(),
        "clio_fake_sdk/core.py": b"def answer() -> int:\n    return 42\n",
        f"{dist_info}/METADATA": (
            "Metadata-Version: 2.1\nName: clio-fake-sdk\n"
            f"Version: {version}\n" + "".join(f"Requires-Dist: {r}\n" for r in requires)
        ).encode(),
        f"{dist_info}/WHEEL": b"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    path = directory / f"clio_fake_sdk-{version}-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as archive:
        record = [_record_line(name, data) for name, data in files.items()]
        for name, data in files.items():
            archive.writestr(name, data)
        archive.writestr(
            f"{dist_info}/RECORD", "\n".join([*record, f"{dist_info}/RECORD,,"]) + "\n"
        )
    return path


def wheel_file(path: Path) -> WheelFile:
    return WheelFile(
        path.name, path.resolve().as_uri(), hashlib.sha256(path.read_bytes()).hexdigest()
    )


class FixedIndex(ReleaseLookup):
    """A release index over local wheels (the lookup contract, no network)."""

    def __init__(self, wheels: dict[str, Path]) -> None:
        super().__init__()
        self._index = ReleaseIndex("clio-fake-sdk", {v: wheel_file(p) for v, p in wheels.items()})

    def releases(self, distribution: str, *, refresh: bool = False) -> ReleaseIndex:
        assert distribution == "clio-fake-sdk"
        return self._index


@pytest.fixture
def wheels(tmp_path: Path) -> dict[str, Path]:
    directory = tmp_path / "wheels"
    directory.mkdir()
    return {
        "1.0.0": build_wheel(directory, "1.0.0"),
        "1.1.0": build_wheel(directory, "1.1.0"),
        "1.2.0": build_wheel(directory, "1.2.0", body='raise ImportError("broken release")\n'),
        "1.3.0": build_wheel(directory, "1.3.0", requires=("clio-fake-dep>=2",)),
    }


@pytest.fixture
def venv(tmp_path: Path, wheels: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> str:
    """A scratch venv with clio-fake-sdk 1.0.0 installed; never the running runtime."""
    monkeypatch.setenv("UV_OFFLINE", "1")
    root = tmp_path / "venv"
    subprocess.run(["uv", "venv", str(root), "--python", sys.executable, "--quiet"], check=True)  # noqa: S607
    python = str(root / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))
    subprocess.run(  # noqa: S607
        ["uv", "pip", "install", "--python", python, "--quiet", str(wheels["1.0.0"])], check=True
    )
    return python


def installed(python: str) -> str:
    out = subprocess.run(
        [python, "-c", "import importlib.metadata as m; print(m.version('clio-fake-sdk'))"],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def env_for(python: str, index: dict[str, Path], **kwargs: object) -> updater.UpdateEnvironment:
    return updater.UpdateEnvironment(python=python, lookup=FixedIndex(index), spec=SPEC, **kwargs)  # type: ignore[arg-type]


def test_update_installs_verifies_and_reports_every_stage(
    venv: str, wheels: dict[str, Path]
) -> None:
    stages: list[str] = []
    runner = updater.ComponentUpdater()

    def _check(python: str, kind: str) -> updater.VerifyOutcome:
        stages.append(runner.job("fake").stage)  # type: ignore[union-attr]
        # The provider check runs in a FRESH interpreter that sees the new code.
        probe = subprocess.run(
            [python, "-c", "import clio_fake_sdk; print(clio_fake_sdk.__version__)"],
            capture_output=True,
            text=True,
            check=False,
        )
        return updater.VerifyOutcome(probe.stdout.strip() == "1.1.0", "fake_checked")

    job = runner.run(
        "fake",
        env_for(venv, {"1.0.0": wheels["1.0.0"], "1.1.0": wheels["1.1.0"]}, verify_provider=_check),
    )

    assert (job.stage, job.error_code) == ("done", "")
    assert job.from_versions == {"clio-fake-sdk": "1.0.0"}
    assert job.to_versions == {"clio-fake-sdk": "1.1.0"}
    assert job.changed is True and job.rolled_back is False
    assert stages == ["verifying"]
    assert installed(venv) == "1.1.0"
    assert job.restart_required is False  # the scratch venv is not this process


def test_update_leaves_checked_hash_bytecode_and_one_dist_info(
    venv: str, wheels: dict[str, Path]
) -> None:
    """The stale-bytecode lesson: recompiled CHECKED_HASH, old dist-info gone."""
    updater.ComponentUpdater().run(
        "fake", env_for(venv, {"1.0.0": wheels["1.0.0"], "1.1.0": wheels["1.1.0"]})
    )
    site = subprocess.run(
        [venv, "-c", "import clio_fake_sdk, os; print(os.path.dirname(clio_fake_sdk.__file__))"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    package = Path(site)
    pycs = list((package / "__pycache__").glob("*.pyc"))
    assert pycs
    for pyc in pycs:
        flags = int.from_bytes(pyc.read_bytes()[4:8], "little")
        assert flags == 0b11, pyc  # hash-based AND check_source
    assert sorted(p.name for p in package.parent.glob("clio_fake_sdk-*.dist-info")) == [
        "clio_fake_sdk-1.1.0.dist-info"
    ]


def test_a_release_that_fails_to_import_rolls_back(venv: str, wheels: dict[str, Path]) -> None:
    """SABOTAGE: skip the rollback -> the broken 1.2.0 stays installed -> red."""
    job = updater.ComponentUpdater().run(
        "fake", env_for(venv, {"1.0.0": wheels["1.0.0"], "1.2.0": wheels["1.2.0"]})
    )
    assert (job.stage, job.error_code) == ("failed", "verify_failed")
    assert "broken release" in job.error
    assert job.rolled_back is True and job.changed is False
    assert installed(venv) == "1.0.0"


def test_a_failed_provider_check_rolls_back_with_its_typed_code(
    venv: str, wheels: dict[str, Path]
) -> None:
    job = updater.ComponentUpdater().run(
        "fake",
        env_for(
            venv,
            {"1.0.0": wheels["1.0.0"], "1.1.0": wheels["1.1.0"]},
            verify_provider=lambda _py, _kind: updater.VerifyOutcome(
                False, "codex_direct_transport_error", "no answer"
            ),
        ),
    )
    assert (job.stage, job.error_code, job.rolled_back) == (
        "failed",
        "codex_direct_transport_error",
        True,
    )
    assert installed(venv) == "1.0.0"


def test_a_release_whose_dependencies_conflict_with_the_kept_set_changes_nothing(
    venv: str, wheels: dict[str, Path]
) -> None:
    """1.3.0 needs a dependency the constraints cannot satisfy: resolution fails first."""
    job = updater.ComponentUpdater().run(
        "fake", env_for(venv, {"1.0.0": wheels["1.0.0"], "1.3.0": wheels["1.3.0"]})
    )
    assert (job.stage, job.error_code) == ("failed", "install_failed")
    assert "clio-fake-dep" in job.error
    assert installed(venv) == "1.0.0"
    assert job.changed is False


def test_no_rollback_wheel_refuses_before_touching_anything(
    venv: str, wheels: dict[str, Path]
) -> None:
    job = updater.ComponentUpdater().run("fake", env_for(venv, {"1.1.0": wheels["1.1.0"]}))
    assert (job.stage, job.error_code) == ("failed", "rollback_unavailable")
    assert installed(venv) == "1.0.0"


def test_an_already_current_component_is_done_without_changes(
    venv: str, wheels: dict[str, Path]
) -> None:
    job = updater.ComponentUpdater().run("fake", env_for(venv, {"1.0.0": wheels["1.0.0"]}))
    assert (job.stage, job.changed, job.to_versions) == ("done", False, {"clio-fake-sdk": "1.0.0"})


def test_a_missing_component_is_typed(
    tmp_path: Path, wheels: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UV_OFFLINE", "1")
    root = tmp_path / "empty"
    subprocess.run(["uv", "venv", str(root), "--python", sys.executable, "--quiet"], check=True)  # noqa: S607
    python = str(root / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))
    job = updater.ComponentUpdater().run("fake", env_for(python, {"1.1.0": wheels["1.1.0"]}))
    assert (job.stage, job.error_code) == ("failed", "component_not_installed")


def test_a_second_update_is_refused_while_one_runs() -> None:
    runner = updater.ComponentUpdater()
    runner._claim("codex")
    with pytest.raises(updater.UpdateInProgressError):
        runner._claim("claude_code")


def test_restart_is_required_only_for_code_this_process_already_imported() -> None:
    loaded = ProviderComponents("x", ("pytest",), ("pytest",), "")
    absent = ProviderComponents("x", ("nope",), ("definitely_not_imported_mod",), "")
    here = updater.UpdateEnvironment(python=sys.executable)
    assert updater._loaded_here(here, loaded) is True
    assert updater._loaded_here(here, absent) is False


def test_download_rejects_a_hash_mismatch(tmp_path: Path, wheels: dict[str, Path]) -> None:
    good = wheel_file(wheels["1.1.0"])
    bad = WheelFile(good.filename, good.url, "0" * 64)
    (tmp_path / "dl").mkdir()
    assert updater.download_wheel(good, tmp_path / "dl").is_file()
    with pytest.raises(updater.UpdateFailed) as caught:
        updater.download_wheel(bad, tmp_path / "dl")
    assert caught.value.code == "download_hash_mismatch"


def test_job_wire_shape_is_typed() -> None:
    job = updater.UpdateJob(
        provider_kind="codex", stage="failed", error_code="install_failed", error="x"
    )
    wire = job.to_wire()
    assert wire["running"] is False
    assert wire["error"] == {"code": "install_failed", "message": "x"}
    assert json.loads(json.dumps(wire)) == wire
