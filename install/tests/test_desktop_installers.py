"""Execute the installers with fixture downloads; never install into a user's account."""

from __future__ import annotations

import hashlib
import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _bash_path(path: Path) -> str:
    """Return a native POSIX or Git Bash path for shell fixture files."""
    value = path.as_posix()
    return f"/{value[0].lower()}{value[2:]}" if os.name == "nt" else value


@pytest.mark.parametrize("system", ["Darwin", "Linux"])
def test_backend_installer_requests_compatible_mac_wheels(tmp_path: Path, system: str) -> None:
    """The registry install uses wheel-only resolution on Mac, retaining Linux source support."""
    binaries = tmp_path / "bin"
    binaries.mkdir()
    args_file = tmp_path / "uv-args"
    shims = {
        "uname": f'if [ "$1" = -s ]; then echo {system}; else echo arm64; fi',
        "curl": "exit 99",
        "uv": 'if [ "$1" = venv ]; then exit 0; fi\nprintf "%s\\n" "$@" > "$TEST_UV_ARGS"\nexit 77',
    }
    for name, contents in shims.items():
        shim = binaries / name
        shim.write_text("#!/bin/bash\n" + contents + "\n", newline="\n")
        shim.chmod(0o755)
    env = {key: value for key, value in os.environ.items() if not key.startswith("CLIO_")}
    env.update(
        HOME=_bash_path(tmp_path),
        TEST_PATH=f"{_bash_path(binaries)}:/usr/bin:/bin",
        CLIO_PREFIX=_bash_path(tmp_path / "install"),
        CLIO_BIN_DIR=_bash_path(tmp_path / "launchers"),
        CLIO_VERSION="0.9.5b2",
        TEST_UV_ARGS=_bash_path(args_file),
    )
    bash = "C:/Program Files/Git/bin/bash.exe" if os.name == "nt" else "bash"
    result = subprocess.run(
        [
            bash,
            "-c",
            'export PATH="$TEST_PATH"; exec bash "$1"',
            "installer-test",
            _bash_path(ROOT / "install/install.sh"),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 77, result.stdout + result.stderr
    args = args_file.read_text().splitlines()
    assert "clio-agent[argonne,flowcept]==0.9.5b2" in args
    assert ("--only-binary=rasterio" in args) == (system == "Darwin")


@pytest.mark.parametrize("version", ["0.9.5b2", "v0.9.5-beta.2", "0.9.4.24"])
@pytest.mark.parametrize("integrity", ["valid", "tampered", "missing", "duplicate"])
@pytest.mark.parametrize("checksum_format", ["canonical", "legacy", "binary"])
def test_verified_desktop_download(
    tmp_path: Path, version: str, integrity: str, checksum_format: str
) -> None:
    """A real hash check must precede setup, including beta normalization and bad manifests."""
    windows = os.name == "nt"
    tag_version = "0.9.5-beta.2" if "b" in version else version
    suffix = "x64-setup-bundled.exe" if windows else "aarch64-bundled.dmg"
    asset_name = f"CLIO.Desktop_{tag_version}_{suffix}"
    payload = tmp_path / "fixture.bin"
    payload.write_bytes(b"release payload")
    digest = hashlib.sha256(payload.read_bytes()).hexdigest()
    checksum_name = (
        asset_name
        if checksum_format == "canonical"
        else asset_name.replace("CLIO.Desktop", "CLIO Desktop")
    )
    marker = "*" if checksum_format == "binary" else " "
    line = f"{digest} {marker}{checksum_name}\n"
    manifest = tmp_path / "checksums"
    manifest.write_text(
        "" if integrity == "missing" else line * (2 if integrity == "duplicate" else 1)
    )
    if integrity == "tampered":
        payload.write_bytes(b"changed payload")
    output = tmp_path / "downloads"
    urls = tmp_path / "urls.txt"
    if windows:

        def quote(path: Path) -> str:
            return "'" + str(path).replace("'", "''") + "'"

        wrapper = tmp_path / "run.ps1"
        wrapper.write_text(
            f"""$ErrorActionPreference = 'Stop'
function Invoke-WebRequest {{
    param($Uri, $OutFile, [switch]$UseBasicParsing)
    Add-Content -LiteralPath {quote(urls)} -Value $Uri
    if ($OutFile) {{ Copy-Item -LiteralPath {quote(payload)} -Destination $OutFile }}
    else {{ return @{{Content = (Get-Content -Raw -LiteralPath {quote(manifest)})}} }}
}}
function Start-Process {{ throw 'DownloadOnly must never start setup' }}
& {quote(ROOT / "install/desktop.ps1")} -Version '{version}' -DownloadOnly -DownloadDirectory {quote(output)}
""",
            encoding="utf-8",
        )
        command = ["pwsh", "-NoProfile", "-File", str(wrapper)]
        env = dict(os.environ)
    else:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        commands = {
            "uname": 'if [ "$1" = -s ]; then echo Darwin; else echo arm64; fi',
            "sysctl": "echo 1",
            "sw_vers": "echo 14.0",
            "curl": """out=""; url=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift 2 ;;
    https://*) url="$1"; shift ;;
    *) shift ;;
  esac
done
printf '%s\\n' "$url" >> "$TEST_URLS"
case "$url" in
  *.dmg) cp "$TEST_PAYLOAD" "$out" ;;
  */SHA256SUMS.*.txt) cp "$TEST_MANIFEST" "$out" ;;
  *) exit 99 ;;
esac""",
            "hdiutil": "echo 'Download-only must never mount' >&2; exit 99",
        }
        for name, contents in commands.items():
            path = bin_dir / name
            path.write_text("#!/bin/bash\nset -eu\n" + contents + "\n")
            path.chmod(0o755)
        env = dict(os.environ)
        env.update(
            PATH=f"{bin_dir}:{os.environ['PATH']}",
            CLIO_VERSION=version,
            TEST_URLS=str(urls),
            TEST_PAYLOAD=str(payload),
            TEST_MANIFEST=str(manifest),
        )
        command = ["bash", str(ROOT / "install/desktop.sh"), "--download-only", str(output)]
    result = subprocess.run(command, env=env, text=True, capture_output=True, timeout=30)
    if integrity == "valid":
        assert result.returncode == 0, result.stdout + result.stderr
        assert (output / asset_name).read_bytes() == payload.read_bytes()
        assert f"/v{tag_version}/{asset_name}" in urls.read_text(encoding="utf-8-sig")
    else:
        assert result.returncode != 0, result.stdout + result.stderr
        assert "checksum" in (result.stdout + result.stderr).lower()
        assert not output.exists()


def test_shell_release_parser_accepts_bsd_whitespace(tmp_path: Path) -> None:
    """The real installer's release fallback parses the pretty JSON from GitHub on BSD sed."""
    source = (ROOT / "install/install.sh").read_text()
    expression = next(
        line.split("sed -nE ", 1)[1].strip().rstrip("\\").strip()
        for line in source.splitlines()
        if "sed -nE" in line and "tag_name" in line
    )
    assert r"\s" not in expression  # BSD sed does not implement GNU's whitespace extension.
    bash = shutil.which("bash")
    if os.name == "nt":
        bash = "C:/Program Files/Git/bin/bash.exe"
    assert bash
    result = subprocess.run(
        [
            bash,
            "-c",
            f"printf '%s\\n' {shlex.quote('  "tag_name": "v0.9.5-beta.2",')} | sed -nE {expression}",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "v0.9.5-beta.2"
