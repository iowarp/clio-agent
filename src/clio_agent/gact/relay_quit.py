"""Close the person's open relays when CLIO Desktop quits, on their say-so.

CLIO Desktop's quit prompt offers "Also close open relays" when the clio-relay plugin
is installed, trusted and has endpoints (sites) set up. Confirming with it ticked calls
the plugin's ``shutdown`` tool once, for every site and the local machine. The tool
stops relay-owned long-lived processes (site serve workers, hop proxies, connectors,
tunnels, frpc, a detached local relay server) and leaves running jobs alone, listing
them as still running. Its result is ``relay.shutdown.v1``::

    {schema, stopped: [{site, kind, pid?, detail}],
     still_running: [{site, kind, id, detail}],
     failures: [{site, error, detail}]}

An unreachable site is a failure entry, never an exception. The call runs in the
plugin's page context, outside any turn, and is bounded: quitting never waits longer
than :data:`DEADLINE_MARGIN_S` past the tool's own ``timeout_s``. The person approved
this exact call in the quit prompt, so it does not raise a second permission question.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

RELAY_PLUGIN = "clio-relay"
SHUTDOWN_TOOL = "shutdown"
SHUTDOWN_SCHEMA = "relay.shutdown.v1"
#: The tool's own per-site bound when the caller names none.
DEFAULT_TIMEOUT_S = 15.0
MAX_TIMEOUT_S = 60.0
#: Extra time for starting the server and returning its answer.
DEADLINE_MARGIN_S = 5.0


def _relay_record(app: Any) -> tuple[Any, Any] | None:
    from clio_agent.gact.agents.resolution import plugin_service  # noqa: PLC0415

    service = plugin_service(app)
    service.ready()
    store = service.store
    record = next((row for row in store.records() if row.id == RELAY_PLUGIN), None)
    return (store, record) if record is not None else None


def _endpoint_count(record: Any) -> int:
    """The entries in the plugin's declared setup files (its ``sites.yaml``)."""

    from clio_agent.plugins.servers import plugin_data_dir  # noqa: PLC0415
    from clio_agent.plugins.setup_state import setup_files  # noqa: PLC0415

    root = plugin_data_dir(str(record.id))
    count = 0
    for relative in setup_files(record):
        path = Path(root, relative)
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError):
            continue
        if isinstance(loaded, list):
            count += sum(1 for item in loaded if item)
        elif isinstance(loaded, Mapping):
            sites = loaded.get("sites")
            count += len(sites) if isinstance(sites, (list, Mapping)) else 0
    return count


def relay_status(app: Any) -> dict[str, Any]:
    """Whether the quit prompt offers to close relays, and how many endpoints there are."""

    found = _relay_record(app)
    if found is None:
        return {"installed": False, "available": False, "endpoints": 0}
    store, record = found
    trusted = bool(store.trusted(record))
    endpoints = _endpoint_count(record) if trusted else 0
    return {
        "installed": True,
        "available": trusted and endpoints > 0,
        "endpoints": endpoints,
        "plugin": RELAY_PLUGIN,
    }


def _empty_result(**extra: Any) -> dict[str, Any]:
    return {"schema": SHUTDOWN_SCHEMA, "stopped": [], "still_running": [], "failures": [], **extra}


