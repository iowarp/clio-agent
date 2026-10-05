"""Locate and verify the SafeTensors file a descriptor's ``uri`` names.

The uri is the path the connector wrote on the GPU node
(``file://<out_dir>/<workflow_id>/<request id>_g<group>.safetensors``). In
order, CLIO reads it:

1. in place, when it runs where that path exists;
2. from ``provenance.attention.files_dir``, a local directory holding the same
   ``<workflow_id>/<file>`` layout (a synced mirror, or an attention bundle's
   ``files/`` directory), re-rooted under it.

Fetching the file from a remote node is not CLIO's job (see
:mod:`.byte_source`). A file is checked against the descriptor before any
read: size and sha256, once per file for the process lifetime. A missing or
different file is a typed reason, never a silent partial answer.
"""

from __future__ import annotations

import threading
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlparse

from clio_agent import conf
from clio_agent.gact.attention.byte_source import LocalFile
from clio_agent.gact.attention.contract import AttentionRecord
from clio_agent.gact.attention.reasons import AttentionUnavailable

_lock = threading.Lock()
_verified: set[tuple[str, int, str]] = set()


def configured_files_dir() -> str:
    """The local mirror of the connector's ``out_dir``, or ``""``."""
    return conf.resolve(
        "provenance.attention.files_dir",
        env="CLIO_PROVENANCE_ATTENTION_FILES_DIR",
        default="",
        cast=conf.as_str,
    ).strip()


def uri_path(uri: str) -> str:
    """The node path of a ``file://`` uri (typed for any other scheme)."""
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        raise AttentionUnavailable(
            "attention_file_unavailable",
            f"attention file uri scheme {parsed.scheme!r} is not readable here",
            {"uri": uri},
        )
    return unquote(parsed.path)


def locate(record: AttentionRecord, files_dir: str | None = None) -> LocalFile:
    """Where ``record``'s file is: in place, else under the local copy."""
    node_path = uri_path(record.uri)
    original = Path(node_path)
    if original.is_file():
        return LocalFile(original)
    root = configured_files_dir() if files_dir is None else files_dir
    tried = [node_path]
    if root:
        parts = PurePosixPath(node_path).parts
        suffix = Path(*parts[-2:]) if len(parts) >= 2 else Path(PurePosixPath(node_path).name)
        mirrored = Path(root) / suffix
        if mirrored.is_file():
            return LocalFile(mirrored)
        tried.append(str(mirrored))
    raise AttentionUnavailable(
        "attention_file_unavailable",
        "the attention file is not reachable from this CLIO (run CLIO where the "
        "connector writes, or set provenance.attention.files_dir to a local copy)",
        {"uri": record.uri, "tried": tried},
    )


def verify(record: AttentionRecord, source: LocalFile) -> None:
    """The file is the one the descriptor describes (size + sha256, once per file)."""
    if not record.sha256 and record.size is None:
        return
    try:
        stat = source.path.stat()
    except OSError as exc:
        raise AttentionUnavailable(
            "attention_file_unavailable", f"cannot stat {source.label}: {exc}"
        ) from exc
    if record.size is not None and stat.st_size != record.size:
        raise AttentionUnavailable(
            "attention_file_mismatch",
            f"{source.label} is {stat.st_size} bytes, the record says {record.size}",
            {"path": source.label},
        )
    key = (f"{source.label}@{stat.st_mtime_ns}", record.size or -1, record.sha256)
    with _lock:
        if key in _verified:
            return
    size, digest = source.stat()
    if record.size is not None and size != record.size:
        raise AttentionUnavailable(
            "attention_file_mismatch",
            f"{source.label} is {size} bytes, the record says {record.size}",
            {"path": source.label},
        )
    if record.sha256 and digest != record.sha256:
        raise AttentionUnavailable(
            "attention_file_mismatch",
            f"{source.label} sha256 differs from the record",
            {"path": source.label},
        )
    with _lock:
        _verified.add(key)


def clear_cache() -> None:
    """Forget verified files (tests)."""
    with _lock:
        _verified.clear()
