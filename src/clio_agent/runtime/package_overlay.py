"""Packages CLIO installs at runtime live outside a sealed desktop runtime.

The desktop app ships a relocatable Python runtime inside its bundle. On macOS that
bundle is code-signed, so any file added to it (a provider SDK installed by the
person's Install action) breaks the seal, and a read-only install (``/Applications``
for a non-admin, ``/usr`` for a Linux package) cannot be written at all. Such a
runtime gets a *package overlay*: a user-writable directory under CLIO's data dir
that holds only what CLIO installed after the bundle was built. It is prepended to
``sys.path`` (``.pth`` files processed) when :mod:`clio_agent` is imported, which also
covers ``python -I`` processes that ignore ``PYTHONPATH``.

The overlay is keyed by the runtime's ``runtime.json``, so an updated app starts with
an empty overlay and the startup restore (:mod:`clio_agent.providers.support_restore`)
reinstalls recorded provider support against the new runtime. A Windows runtime whose
pure-Python distributions are zipped (``site-packages\\clio-pure.zip``) is sealed too:
an installer cannot see a distribution inside the zip, so an in-place install would
reinstall the runtime's own packages over the running backend's loaded extensions.
A runtime that is not sealed (a dev venv, a pip install) keeps installing in place.
``CLIO_AGENT_PACKAGE_OVERLAY=1`` forces the overlay and ``=0`` disables it.

This module is imported on every ``clio_agent`` import: keep it standard-library only
and cheap until a bundled runtime is detected.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
from collections.abc import Mapping
from pathlib import Path

#: Environment switch: ``1`` forces the overlay, ``0`` keeps installs in place.
OVERLAY_ENV = "CLIO_AGENT_PACKAGE_OVERLAY"

#: The zip ``install/zip_site_packages.py`` packs a Windows runtime's pure-Python
#: distributions (and their ``.dist-info``) into.
ZIPPED_SITE_PACKAGES = "clio-pure.zip"

_OVERLAY_PARENT = "runtime-packages"
#: Name prefix of an install's staging directory beside the overlay (see :func:`staging_dir`).
_STAGING_PREFIX = ".staging-"
#: A staging directory older than this belongs to a killed install and is pruned.
_STAGING_MAX_AGE_SECONDS = 6 * 3600
_activated: list[str] = []


def _runtime_root(executable: str | None = None) -> Path | None:
    """The bundled runtime root (the dir holding ``runtime.json``) of ``executable``."""
    resolved = Path(executable or sys.executable).resolve()
    return next(
        (parent for parent in resolved.parents if (parent / "runtime.json").is_file()), None
    )


def _site_packages() -> Path:
    stdlib = Path(os.__file__).resolve().parent
    return stdlib / "site-packages"


def _inside_app_bundle(path: Path) -> bool:
    parts = path.parts
    return any(
        part.endswith(".app") and index + 1 < len(parts) and parts[index + 1] == "Contents"
        for index, part in enumerate(parts)
    )


def overlay_in_use() -> bool:
    """Whether runtime installs for THIS interpreter go to the overlay, not its site-packages."""
    switch = os.environ.get(OVERLAY_ENV, "").strip()
    if switch in {"0", "1"}:
        return switch == "1"
    root = _runtime_root()
    if root is None:
        return False
    if sys.platform == "darwin" and _inside_app_bundle(root):
        return True
    site_packages = _site_packages()
    if (site_packages / ZIPPED_SITE_PACKAGES).is_file():
        return True
    return not os.access(site_packages, os.W_OK)


def _runtime_key() -> str:
    root = _runtime_root()
    identity = hashlib.sha256()
    if root is not None:
        identity.update((root / "runtime.json").read_bytes())
    else:
        identity.update(os.fsencode(Path(sys.prefix).resolve()))
    identity.update(f"|{sys.version}|{sys.platform}|{sys.implementation.cache_tag}".encode())
    return identity.hexdigest()[:16]


def overlay_dir() -> Path:
    """This runtime's overlay directory (it may not exist yet)."""
    from clio_agent import paths  # noqa: PLC0415 - only for a bundled runtime

    return paths.user_data_dir(env=os.environ) / _OVERLAY_PARENT / _runtime_key()


def overlay_install_cache() -> Path:
    """The installer cache for packages added to the overlay (CLIO's own cache root).

    A provider's support package (``claude-agent-sdk`` bundles the Claude CLI: ~340 MB)
    otherwise lands in the person's ``~/.cache/uv``, which outlives CLIO: the uninstaller
    cannot remove a cache other tools share. Under CLIO's cache root it goes with
    ``uninstall --purge-caches``.
    """
    from clio_agent import paths  # noqa: PLC0415 - only for a bundled runtime

    return paths.user_cache_dir() / "overlay-installs"


