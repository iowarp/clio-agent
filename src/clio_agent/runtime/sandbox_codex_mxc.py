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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout

from clio_agent import paths
from clio_agent.runtime.sandbox_codex_probe import run_probe

logger = logging.getLogger(__name__)
#: Earlier clients reject CLIO's policy (``:root`` reads plus denied paths) on any machine
#: with a mount the sandbox cannot open, such as a mapped network share. Codex 0.166.0
#: dropped that preflight and leaves unrelated network shares out of the default reads.
MXC_MIN_VERSION = (0, 166, 0)
_RECEIPT_VERSION = 2
REASON_MXC_VERIFIED = "codex_mxc_verified"
#: Codex ran the probe, but a filesystem or network witness did not hold.
REASON_MXC_UNVERIFIED = "codex_mxc_unverified"
#: Codex exited before the probe wrote its witnesses (a rejected policy, a launcher error).
REASON_MXC_LAUNCH_FAILED = "codex_mxc_launch_failed"
REASON_MXC_PROBE_TIMEOUT = "codex_mxc_probe_timeout"
#: The probe could not be prepared or launched at all.
REASON_MXC_PROBE_FAILED = "codex_mxc_probe_failed"
#: CLIO runs on Microsoft Store (MSIX-packaged) Python, which MXC refuses to launch.
REASON_MXC_PACKAGED_PYTHON = "codex_mxc_packaged_python"
PACKAGED_PYTHON_ACTION = (
    "Reinstall CLIO with the installer, which uses uv-managed Python; for a source "
    "checkout run `uv sync --managed-python` (or set UV_MANAGED_PYTHON=1) to rebuild "
    "its environment, then retry Set up protected execution."
)
_UNAVAILABLE_REASONS = (
    REASON_MXC_UNVERIFIED,
    REASON_MXC_LAUNCH_FAILED,
    REASON_MXC_PROBE_TIMEOUT,
    REASON_MXC_PROBE_FAILED,
)
_PROBE_TIMEOUT_S = 30
_SETUP_LOCK_TIMEOUT_S = 40.0  # The native probe has a 30-second deadline, then a tree kill.
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


@dataclass(frozen=True)
class MxcProbe:
    """One qualification: whether MXC enforced CLIO's policy, why not, and Codex's words."""

    verified: bool
    reason: str
    detail: str = ""


def _receipt_path() -> Path:
    return paths.user_config_dir() / "sandbox" / "codex-mxc-verified.json"


def packaged_python() -> str:
    """The Microsoft Store (MSIX) Python CLIO runs on, or ``""``.

    A virtual environment hides its base: ``sys.executable`` is the venv's launcher,
    while ``sys._base_executable`` and ``sys.base_prefix`` name the Store alias and
    package. MXC refuses every packaged executable, so a probe or a confined Python
    child started from such an interpreter can never launch.
    """
    if sys.platform != "win32":
        return ""
    for candidate in (sys.executable, getattr(sys, "_base_executable", ""), sys.base_prefix):
        folded = "\\" + candidate.replace("/", "\\").casefold() + "\\"
        if candidate and "\\windowsapps\\" in folded:
            return candidate
    return ""


def packaged_python_outcome(interpreter: str) -> dict[str, Any]:
    """The typed refusal for a packaged interpreter, before any Codex launch."""
    return {
        "status": "unavailable",
        "reason": REASON_MXC_PACKAGED_PYTHON,
        "detail": (
            f"CLIO runs on Microsoft Store Python ({interpreter}). Windows protected "
            "execution cannot launch packaged apps, so CLIO's sandboxed Python never starts"
        ),
    }


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
    """Read the native Windows build and revision independently of CLIO's platform cache.

    The build number comes from the registry: ``platform_version`` reports the base
    build of a servicing family (26100 on a 26200 system), not the installed build.
    """
    if sys.platform == "win32":
        import winreg  # noqa: PLC0415

        major, minor, build = sys.getwindowsversion().platform_version
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"
        ) as key:
            revision = winreg.QueryValueEx(key, "UBR")[0]
            try:
                build = int(winreg.QueryValueEx(key, "CurrentBuildNumber")[0])
            except (OSError, ValueError):
                logger.info("reason=windows_build_from_platform_version build=%s", build)
        return ".".join(str(part) for part in (major, minor, build, revision))
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
        not in ((REASON_MXC_VERIFIED,) if receipt["status"] == "verified" else _UNAVAILABLE_REASONS)
    ):
        return {}
    try:
        identity = _identity(binary, version or recorded_identity["version"])
    except (OSError, ValueError, TypeError):
        return {}
    return receipt if receipt.get("identity") == identity else {}


