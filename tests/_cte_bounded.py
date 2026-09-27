"""Bound every test call into a REAL clio-core daemon (a stuck daemon fails in seconds).

WHY. The clio-core CTE binding holds the GIL for the whole of every blocking RPC
(PR #1473). A daemon that stops answering therefore freezes the whole test process:
no Python timer, no stall ladder (``arc/rpc_liveness.py``), no pytest-timeout thread
can run. Before this module a stuck private daemon cost the per-test limit at best
and GitHub's 6-hour job limit at worst.

WHAT. :func:`install` wraps every public :class:`~clio_agent.arc.storage.ClioCoreStore`
operation (and its constructor, which attaches to or spawns the daemon) in
:func:`tests._hang_guard.bounded_native_call`. That arms the GIL-independent C
watchdog with a short per-call deadline: a call that does not return in time dumps
every thread's stack (the frames name the store op and the test) and ends the
process, which fails the test. Only stores bound to the REAL binding are bounded;
the suite's fakes (``store._cte`` replaced by a Python stub) stay under the ordinary
per-test limit, because their stall-ladder tests legitimately wait.

The bounds are config, not magic: ``CLIO_TEST_CTE_CALL_BOUND_S`` (one store op) and
``CLIO_TEST_CTE_ATTACH_BOUND_S`` (attach, including a cold daemon spawn). The defaults
are a small multiple of the slowest call measured across the suite (see the
constants below).
"""

from __future__ import annotations

import functools
import os
from collections.abc import Callable, Iterator
from typing import Any

from tests import _hang_guard

CALL_BOUND_ENV = "CLIO_TEST_CTE_CALL_BOUND_S"
ATTACH_BOUND_ENV = "CLIO_TEST_CTE_ATTACH_BOUND_S"

# Measured on the private suite daemon (Windows, -n 2, 2026-09-26, 3559 bounded calls
# across tests/test_arc, tests/test_equivalence and the gact cte legs): the slowest
# store op took 0.89 s, so 15 s is ~17x headroom for a loaded CI runner while a stuck
# daemon still fails the test in seconds.
DEFAULT_CALL_BOUND_S = 15.0
# The constructor can cold-spawn the daemon (``clio_run start`` + port bind + client
# handshake + ``initialize_cte``): 1.88 s measured cold, bounded at ~16x.
DEFAULT_ATTACH_BOUND_S = 30.0

_BOUNDED_METHODS = ("put", "get", "exists", "delete", "clear", "search")

_installed = False


def _bound_from_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    value = float(raw)  # a malformed override is a test-config error: fail loud
    if value <= 0:
        raise ValueError(f"{name} must be a positive number of seconds, got {raw!r}")
    return value


def _is_real_binding(store: Any) -> bool:
    """True when ``store`` talks to the real native binding, not a test stub."""
    cte = getattr(store, "_cte", None)
    return getattr(cte, "__name__", None) == "clio_cte_core_ext"


def _wrap_op(name: str, original: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(original)
    def bounded(self: Any, *args: Any, **kwargs: Any) -> Any:
        if not _is_real_binding(self):
            return original(self, *args, **kwargs)
        bound = _bound_from_env(CALL_BOUND_ENV, DEFAULT_CALL_BOUND_S)
        with _hang_guard.bounded_native_call(bound):
            return original(self, *args, **kwargs)

    return bounded


def _wrap_scan(original: Callable[..., Iterator[Any]]) -> Callable[..., Iterator[Any]]:
    # scan() is a generator: bound each step (the listing RPC, then each per-blob read
    # through the already-bounded get()), never the consumer's time between steps.
    @functools.wraps(original)
    def bounded(self: Any, *args: Any, **kwargs: Any) -> Iterator[Any]:
        iterator = original(self, *args, **kwargs)
        if not _is_real_binding(self):
            yield from iterator
            return
        bound = _bound_from_env(CALL_BOUND_ENV, DEFAULT_CALL_BOUND_S)
        while True:
            with _hang_guard.bounded_native_call(bound):
                try:
                    item = next(iterator)
                except StopIteration:
                    return
            yield item

    return bounded


def _wrap_init(original: Callable[..., None]) -> Callable[..., None]:
    @functools.wraps(original)
    def bounded(self: Any, *args: Any, **kwargs: Any) -> None:
        bound = _bound_from_env(ATTACH_BOUND_ENV, DEFAULT_ATTACH_BOUND_S)
        with _hang_guard.bounded_native_call(bound):
            original(self, *args, **kwargs)

    return bounded


def install() -> None:
    """Wrap the real ``ClioCoreStore`` ops in per-call hard bounds (idempotent)."""
    global _installed
    if _installed:
        return
    from clio_agent.arc.storage import ClioCoreStore  # noqa: PLC0415

    for name in _BOUNDED_METHODS:
        setattr(ClioCoreStore, name, _wrap_op(name, getattr(ClioCoreStore, name)))
    ClioCoreStore.scan = _wrap_scan(ClioCoreStore.scan)  # type: ignore[method-assign]
    ClioCoreStore.__init__ = _wrap_init(ClioCoreStore.__init__)  # type: ignore[method-assign]
    _installed = True
