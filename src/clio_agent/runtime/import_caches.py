"""Refresh Python's import caches safely from any thread.

``importlib.invalidate_caches()`` is not thread-safe. ``PathFinder.invalidate_caches``
copies ``sys.path_importer_cache`` and then deletes every entry whose finder is
``None``. Two threads refreshing at once delete the same entry, and the second
raises ``KeyError`` with its path. A ``sys.path`` entry that does not exist always
has such an entry, for example the ``pythonXY.zip`` of a uv-managed interpreter. The
provider component routes refresh once per request, and the Desktop asks for several
providers at once, so the race surfaced as a 500.
"""

from __future__ import annotations

import importlib
import threading

_LOCK = threading.Lock()


def invalidate_import_caches() -> None:
    """Run ``importlib.invalidate_caches()`` with every CLIO caller serialized."""
    with _LOCK:
        try:
            importlib.invalidate_caches()
        except KeyError:
            # Code outside CLIO deleted the same entry first: finish the refresh.
            importlib.invalidate_caches()