def mxc_ready(binary: str, *, version: str = "") -> bool:
    """Reuse only a successful proof for this executable, host and Windows update."""
    if packaged_python():
        return False
    return _matching_receipt(binary, version).get("status") == "verified"


def _outcome(receipt: dict[str, Any]) -> dict[str, Any]:
    outcome = {"status": receipt["status"], "reason": receipt["reason"]}
    if receipt.get("detail"):
        outcome["detail"] = receipt["detail"]
    return outcome


def _reusable(cached: dict[str, Any], *, force: bool, retry_unavailable: bool) -> bool:
    if not cached or force:
        return False
    return cached["status"] == "verified" or not retry_unavailable


def prepare_mxc(
    binary: str,
    version: str,
    *,
    progress: Callable[[str], None] | None = None,
    probe: Callable[[str], MxcProbe | bool] | None = None,
    force: bool = False,
    retry_unavailable: bool = False,
) -> dict[str, Any]:
    """Probe strict MXC once per client/host revision, retaining a safe legacy fallback.

    Never falls back after a user command starts. Unsupported or unverified MXC
    is rejected during setup; the caller may then qualify legacy elevated mode.
    Failed probes are cached too, so a launch does not repeat a slow probe. An
    explicit setup passes ``retry_unavailable`` to probe again: the host may have
    changed (a removed mount, an unlocked volume) without a new client or build.
    Processes sharing this user's receipt serialize verification and reread the
    result after acquiring ownership. A busy verifier never triggers legacy setup.
    """
    from clio_agent.runtime.sandbox_codex import parse_version  # noqa: PLC0415

    if sys.platform != "win32" or parse_version(version) < MXC_MIN_VERSION:
        return {"status": "unavailable", "reason": "codex_mxc_version_unsupported"}
    # No receipt: the interpreter is not part of its identity, and a rebuilt
    # environment must qualify on the next boot without an explicit retry.
    if interpreter := packaged_python():
        outcome = packaged_python_outcome(interpreter)
        logger.warning("reason=%s %s", outcome["reason"], outcome["detail"])
        if progress:
            progress(f"Protected execution unavailable: {outcome['detail']}.")
        return outcome
    cached = _matching_receipt(binary, version)
    if _reusable(cached, force=force, retry_unavailable=retry_unavailable):
        if progress:
            progress(f"Protected execution: MXC {cached['status']} (cached check).")
        return _outcome(cached)
    try:
        receipt = _receipt_path()
        receipt.parent.mkdir(parents=True, exist_ok=True)
        lock = FileLock(str(receipt.with_suffix(".lock")), timeout=_SETUP_LOCK_TIMEOUT_S)
        try:
            lock.acquire(timeout=0)
        except Timeout:
            if progress:
                progress(
                    "Another CLIO process is verifying protected execution; waiting for its result..."
                )
            lock.acquire()
        try:
            return _prepare_mxc_locked(
                binary,
                version,
                progress=progress,
                probe=probe,
                force=force,
                retry_unavailable=retry_unavailable,
            )
        finally:
            lock.release()
    except Timeout:
        return {"status": "busy", "reason": "codex_mxc_setup_in_progress"}
    except OSError as exc:
        logger.warning(
            "reason=%s MXC setup coordination unavailable: %r", REASON_MXC_PROBE_FAILED, exc
        )
        return {"status": "unavailable", "reason": REASON_MXC_PROBE_FAILED, "detail": repr(exc)}


