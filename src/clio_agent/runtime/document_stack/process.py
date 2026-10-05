"""Portable, bounded subprocess and native document-tool discovery."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any


class DocumentError(RuntimeError):
    """A document operation failed without producing verified output."""


def scratch_root(workspace: Path, *, create: bool = True) -> Path:
    """Create workspace-owned scratch storage without following an escaping link."""
    workspace = workspace.resolve()
    root = (workspace / ".tmp" / "clio-documents").resolve()
    if not root.is_relative_to(workspace):
        raise DocumentError("Document scratch storage escapes the active workspace")
    if create:
        root.mkdir(parents=True, exist_ok=True)
    return root


def run(
    command: list[str], *, cwd: Path, timeout: float = 120.0, env: dict[str, str] | None = None
) -> str:
    """Run argv without a shell and return stdout, or a bounded diagnostic."""
    flags: dict[str, Any] = {}
    if os.name == "nt":
        flags["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            **flags,
        )
    except OSError as exc:
        raise DocumentError(f"Could not run {Path(command[0]).name}: {exc}") from exc
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        import psutil

        try:
            children = psutil.Process(process.pid).children(recursive=True)
        except psutil.NoSuchProcess:
            children = []
        for child in reversed(children):
            try:
                child.kill()
            except psutil.NoSuchProcess:
                pass
        process.kill()
        process.communicate()
        raise DocumentError(f"{Path(command[0]).name} exceeded {timeout:g} seconds") from exc
    if process.returncode:
        detail = (stderr or stdout).strip()[-4000:]
        raise DocumentError(f"{Path(command[0]).name} exited {process.returncode}: {detail}")
    return stdout or stderr


def find_native(name: str) -> str | None:
    """Find a configured native tool, including standard desktop install paths."""
    override = os.environ.get(f"CLIO_DOCUMENT_{name.upper()}", "").strip()
    if override:
        candidate = Path(override).expanduser().resolve()
        if not candidate.is_file():
            raise DocumentError(f"CLIO_DOCUMENT_{name.upper()} is not an executable file")
        return str(candidate)
    found = shutil.which(name)
    if found:
        return found
    if name == "soffice":
        from importlib import import_module

        office = import_module(f"{__package__}.office" if __package__ else "office")
        roots = (
            [Path(os.environ["CLIO_DOCUMENT_OFFICE_ROOT"])]
            if os.environ.get("CLIO_DOCUMENT_OFFICE_ROOT")
            else []
        )
        if bundled := os.environ.get("GACT_BUNDLED_RUNTIME_DIR"):
            roots.append(Path(bundled) / "documents" / "office")
        roots.extend(
            parent / "documents" / "office" for parent in Path(sys.executable).resolve().parents[:4]
        )
        for root in roots:
            if private_candidate := office.executable(root):
                return str(private_candidate)
        candidates = [Path("/Applications/LibreOffice.app/Contents/MacOS/soffice")]
        if sys.platform == "win32":
            candidates.extend(
                Path(install_root) / "LibreOffice" / "program" / "soffice.com"
                for key in ("ProgramFiles", "ProgramFiles(x86)")
                if (install_root := os.environ.get(key))
            )
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate)
        return shutil.which("libreoffice")
    return None


def office_convert(
    source: Path,
    output_dir: Path,
    target: str,
    *,
    execute: Callable[[list[str], Path], None] | None = None,
    executable_path: str | None = None,
    workspace: Path | None = None,
) -> Path:
    """Convert a copy using a fresh LibreOffice profile; never overwrite the source."""
    executable = executable_path or find_native("soffice")
    if not executable:
        root = os.environ.get("CLIO_DOCUMENT_OFFICE_ROOT", "")
        if not root:
            raise DocumentError(
                "LibreOffice is unavailable; prepare Clio's execution runtime first"
            )
        from importlib import import_module

        office = import_module(f"{__package__}.office" if __package__ else "office")
        executable = office.ensure_office(Path(root))
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"{source.stem}.{target}"
    if output.exists() or output.resolve() == source.resolve():
        raise DocumentError(f"Refusing to overwrite {output}")
    profile_root = scratch_root(workspace) if workspace is not None else None
    if profile_root is None and (configured := os.environ.get("CLIO_DOCUMENT_SCRATCH")):
        profile_root = Path(configured).resolve()
    # Native Windows profile APIs still have MAX_PATH limits. Only the disposable
    # profile may need a short allowed TMP path; document staging stays in .tmp.
    if profile_root is not None and os.name == "nt" and len(str(profile_root)) > 80:
        profile_root = Path(os.environ.get("CLIO_DOCUMENT_SHORT_TEMP", tempfile.gettempdir()))
    temporary = tempfile.TemporaryDirectory(prefix="lo-", dir=profile_root)
    try:
        profile = Path(temporary.name).resolve() / "profile"
        profile.mkdir()
        (profile / "registrymodifications.xcu").write_text(
            '<oor:items xmlns:oor="http://openoffice.org/2001/registry">'
            '<item oor:path="/org.openoffice.Office.Common/Security/Scripting">'
            '<prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop>'
            "</item></oor:items>",
            encoding="utf-8",
        )
        command = [
            executable,
            f"-env:UserInstallation={profile.as_uri()}",
            "--headless",
            "--nologo",
            "--nodefault",
            "--nofirststartwizard",
            "--convert-to",
            target,
            "--outdir",
            str(output_dir),
            str(source),
        ]
        if execute is None:
            run(command, cwd=output_dir)
        else:
            execute(command, output_dir)
    finally:
        _cleanup_profile(temporary)
    if not output.is_file() or not output.stat().st_size:
        raise DocumentError("LibreOffice returned without producing a nonempty output file")
    return output


def _cleanup_profile(temporary: tempfile.TemporaryDirectory[str]) -> None:
    # Windows can release Office's profile handles just after the console exits.
    for attempt in range(20):
        try:
            if os.name == "nt":
                root = str(Path(temporary.name).resolve())
                extended = (
                    "\\\\?\\UNC\\" + root[2:] if root.startswith("\\\\") else "\\\\?\\" + root
                )
                try:
                    shutil.rmtree(extended)
                except FileNotFoundError:
                    pass
            temporary.cleanup()
            return
        except OSError as exc:
            if getattr(exc, "winerror", None) not in {5, 32, 145} or attempt == 19:
                raise DocumentError(f"Could not clean the private Office profile: {exc}") from exc
            time.sleep(0.1)
