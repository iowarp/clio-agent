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
    from tests.test_scripts.installer_shims import RELEASE_STOP, run_installer, write_release

    for tag in ("v0.9.5-beta.2", "v0.9.5-beta.5.1"):
        write_release(tmp_path / "fixtures", tag, python="3.14")
    result, calls = run_installer(tmp_path, "--version", version, system=system)
    assert result.returncode == RELEASE_STOP, result.stdout + result.stderr
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
# Downloads use curl.exe when Windows has it; hide it so the stub above serves them.
$env:PATH = (($env:PATH -split ';') | Where-Object {{ $_ -and -not (Test-Path (Join-Path $_ 'curl.exe')) }}) -join ';'
& {quote(ROOT / "install/install.ps1")} -Desktop -Version '{version}' -DownloadOnly {quote(output)}
exit $LASTEXITCODE
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
            "sw_vers": "echo 15.0",
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
    # The release's locked versions come first (uv export to a constraints file).
    if ($args[0] -eq 'export') {{
        Set-Content -LiteralPath $args[[array]::IndexOf($args, '-o') + 1] -Value ''
        $global:LASTEXITCODE = 0; return
    }}
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
    assert (
        "clio-agent[argonne,flowcept]==0.9.5b5.post1"
        in args_file.read_text(encoding="utf-8-sig").splitlines()
    )