def _rows(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _structured(raw: Any) -> Mapping[str, Any] | None:
    structured = getattr(raw, "structured_content", None)
    if structured is None and isinstance(raw, Mapping):
        structured = raw.get("structuredContent")
    if isinstance(structured, Mapping) and isinstance(structured.get("result"), Mapping):
        structured = structured["result"]  # a tool returning a bare model wraps it
    return structured if isinstance(structured, Mapping) else None


def read_shutdown_result(raw: Any) -> dict[str, Any]:
    """The tool's ``relay.shutdown.v1`` answer, or one failure saying why it was unreadable."""

    from clio_agent.gact.server_actions import error_text, is_error_result  # noqa: PLC0415

    if is_error_result(raw):
        return _empty_result(
            failures=[{"site": "", "error": "tool_error", "detail": error_text(raw)}]
        )
    structured = _structured(raw)
    if structured is None:
        return _empty_result(
            failures=[{"site": "", "error": "unreadable_result", "detail": "no structured content"}]
        )
    return {
        "schema": str(structured.get("schema") or SHUTDOWN_SCHEMA),
        "stopped": _rows(structured.get("stopped")),
        "still_running": _rows(structured.get("still_running")),
        "failures": _rows(structured.get("failures")),
    }


class _Unsupported(RuntimeError):
    """The installed relay server has no ``shutdown`` tool (an older plugin)."""


def _call_shutdown(namespace: str, timeout_s: float):
    def call(executor: Any) -> Any:
        from clio_agent.gact.plugin_tabs import _prepare_server  # noqa: PLC0415
        from clio_agent.gact.runtime.tool_permission_layers import (  # noqa: PLC0415
            person_answered,
        )
        from clio_agent.gact.server_actions import _listed_name, server_tools  # noqa: PLC0415

        _prepare_server(executor, namespace)
        if SHUTDOWN_TOOL not in {_listed_name(tool) for tool in server_tools(executor, namespace)}:
            raise _Unsupported(f"server {namespace!r} has no {SHUTDOWN_TOOL!r} tool")
        full = f"{namespace}_{SHUTDOWN_TOOL}"
        with person_answered(full):
            return executor.call_tool_result(full, {"all": True, "timeout_s": timeout_s})

    return call


def _log_result(result: Mapping[str, Any]) -> None:
    logger.info(
        "relay_shutdown stopped=%d still_running=%d failures=%d",
        len(result.get("stopped") or ()),
        len(result.get("still_running") or ()),
        len(result.get("failures") or ()),
    )
    for row in result.get("stopped") or ():
        logger.info(
            "relay_shutdown stopped site=%s kind=%s pid=%s detail=%s",
            row.get("site"),
            row.get("kind"),
            row.get("pid", ""),
            row.get("detail", ""),
        )
    for row in result.get("still_running") or ():
        logger.info(
            "relay_shutdown still_running site=%s kind=%s id=%s detail=%s",
            row.get("site"),
            row.get("kind"),
            row.get("id", ""),
            row.get("detail", ""),
        )
    for row in result.get("failures") or ():
        logger.warning(
            "relay_shutdown failure site=%s error=%s detail=%s",
            row.get("site"),
            row.get("error"),
            row.get("detail", ""),
        )


async def shutdown_relays(app: Any, *, timeout_s: float = DEFAULT_TIMEOUT_S) -> dict[str, Any]:
    """Stop every open relay, bounded by ``timeout_s`` plus :data:`DEADLINE_MARGIN_S`.

    Never raises: a missing plugin is ``skipped``; a slow or failing call is a failure row.
    """

    from clio_agent.gact.mcp_apps import _run_bound  # noqa: PLC0415
    from clio_agent.gact.off_loop import run_off_loop  # noqa: PLC0415
    from clio_agent.gact.plugin_pages import declared_pages, plugin_pages  # noqa: PLC0415
    from clio_agent.gact.plugin_tabs import PluginTabError  # noqa: PLC0415

    timeout_s = min(max(float(timeout_s), 1.0), MAX_TIMEOUT_S)
    deadline = timeout_s + DEADLINE_MARGIN_S
    loop = asyncio.get_running_loop()
    started = loop.time()

    async def run() -> dict[str, Any]:
        status = await run_off_loop(relay_status, app)
        if not status["available"]:
            reason = "not_installed" if not status["installed"] else "no_endpoints"
            return _empty_result(skipped=reason)
        pages = await run_off_loop(declared_pages, app)
        row = next((page for page in pages if page["plugin"] == RELAY_PLUGIN), None)
        if row is None:
            return _empty_result(skipped="no_page")
        context = await plugin_pages(app).context(row["plugin"], row["page"], create=True)
        raw = await _run_bound(app, context, _call_shutdown(str(row["server"]), timeout_s))
        return read_shutdown_result(raw)

    try:
        result = await asyncio.wait_for(run(), timeout=deadline)
    except TimeoutError:
        result = _empty_result(
            failures=[
                {
                    "site": "",
                    "error": "timeout",
                    "detail": f"relays did not answer within {deadline:.0f} s",
                }
            ]
        )
    except _Unsupported as exc:
        result = _empty_result(skipped="unsupported", detail=str(exc))
    except PluginTabError as exc:
        result = _empty_result(failures=[{"site": "", "error": exc.reason, "detail": str(exc)}])
    except Exception as exc:  # noqa: BLE001 - quitting must go on; the reason is logged
        logger.exception("relay_shutdown failed")
        result = _empty_result(
            failures=[{"site": "", "error": "shutdown_failed", "detail": str(exc)}]
        )
    result["elapsed_ms"] = int((loop.time() - started) * 1000)
    if result.get("skipped"):
        logger.info("relay_shutdown skipped=%s", result["skipped"])
    _log_result(result)
    return result


__all__ = [
    "RELAY_PLUGIN",
    "SHUTDOWN_SCHEMA",
    "read_shutdown_result",
    "relay_status",
    "shutdown_relays",
]
