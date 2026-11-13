"""What a view is called: its connector and its subject (FP-D2).

A view used to be named by its tool's imperative title ("Open a ParaView view"),
so every tab, reopen notice and frame read like a command. A view is named by what
it shows instead: ``"<connector>: <subject>"``.

* **Connector**: the label the person's saved connection carries (a catalog
  connector saves its entry name), else the catalog entry whose id is the server's
  namespace, else the tool's title, else the namespace itself.
* **Subject**: from the opening call's arguments, in the order the tool declares
  them. The first argument whose value names a file or a URL wins, reduced to its
  last path segment; otherwise the first short, single-line string argument. Code,
  long text and non-strings are never a subject.

The rule is presentation only; it decides nothing about the call.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any

logger = logging.getLogger(__name__)

#: Longer values are text or code, not a name.
SUBJECT_MAX = 80
#: A label is cut to this many characters (the client truncates visually as well).
LABEL_MAX = 120

_PATHLIKE = re.compile(r"[\\/]|\.[A-Za-z0-9]{1,8}$")


def _mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        dumped = dump(by_alias=True, exclude_none=True)
        if isinstance(dumped, Mapping):
            return dumped
    return {}


def tool_title(tool: Any) -> str:
    """The tool's human title (``title`` or ``annotations.title``), or ``""``."""

    tool_map = _mapping(tool)
    title = getattr(tool, "title", None) or tool_map.get("title")
    if not title:
        annotations = getattr(tool, "annotations", None) or tool_map.get("annotations")
        title = getattr(annotations, "title", None) or _mapping(annotations).get("title")
    return str(title).strip() if isinstance(title, str) else ""


def _declared_order(tool: Any) -> list[str]:
    tool_map = _mapping(tool)
    schema = getattr(tool, "inputSchema", None) or tool_map.get("inputSchema")
    properties = _mapping(schema).get("properties")
    return [str(key) for key in properties] if isinstance(properties, Mapping) else []


def _short(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text or len(text) > SUBJECT_MAX or "\n" in text:
        return ""
    return text


def _last_segment(text: str) -> str:
    trimmed = text.split("?", 1)[0].split("#", 1)[0].rstrip("/\\")
    return re.split(r"[\\/]", trimmed)[-1] or text


def call_subject(tool: Any, arguments: Mapping[str, Any] | None) -> str:
    """What the opening call was about, from its arguments (``""`` when nothing fits)."""

    if not arguments:
        return ""
    declared = _declared_order(tool)
    order = declared + [key for key in arguments if key not in declared]
    values = [_short(arguments.get(key)) for key in order]
    for value in values:
        if value and _PATHLIKE.search(value):
            return _last_segment(value)
    return next((value for value in values if value), "")


def connector_name(namespace: str | None) -> str:
    """The person-facing name of the server ``namespace`` (``""`` when none is known)."""

    if not namespace:
        return ""
    from clio_agent.gact.mcp_user_configuration import (  # noqa: PLC0415
        McpUserConfigurationError,
        user_mcp_labels,
    )

    try:
        label = user_mcp_labels().get(namespace, "")
    except McpUserConfigurationError as exc:
        # The broken file is reported where it is read for connections; here it only
        # means the saved label is unknown, so the catalog name is used.
        logger.info("mcp_app_label_unreadable_labels namespace=%s error=%s", namespace, exc)
        label = ""
    if label.strip():
        return label.strip()
    from clio_agent.connectors.entries import load_catalog  # noqa: PLC0415

    entry = load_catalog().entries.get(namespace)
    return entry.name if entry is not None else ""


def view_label(namespace: str | None, tool: Any, arguments: Mapping[str, Any] | None) -> str:
    """``"<connector>: <subject>"``, or as much of it as is known."""

    name = connector_name(namespace) or tool_title(tool) or (namespace or "")
    subject = call_subject(tool, arguments)
    label = f"{name}: {subject}" if name and subject else name or subject
    return label[:LABEL_MAX]
