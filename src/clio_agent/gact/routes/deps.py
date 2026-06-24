"""Cross-concern dependency seam for the GACT route factories (#714).

The router-factory decomposition moves the ``@app.<verb>`` handlers out of the
:func:`clio_agent.gact.app.build_app` closure into ``register_<concern>_routes``
factories (see :mod:`clio_agent.gact.routes`). Handlers keep closing over the
``app`` argument (FastAPI's decorators need it) and reach ``app.state`` directly,
but anything they previously reached as a ``build_app``-local closure now travels
explicitly through :class:`GactDeps`.

``GactDeps`` is built *once* in ``build_app`` and passed to every
``register_<concern>_routes`` call. Keep it minimal: add a field only when a
moved handler genuinely needs a ``build_app``-local helper/closure beyond
``app.state``. Concern-private helpers move with their concern module instead.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from fastapi import FastAPI


class _GuardDirectDestructiveAction(Protocol):
    """Callable seam for the shared direct-destructive-action permission guard.

    ``_guard_direct_destructive_action`` (in :mod:`clio_agent.gact.app`) applies
    permission policy + audit semantics before a direct GACT ``DELETE`` mutates
    state. It is a genuinely cross-concern seam: workspace, session, agent,
    blueprint, memory and prompt delete routes all call it. Carrying it on
    ``GactDeps`` lets the moved handlers invoke it without importing back into
    ``gact.app`` (which would violate the no-cycle invariant).
    """

    def __call__(
        self,
        app: "FastAPI",
        *,
        session_id: str = ...,
        workspace_id: str = ...,
        tool_name: str,
        args: Mapping[str, Any],
        summary: str,
        reason: str,
    ) -> None: ...


@dataclass(frozen=True)
class GactDeps:
    """Cross-concern seams the extracted route factories need beyond ``app.state``.

    Built once in ``build_app`` and threaded through every
    ``register_<concern>_routes(app, deps)`` call. Fields are the shared
    ``build_app``-local helpers/closures that more than one concern reaches for;
    concern-private helpers live in the concern module, not here.
    """

    guard_direct_destructive_action: _GuardDirectDestructiveAction
