"""How an ``offer_plugin`` call reads (the tool presentation contract).

One row: "Offer <plugin or connector>". Expansion shows what the plugin is (its own
description) and the agent's reason, then what
the person chose and what joined: for a connector its tools (the connector offer's own
presentation, :func:`clio_agent.gact.connector_offer.present`); for a plugin its skills
and tools, or the typed refusal. With no plugin named, "List" and the rows that could be
added.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _listed(row: Mapping[str, Any]) -> str:
    lines = []
    for item in row.get("offerable") or []:
        kind = str(item.get("kind") or "")
        where = (
            f" (connector, {'on' if item.get('enabled') else 'off'})"
            if kind == "connector"
            else (f" [{item['marketplace']}]" if item.get("marketplace") else "")
        )
        lines.append(f"{item.get('name')}{where}: {item.get('summary') or ''}".rstrip(": "))
    return "\n".join(lines) or "None"


def present(args: Mapping[str, Any], result: Any, structured: Any) -> dict[str, Any]:
    """The finished call's row and blocks."""

    from clio_agent.gact import connector_offer, plugin_offer  # noqa: PLC0415

    row = structured if isinstance(structured, Mapping) else result
    row = row if isinstance(row, Mapping) else {}
    if "offerable" in row and not row.get("error"):
        return {
            "action": "List",
            "subject": "listed",
            "summary": str(row.get("message") or ""),
            "blocks": [
                {"id": "listed", "type": "text", "text": "plugins to offer"},
                {"id": "offerable", "type": "text", "label": "Can be added", "text": _listed(row)},
            ],
        }
    if "connector" in row or "connector_name" in row:
        return connector_offer.present({"connector": args.get("plugin"), **args}, result, row)
    name = str(row.get("plugin_name") or row.get("plugin") or args.get("plugin") or "plugin")
    blocks: list[dict[str, Any]] = [{"id": "plugin", "type": "text", "text": name}]
    if description := str(row.get("description") or ""):
        blocks.append({"id": "what", "type": "text", "label": "What it is", "text": description})
    if reason := str(row.get("reason") or args.get("reason") or ""):
        blocks.append({"id": "reason", "type": "text", "label": "Why", "text": reason})
    head = {"action": "Offer", "subject": "plugin", "summary": str(row.get("message") or "")}
    if row.get("refusal") == plugin_offer.OFFER_DECLINED:
        blocks.append({"id": "outcome", "type": "text", "label": "Answer", "text": "Not now"})
        return {**head, "blocks": blocks}
    if error := str(row.get("error") or ""):
        blocks.append(
            {
                "id": "outcome",
                "type": "text",
                "label": "Not offered",
                "severity": "error",
                "text": str(row.get("message") or error),
                "detail": error,
            }
        )
        if row.get("offerable"):
            blocks.append(
                {"id": "offerable", "type": "text", "label": "Can be added", "text": _listed(row)}
            )
        status = "cancelled" if row.get("outcome") == "cancelled" else "failed"
        return {**head, "status": status, "blocks": blocks}
    answer = "Already here" if row.get("already_here") else "Added"
    blocks += [
        {"id": "outcome", "type": "text", "label": "Answer", "text": answer},
        {"id": "scope", "type": "text", "label": "Where", "text": "This conversation"},
    ]
    if skills := list(row.get("skills") or []):
        blocks.append(
            {
                "id": "skills",
                "type": "text",
                "label": f"Skills ({len(skills)})",
                "text": "\n".join(
                    f"{skill.get('id')}: {skill.get('description') or ''}".rstrip(": ")
                    for skill in skills
                ),
            }
        )
    tools = [str(name) for name in row.get("tools") or []]
    if tools:
        blocks.append(
            {
                "id": "tools",
                "type": "text",
                "label": f"Tools ({len(tools)})",
                "text": "\n".join(tools),
            }
        )
    if later := [str(name) for name in row.get("tools_next_message") or []]:
        blocks.append(
            {
                "id": "later",
                "type": "text",
                "label": "With the next message",
                "severity": "warning",
                "text": "\n".join(later),
            }
        )
    elif reason := str(row.get("join_reason") or ""):
        blocks.append(
            {
                "id": "later",
                "type": "text",
                "label": "Not joined this turn",
                "severity": "warning",
                "text": reason,
            }
        )
    return {**head, "blocks": blocks}


def present_start(args: Mapping[str, Any]) -> dict[str, Any]:
    """While the card waits: "Offer <plugin>" with the agent's reason."""

    name = str(args.get("plugin") or "")
    if not name:
        return {
            "action": "List",
            "subject": "listed",
            "summary": "",
            "blocks": [{"id": "listed", "type": "text", "text": "plugins to offer"}],
        }
    blocks: list[dict[str, Any]] = [{"id": "plugin", "type": "text", "text": name}]
    if reason := str(args.get("reason") or ""):
        blocks.append({"id": "reason", "type": "text", "label": "Why", "text": reason})
    return {"action": "Offer", "subject": "plugin", "summary": "", "blocks": blocks}