def overlay_for(python_executable: str) -> Path | None:
    """The overlay that installs for ``python_executable`` must use, or ``None`` (in place).

    Only this process's own interpreter has an overlay: another interpreter (a native
    service venv) is never redirected.
    """
    try:
        same = os.path.samefile(python_executable, sys.executable)
    except OSError:
        same = False
    return overlay_dir() if same and overlay_in_use() else None


def prepend(directory: Path) -> None:
    """Put ``directory`` first on ``sys.path``, processing its ``.pth`` files."""
    import site  # noqa: PLC0415

    entry = str(directory)
    before = [path for path in sys.path if path != entry]
    sys.path[:] = before
    site.addsitedir(entry)
    if entry not in sys.path:
        sys.path.append(entry)
    added = [path for path in sys.path if path not in before]
    sys.path[:] = [*added, *before]
    if entry not in _activated:
        _activated.append(entry)


def activation_prelude(directory: Path) -> str:
    """Python source that activates ``directory`` in a ``-c`` child of the runtime.

    For children that never import :mod:`clio_agent` (installer probes, the Copilot
    runtime download). Standard library only.
    """
    return (
        "import site as _s, sys as _y\n"
        f"_d = {str(directory)!r}\n"
        "_b = [p for p in _y.path if p != _d]; _y.path[:] = _b; _s.addsitedir(_d)\n"
        "_y.path[:] = [p for p in _y.path if p not in _b] + _b\n"
        "if _d not in _y.path: _y.path.insert(0, _d)\n"
    )


def module_command(python_executable: str, directory: Path, module: str, *args: str) -> list[str]:
    """``python -m module args`` with the overlay active in the child."""
    script = (
        activation_prelude(directory)
        + f"import runpy as _r; _r.run_module({module!r}, run_name='__main__', alter_sys=True)\n"
    )
    return [python_executable, "-c", script, *args]


def activate() -> None:
    """Prepend this runtime's overlay when it has one.

    An unusable overlay (no resolvable data dir, unreadable manifest) must not stop
    CLIO importing: it is logged, and the runtime runs with what it ships.
    """
    try:
        if overlay_in_use():
            prepend(overlay_dir())
    except (OSError, ValueError, RuntimeError, ImportError) as exc:
        import logging  # noqa: PLC0415

        logging.getLogger(__name__).warning(
            "package overlay unavailable reason=overlay_activation_failed detail=%s", exc
        )


def _within(path: Path, directory: Path) -> bool:
    try:
        path.resolve().relative_to(directory.resolve())
    except ValueError:
        return False
    return True


def _runtime_versions(*excluded: Path) -> dict[str, str]:
    """Normalized name -> version for this interpreter's distributions outside ``excluded``."""
    import importlib.metadata as metadata  # noqa: PLC0415

    base = [
        path
        for path in sys.path
        if path and not any(_within(Path(path), directory) for directory in excluded)
    ]
    return {
        normalized_name(dist.metadata["Name"]): dist.version
        for dist in metadata.distributions(path=base)
        if dist.metadata["Name"]
    }


def staging_dir(directory: Path) -> Path:
    """A fresh, empty sibling of the overlay ``directory`` for an installer to ``--target``.

    Installing straight into the live overlay exposes it half-written: ``--target`` lays
    down every dependency (pydantic included) before the duplicates are pruned, and any
    CLIO process importing meanwhile (``clio-agent migrate`` during an install, a tool
    server, a second backend) picks up the partial copy ahead of the runtime's own.
    Installs land here instead, and :func:`publish` moves only the finished result in.
    """
    import tempfile  # noqa: PLC0415 - this module stays cheap to import

    directory.parent.mkdir(parents=True, exist_ok=True)
    return Path(
        tempfile.mkdtemp(prefix=f"{_STAGING_PREFIX}{directory.name}-", dir=directory.parent)
    )


def publish(
    staging: Path, directory: Path, runtime_versions: Mapping[str, str] | None = None
) -> list[str]:
    """Move a finished install from ``staging`` into the overlay ``directory``.

    Duplicates of the runtime are pruned in staging first, so they never appear in the
    overlay. A distribution the overlay already holds is replaced by the staged one. Each
    entry moves with a rename on the same filesystem, so readers see either the old or
    the new package, never a partial one. ``staging`` is removed in every case. Returns
    the distribution names published.
    """
    import importlib.metadata as metadata  # noqa: PLC0415

    try:
        versions = (
            runtime_versions
            if runtime_versions is not None
            else _runtime_versions(directory, staging)
        )
        installed = {
            normalized_name(dist.metadata["Name"])
            for dist in metadata.distributions(path=[str(staging)])
            if dist.metadata["Name"]
        }
        # A staged copy equal to the runtime's is dropped, but it still supersedes the
        # overlay's own copy (a rollback): the runtime's version then shows through.
        duplicates = set(prune_duplicates(staging, versions))
        directory.mkdir(parents=True, exist_ok=True)
        for dist in list(metadata.distributions(path=[str(directory)])):
            if normalized_name(dist.metadata["Name"] or "") in installed:
                _remove_distribution(dist, directory)
        _merge_tree(staging, directory)
        return sorted(installed - duplicates)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _merge_tree(source: Path, destination: Path) -> None:
    """Rename every entry of ``source`` into ``destination``, merging shared directories."""
    for entry in source.iterdir():
        target = destination / entry.name
        if entry.is_dir() and target.is_dir():
            _merge_tree(entry, target)
        else:
            os.replace(entry, target)


