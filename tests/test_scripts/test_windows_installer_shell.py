"""The Windows installers work in Windows PowerShell 5.1, the shell `irm ... | iex` runs in.

Windows PowerShell 5.1 drops the double quotes inside an argument it passes to a native
program, so inline Python such as ``python -c 'f("x")'`` reaches Python as ``f(x)``
(#1913). Its progress bar also slows a large ``Invoke-WebRequest`` many times over, and
``exit`` in a script run from a URL closes the user's window. CI runs the scripts under
5.1 on Windows (install-matrix.yml); these checks keep the rules on every platform.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = sorted((ROOT / "install").glob("*.ps1"))


def _code_lines(path: Path) -> list[tuple[int, str]]:
    return [
        (number, line)
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if not line.lstrip().startswith("#")
    ]


def test_no_inline_code_with_double_quotes_reaches_a_native_program() -> None:
    offenders = []
    for path in SCRIPTS:
        for number, line in _code_lines(path):
            # -c '...' with a double quote inside, or -c $variable (a here-string of code).
            if re.search(r"""\s-c\s+'[^']*"[^']*'""", line) or re.search(r"\s-c\s+\$\w", line):
                offenders.append(f"{path.name}:{number}: {line.strip()}")
    assert not offenders, "\n".join(offenders)


def test_install_ps1_resolves_releases_in_powershell() -> None:
    script = (ROOT / "install/install.ps1").read_text(encoding="utf-8")
    assert "function Resolve-ClioRelease" in script
    assert "--with packaging" not in script
    # The desktop and the agent use the one resolver, not GitHub's "latest" marker.
    assert "releases/latest" not in script
    assert script.count("Resolve-ClioRelease $OptDev") == 2


def test_progress_bar_is_off_before_any_web_request() -> None:
    lines = _code_lines(ROOT / "install/install.ps1")
    off = next(n for n, line in lines if "$ProgressPreference = 'SilentlyContinue'" in line)
    first_request = next(
        n for n, line in lines if "Invoke-WebRequest" in line or "Invoke-RestMethod" in line
    )
    assert off < first_request


def test_large_downloads_resume_through_curl() -> None:
    script = (ROOT / "install/install.ps1").read_text(encoding="utf-8")
    body = script[script.index("function Save-ClioDownload") :]
    body = body[: body.index("\n}\n")]
    for flag in ("--retry", "--continue-at -", "--fail", "--location"):
        assert flag in body
    assert "Save-ClioDownload $webUrl $webZip" in script
    # The desktop's downloads run at once, each a curl.exe that retries and resumes.
    body = script[script.index("function Save-ClioDownloads") :]
    body = body[: body.index("\n}\n")]
    for flag in ("--retry", "--continue-at -", "--fail", "--location"):
        assert flag in body
    assert "Save-ClioDownloads $items $total" in script


def test_desktop_hands_the_plan_downloads_to_the_setup() -> None:
    script = (ROOT / "install/install.ps1").read_text(encoding="utf-8")
    body = script[script.index("function Install-ClioDesktopRelease") :]
    body = body[: body.index("\n}\n")]
    assert "install-plan.$triple.bundled.txt" in body
    assert "$env:CLIO_INSTALL_DOWNLOADS = $downloads" in body
    assert "$env:CLIO_INSTALL_DOWNLOADS = $previousDownloads" in body
    # Hashed as the bytes arrive: verify compares, it does not read the setup again
    # unless that inline hash disagrees.
    assert "$installer.Sha -ne $expected -and" in body


def test_install_ps1_never_exits_a_window_it_was_pasted_into() -> None:
    lines = _code_lines(ROOT / "install/install.ps1")
    exits = [(n, line) for n, line in lines if re.search(r"(^\s*|[;{]\s*)exit\b", line)]
    assert exits, "Exit-Installer still exits a script run as a file"
    assert all("if ($PSCommandPath) { exit $code }" in line for _, line in exits), exits


def test_short_commands_are_served_from_install() -> None:
    desktop_ps1 = (ROOT / "install/desktop.ps1").read_text(encoding="utf-8")
    desktop_sh = (ROOT / "install/desktop.sh").read_text(encoding="utf-8")
    assert "https://clio.iowarp.ai/install.ps1'))) -Desktop @args" in desktop_ps1
    assert "curl -fsSL https://clio.iowarp.ai/install.sh" in desktop_sh
    assert 'install.sh --desktop "$@"' in desktop_sh
    pages = (ROOT / ".github/workflows/pages.yml").read_text(encoding="utf-8")
    assert (
        "cp ../install/install.sh ../install/install.ps1 ../install/desktop.sh "
        "../install/desktop.ps1 public/"
    ) in pages
    for doc in ("README.md", "docs/INSTALL.md", "site/public/install.md"):
        text = (ROOT / doc).read_text(encoding="utf-8")
        assert "irm https://clio.iowarp.ai/desktop.ps1 | iex" in text, doc
        assert "curl -fsSL https://clio.iowarp.ai/desktop.sh | bash" in text, doc
