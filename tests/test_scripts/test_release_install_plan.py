"""The release install plan names exactly the files the first launch would download."""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

from clio_agent.runtime import document_runtime
from clio_agent.runtime.document_stack import office

ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "release_install_plan", ROOT / "scripts/release_install_plan.py"
)
assert _SPEC and _SPEC.loader
plan = importlib.util.module_from_spec(_SPEC)
sys.modules["release_install_plan"] = plan
_SPEC.loader.exec_module(plan)

# install.sh's plan_entries accepts only these shapes.
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
_SHA = re.compile(r"^[0-9a-f]{64}$")


@pytest.mark.parametrize("target", sorted(plan.TARGETS))
def test_each_entry_is_what_prepare_would_fetch(target: str, monkeypatch) -> None:
    (system, machine), pnpm_host = plan.TARGETS[target]
    monkeypatch.setattr(office.platform, "system", lambda: system)
    monkeypatch.setattr(office.platform, "machine", lambda: machine)
    entries = plan.entries(target)

    kind, sha, name, url = entries[0]
    assert (kind, (url, name, sha)) == ("office", office.asset())

    npm = {name: (sha, url) for kind, sha, name, url in entries if kind == "npm"}
    for package in ("pnpm", f"@pnpm/exe.{pnpm_host}"):
        if package not in document_runtime.PNPM_PACKAGES:
            assert document_runtime.pnpm_tarball(package) not in npm
            continue
        tarball = document_runtime.pnpm_tarball(package)
        sha, url = npm[tarball]
        assert sha == document_runtime.PNPM_PACKAGES[package]
        assert url == f"https://registry.npmjs.org/{package}/-/{tarball}"
    for _kind, sha, name, url in entries:
        assert _NAME.match(name) and _SHA.match(sha) and url.startswith("https://")


def test_apple_silicon_plan_prefetches_libreoffice_and_native_pnpm(tmp_path: Path) -> None:
    out = tmp_path / "install-plan.aarch64-apple-darwin.bundled.txt"
    assert plan.main(["--target", "aarch64-apple-darwin", "--output", str(out), "--no-sizes"]) == 0
    lines = [line.split() for line in out.read_text().splitlines()]
    assert lines[0] == ["capability", office.INSTALLER_PLAN_CAPABILITY]
    lines = lines[1:]
    assert [(line[0], line[3]) for line in lines] == [
        ("office", f"LibreOffice_{office.VERSION}_MacOS_aarch64.dmg"),
        ("npm", f"pnpm-{document_runtime.PNPM_VERSION}.tgz"),
        ("npm", f"exe.darwin-arm64-{document_runtime.PNPM_VERSION}.tgz"),
    ]
    assert all(len(line) == 5 and line[2] == "0" for line in lines)


def test_windows_plan_prefetches_the_msi_and_native_pnpm(tmp_path: Path) -> None:
    out = tmp_path / "install-plan.x86_64-pc-windows-msvc.bundled.txt"
    assert (
        plan.main(["--target", "x86_64-pc-windows-msvc", "--output", str(out), "--no-sizes"]) == 0
    )
    lines = [line.split() for line in out.read_text().splitlines()]
    assert lines[0] == ["capability", office.INSTALLER_PLAN_CAPABILITY]
    lines = lines[1:]
    assert [(line[0], line[3]) for line in lines] == [
        ("office", f"LibreOffice_{office.VERSION}_Win_x86-64.msi"),
        ("npm", f"pnpm-{document_runtime.PNPM_VERSION}.tgz"),
        ("npm", f"exe.win32-x64-{document_runtime.PNPM_VERSION}.tgz"),
    ]
    assert all(len(line) == 5 and line[2] == "0" for line in lines)


def test_windows_host_package_has_a_pin(monkeypatch) -> None:
    monkeypatch.setattr(document_runtime.sys, "platform", "win32")
    monkeypatch.setattr(document_runtime.platform, "machine", lambda: "AMD64")
    assert document_runtime._pnpm_host_package() in document_runtime.PNPM_PACKAGES


def test_unknown_target_has_no_plan() -> None:
    with pytest.raises(SystemExit):
        plan.entries("aarch64-pc-windows-msvc")


def test_size_falls_back_to_a_one_byte_range_when_head_has_no_length(monkeypatch) -> None:
    class Response:
        def __init__(self, headers: dict[str, str]) -> None:
            self.headers = headers

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    seen: list[tuple[str, str | None]] = []

    def urlopen(request, timeout):
        seen.append((request.get_method(), request.get_header("Range")))
        if request.get_method() == "HEAD":
            return Response({})
        return Response({"Content-Range": "bytes 0-0/1028233"})

    monkeypatch.setattr(plan.urllib.request, "urlopen", urlopen)
    assert plan.remote_size("https://registry.npmjs.org/pnpm/-/pnpm-12.9.1.tgz") == 1028233
    assert seen == [("HEAD", None), ("GET", "bytes=0-0")]
