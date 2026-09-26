"""Keep litellm's vendored tiktoken rank files usable offline (no delete + re-download).

Why this exists
---------------
litellm points ``TIKTOKEN_CACHE_DIR`` at its bundled ``litellm_core_utils/tokenizers``
directory so tiktoken can load ``cl100k_base`` / ``o200k_base`` / ``p50k_base`` offline.
The ``litellm==1.102.1`` wheel ships those rank files with CRLF line endings, so their
bytes do not match the sha256 tiktoken pins for each encoding. tiktoken's
``read_file_cached`` treats that as a corrupt cache entry: on the first real load it
``os.remove``\\s the vendored file and downloads the encoding from
``openaipublic.blob.core.windows.net`` into the package directory.

That has two consequences:

* **Every fresh install needs the network** for its first OpenAI-native token count.
  On an offline node the load raises instead of using the file it already has.
* **Concurrent first loads race on one shared file.** Two processes (``pytest -n 2``
  workers, or two servers on one install) both see the mismatch, both delete, both
  download, and both ``os.rename`` onto the same path. On Windows the loser's rename
  raises ``FileExistsError``, and tiktoken re-raises it because the cache dir is
  user-specified. A reader that opens the path between one worker's delete and the
  other's rename gets ``FileNotFoundError``. Once one download lands, the good copy is
  cached for good, so a rerun passes and the failure never reproduces in that venv.

What this does
--------------
:func:`repair_vendored_rank_files` rewrites each vendored rank file whose
CRLF-to-LF normalised bytes match tiktoken's pinned hash, using an atomic
``os.replace``. After it runs, tiktoken finds a valid cache entry and never deletes or
downloads. The check-and-replace runs under a cross-process file lock, so exactly one
process rewrites the file and every later one sees it valid and leaves it alone. That
matters on Windows, where ``open()`` on a path another process is replacing fails with
``PermissionError``: the file is replaced once, before any repaired process reads it,
and never again. It changes line endings only: tiktoken parses the file with
``splitlines()``, so the ranks are the same either way.

It must run before anything in the process loads one of these encodings.
:func:`clio_agent.lm.lazy_tiktoken.install_lazy_cl100k` (the first step of
``create_lm``) calls it before the first ``import litellm``. Every outcome other than
"already valid" or "repaired" is logged with a typed ``reason=`` and never raises.
"""

from __future__ import annotations

import hashlib
import importlib.util
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from filelock import FileLock

from clio_agent import paths

logger = logging.getLogger(__name__)

# tiktoken's cache key is sha1(blob URL); the expected sha256 values are the ones
# ``tiktoken_ext.openai_public`` pins (tiktoken 0.12.0). Only encodings litellm vendors.
_OPENAI_PUBLIC = "https://openaipublic.blob.core.windows.net/encodings/"
VENDORED_RANK_FILES: dict[str, str] = {
    f"{_OPENAI_PUBLIC}cl100k_base.tiktoken": (
        "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7"
    ),
    f"{_OPENAI_PUBLIC}o200k_base.tiktoken": (
        "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d"
    ),
    f"{_OPENAI_PUBLIC}p50k_base.tiktoken": (
        "94b5ca7dff4d00767bc256fdd1b27e5b17361d7b8a5f968547f9f23eb70d2069"
    ),
}

# A concurrent reader holding the file open makes ``os.replace`` fail on Windows
# (sharing violation). The reader closes within milliseconds, so retry with backoff.
_REPLACE_ATTEMPTS = 6
_REPLACE_BACKOFF_INITIAL_S = 0.02

RepairOutcome = Literal[
    "valid",  # already hash-valid, untouched
    "repaired",  # CRLF normalised in place, now hash-valid
    "absent",  # no vendored file under that key (tiktoken will fetch it)
    "unrecognised",  # neither raw nor normalised bytes match the pin
    "write_failed",  # the atomic replace failed and no other writer fixed it
]


@dataclass(frozen=True)
class RepairResult:
    """Outcome of repairing one vendored rank file.

    Attributes:
        path: The cache file examined.
        outcome: What happened (see :data:`RepairOutcome`).
    """

    path: Path
    outcome: RepairOutcome


_lock = threading.Lock()
_done = False


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _holds_valid(path: Path, expected_hash: str) -> bool:
    """Whether ``path`` currently holds hash-valid bytes (``False`` if unreadable)."""

    try:
        return _sha256(path.read_bytes()) == expected_hash
    except OSError:
        return False


def cache_key(url: str) -> str:
    """Return tiktoken's cache file name for ``url`` (``sha1(url)``, as tiktoken computes it)."""

    return hashlib.sha1(url.encode()).hexdigest()  # noqa: S324 - tiktoken's cache-key scheme