def _run_qualification(binary: str, probe: Callable[[str], MxcProbe | bool] | None) -> MxcProbe:
    try:
        outcome = (probe or _probe_mxc)(binary)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return MxcProbe(False, REASON_MXC_PROBE_FAILED, repr(exc))
    if isinstance(outcome, MxcProbe):
        return outcome
    return (
        MxcProbe(True, REASON_MXC_VERIFIED) if outcome else MxcProbe(False, REASON_MXC_UNVERIFIED)
    )


def _prepare_mxc_locked(
    binary: str,
    version: str,
    *,
    progress: Callable[[str], None] | None,
    probe: Callable[[str], MxcProbe | bool] | None,
    force: bool,
    retry_unavailable: bool,
) -> dict[str, Any]:
    """Reuse the preceding owner's result or qualify once while holding the lock."""
    cached = _matching_receipt(binary, version)
    if _reusable(cached, force=force, retry_unavailable=retry_unavailable):
        if progress:
            progress(f"Protected execution: MXC {cached['status']} (shared check).")
        return _outcome(cached)
    if progress:
        progress("Checking native Windows protected execution (MXC; no administrator setup)...")
    try:
        identity = _identity(binary, version)
        outcome = _run_qualification(binary, probe)
        if not outcome.verified:
            logger.warning(
                "reason=%s MXC qualification unavailable: %s", outcome.reason, outcome.detail
            )
        result = {
            "status": "verified" if outcome.verified else "unavailable",
            "reason": outcome.reason,
        }
        if outcome.detail:
            result["detail"] = outcome.detail
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
        logger.warning("reason=%s MXC qualification unavailable: %r", REASON_MXC_PROBE_FAILED, exc)
        return {"status": "unavailable", "reason": REASON_MXC_PROBE_FAILED, "detail": repr(exc)}


def _probe_mxc(binary: str) -> MxcProbe:
    """Launch real CLIO policy and require filesystem and network denial witnesses."""
    from clio_agent.runtime.sandbox_codex import (  # noqa: PLC0415
        codex_prefix,
        synthesize_codex_profile,
        write_codex_layer,
    )

    # A probe killed at its deadline can leave a handle behind for a moment; cleanup
    # must never replace the probe's own outcome.
    with tempfile.TemporaryDirectory(
        prefix="clio-mxc-check-", ignore_cleanup_errors=True
    ) as directory:
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
        run = run_probe(
            [*argv, sys.executable, "-I", "-B", "-c", _PROBE_CODE],
            timeout=_PROBE_TIMEOUT_S,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if run.timed_out:
            return MxcProbe(False, REASON_MXC_PROBE_TIMEOUT, run.describe())
        if run.returncode != 0 or not (workspace / "checks.json").is_file():
            return MxcProbe(False, REASON_MXC_LAUNCH_FAILED, run.describe())
        checks = json.loads((workspace / "checks.json").read_text())
        witnesses = {
            name: isinstance(checks, dict) and checks.get(name) is True for name in sorted(_CHECKS)
        }
        logger.info("MXC policy witnesses: %s", witnesses)
        failed = [name for name, held in witnesses.items() if not held]
        if not isinstance(checks, dict) or set(checks) != _CHECKS:
            failed.append("witness_set")
        if (workspace / "inside.txt").read_text() != "allowed":
            failed.append("inside_file")
        if (source / "data.txt").read_text() != "approved":
            failed.append("source_unchanged")
        if (root / "outside.txt").exists():
            failed.append("outside_absent")
        if failed:
            return MxcProbe(False, REASON_MXC_UNVERIFIED, "witnesses failed: " + ", ".join(failed))
        return MxcProbe(True, REASON_MXC_VERIFIED)
