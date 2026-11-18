"""The plugins that ship inside clio-agent, loaded once and re-stamped in the background.

Split out of :mod:`clio_agent.plugins.store`, which re-exports :func:`builtin_records`
and :data:`BUILTIN_MARKETPLACE` for its callers.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from clio_agent.plugins.commands import BUILTIN_ROOT
from clio_agent.plugins.loader import load_plugin
from clio_agent.plugins.package_files import source_stamp
from clio_agent.plugins.records import PluginRecord

BUILTIN_MARKETPLACE = "clio-builtin"
logger = logging.getLogger(__name__)

#: The built-in records last loaded, with the (root, file stamp) they were loaded from.
_BUILTIN_LOADED: tuple[tuple[str, str], tuple[PluginRecord, ...]] | None = None
#: When the stamp was last taken; built-ins change only in a dev checkout, so the
#: tree walk runs at most once per this many seconds, on its own thread.
_BUILTIN_RECHECK_S = 2.0
_builtin_checked_at = 0.0
_builtin_recheck = threading.Lock()  # held by the one thread re-stamping the tree
_builtin_executor: ThreadPoolExecutor | None = None


def _builtin_rechecker() -> ThreadPoolExecutor:
    # A standing worker: starting a thread per check blocks the caller until it runs.
    global _builtin_executor
    if _builtin_executor is None:
        _builtin_executor = ThreadPoolExecutor(1, thread_name_prefix="clio-builtin-plugins")
    return _builtin_executor


def _load_builtins(key: tuple[str, str]) -> tuple[PluginRecord, ...]:
    global _BUILTIN_LOADED
    records = tuple(
        load_plugin(child, marketplace=BUILTIN_MARKETPLACE, trusted=True)
        for child in sorted(BUILTIN_ROOT.iterdir())
        if (child / "plugin.json").is_file()
    )
    _BUILTIN_LOADED = (key, records)
    return records


def _recheck_builtins() -> None:
    """Re-stamp the built-in tree and reload it if it moved (background thread)."""
    global _builtin_checked_at
    try:
        key = (str(BUILTIN_ROOT), source_stamp(BUILTIN_ROOT))
        loaded = _BUILTIN_LOADED
        if loaded is None or loaded[0] != key:
            _load_builtins(key)
    except Exception:  # noqa: BLE001 - logged; loaded records stay, next due read retries
        logger.warning("built-in plugin recheck failed", exc_info=True)
    finally:
        _builtin_checked_at = time.monotonic()
        _builtin_recheck.release()


def builtin_records() -> list[PluginRecord]:
    """Plugins that ship inside clio-agent itself (trusted like any pip package).

    Loading hashes and parses every built-in file; every catalog read (each MCP
    listing, agent resolution and doctor pass) needs them. They are loaded again
    only when the tree's file metadata (:func:`source_stamp`) moves. Once loaded, a
    due check (every :data:`_BUILTIN_RECHECK_S`) runs on one background thread and
    readers keep the loaded records meanwhile, so no request pays for the tree walk.
    """

    global _builtin_checked_at
    loaded = _BUILTIN_LOADED
    if loaded is not None and loaded[0][0] == str(BUILTIN_ROOT):
        if time.monotonic() - _builtin_checked_at >= _BUILTIN_RECHECK_S and (
            _builtin_recheck.acquire(blocking=False)
        ):
            _builtin_rechecker().submit(_recheck_builtins)
        return list(loaded[1])
    if not BUILTIN_ROOT.is_dir():
        return []
    key = (str(BUILTIN_ROOT), source_stamp(BUILTIN_ROOT))
    records = _load_builtins(key)
    _builtin_checked_at = time.monotonic()
    return list(records)
