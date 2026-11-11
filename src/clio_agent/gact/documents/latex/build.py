"""The one LaTeX build: Tectonic on a staged project, with typed diagnostics (LaTeX plan rule 1).

The rendition service and the ``latex_build`` tool both call :func:`build_project`; nothing
else runs Tectonic. A build:

- stages the entry's folder (:mod:`.project`) into a disposable directory under the
  workspace's scratch root;
- runs the pinned Tectonic with ``--untrusted`` (no shell escape), ``--synctex``,
  ``--keep-logs``, the pinned ``--bundle`` and ``--makefile-rules``, inside the OS write fence;
- reaches the network only for the bundle's hosts (ledger L6): the fence's per-child egress
  channel is limited to :data:`~clio_agent.runtime.tectonic.BUNDLE_HOSTS`. Where no fence is
  active the result says so (``network``), rather than claiming a restriction it did not have;
- reads the dependency list into the set of project files the run read, with their hashes;
- returns the PDF, SyncTeX file, page count and typed diagnostics. A build with errors or
  broken references is failed even when Tectonic exits cleanly.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from clio_agent.gact.documents.latex.diagnostics import (
    Diagnostic,
    build_failed,
    parse_diagnostics,
)
from clio_agent.gact.documents.latex.project import StagedProject, stage_project
from clio_agent.runtime.document_stack.process import inherit_parent_access, scratch_root

__all__ = ["BuildResult", "LatexBuildError", "build_project", "read_dependencies"]

_TIMEOUT_SECONDS = 600.0


class LatexBuildError(RuntimeError):
    """The build could not run at all (no Tectonic, no fence, a timeout); ``reason`` is typed."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass
class BuildResult:
    """What one build produced, read from its directory before it is removed."""

    entry: str
    ok: bool
    exit_code: int
    diagnostics: list[Diagnostic]
    sources: dict[str, str]
    read: dict[str, str]
    pdf: bytes = b""
    synctex: bytes = b""
    page_count: int = 0
    build_root: str = ""
    network: str = ""
    output: str = ""
    notes: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        """Plain data for tool results: no bytes."""
        return {
            "entry": self.entry,
            "ok": self.ok,
            "page_count": self.page_count,
            "diagnostics": [item.as_dict() for item in self.diagnostics],
            "read": sorted(self.read),
            "network": self.network,
            "notes": list(self.notes),
        }


def read_dependencies(
    rules: str, staged: StagedProject, *, out_dir: Path | None = None
) -> dict[str, str]:
    """The project files a ``--makefile-rules`` file lists, with their staged hashes.

    Tectonic 0.17 names the entry relative to the working folder but prefixes the other inputs
    with the output folder although it read them from the sources, so a path is taken relative
    to either folder.
    """
    tokens = rules.replace("\\\n", " ").split()
    bases = [base for base in (staged.root, out_dir) if base is not None]
    read: dict[str, str] = {}
    for token in tokens:
        raw = token.rstrip(":")
        if not raw:
            continue
        names = [raw.removeprefix("./")]
        path = Path(raw)
        if path.is_absolute():
            for base in bases:
                try:
                    names.append(path.relative_to(base).as_posix())
                except ValueError:
                    continue
        for name in names:
            if name in staged.sources:
                read[name] = staged.sources[name]
                break
    if staged.entry in staged.sources:
        read.setdefault(staged.entry, staged.sources[staged.entry])
    return read


def _page_count(pdf: bytes) -> int:
    if not pdf:
        return 0
    import io  # noqa: PLC0415 - only for a successful build

    from pypdf import PdfReader  # noqa: PLC0415 - heavy import on demand

    return len(PdfReader(io.BytesIO(pdf)).pages)


