"""A dieted stdio transport respawns from the declared command once its plan is gone.

``transport_for`` builds a server's transport when its executor mounts it, and a
learned spawn plan (:mod:`clio_agent.tools.spawn_diet`) is resolved then: the transport
keeps the plan's interpreter as its argv for its whole life. Two things can retire the
plan afterwards while that transport lives on, and every later spawn then exec'd a dead
path ("Failed to execvp …/mcp-environments/<old>/bin/python") and every call answered
"Connection closed" until CLIO restarted:

* ``clio-kit mcp-server --prepare`` replaced the plan's environment (a new source hash,
  or the old one removed as broken), so the plan is dropped ``env_superseded``
  (:func:`record_prepared`);
* the dieted spawn failed to connect, so the plan is dropped ``spawn_failed``.

:class:`DietStdioTransport` checks its plan before each spawn. When the plan is no longer
in the cache it rebuilds its launch from ``transport_for`` (which, with no plan, gives
the declared command inside the same confinement) and spawns that, with the typed reason
``transport_redeclared``. A live session is never touched.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from functools import partial
from typing import Any

from fastmcp.client.transports import StdioTransport

from clio_agent.runtime import trace
from clio_agent.tools import spawn_diet

#: The typed reason a dieted transport spawns its declared command instead.
TRANSPORT_REDECLARED = "transport_redeclared"


class DietStdioTransport(StdioTransport):
    """A stdio transport built from a learned plan; it re-declares once the plan is gone."""

    def __init__(
        self,
        *args: Any,
        namespace: str,
        plan_key: str | None,
        rebuild: Callable[[], Any],
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.namespace = namespace
        self.plan_key = plan_key
        self._rebuild = rebuild
        self._replacement: Any = None  # keeps the rebuilt launch's confinement alive

    def _redeclare_if_stale(self) -> None:
        if self.plan_key is None or spawn_diet.plan_cached(self.plan_key):
            return
        fresh = self._rebuild()
        self.command, self.args, self.env, self.cwd = (
            fresh.command,
            fresh.args,
            fresh.env,
            fresh.cwd,
        )
        self.log_file = getattr(fresh, "log_file", self.log_file)
        # A plan learned again since then is checked the same way on the next spawn.
        self.plan_key = getattr(fresh, "plan_key", None)
        self._replacement = fresh
        trace.event(
            "TOOLS",
            "mcp_spawn_diet_fallback server=%s reason=%s argv0=%s",
            self.namespace,
            TRANSPORT_REDECLARED,
            fresh.command,
        )

    async def connect(self, **kwargs: Any) -> Any:
        """Spawn (or reuse) the server; a spawn whose plan was dropped re-declares first."""

        if self._connect_task is None or self._connect_task.done():
            self._redeclare_if_stale()
        return await super().connect(**kwargs)


def stdio_transport(spec: Any, cwd: str | None, dieted: bool) -> Callable[..., Any]:
    """The constructor ``transport_for`` builds a stdio transport with.

    A dieted launch gets :class:`DietStdioTransport`, bound to the plan it applied and to
    a rebuild of the same spec; any other launch is a plain ``StdioTransport``.
    """

    if not dieted:
        return StdioTransport
    from clio_agent.tools.mcp_config import transport_for  # noqa: PLC0415 - cycle

    return partial(
        DietStdioTransport,
        namespace=spec.name,
        plan_key=spawn_diet.plan_key_for(spec.name),
        rebuild=partial(transport_for, spec, cwd=cwd),
    )


def record_prepared(namespace: str, result: Mapping[str, Any]) -> None:
    """Record what ``clio-kit mcp-server --prepare`` reported, and keep the plan true to it."""

    environment = str(result.get("environment") or "")
    trace.event(
        "TOOLS",
        "mcp_prepare_kit_result namespace=%s status=%s environment=%s kit_seconds=%s",
        namespace,
        result.get("status"),
        environment,
        result.get("seconds", ""),
    )
    spawn_diet.environment_prepared(namespace, environment)


__all__ = ["TRANSPORT_REDECLARED", "DietStdioTransport", "record_prepared", "stdio_transport"]
