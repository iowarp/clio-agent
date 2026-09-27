"""Stop the background threads a test's objects started, at that test's teardown.

WHY. Two objects start a daemon thread in their constructor and stop it only in an
explicit ``close()`` that product code runs at a lifecycle edge a test often never
reaches:

* :class:`~clio_agent.arc.lsm.LSMTree` -- every ``ARCMemory`` starts a compaction
  thread (woken every 5 s) and closes it only from ``ARCMemory.__del__``, which a
  reference cycle defers indefinitely;
* :class:`~clio_agent.gact.provenance.dispatcher.ProvenanceDispatcher` -- every
  ``build_app`` starts one provider worker per provenance backend, closed only by the
  app lifespan's teardown, which a test that never enters ``TestClient(app)`` skips.

Measured: 121 gact tests left 43 compaction threads and 30 provider workers running in
one worker; over a full run that is thousands. Beyond the waste, it hid hangs:
faulthandler dumps at most 100 threads, newest first, so the hang guard's dump of a
stuck test (tests/_hang_guard.py) never reached the main thread's stack.

WHAT. :func:`install` records every instance created in this process (weakly), and
:func:`close_created_since` closes the ones created during one test. No fixture in the
suite shares these objects across tests (none is module- or session-scoped), so
closing at the creating test's teardown is exactly their intended lifetime. ``close``
is idempotent on both, so objects a test already closed are unaffected.
"""

from __future__ import annotations

import weakref
from typing import Any

_created: list[weakref.ref[Any]] = []
_installed = False


def _record_instances(cls: type) -> None:
    original = cls.__init__

    def __init__(self: Any, *args: Any, **kwargs: Any) -> None:  # noqa: N807 - wraps __init__
        original(self, *args, **kwargs)
        _created.append(weakref.ref(self))

    __init__.__wrapped__ = original  # type: ignore[attr-defined]
    cls.__init__ = __init__  # type: ignore[method-assign]


def install() -> None:
    """Start recording LSM trees and provenance dispatchers (idempotent)."""
    global _installed
    if _installed:
        return
    from clio_agent.arc.lsm import LSMTree  # noqa: PLC0415
    from clio_agent.gact.provenance.dispatcher import ProvenanceDispatcher  # noqa: PLC0415

    _record_instances(LSMTree)
    _record_instances(ProvenanceDispatcher)
    _installed = True


def mark() -> int:
    """A position to pass to :func:`close_created_since` after the test."""
    return len(_created)


def close_created_since(position: int) -> None:
    """Close every recorded instance created after ``position`` that is still alive."""
    created = _created[position:]
    del _created[position:]
    for ref in created:
        instance = ref()
        if instance is None:
            continue
        try:
            instance.close()
        except FileNotFoundError:
            # LSMTree.close flushes its memtable into its data dir; this teardown runs
            # after the test's own fixtures, which may already have removed that dir.
            # The compaction thread is stopped before the flush, so it is still gone.
            pass