def _run(
    command: list[str], *, cwd: Path, workspace_root: Path, cache_dir: Path, timeout: float
) -> tuple[int, str, str]:
    """Run Tectonic in the fence with bundle-only egress. Returns ``(code, output, network)``."""
    from clio_agent.runtime import net_chokepoint, sandbox  # noqa: PLC0415 - heavy, on demand
    from clio_agent.runtime.tectonic import BUNDLE_HOSTS  # noqa: PLC0415

    write_roots = (
        *sandbox.effective_write_roots(sandbox.PROFILE_SHELL, workspace_root=str(workspace_root)),
        cache_dir,
        cwd,
    )
    try:
        confined = sandbox.wrap_confined(
            command[0],
            command[1:],
            write_roots=write_roots,
            net_policy=sandbox.NET_ALLOW_RECORD,
            profile=sandbox.PROFILE_SHELL,
            cwd=cwd,
        )
    except sandbox.SandboxCompositionError as exc:
        raise LatexBuildError("sandbox_composition_failed", str(exc)) from exc
    child = str(confined.result.details.get("net_child_id") or "")
    if child:
        net_chokepoint.restrict_child_hosts(child, BUNDLE_HOSTS)
        network = "bundle_hosts"
    else:
        reason = confined.result.reason or confined.result.mechanism or "no_fence"
        network = f"unrestricted: no egress channel ({reason})"
    env = {**os.environ, **confined.env_overlay, "TECTONIC_CACHE_DIR": str(cache_dir)}
    flags: dict[str, Any] = {}
    if os.name == "nt":
        flags["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    try:
        completed = subprocess.run(
            [confined.command, *confined.args],
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            **confined.popen_kwargs,
            **flags,
        )
    except subprocess.TimeoutExpired as exc:
        raise LatexBuildError(
            "latex_build_timeout", f"the build exceeded {timeout:g} seconds"
        ) from exc
    except OSError as exc:
        raise LatexBuildError("latex_build_failed_to_start", str(exc)) from exc
    finally:
        if child:
            net_chokepoint.release_child_hosts(child)
        confined.release()
    return completed.returncode, (completed.stdout or "") + (completed.stderr or ""), network


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def build_project(
    entry: Path,
    *,
    workspace_root: Path,
    timeout: float = _TIMEOUT_SECONDS,
) -> BuildResult:
    """Build the project ``entry`` belongs to.

    Raises:
        ProjectError: the project cannot be staged (no entry, too large).
        LatexBuildError: Tectonic is unavailable, the fence cannot compose, or the build timed
            out.
    """
    from clio_agent.runtime.tectonic import (  # noqa: PLC0415 - provisioning on demand
        BUNDLE_URL,
        TectonicMissing,
        package_cache_dir,
        tectonic_executable,
    )

    try:
        tectonic = tectonic_executable()
    except TectonicMissing as exc:
        raise LatexBuildError(exc.reason, str(exc)) from exc
    cache_dir = package_cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="latex-", dir=scratch_root(workspace_root)) as raw:
        build_root = Path(raw)
        # A different sandbox user may run the build on Windows; keep the folder reachable.
        inherit_parent_access(build_root)
        staged = stage_project(entry, build_root / "src")
        out = build_root / "out"
        out.mkdir()
        rules = build_root / "deps.mk"
        command = [
            str(tectonic),
            "--untrusted",
            "--synctex",
            "--keep-logs",
            "--bundle",
            BUNDLE_URL,
            "--makefile-rules",
            str(rules),
            "--outdir",
            str(out),
            staged.entry,
        ]
        code, output, network = _run(
            command,
            cwd=staged.root,
            workspace_root=workspace_root,
            cache_dir=cache_dir,
            timeout=timeout,
        )
        stem = Path(staged.entry).stem
        pdf_path = out / f"{stem}.pdf"
        pdf = pdf_path.read_bytes() if pdf_path.is_file() else b""
        synctex_path = out / f"{stem}.synctex.gz"
        synctex = synctex_path.read_bytes() if synctex_path.is_file() else b""
        diagnostics = parse_diagnostics(
            output,
            log=_read(out / f"{stem}.log"),
            blg=_read(out / f"{stem}.blg"),
            texts=staged.texts(),
        )
        if code and not any(item.kind in {"error", "missing_file"} for item in diagnostics):
            diagnostics.append(
                Diagnostic(
                    "error",
                    f"Tectonic exited {code} without a located error; see the build output.",
                )
            )
        read = read_dependencies(_read(rules), staged, out_dir=out) if rules.is_file() else {}
        result = BuildResult(
            entry=staged.entry,
            ok=bool(pdf) and code == 0 and not build_failed(diagnostics),
            exit_code=code,
            diagnostics=diagnostics,
            sources=dict(staged.sources),
            read=read or {staged.entry: staged.sources[staged.entry]},
            pdf=pdf,
            synctex=synctex,
            page_count=_page_count(pdf),
            build_root=str(build_root),
            network=network,
            output=output[-20_000:],
        )
        if not rules.is_file():
            result.notes.append("dependency_list_missing: the read set is the entry file only")
        return result
