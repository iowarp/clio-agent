"""Qualify native Windows MXC before selecting it, without accounts or elevation."""

from __future__ import annotations

import json
import logging
import ntpath
import platform
import socket
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from clio_agent import paths

logger = logging.getLogger(__name__)
MXC_MIN_VERSION = (0, 162, 1)
_RECEIPT_VERSION = 2
_CHECKS = {
    "workspace_write",
    "outside_write_denied",
    "source_read",
    "source_write_denied",
    "private_read_denied",
    "descendant_read_denied",
    "direct_network_denied",
}

# This code runs inside the actual sandbox. Positive witnesses distinguish a
# working fence from a failed launcher; explicit denials must be PermissionError.
_PROBE_CODE = """
import json, pathlib, socket, subprocess, sys
root = pathlib.Path.cwd()
checks = {}
(root / 'inside.txt').write_text('allowed')
checks['workspace_write'] = True
checks['source_read'] = (root / 'source/data.txt').read_text() == 'approved'
for name, path, operation in (
    ('outside_write_denied', root.parent / 'outside.txt', 'write'),
    ('source_write_denied', root / 'source/data.txt', 'write'),
    ('private_read_denied', root / 'private/secret.txt', 'read'),
):
    try:
        path.read_text() if operation == 'read' else path.write_text('changed')
        checks[name] = False
    except PermissionError:
        checks[name] = True
child = subprocess.run([sys.executable, '-I', '-B', '-c',
    "from pathlib import Path; import sys\\ntry: Path('private/secret.txt').read_text()"
    "\\nexcept PermissionError: sys.exit(42)"],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
checks['descendant_read_denied'] = child.returncode == 42
try:
    socket.create_connection(('192.0.2.1', 9), timeout=2).close()
    checks['direct_network_denied'] = False
except PermissionError:
    checks['direct_network_denied'] = True
except OSError:
    checks['direct_network_denied'] = False
(root / 'checks.json').write_text(json.dumps(checks))
"""


def _receipt_path() -> Path:
    return paths.user_config_dir() / "sandbox" / "codex-mxc-verified.json"


def _binary_identity_path(executable: Path) -> str:
    """Compare Windows spelling aliases without changing the path used for I/O."""
    name = str(executable)
    if sys.platform != "win32":
        return name
    if name[:8].lower() == "\\\\?\\unc\\":
        name = "\\\\" + name[8:]
    elif name.startswith("\\\\?\\") and ntpath.splitdrive(name[4:])[0].endswith(":"):
        name = name[4:]
    return ntpath.normcase(name)


def _host_build() -> str:
    """Read the native Windows revision independently of CLIO's platform cache."""
    if sys.platform == "win32":
        import winreg  # noqa: PLC0415

        native = sys.getwindowsversion().platform_version
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"
        ) as key:
            revision = winreg.QueryValueEx(key, "UBR")[0]
        return ".".join(str(part) for part in (*native, revision))
    return platform.version()


def _identity(binary: str, version: str = "") -> dict[str, Any]:
    executable = Path(binary).resolve(strict=True)
    stat = executable.stat()
    return {
        "receipt_version": _RECEIPT_VERSION,
        "binary": _binary_identity_path(executable),
        "size": stat.st_size,
        "modified_ns": stat.st_mtime_ns,
        "file_id": stat.st_ino,
        "host": socket.gethostname().casefold(),
        "build": _host_build(),
        "version": version,
    }


