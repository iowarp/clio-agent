"""Execute the installers with fixture downloads; never install into a user's account."""

from __future__ import annotations

import hashlib
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


# The shared installer shims live in the repo's test package; this suite also runs
# from install/tests alone (macos-bundle-smoke), where the repo root isn't importable.
_REPO = str(Path(__file__).resolve().parents[2])
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)


def _bash_path(path: Path) -> str:
    """Return a native POSIX or Git Bash path for shell fixture files."""
    value = path.as_posix()
    return f"/{value[0].lower()}{value[2:]}" if os.name == "nt" else value


@pytest.mark.parametrize("system", ["Darwin", "Linux"])
@pytest.mark.parametrize("version", ["0.9.5b2", "v0.9.5-beta.5.1"])
def test_backend_installer_requests_compatible_mac_wheels(
    tmp_path: Path, system: str, version: str
) -> None:
    """The registry install uses wheel-only resolution on Mac, retaining Linux source support."""
    from tests.test_scripts.installer_shims import STOP, run_installer, write_release

    for tag in ("v0.9.5-beta.2", "v0.9.5-beta.5.1"):
        write_release(tmp_path / "fixtures", tag, python="3.14")
    result, calls = run_installer(tmp_path, "--version", version, system=system)
    assert result.returncode == STOP, result.stdout + result.stderr
    install = next(call for call in calls if call.startswith("uv pip install"))
    package_version = "0.9.5b5.post1" if version == "v0.9.5-beta.5.1" else version
    assert f"clio-agent[argonne,flowcept]=={package_version}" in install
    assert ("--only-binary=rasterio" in install) == (system == "Darwin")


@pytest.mark.parametrize(
    "version", ["0.9.5b2", "v0.9.5-beta.2", "0.9.4.24", "0.9.5b5.post1", "v0.9.5-beta.5.1"]
)
@pytest.mark.parametrize("integrity", ["valid", "tampered", "missing", "duplicate"])
@pytest.mark.parametrize("checksum_format", ["canonical", "legacy", "binary"])
@pytest.mark.parametrize("system", ["Darwin", "Linux"])
def test_verified_desktop_download(
    tmp_path: Path, version: str, integrity: str, checksum_format: str, system: str
) -> None:
    """``install --desktop``: a real hash check precedes setup, with beta normalization
    and bad manifests, on every OS (Windows runs ``install.ps1 -Desktop``)."""
    windows = os.name == "nt"
    if windows and system == "Linux":
        pytest.skip("one Windows variant")
    tag_version = {
        "0.9.5b2": "0.9.5-beta.2",
        "0.9.5b5.post1": "0.9.5-beta.5.1",
    }.get(version, version.removeprefix("v"))
    suffix = (
        "x64-setup-bundled.exe"
        if windows
        else "aarch64-bundled.dmg"
        if system == "Darwin"
        else "amd64-bundled.deb"
    )
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
& {quote(ROOT / "install/install.ps1")} -Desktop -Version '{version}' -DownloadOnly {quote(output)}
""",
            encoding="utf-8",
        )
        command = ["pwsh", "-NoProfile", "-File", str(wrapper)]
        env = dict(os.environ)
    else:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        commands = {
            "uname": f'if [ "$1" = -s ]; then echo {system}; else echo '
            + ("arm64" if system == "Darwin" else "x86_64")
            + "; fi",
            "dpkg": "exit 0",
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
  *.dmg|*.deb) cp "$TEST_PAYLOAD" "$out" ;;
  */SHA256SUMS.*.txt) cp "$TEST_MANIFEST" "$out" ;;
  *) exit 99 ;;
esac""",
            "hdiutil": "echo 'Download-only must never mount' >&2; exit 99",
            "apt-get": "echo 'Download-only must never install' >&2; exit 99",
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
        command = [
            "bash",
            str(ROOT / "install/install.sh"),
            "--desktop",
            "--download-only",
            str(output),
        ]
    result = subprocess.run(command, env=env, text=True, capture_output=True, timeout=30)
    if integrity == "valid":
        assert result.returncode == 0, result.stdout + result.stderr
        assert (output / asset_name).read_bytes() == payload.read_bytes()
        assert f"/v{tag_version}/{asset_name}" in urls.read_text(encoding="utf-8-sig")
    else:
        assert result.returncode != 0, result.stdout + result.stderr
        assert "checksum" in (result.stdout + result.stderr).lower()
        assert not output.exists()


def test_windows_backend_installer_normalizes_hotfix_package_version(tmp_path: Path) -> None:
    """The public hotfix tag selects its real PyPI version before any installation."""
    if os.name != "nt":
        pytest.skip("Windows installer execution requires PowerShell on Windows")
    args_file = tmp_path / "uv-args"
    wrapper = tmp_path / "run.ps1"

    def quote(path: Path) -> str:
        return "'" + str(path).replace("'", "''") + "'"

    wrapper.write_text(
        f"""$ErrorActionPreference = 'Stop'
$env:CLIO_PREFIX = {quote(tmp_path / "install")}
$env:CLIO_BIN_DIR = {quote(tmp_path / "launchers")}
$env:CLIO_VERSION = 'v0.9.5-beta.5.1'
$env:CLIO_REF = ''
$env:GACT_REF = ''
function uv {{
    if ($args[0] -eq 'venv') {{ $global:LASTEXITCODE = 0; return }}
    $args | Set-Content -LiteralPath {quote(args_file)}
    exit 77
}}
& {quote(ROOT / "install/install.ps1")}
exit $LASTEXITCODE
""",
        encoding="utf-8",
    )
    result = subprocess.run(
        ["pwsh", "-NoProfile", "-File", str(wrapper)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 77, result.stdout + result.stderr
    assert "clio-agent==0.9.5b5.post1" in args_file.read_text(encoding="utf-8-sig").splitlines()


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
