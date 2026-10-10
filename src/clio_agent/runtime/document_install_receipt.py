"""Reuse installation checks while the verified package files remain unchanged.

This is a package cache, not a sandbox qualification. Protected execution is
checked separately on every startup. File metadata detects ordinary updates,
missing files and damaged caches without launching every converter again.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
import sys
from pathlib import Path
from typing import Any

from filelock import FileLock

from clio_agent import paths
from clio_agent.runtime.document_runtime import STACK_ROOT, _fingerprint, office_runtime_root
from clio_agent.runtime.document_stack.process import scratch_root

logger = logging.getLogger(__name__)


def _receipt_path(cache_root: Path | None, runtime_id: str | None = None) -> Path:
    return (
        (cache_root or paths.user_cache_dir() / "document-runtime")
        / (runtime_id or _fingerprint())
        / "installed.json"
    )


def _roots(result: dict[str, Any]) -> list[Path]:
    javascript = Path(result["javascript"]["workspace"])
    office = Path(result["native_tools"]["soffice"]["path"])
    private_office = office_runtime_root().resolve()
    roots = [
        STACK_ROOT,
        Path(__file__),
        Path(__file__).with_name("document_install.py"),
        Path(__file__).with_name("document_runtime.py"),
        Path(sys.executable),
        Path(result["python"]).parent.parent,
        Path(result["uv"]),
        javascript / "node_modules",
        javascript / "package.json",
        javascript / "pnpm-lock.yaml",
        Path(result["javascript"]["pnpm_argv"][1]).parent.parent,
        private_office if office.resolve().is_relative_to(private_office) else office.parent,
        Path(result["native_tools"]["gh"]["path"]),
    ]
    if result.get("helper"):
        roots.append(Path(result["helper"]))
    roots.extend(
        Path(tool["path"])
        for tool in result["native_tools"].values()
        if tool.get("path") and tool.get("status") == "available"
    )
    roots.extend(Path(font) for font in result.get("font_files", []))
    # Detect added/removed fonts as well as edits to the fonts used by inventory.
    roots.extend(
        root
        for root in (Path("/usr/share/fonts"), Path("/Library/Fonts"), Path("C:/Windows/Fonts"))
        if root.is_dir()
    )
    return sorted(set(roots), key=str)


def _tree_metadata(root: Path) -> str:
    digest = hashlib.sha256()

    def record(path: Path, info: os.stat_result) -> None:
        metadata: list[Any] = [str(path.relative_to(root)), info.st_mode, info.st_dev, info.st_ino]
        if not stat.S_ISDIR(info.st_mode):
            metadata.extend((info.st_size, info.st_mtime_ns, info.st_ctime_ns))
        if stat.S_ISLNK(info.st_mode):
            target = path.stat()
            metadata.extend((os.readlink(path), target.st_size, target.st_mtime_ns))
        digest.update(json.dumps(metadata).encode())

    info = root.lstat()
    record(root, info)
    directories = [root] if stat.S_ISDIR(info.st_mode) else []
    while directories:
        with os.scandir(directories.pop()) as scan:
            for entry in sorted(scan, key=lambda item: item.name):
                if entry.name == "__pycache__" or entry.name.endswith((".pyc", ".pyo")):
                    continue
                path = Path(entry.path)
                # Windows directory enumeration already supplies these attributes;
                # retain DirEntry's cached stat instead of restatting every file.
                info = entry.stat(follow_symlinks=False)
                record(path, info)
                if stat.S_ISDIR(info.st_mode):
                    directories.append(path)
    return digest.hexdigest()


def _identity(workspace: Path, result: dict[str, Any]) -> str:
    expected_output = scratch_root(workspace, create=False) / "output"
    if Path(result["output_directory"]).resolve() != expected_output.resolve():
        raise ValueError("Installation belongs to a different workspace")
    if result["status"] != "ready" or result["javascript"]["status"] != "ready":
        raise ValueError("Installation is incomplete")
    if result["runtime_id"] != _fingerprint():
        raise ValueError("Installation package locks changed")
    context = {
        "workspace": str(workspace.resolve()),
        "interpreter": sys.version,
        "runtime_id": _fingerprint(),
        "office_root": str(office_runtime_root()),
        "environment": {
            name: value
            for name, value in os.environ.items()
            if name in {"PATH", "GACT_BUNDLED_RUNTIME_DIR"} or name.startswith("CLIO_DOCUMENT_")
        },
        "inventory": result,
        "files": [(str(root), _tree_metadata(root)) for root in _roots(result)],
    }
    return hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest()


def save_install_receipt(
    workspace: Path, result: dict[str, Any], *, cache_root: Path | None = None
) -> None:
    """Atomically record completed installation and its package-file identities."""
    receipt = _receipt_path(cache_root, result["runtime_id"])
    receipt.parent.mkdir(parents=True, exist_ok=True)
    # Nonstandard callers can return a smaller inventory. Retain their result,
    # but never mark it reusable without the actual managed files.
    try:
        identity = _identity(workspace, result)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError) as error:
        logger.debug("Installation cannot be reused: %s", error)
        identity = None
    payload = {**result, "installation_check": {"format": 1, "identity": identity}}
    with FileLock(str(receipt.with_suffix(".lock")), timeout=240):
        temporary = receipt.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temporary.replace(receipt)


def reuse_install_receipt(
    workspace: Path, *, cache_root: Path | None = None
) -> dict[str, Any] | None:
    """Return the verified inventory only if its runtime, workspace and files still match."""
    receipt = _receipt_path(cache_root)
    try:
        result = json.loads(receipt.read_text(encoding="utf-8"))
        proof = result.pop("installation_check")
        if proof["format"] != 1 or not isinstance(proof["identity"], str):
            return None
        if proof["identity"] != _identity(workspace, result):
            return None
        return result
    except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError) as error:
        logger.debug("Installation requires full preparation: %s", error)
        return None