def prune_duplicates(
    directory: Path, runtime_versions: Mapping[str, str] | None = None
) -> list[str]:
    """Remove distributions the overlay holds at the same version as the runtime.

    ``--target`` installs every dependency into the overlay, including those the
    runtime already ships. Constrained to the runtime's versions, they are exact
    duplicates; dropping them leaves only what the runtime lacks or what CLIO
    deliberately updated. ``runtime_versions`` (normalized name -> version) defaults
    to this interpreter's own distributions outside the overlay. Returns the removed
    distribution names.
    """
    import importlib.metadata as metadata  # noqa: PLC0415

    if runtime_versions is None:
        runtime_versions = _runtime_versions(directory)
    else:
        runtime_versions = {normalized_name(k): v for k, v in runtime_versions.items()}
    removed: list[str] = []
    for dist in list(metadata.distributions(path=[str(directory)])):
        name = normalized_name(dist.metadata["Name"] or "")
        if not name or runtime_versions.get(name) != dist.version:
            continue
        _remove_distribution(dist, directory)
        removed.append(name)
    return sorted(removed)


def normalized_name(name: str) -> str:
    return name.lower().replace("_", "-").replace(".", "-")


def _remove_distribution(dist: object, directory: Path) -> None:
    files = getattr(dist, "files", None) or []
    parents: set[Path] = set()
    for record in files:
        located = Path(record.locate())
        if not _within(located, directory):
            continue
        located.unlink(missing_ok=True)
        parents.add(located.parent)
    info = getattr(dist, "_path", None)
    if isinstance(info, Path) and _within(info, directory):
        shutil.rmtree(info, ignore_errors=True)
    for parent in sorted(parents, key=lambda path: len(path.parts), reverse=True):
        current = parent
        while _within(current, directory) and current.resolve() != directory.resolve():
            _drop_orphan_bytecode(current)
            try:
                current.rmdir()
            except OSError:
                break
            current = current.parent


def _drop_orphan_bytecode(package: Path) -> None:
    pycache = package / "__pycache__"
    if package.name == "__pycache__" or not pycache.is_dir():
        return
    for compiled in pycache.glob("*.pyc"):
        if not (package / f"{compiled.name.split('.')[0]}.py").exists():
            compiled.unlink(missing_ok=True)
    try:
        pycache.rmdir()
    except OSError:
        pass


def _abandoned(staging: Path) -> bool:
    import time  # noqa: PLC0415 - this module stays cheap to import

    try:
        return time.time() - staging.stat().st_mtime > _STAGING_MAX_AGE_SECONDS
    except OSError:
        return False  # already published or removed by its own install


def prune_stale(directory: Path) -> list[Path]:
    """Remove overlays of runtimes that are no longer this one (an updated app).

    A staging directory is another install in progress and is left alone, unless it is
    older than :data:`_STAGING_MAX_AGE_SECONDS` (an install that was killed).
    """
    parent = directory.parent
    if parent.name != _OVERLAY_PARENT or not parent.is_dir():
        return []
    stale = [
        entry
        for entry in parent.iterdir()
        if entry.is_dir()
        and entry != directory
        and (not entry.name.startswith(_STAGING_PREFIX) or _abandoned(entry))
    ]
    for entry in stale:
        shutil.rmtree(entry, ignore_errors=True)
    return stale


def runtime_constraints(directory: Path) -> list[str]:
    """``name==version`` for every distribution the runtime itself ships."""
    import importlib.metadata as metadata  # noqa: PLC0415

    base = [path for path in sys.path if path and not _within(Path(path), directory)]
    pins: dict[str, str] = {}
    for dist in metadata.distributions(path=base):
        name = dist.metadata["Name"]
        if name:
            pins.setdefault(normalized_name(name), dist.version)
    return [f"{name}=={version}" for name, version in sorted(pins.items())]


__all__ = [
    "OVERLAY_ENV",
    "activate",
    "activation_prelude",
    "module_command",
    "normalized_name",
    "overlay_dir",
    "overlay_for",
    "overlay_in_use",
    "prepend",
    "prune_duplicates",
    "prune_stale",
    "publish",
    "runtime_constraints",
    "staging_dir",
]
