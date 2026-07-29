"""gact-side adapter for the #735 tool-runtime seam (unified-concurrency §4 Site 1).

A NEW owner module (no-accretion: nothing bolted onto ``globals.py`` or
``build_app``). It holds the STATELESS resolver ``build_app`` installs once into
the low ``tools.execution`` layer:

* :func:`resolve_tool_runtime` reads the live turn's ``active_app().state.pending_*``
  into a :class:`~clio_agent.tools.execution.ToolRuntimeHooks`, returning ``None``
  when app-less so ``current_tool_runtime`` takes the reason-logged fallback path
  (never a silent empty gate).
* :func:`per_app_dict` is the shared per-app keyed-store helper used by the #770
  expert-cache migration (Site 2).

Imports only gact *leaves* (``gact.context``) + the low ``tools`` layer, so the
edge set ``tools.execution <- gact.runtime.app_state <- gact.app`` stays strictly
acyclic (``tools.execution`` imports no ``gact``).
"""

from __future__ import annotations

from typing import Any

from clio_agent.gact import context as _ctx
from clio_agent.tools.execution import ToolRuntimeHooks


def resolve_tool_runtime() -> ToolRuntimeHooks | None:
    """Resolve the live turn's tool-runtime hooks from ``active_app().state``.

    Returns ``None`` when no app is bound on the current context (app-less /
    out-of-band) so the tools layer emits its structured fallback reason instead
    of silently dropping a permission gate. When an app IS bound, returns its
    ``pending_*`` hooks (any unset hook is ``None`` — a no-op, not a lookup
    elsewhere), isolating concurrent apps to their own ``app.state``.
    """

    app = _ctx.active_app()
    state = getattr(app, "state", None) if app is not None else None
    if state is None:
        return None
    return ToolRuntimeHooks(
        permission_gate=getattr(state, "pending_permission_gate", None),
        tool_observer=getattr(state, "pending_tool_observer", None),
        tool_interceptor=getattr(state, "pending_tool_interceptor", None),
        cancellation_checker=getattr(state, "pending_cancellation_checker", None),
        loop_inbox_drain=getattr(state, "pending_loop_inbox_drain", None),
        mcp_app_observer=getattr(state, "pending_mcp_app_observer", None),
        post_tool=getattr(state, "pending_post_tool", None),
    )


def per_app_dict(name: str, app: Any = None) -> dict[str, Any]:
    """Return the per-app dict named ``name``, created on ``app.state`` on demand.

    Resolves ``app`` from the live turn (``active_app()``) when not passed. When
    genuinely app-less returns a FRESH empty dict — a structured empty, never a
    sibling app's store. The shared home for the #770 expert caches (Site 2).
    """

    if app is None:
        app = _ctx.active_app()
    state = getattr(app, "state", None) if app is not None else None
    if state is None:
        return {}
    store = getattr(state, name, None)
    if not isinstance(store, dict):
        store = {}
        setattr(state, name, store)
    return store