def _read_receipt() -> dict[str, Any]:
    try:
        value = json.loads(_receipt_path().read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _matching_receipt(binary: str, version: str = "") -> dict[str, Any]:
    receipt = _read_receipt()
    recorded_identity = receipt.get("identity")
    if (
        not isinstance(recorded_identity, dict)
        or not isinstance(recorded_identity.get("version"), str)
        or receipt.get("status") not in ("verified", "unavailable")
        or receipt.get("reason")
        not in (
            ("codex_mxc_verified",)
            if receipt["status"] == "verified"
            else ("codex_mxc_unverified", "codex_mxc_probe_failed")
        )
    ):
        return {}
    try:
        identity = _identity(binary, version or recorded_identity["version"])
    except (OSError, ValueError, TypeError):
        return {}
    return receipt if receipt.get("identity") == identity else {}


def mxc_ready(binary: str, *, version: str = "") -> bool:
    """Reuse only a successful proof for this executable, host and Windows update."""
    return _matching_receipt(binary, version).get("status") == "verified"


def prepare_mxc(
    binary: str,
    version: str,
    *,
    progress: Callable[[str], None] | None = None,
    probe: Callable[[str], bool] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Probe strict MXC once per client/host revision, retaining a safe legacy fallback.

    Never falls back after a user command starts. Unsupported or unverified MXC
    is rejected during setup; the caller may then qualify legacy elevated mode.
    Failed probes are cached too, so unsupported hosts do not repeat slow probes.
    """
    from clio_agent.runtime.sandbox_codex import parse_version  # noqa: PLC0415

    if sys.platform != "win32" or parse_version(version) < MXC_MIN_VERSION:
        return {"status": "unavailable", "reason": "codex_mxc_version_unsupported"}
    cached = _matching_receipt(binary, version)
    if cached and not force:
        if progress:
            progress(f"Protected execution: MXC {cached['status']} (cached check).")
        return {"status": cached["status"], "reason": cached["reason"]}
    if progress:
        progress("Checking native Windows protected execution (MXC; no administrator setup)...")
    try:
        identity = _identity(binary, version)
        failed = False
        try:
            verified = (probe or _probe_mxc)(binary)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            logger.warning("MXC qualification unavailable: %s", type(exc).__name__)
            verified, failed = False, True
        result = {
            "status": "verified" if verified else "unavailable",
            "reason": (
                "codex_mxc_verified"
                if verified
                else "codex_mxc_probe_failed"
                if failed
                else "codex_mxc_unverified"
            ),
        }
        receipt = _receipt_path()
        receipt.parent.mkdir(parents=True, exist_ok=True)
        # Atomic replacement prevents a concurrent reader accepting a partial file.
        with tempfile.NamedTemporaryFile("w", dir=receipt.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump({"identity": identity, **result}, stream)
        try:
            temporary.replace(receipt)
        finally:
            temporary.unlink(missing_ok=True)
        return result
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        logger.warning("MXC qualification unavailable: %s", type(exc).__name__)
        return {"status": "unavailable", "reason": "codex_mxc_probe_failed"}


def _probe_mxc(binary: str) -> bool:
    """Launch real CLIO policy and require filesystem and network denial witnesses."""
    from clio_agent.runtime.sandbox_codex import (  # noqa: PLC0415
        codex_prefix,
        synthesize_codex_profile,
        write_codex_layer,
    )

    with tempfile.TemporaryDirectory(prefix="clio-mxc-check-") as directory:
        root = Path(directory)
        workspace = root / "workspace"
        workspace.mkdir()
        source = workspace / "source"
        private = workspace / "private"
        source.mkdir()
        private.mkdir()
        (source / "data.txt").write_text("approved")
        (private / "secret.txt").write_text("private-fixture")
        profile = synthesize_codex_profile([workspace], mxc=True)
        profile["filesystem"].update({str(source): "read", str(private): "deny"})
        layer = write_codex_layer("clio-mxc-check", profile, elevated=False, windows_sandbox="mxc")
        argv = codex_prefix(
            binary, "clio-mxc-check", workspace, layer_name=layer, include_managed_config=True
        )
        process = subprocess.run(
            [*argv, sys.executable, "-I", "-B", "-c", _PROBE_CODE],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if process.returncode != 0 or not (workspace / "checks.json").is_file():
            logger.info("MXC policy check did not complete returncode=%s", process.returncode)
            return False
        checks = json.loads((workspace / "checks.json").read_text())
        logger.info(
            "MXC policy witnesses: %s",
            {
                name: isinstance(checks, dict) and checks.get(name) is True
                for name in sorted(_CHECKS)
            },
        )
        return (
            isinstance(checks, dict)
            and set(checks) == _CHECKS
            and all(value is True for value in checks.values())
            and (workspace / "inside.txt").read_text() == "allowed"
            and (source / "data.txt").read_text() == "approved"
            and not (root / "outside.txt").exists()
        )
