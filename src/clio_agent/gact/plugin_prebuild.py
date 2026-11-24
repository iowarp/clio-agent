"""Build a plugin's MCP server environments when it is installed, so its first use is fast.

A plugin's stdio server is otherwise provisioned (``uv sync``, a ``uvx`` warm-up, a
``clio-kit --prepare``) the first time a page or a turn starts it, which makes that first
open wait for the whole build. After an install, an update or a trust decision
(:func:`clio_agent.gact.plugin_authoring.installed_changed`) this module runs the same
provisioning step (:func:`clio_agent.tools.mcp_prepare.provision`) in the background for
every trusted installed plugin it has not built yet, with ``UV_COMPILE_BYTECODE=1`` so
the server's first import does not compile either. The provisioning marker it leaves
lets the first start find the environment cached.

Nothing here can fail an install: every error is logged and the server's first start
builds as it would have. An untrusted plugin is not built (building runs its build
backend), and ``tools.mcp.install_prebuild`` turns the step off.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
#: ``(marketplace, plugin, root)`` of every plugin a build was started for in this process.
_STARTED: set[tuple[str, str, str]] = set()


def enabled() -> bool:
    """``tools.mcp.install_prebuild`` (true): build environments when a plugin is installed."""

    from clio_agent import conf  # noqa: PLC0415

    return conf.resolve(
        "tools.mcp.install_prebuild",
        env="CLIO_MCP_INSTALL_PREBUILD",
        default=True,
        cast=conf.as_bool,
    )


def _plugin_specs(app: Any, record: Any) -> dict[str, Any]:
    """``{server name: MCPServerSpec}`` of ``record``'s own stdio servers, expanded."""

    from clio_agent.gact.agents.resolution import supplied_server_values  # noqa: PLC0415
    from clio_agent.plugins.server_rows import server_row  # noqa: PLC0415
    from clio_agent.plugins.servers import server_declarations  # noqa: PLC0415
    from clio_agent.tools.mcp_config import specs_from_mapping  # noqa: PLC0415

    rows = {
        name: server_row(record, name, spec)
        for name, spec in record.mcp_servers.items()
        if isinstance(spec, Mapping) and spec.get("type") != "clio-connection"
    }
    if not rows:
        return {}
    supplied = supplied_server_values(app, rows, workspace_root=None)
    grouped = server_declarations(rows, supplied)
    return {
        name: spec
        for group in grouped.values()
        for name, spec in specs_from_mapping(group, source=f"plugin:{record.id}").items()
        if spec.usable and spec.transport == "stdio"
    }


def prebuild(app: Any, record: Any) -> dict[str, str]:
    """Provision ``record``'s stdio servers now; return ``{server: outcome or error}``."""

    from clio_agent.tools import mcp_prepare  # noqa: PLC0415

    outcomes: dict[str, str] = {}
    try:
        specs = _plugin_specs(app, record)
    except Exception as exc:  # noqa: BLE001 - a prebuild never fails an install
        logger.warning("plugin prebuild skipped plugin=%s error=%r", record.id, exc)
        return {"*": f"skipped: {exc}"}
    for name, spec in specs.items():
        compiled = replace(spec, env={"UV_COMPILE_BYTECODE": "1", **dict(spec.env)})
        try:
            outcomes[name] = mcp_prepare.provision(name, compiled)
        except Exception as exc:  # noqa: BLE001 - the first start builds it again
            outcomes[name] = f"failed: {exc}"
            logger.warning(
                "plugin prebuild failed plugin=%s server=%s error=%r", record.id, name, exc
            )
    if outcomes:
        logger.info("plugin prebuild plugin=%s outcomes=%s", record.id, outcomes)
    return outcomes


def _pending(app: Any) -> list[Any]:
    from clio_agent.gact.agents.resolution import plugin_service  # noqa: PLC0415

    store = plugin_service(app).store
    pending = []
    with _LOCK:
        for record in store.installed_records():
            key = (str(record.marketplace), str(record.id), str(record.root))
            if key in _STARTED or not record.mcp_servers or not store.trusted(record):
                continue
            _STARTED.add(key)
            pending.append(record)
    return pending


def schedule(app: Any) -> threading.Thread | None:
    """Start building every trusted installed plugin not built yet; never raises."""

    try:
        if not enabled():
            return None
    except Exception as exc:  # noqa: BLE001 - a prebuild never fails an install
        logger.warning("plugin prebuild not scheduled error=%r", exc)
        return None

    def run() -> None:
        try:
            records = _pending(app)
        except Exception as exc:  # noqa: BLE001 - a prebuild never fails an install
            logger.warning("plugin prebuild not started error=%r", exc)
            return
        for record in records:
            prebuild(app, record)

    thread = threading.Thread(target=run, name="clio-plugin-prebuild", daemon=True)
    thread.start()
    return thread


def forget() -> None:
    """Forget which plugins were built (tests)."""

    with _LOCK:
        _STARTED.clear()
