"""Per-backend ``on_poll`` observer registry (#1231): the generic wiring seam.

The transparent SEP-2663 client extension (:mod:`clio_agent.tools.mcp_task_extension`,
``ClioTasksClientExtension._resolve_task``) drives EVERY auto-claimed task through
:func:`clio_agent.tools.mcp_tasks.drive_task_to_terminal`, which already accepts a
generic, backend-agnostic ``on_poll`` hook (#1231 Part 2). Nothing wired one in,
though: the explicit ``relay_wait``/``relay_observe`` path
(:meth:`clio_agent.tools.relay_transport.RelayTransportClient.wait`/``poll``) passes
:func:`clio_agent.tools.relay_console.make_console_on_poll` by hand, but a relay-backed
tool call that resolves through the TRANSPARENT extension — any call whose
``CreateTaskResult`` the client claims and drives itself, never routed through
``RelayTransportClient``'s own manual poll loop — reached ``drive_task_to_terminal``
with no hook at all, so its task record's console tail stayed empty forever
(run-14 live evidence: 13 relay-driven task records, all console bytes 0).

This module is the missing seam, and it stays deliberately ignorant of relay (or any
other backend): a plain ``server_id -> factory`` dict, mirroring the single-hook
registries already in :mod:`clio_agent.tools.mcp_task_records`
(``set_task_session_resolver`` / ``set_task_canceller`` / ``set_task_change_listener``)
but keyed per backend instead of process-global, since more than one task-serving
backend can be live in one process. ``mcp_task_extension.py`` calls
:func:`resolve_task_observer` once per drive and forwards whatever it returns
(``None`` included) straight into ``on_poll=`` — it carries no relay-specific
knowledge, and neither does this module. The relay-specific factory
(``make_console_on_poll`` bound to one open ``RelayTransportClient``) is registered
by ``relay_transport.py`` itself, the one module that already owns that HTTP door.

A registered FACTORY is called synchronously, once, at drive start (never per poll —
that per-poll robustness is the returned hook's own job, e.g.
``make_console_on_poll``'s internal try/except). A factory that raises must still
never break the drive it was about to observe, so the failure is caught HERE and
degraded to ``None`` with one typed warning — the registry is the single call site
for every backend's factory, so guarding it here (rather than in every caller of
:func:`resolve_task_observer`) is both the one place that can enforce it and the
one place that needs to.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING

from clio_agent.errors import MCP_TASK_OBSERVER_FACTORY_FAILED

if TYPE_CHECKING:  # pragma: no cover - typing only
    from clio_agent.tools.mcp_task_records import TaskKey
    from clio_agent.tools.mcp_tasks import OnPollHook

logger = logging.getLogger(__name__)

__all__ = [
    "TaskObserverFactory",
    "register_task_observer_factory",
    "resolve_task_observer",
    "unregister_task_observer_factory",
]

#: One backend's ``on_poll`` hook builder. Called with the FULL composite
#: :class:`~clio_agent.tools.mcp_task_records.TaskKey` of the task about to be
#: driven, so a factory can bind its hook to that exact task id (mirrors
#: :func:`clio_agent.tools.relay_console.make_console_on_poll`'s ``job_id``
#: argument). Returning ``None`` is a legitimate answer (e.g. console tailing
#: disabled by config) -- :func:`resolve_task_observer` then drives with the
#: GENERIC wait-surfacing default (#1282 D3) instead, identical to an
#: unregistered backend: an opt-out of backend-specific enrichment is never an
#: opt-out of the baseline "every wait names what it waits on" guarantee.
TaskObserverFactory = Callable[["TaskKey"], "OnPollHook | None"]

_LOCK = threading.Lock()
_FACTORIES: dict[str, TaskObserverFactory] = {}


def register_task_observer_factory(server_id: str, factory: TaskObserverFactory) -> None:
    """Install the ``on_poll`` hook factory for one backend's ``server_id``.

    Last-writer-wins per ``server_id``, matching the granularity
    :func:`clio_agent.tools.mcp_task_extension.backend_identity` already uses: two
    client instances dialing the SAME backend (URL/command locator) share one
    ``server_id`` today, so overlapping opens of the same backend race on which
    factory is registered. That mirrors the existing coarse-grained identity (a task
    id is only unique WITHIN a ``server_id``, not per client instance) rather than
    introducing a new, finer-grained one here; a transient loss of console folding
    during an overlapping open/close is a graceful degrade (the drive itself is
    unaffected), never a correctness break.
    """

    with _LOCK:
        _FACTORIES[server_id] = factory


def unregister_task_observer_factory(server_id: str) -> None:
    """Remove the ``on_poll`` hook factory for one backend's ``server_id``, if any.

    Idempotent: unregistering an id with nothing registered is a no-op.
    """

    with _LOCK:
        _FACTORIES.pop(server_id, None)


def resolve_task_observer(key: "TaskKey") -> "OnPollHook":
    """Resolve the ``on_poll`` hook for one task's backend.

    A REGISTERED backend-specific factory (relay's console-tail today) wins.
    Otherwise (#1282, C1-S2 D3) falls back to
    :func:`~clio_agent.tools.mcp_wait_ladder.default_task_wait_observer` — the
    generic wait-surfacing hook every non-relay task-capable backend was
    missing entirely (the six #1274 wait constraints' "every wait names what
    it waits on" rule was silently unmet for them; only relay's console tail
    was ever visible). A REGISTERED factory that raises while building its
    hook is the one failure this function guards: caught, reported at WARNING
    with the typed reason ``mcp_task_observer_factory_failed``, and downgraded
    to the SAME default (never bare ``None``, and never breaks the drive this
    was about to observe) — a broken backend-specific factory must not also
    cost the drive its generic wait visibility. Called exactly once per drive
    (:meth:`clio_agent.tools.mcp_task_extension.ClioTasksClientExtension.
    _resolve_task` calls it once before ``drive_task_to_terminal`` starts), so
    one warning here already IS "once per drive" -- no extra dedup state
    needed.
    """

    from clio_agent.tools.mcp_wait_ladder import default_task_wait_observer  # noqa: PLC0415

    with _LOCK:
        factory = _FACTORIES.get(key.server_id)
    if factory is None:
        return default_task_wait_observer(key)
    try:
        hook = factory(key)
    except Exception as exc:  # noqa: BLE001 - a broken factory must never break the drive
        logger.warning(
            "mcp task observer factory failed reason=%s server_id=%s task_id=%s: %s",
            MCP_TASK_OBSERVER_FACTORY_FAILED,
            key.server_id,
            key.task_id,
            exc,
        )
        return default_task_wait_observer(key)
    return hook if hook is not None else default_task_wait_observer(key)
