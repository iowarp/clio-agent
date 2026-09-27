"""Locate and verify the SafeTensors file a descriptor's ``uri`` names.

The uri is the path the connector wrote on the GPU node
(``file://<out_dir>/<workflow_id>/<request id>_g<group>.safetensors``). CLIO
reads it in place when it runs where that path exists; anywhere else,
``provenance.attention.files_dir`` names a local directory holding the same
``<workflow_id>/<file>`` layout (a synced mirror, or an attention bundle's
``files/`` directory), which is re-rooted under it.

A file is checked against the descriptor before any read: size always, sha256
once per (path, size, mtime) for the process lifetime. A missing or different
file is a typed reason, never a silent partial answer.
"""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path
from urllib.parse import unquote, urlparse

from clio_agent import conf
from clio_agent.gact.attention.contract import AttentionRecord
from clio_agent.gact.attention.reasons import AttentionUnavailable

_lock = threading.Lock()
_verified: dict[tuple[str, int, int], str] = {}


def configured_files_dir() -> str:
    """The local mirror of the connector's ``out_dir``, or ``""``."""
    return conf.resolve(
        "provenance.attention.files_dir",
        env="CLIO_PROVENANCE_ATTENTION_FILES_DIR",
        default="",
        cast=conf.as_str,
    ).strip()


def uri_path(uri: str) -> Path:
    """The filesystem path of a ``file://`` uri (typed for any other scheme)."""
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        raise AttentionUnavailable(
            "attention_file_unavailable",
            f"attention file uri scheme {parsed.scheme!r} is not readable here",
            {"uri": uri},
        )
    return Path(unquote(parsed.path))


def locate(record: AttentionRecord, files_dir: str | None = None) -> Path:
    """The local path of ``record``'s file: in place, else under ``files_dir``."""
    original = uri_path(record.uri)
    if original.is_file():
        return original
    root = configured_files_dir() if files_dir is None else files_dir
    tried = [str(original)]
    if root:
        parts = original.parts
        suffix = Path(*parts[-2:]) if len(parts) >= 2 else Path(original.name)
        mirrored = Path(root) / suffix
        if mirrored.is_file():
            return mirrored
        tried.append(str(mirrored))
    raise AttentionUnavailable(
        "attention_file_unavailable",
        "the attention file is not reachable from this CLIO"
        + ("" if root else " (set provenance.attention.files_dir to a local copy)"),
        {"uri": record.uri, "tried": tried},
    )


def verify(record: AttentionRecord, path: Path) -> None:
    """The file is the one the descriptor describes (size, then sha256 once)."""
    try:
        stat = path.stat()
    except OSError as exc:
        raise AttentionUnavailable(
            "attention_file_unavailable", f"cannot stat {path}: {exc}", {"path": str(path)}
        ) from exc
    if record.size is not None and stat.st_size != record.size:
        raise AttentionUnavailable(
            "attention_file_mismatch",
            f"{path.name} is {stat.st_size} bytes, the record says {record.size}",
            {"path": str(path)},
        )
    if not record.sha256:
        return
    key = (str(path), stat.st_size, stat.st_mtime_ns)
    with _lock:
        digest = _verified.get(key)
    if digest is None:
        sha = hashlib.sha256()
        try:
            with path.open("rb") as fh:
                for chunk in iter(lambda: fh.read(8 * 1024 * 1024), b""):
                    sha.update(chunk)
        except OSError as exc:
            raise AttentionUnavailable(
                "attention_file_unavailable", f"cannot read {path}: {exc}", {"path": str(path)}
            ) from exc
        digest = sha.hexdigest()
        with _lock:
            _verified[key] = digest
    if digest != record.sha256:
        raise AttentionUnavailable(
            "attention_file_mismatch",
            f"{path.name} sha256 differs from the record",
            {"path": str(path)},
        )


def clear_cache() -> None:
    """Forget verified digests (tests)."""
    with _lock:
        _verified.clear()