def test_shell_desktop_release_parser_skips_drafts() -> None:
    """Exercise the shipped Desktop parser on GitHub's pretty JSON using native awk."""
    source = (ROOT / "install/install.sh").read_text()
    function = (
        "desktop_tag() {" + source.split("desktop_tag() {", 1)[1].split("\n}\n", 1)[0] + "\n}\n"
    )
    bash = "C:/Program Files/Git/bin/bash.exe" if os.name == "nt" else shutil.which("bash")
    assert bash
    listing = (
        '[\n  {\n    "tag_name": "v0.9.5-beta.9",\n    "draft": true\n  },\n'
        '  {\n    "tag_name": "v0.9.5-beta.2",\n    "draft": false\n  }\n]\n'
    )
    result = subprocess.run(
        [
            bash,
            "-c",
            "set -euo pipefail\n"
            "OPT_DEV=1\nOPT_VERSION=\nCLIO_VERSION=\n"
            f"curl() {{ printf '%s' {shlex.quote(listing)}; }}\n" + function + "desktop_tag\n",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "v0.9.5-beta.2"


@pytest.mark.skipif(os.name == "nt", reason="install.sh runs on macOS and Linux")
def test_intel_mac_desktop_is_refused_before_any_download(tmp_path: Path) -> None:
    """Intel Macs have only the attach-only app, which is not offered: say so, fetch nothing."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, contents in {
        "uname": 'if [ "$1" = -s ]; then echo Darwin; else echo x86_64; fi',
        "sysctl": "echo 0",
        "sw_vers": "echo 15.0",
        "curl": "echo 'nothing may be downloaded' >&2; exit 99",
    }.items():
        path = bin_dir / name
        path.write_text("#!/bin/bash\nset -eu\n" + contents + "\n")
        path.chmod(0o755)
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", CLIO_VERSION="0.9.4.24")
    result = subprocess.run(
        ["bash", str(ROOT / "install/install.sh"), "--desktop"],
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert "not available for Intel Macs" in result.stdout + result.stderr
    assert "nothing may be downloaded" not in result.stderr


@pytest.mark.skipif(os.name == "nt", reason="install.sh runs on macOS and Linux")
def test_macos_14_desktop_is_refused_before_any_download(tmp_path: Path) -> None:
    """macOS 15 is the floor (iowarp-core 2.4 ships macOS 15 wheels): say so, fetch nothing."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, contents in {
        "uname": 'if [ "$1" = -s ]; then echo Darwin; else echo arm64; fi',
        "sysctl": "echo 1",
        "sw_vers": "echo 14.7",
        "curl": "echo 'nothing may be downloaded' >&2; exit 99",
    }.items():
        path = bin_dir / name
        path.write_text("#!/bin/bash\nset -eu\n" + contents + "\n")
        path.chmod(0o755)
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", CLIO_VERSION="0.9.4.24")
    result = subprocess.run(
        ["bash", str(ROOT / "install/install.sh"), "--desktop"],
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert "requires macOS 15 or newer" in result.stdout + result.stderr
    assert "nothing may be downloaded" not in result.stderr


def _prepare_functions() -> str:
    """install.sh's preparation step and the helpers it uses, without the rest."""
    script = (ROOT / "install/install.sh").read_text(encoding="utf-8")
    wanted = ("say", "desktop_prepares", "prepare_desktop")
    blocks, keep = [], False
    for line in script.splitlines():
        if any(line.startswith(f"{name}()") for name in wanted):
            keep = True
        if keep:
            blocks.append(line)
            if line == "}" or (line.startswith("say()") and line.rstrip().endswith("}")):
                keep = False
    return "\n".join(blocks)


@pytest.mark.skipif(os.name == "nt", reason="install.sh runs on macOS and Linux")
@pytest.mark.parametrize(
    ("tag", "prepares"),
    [("v0.9.4.24", False), ("v0.9.5-beta.8", False), ("v0.9.5", True), ("v0.9.6-beta.1", True)],
)
def test_desktop_is_prepared_after_install_when_its_release_can(
    tmp_path: Path, tag: str, prepares: bool
) -> None:
    """The first launch's setup runs at install, as on Windows; never an unknown flag."""
    calls = tmp_path / "calls"
    desktop = tmp_path / "clio-desktop"
    desktop.write_text(f'#!/bin/bash\necho "$*" >> {shlex.quote(str(calls))}\n')
    desktop.chmod(0o755)
    result = subprocess.run(
        [
            "bash",
            "-c",
            _prepare_functions() + f"\nprepare_desktop {tag} {shlex.quote(str(desktop))}",
        ],
        text=True,
        capture_output=True,
        timeout=30,
        env={k: v for k, v in os.environ.items() if k != "SUDO_USER"},
    )
    assert result.returncode == 0, result.stderr
    if prepares:
        assert calls.read_text() == "--prepare-runtime\n"
        assert "ready to open" in result.stdout + result.stderr
    else:
        assert not calls.exists()


@pytest.mark.skipif(os.name == "nt", reason="install.sh runs on macOS and Linux")
def test_a_failed_preparation_is_reported_and_left_to_first_launch(tmp_path: Path) -> None:
    desktop = tmp_path / "clio-desktop"
    desktop.write_text("#!/bin/bash\necho 'no network' >&2\nexit 3\n")
    desktop.chmod(0o755)
    result = subprocess.run(
        [
            "bash",
            "-c",
            _prepare_functions() + f"\nprepare_desktop v0.9.5 {shlex.quote(str(desktop))}",
        ],
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0
    assert "did not finish" in result.stdout + result.stderr
    assert "first launch" in result.stdout + result.stderr


def _package_fakes(tmp_path: Path) -> tuple[dict[str, str], Path]:
    """Fake macOS tools for ``--desktop-package``: nothing is mounted or installed for real."""
    calls = tmp_path / "calls.txt"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    commands = {
        "uname": 'if [ "$1" = -s ]; then echo Darwin; else echo arm64; fi',
        "pgrep": "exit 1",
        "hdiutil": """echo "hdiutil $*" >> "$TEST_CALLS"
if [ "$1" = attach ]; then
  while [ "$#" -gt 0 ]; do [ "$1" = -mountpoint ] && mnt="$2"; shift; done
  app="$mnt/CLIO Desktop.app/Contents"; mkdir -p "$app/MacOS"
  printf '#!/bin/bash\\necho "app $*" >> "$TEST_CALLS"\\n' > "$app/MacOS/clio-desktop"
  chmod +x "$app/MacOS/clio-desktop"
fi""",
        "ditto": 'echo "ditto $*" >> "$TEST_CALLS"\nsrc="${*: -2:1}"; dst="${*: -1}"\ncp -R "$src" "$dst"',
        "codesign": 'echo "codesign $*" >> "$TEST_CALLS"',
        "plutil": "echo 0.9.6-test",
        "curl": 'echo "curl $*" >> "$TEST_CALLS"; exit 99',
    }
    for name, contents in commands.items():
        path = bin_dir / name
        path.write_text("#!/bin/bash\nset -eu\n" + contents + "\n")
        path.chmod(0o755)
    env = dict(os.environ)
    env.update(
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        TEST_CALLS=str(calls),
        CLIO_DESKTOP_DIR=str(tmp_path / "Applications"),
        TMPDIR=str(tmp_path),
    )
    return env, calls


def _run_installer(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [shutil.which("bash") or "bash", str(ROOT / "install/install.sh"), *args],
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )


@pytest.mark.skipif(os.name == "nt", reason="the macOS shell installer")
def test_desktop_test_package_installs_unquarantined_and_prepares(tmp_path: Path) -> None:
    """A local test build is checked, installed without quarantine, and prepared."""
    env, calls = _package_fakes(tmp_path)
    image = tmp_path / "CLIO Desktop_0.9.6_aarch64.dmg"
    image.write_bytes(b"test build")
    digest = hashlib.sha256(image.read_bytes()).hexdigest().upper()

    result = _run_installer(["--desktop-package", str(image), "--desktop-sha256", digest], env)

    assert result.returncode == 0, result.stdout + result.stderr
    log = calls.read_text()
    assert f"hdiutil attach {image} -readonly" in log
    assert "ditto --noqtn " in log
    assert "codesign --verify --deep --strict" in log
    assert "app --prepare-runtime" in log
    assert "curl" not in log
    assert (tmp_path / "Applications/CLIO Desktop.app/Contents/MacOS/clio-desktop").is_file()
    assert "test build 0.9.6-test" in result.stdout


@pytest.mark.skipif(os.name == "nt", reason="the macOS shell installer")
@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--desktop-sha256", "0" * 64], "checksum mismatch"),
        (["--desktop-sha256", "not-a-sha"], "must be a SHA-256"),
        (["--download-only", "out"], "--download-only"),
    ],
)
def test_desktop_test_package_refusals_install_nothing(
    tmp_path: Path, args: list[str], message: str
) -> None:
    env, calls = _package_fakes(tmp_path)
    image = tmp_path / "test.dmg"
    image.write_bytes(b"test build")

    result = _run_installer(["--desktop-package", str(image), *args], env)

    assert result.returncode != 0
    assert message in result.stderr
    assert not calls.exists()
    assert not (tmp_path / "Applications").exists()


@pytest.mark.skipif(os.name == "nt", reason="the macOS shell installer")
def test_desktop_test_package_url_needs_a_checksum(tmp_path: Path) -> None:
    env, calls = _package_fakes(tmp_path)

    result = _run_installer(["--desktop-package", "https://example.invalid/t.dmg"], env)

    assert result.returncode != 0
    assert "needs --desktop-sha256" in result.stderr
    assert not calls.exists()


@pytest.mark.skipif(os.name == "nt", reason="the macOS shell installer")
def test_release_desktop_install_keeps_its_plain_copy() -> None:
    """Only the explicit test package skips quarantine; the release path is unchanged."""
    source = (ROOT / "install/install.sh").read_text()
    release = source.split("install_desktop() {", 1)[1].split("\n}\n", 1)[0]
    assert 'install_desktop_mac "$scratch" "$scratch/$asset"\n' in release


def _git_functions() -> str:
    """install.sh's --source git resolution, without the rest."""
    script = (ROOT / "install/install.sh").read_text(encoding="utf-8")
    blocks, keep = [], False
    for line in script.splitlines():
        if line.startswith(("macos_git_placeholder()", "source_git()")):
            keep = True
        if keep:
            blocks.append(line)
            if line == "}":
                keep = False
    return "\n".join(blocks)


def _source_git(tmp_path: Path, env: dict[str, str], call: str = "source_git") -> str:
    result = subprocess.run(
        ["/bin/bash", "-c", "OS=darwin\n" + _git_functions() + f"\n{call}"],
        text=True,
        capture_output=True,
        timeout=30,
        env={"HOME": str(tmp_path), **env},
    )
    return result.stdout if call == "source_git" else str(result.returncode)


def _executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return path


@pytest.mark.skipif(os.name == "nt", reason="install.sh runs on macOS and Linux")
def test_source_git_prefers_clio_git_then_a_real_git_then_the_desktops(tmp_path: Path) -> None:
    """--source never runs the macOS /usr/bin/git placeholder (CLT dialog); git is not run."""
    chosen = _executable(tmp_path / "chosen/git")
    on_path = _executable(tmp_path / "bin/git")
    bundled = _executable(
        tmp_path / "Apps/CLIO Desktop.app/Contents/Resources/gact-runtime/git/libexec/git-core/git"
    )
    empty = tmp_path / "empty"
    empty.mkdir()
    apps = {"CLIO_DESKTOP_DIR": str(tmp_path / "Apps")}
    assert _source_git(tmp_path, {"CLIO_GIT": str(chosen), "PATH": str(on_path.parent)}) == str(
        chosen
    )
    assert _source_git(tmp_path, {"PATH": str(on_path.parent), **apps}) == str(on_path)
    assert _source_git(tmp_path, {"PATH": str(empty), **apps}) == str(bundled)
    assert _source_git(tmp_path, {"PATH": str(empty)}) == ""


@pytest.mark.skipif(os.name == "nt", reason="install.sh runs on macOS and Linux")
def test_usr_bin_git_counts_as_real_once_developer_tools_provide_it(tmp_path: Path) -> None:
    developer = tmp_path / "Developer"
    _executable(developer / "usr/bin/git")
    check = "macos_git_placeholder /usr/bin/git"
    assert _source_git(tmp_path, {"DEVELOPER_DIR": str(developer)}, check) == "1"
    assert _source_git(tmp_path, {}, "macos_git_placeholder /opt/homebrew/bin/git") == "1"
    no_tools = (
        not any(
            Path(d, "usr/bin/git").is_file()
            for d in (
                "/Applications/Xcode.app/Contents/Developer",
                "/Library/Developer/CommandLineTools",
            )
        )
        and not Path("/var/db/xcode_select_link").exists()
    )
    if no_tools:
        assert _source_git(tmp_path, {}, check) == "0"