def litellm_vendored_dir() -> Path | None:
    """Locate litellm's bundled tokenizer directory without importing litellm.

    Importing litellm here would run its eager ``cl100k_base`` load before the lazy
    proxy is installed, so the package is found through its import spec only.

    Returns:
        The ``litellm_core_utils/tokenizers`` directory, or ``None`` when litellm is
        not installed.
    """

    spec = importlib.util.find_spec("litellm")
    if spec is None or not spec.submodule_search_locations:
        return None
    return Path(next(iter(spec.submodule_search_locations))) / "litellm_core_utils" / "tokenizers"


def _atomic_write(path: Path, data: bytes, expected_hash: str) -> bool:
    """Replace ``path`` with ``data`` atomically, tolerating a concurrent reader or writer.

    Returns:
        ``True`` once ``path`` holds hash-valid bytes (ours or another process's).
    """

    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.clio-tmp")
    delay = _REPLACE_BACKOFF_INITIAL_S
    try:
        tmp.write_bytes(data)
        for attempt in range(_REPLACE_ATTEMPTS):
            try:
                os.replace(tmp, path)
                return True
            except PermissionError:
                # Another process has the file open (Windows sharing violation). If it
                # was a concurrent repair, the file may already be valid.
                if _holds_valid(path, expected_hash):
                    return True
                if attempt == _REPLACE_ATTEMPTS - 1:
                    raise
                time.sleep(delay)
                delay *= 2
    except OSError as exc:
        logger.warning(
            "tiktoken vendored rank file repair failed path=%s "
            "reason=tiktoken_vendored_repair_write_failed error=%s",
            path,
            exc,
        )
        return _holds_valid(path, expected_hash)
    finally:
        tmp.unlink(missing_ok=True)
    return False


def _repair_lock(path: Path) -> FileLock:
    """The cross-process lock serialising check-and-replace of one cache file.

    Kept in clio's user cache dir rather than next to the file, so no lock files
    accumulate inside the litellm package.
    """

    digest = hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:24]
    lock_dir = paths.user_cache_dir() / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    return FileLock(str(lock_dir / f"tiktoken-vendored-{digest}.lock"))


def repair_rank_file(path: Path, expected_hash: str) -> RepairResult:
    """Make one cached rank file hash-valid by normalising CRLF to LF, if that suffices.

    Args:
        path: The tiktoken cache file (``<cache dir>/sha1(url)``).
        expected_hash: The sha256 tiktoken pins for that URL.

    Returns:
        The typed outcome. Never raises.
    """

    try:
        lock = _repair_lock(path)
    except OSError as exc:
        logger.warning(
            "tiktoken vendored rank file repair lock unavailable path=%s "
            "reason=tiktoken_vendored_repair_lock_unavailable error=%s",
            path,
            exc,
        )
        return RepairResult(path, "write_failed")
    with lock:
        return _repair_locked(path, expected_hash)


def _repair_locked(path: Path, expected_hash: str) -> RepairResult:
    """Check and, if needed, rewrite ``path``. The caller holds :func:`_repair_lock`."""

    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return RepairResult(path, "absent")
    except OSError as exc:
        logger.warning(
            "tiktoken vendored rank file unreadable path=%s "
            "reason=tiktoken_vendored_repair_unreadable error=%s",
            path,
            exc,
        )
        return RepairResult(path, "write_failed")
    if _sha256(raw) == expected_hash:
        return RepairResult(path, "valid")
    normalised = raw.replace(b"\r\n", b"\n")
    if _sha256(normalised) != expected_hash:
        logger.warning(
            "tiktoken vendored rank file does not match the pinned hash even after "
            "line-ending normalisation path=%s reason=tiktoken_vendored_unrecognised "
            "(tiktoken will delete it and download the encoding)",
            path,
        )
        return RepairResult(path, "unrecognised")
    if not _atomic_write(path, normalised, expected_hash):
        return RepairResult(path, "write_failed")
    logger.info(
        "normalised CRLF line endings in vendored tiktoken rank file path=%s "
        "reason=tiktoken_vendored_crlf_repaired (keeps the load offline, no re-download)",
        path,
    )
    return RepairResult(path, "repaired")


def repair_vendored_rank_files(
    directory: Path | None = None, *, force: bool = False
) -> list[RepairResult]:
    """Repair every vendored rank file litellm bundles, once per process.

    Args:
        directory: The cache directory to repair. ``None`` means litellm's bundled
            directory, unless ``CUSTOM_TIKTOKEN_CACHE_DIR`` redirects litellm elsewhere
            (then there is nothing vendored to repair).
        force: Run even if this process already did (tests use this).

    Returns:
        One :class:`RepairResult` per vendored encoding, or an empty list when there is
        nothing to repair. Never raises.
    """

    global _done
    with _lock:
        if _done and not force and directory is None:
            return []
        if directory is None:
            if os.environ.get("CUSTOM_TIKTOKEN_CACHE_DIR"):
                _done = True
                return []
            directory = litellm_vendored_dir()
            _done = True
            if directory is None:
                return []
        return [
            repair_rank_file(directory / cache_key(url), expected)
            for url, expected in VENDORED_RANK_FILES.items()
        ]
