"""Complete the Python-distributed Codex client with its matching Windows helpers.

The Python wheel ships codex.exe alone. Windows protected execution also needs
the command runner and setup helper from the SAME official Codex release. This
module owns their installation; it never provisions accounts or relaxes policy.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

import httpx

_HELPERS = ("codex-command-runner", "codex-windows-sandbox-setup")
_RECEIPT = "clio-windows-helpers.json"
_MAX_BYTES = 64 * 1024 * 1024
_LOCK = threading.RLock()


class CodexWindowsHelpersError(RuntimeError):
    """A matching, verified set of Windows execution helpers could not be installed."""


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _target(binary: Path) -> str:
    # Match the executable, not the host: x64 Codex can run under ARM64 emulation.
    with binary.open("rb") as stream:
        header = stream.read(64)
        if len(header) != 64 or header[:2] != b"MZ":
            raise CodexWindowsHelpersError("Managed Codex executable has no Windows PE header")
        offset = int.from_bytes(header[60:64], "little")
        if not 64 <= offset <= 1024 * 1024:
            raise CodexWindowsHelpersError("Managed Codex executable has an invalid PE offset")
        stream.seek(offset)
        pe = stream.read(6)
    targets = {0x8664: "x86_64", 0xAA64: "aarch64"}
    machine = int.from_bytes(pe[4:6], "little")
    if pe[:4] != b"PE\0\0" or machine not in targets:
        raise CodexWindowsHelpersError("Unsupported Windows Codex executable architecture")
    return f"{targets[machine]}-pc-windows-msvc"


def _cached(directory: Path, version: str, target: str) -> bool:
    try:
        receipt = json.loads((directory / _RECEIPT).read_text(encoding="utf-8"))
        if receipt["version"] != version or receipt["target"] != target:
            return False
        return all(
            _sha256(directory / f"{name}.exe") == receipt["sha256"][name] for name in _HELPERS
        )
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _release_assets(client: httpx.Client, version: str, target: str) -> dict[str, dict[str, Any]]:
    tag = f"rust-v{version}"
    response = client.get(f"https://api.github.com/repos/openai/codex/releases/tags/{tag}")
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("tag_name") != tag:
        raise CodexWindowsHelpersError(f"Official Codex release {tag} did not match the client")
    assets = payload.get("assets")
    if not isinstance(assets, list):
        raise CodexWindowsHelpersError(f"Official Codex release {tag} has no asset inventory")
    selected: dict[str, dict[str, Any]] = {}
    for name in _HELPERS:
        asset_name = f"{name}-{target}.exe"
        matches = [
            item for item in assets if isinstance(item, dict) and item.get("name") == asset_name
        ]
        if len(matches) != 1:
            raise CodexWindowsHelpersError(f"Matching Codex helper {asset_name} is unavailable")
        asset = matches[0]
        digest = asset.get("digest", "")
        size = asset.get("size")
        url = f"https://github.com/openai/codex/releases/download/{tag}/{asset_name}"
        if (
            not isinstance(digest, str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
            or type(size) is not int
            or not 0 < size <= _MAX_BYTES
            or asset.get("browser_download_url") != url
        ):
            raise CodexWindowsHelpersError(
                f"Official Codex helper {asset_name} lacks verified metadata"
            )
        selected[name] = {"url": url, "size": size, "sha256": digest.removeprefix("sha256:")}
    return selected


def _download(client: httpx.Client, asset: dict[str, Any], output: Path) -> None:
    total = 0
    digest = hashlib.sha256()
    with client.stream("GET", asset["url"]) as response:
        response.raise_for_status()
        with output.open("wb") as stream:
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > asset["size"]:
                    raise CodexWindowsHelpersError("Codex helper exceeds its declared release size")
                digest.update(chunk)
                stream.write(chunk)
    if total != asset["size"] or digest.hexdigest() != asset["sha256"]:
        raise CodexWindowsHelpersError("Codex helper does not match the official release SHA-256")


def ensure_codex_windows_helpers(
    binary: Path, version: str, *, platform_name: str = sys.platform
) -> dict[str, Any]:
    """Install both exact-version official helpers beside a managed Windows Codex exe.

    Verify cached bytes before reuse, verify both downloads before replacing any
    executable, and record a receipt last. A failed download never replaces a
    working pair. No latest-version fallback or unrelated global helper is used.
    """
    if platform_name != "win32":
        return {"status": "not_required"}
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?", version):
        raise CodexWindowsHelpersError("Codex did not report a usable release version")
    if binary.name.lower() != "codex.exe" or not binary.is_file():
        raise CodexWindowsHelpersError("Managed Codex executable is missing")
    target = _target(binary)
    directory = binary.parent
    with _LOCK:
        if _cached(directory, version, target):
            return {"status": "available", "version": version, "target": target}
        try:
            with (
                httpx.Client(
                    follow_redirects=True,
                    timeout=120,
                    headers={
                        "User-Agent": "CLIO-Codex-runtime",
                        "Accept": "application/vnd.github+json",
                    },
                ) as client,
                tempfile.TemporaryDirectory(prefix=".clio-helpers-", dir=directory) as staging,
            ):
                assets = _release_assets(client, version, target)
                stage = Path(staging)
                for name, asset in assets.items():
                    _download(client, asset, stage / f"{name}.exe")
                receipt = {
                    "version": version,
                    "target": target,
                    "sha256": {name: asset["sha256"] for name, asset in assets.items()},
                }
                (stage / _RECEIPT).write_text(json.dumps(receipt, indent=2), encoding="utf-8")
                for name in _HELPERS:
                    (stage / f"{name}.exe").replace(directory / f"{name}.exe")
                (stage / _RECEIPT).replace(directory / _RECEIPT)
        except (httpx.HTTPError, OSError, ValueError) as exc:
            raise CodexWindowsHelpersError(
                f"Could not prepare Codex {version} Windows helpers: {exc}"
            ) from exc
    return {"status": "available", "version": version, "target": target}


def ensure_bundled_codex_windows_helpers() -> dict[str, Any]:
    """Complete the active Python wheel's Windows client during install or update."""
    if sys.platform != "win32":
        return {"status": "not_required"}
    import codex_cli_bin  # noqa: PLC0415

    from clio_agent.runtime.sandbox_codex import _read_codex_version  # noqa: PLC0415

    if not codex_cli_bin.__file__:
        raise CodexWindowsHelpersError("Codex Python distribution has no package location")
    binary = Path(codex_cli_bin.__file__).parent / "bin" / "codex.exe"
    return ensure_codex_windows_helpers(binary, _read_codex_version(str(binary)))
