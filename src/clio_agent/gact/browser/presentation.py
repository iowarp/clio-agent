"""How a browser call reads in the transcript, nested under its ``run_code`` row.

Each row is the action, a short form of the target (role and name, or the ref) and the
host: "Act on page · click button "Next" · arxiv.org". Expanding shows what came back: a
snapshot collapsed to its line count with the text on expand, a value read, the
screenshot as its artifact (the UI previews image artifacts inline), downloads, and what
changed under the agent (the person acted, a host was blocked). Exact arguments and the
raw result stay in technical details. A failed call keeps its typed reason, which the
observer adds as its own error block.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from clio_agent.gact.browser.targets import describe

__all__ = ["presenter_for", "starting_for"]

Presenter = Callable[[Mapping[str, Any], Any, Any], dict[str, Any]]
_VALUE_CHARS = 4000


def _row(result: Any, structured: Any) -> Mapping[str, Any]:
    if isinstance(structured, Mapping):
        return structured
    if isinstance(result, Mapping):
        return result
    if isinstance(result, str) and result[:1] == "{":
        try:
            parsed = json.loads(result)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, Mapping) else {}
    return {}


def _host(args: Mapping[str, Any], row: Mapping[str, Any]) -> str:
    page = row.get("page") if isinstance(row.get("page"), Mapping) else {}
    host = str(page.get("host") or "")
    if host:
        return host
    from urllib.parse import urlsplit  # noqa: PLC0415

    return (urlsplit(str(args.get("url") or "")).hostname or "").lower()


def _subject(name: str, args: Mapping[str, Any], row: Mapping[str, Any]) -> str:
    target = describe(args.get("target")) if args.get("target") else ""
    action = str(args.get("action") or args.get("what") or args.get("until") or "")
    head = " ".join(part for part in (action, target) if part)
    host = _host(args, row)
    if name in {"browser_open", "browser_navigate"}:
        url = str(args.get("url") or "")
        head = " ".join(p for p in (action if name == "browser_navigate" else "", url) if p)
    parts = [p for p in (head, host) if p]
    return " · ".join(parts) or str(args.get("page") or "")


def _notes(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    if row.get("person_acted"):
        blocks.append(
            {
                "id": "person-acted",
                "type": "text",
                "severity": "info",
                "text": "The person used this page since the agent's previous action on it.",
            }
        )
    blocked = row.get("navigation_blocked")
    if isinstance(blocked, Mapping):
        blocks.append(
            {
                "id": "navigation-blocked",
                "type": "text",
                "severity": "warning",
                "label": "Navigation blocked",
                "text": f"The page tried to load {blocked.get('host')}, which is not allowed "
                f"for this conversation ({blocked.get('reason')}).",
            }
        )
    for index, download in enumerate(row.get("downloads") or ()):
        if not isinstance(download, Mapping):
            continue
        name = str(download.get("name") or "download")
        if download.get("artifact_id"):
            blocks.append(
                {
                    "id": f"download-{index}",
                    "type": "link",
                    "target": "artifact",
                    "uri": str(download["artifact_id"]),
                    "label": f"Downloaded {name}",
                }
            )
        else:
            blocks.append(
                {
                    "id": f"download-{index}",
                    "type": "text",
                    "severity": "error" if download.get("error") else "info",
                    "text": f"Download {name}: {download.get('error') or download.get('path')}",
                }
            )
    return blocks


def _page_link(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    page = row.get("page") if isinstance(row.get("page"), Mapping) else {}
    url = str(page.get("url") or "")
    if not url.startswith(("http://", "https://")):
        return []
    label = str(page.get("title") or url)
    return [{"id": "page", "type": "link", "target": "url", "uri": url, "label": label}]


def _value_text(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, indent=1, default=str)
    return text[:_VALUE_CHARS] + ("\n…" if len(text) > _VALUE_CHARS else "")


def _body(
    name: str, args: Mapping[str, Any], row: Mapping[str, Any]
) -> tuple[str, list[dict[str, Any]]]:
    page = row.get("page") if isinstance(row.get("page"), Mapping) else {}
    page_id = str(page.get("id") or args.get("page") or "")
    if name == "browser_open":
        return f"Opened {page_id}", _page_link(row)
    if name == "browser_pages":
        pages = [p for p in row.get("pages") or () if isinstance(p, Mapping)]
        lines = [f"{p.get('id')} · {p.get('label') or 'untitled'} · {p.get('url')}" for p in pages]
        summary = f"{len(pages)} page{'s' if len(pages) != 1 else ''}"
        text = "\n".join(lines) or "No pages are open."
        return summary, [{"id": "pages", "type": "text", "text": text}]
    if name == "browser_close":
        return f"Closed {page_id}", []
    if name == "browser_navigate":
        status = row.get("status")
        return (f"HTTP {status}" if status else "Loaded"), _page_link(row)
    if name == "browser_snapshot":
        lines = int(row.get("lines") or 0)
        block = {
            "id": "snapshot",
            "type": "code",
            "language": "yaml",
            "label": "Page structure",
            "text": str(row.get("snapshot") or ""),
        }
        return f"{lines} line{'s' if lines != 1 else ''}", [block]
    if name == "browser_read":
        value = row.get("value")
        text = _value_text(value)
        summary = text.splitlines()[0][:80] if text else "Empty"
        return summary, [{"id": "value", "type": "text", "label": "Read", "text": text}]
    if name == "browser_act":
        return "Done", []
    if name == "browser_wait":
        return "Reached", []
    if name == "browser_screenshot":
        shot = row.get("screenshot") if isinstance(row.get("screenshot"), Mapping) else {}
        size = int(shot.get("size_bytes") or 0)
        blocks: list[dict[str, Any]] = []
        if shot.get("artifact_id"):
            blocks.append(
                {
                    "id": "screenshot",
                    "type": "link",
                    "target": "artifact",
                    "uri": str(shot["artifact_id"]),
                    "label": f"Screenshot of {page.get('host') or page_id}",
                    "media_type": "image/png",
                }
            )
        elif shot.get("path"):
            blocks.append(
                {"id": "screenshot", "type": "text", "label": "Saved", "text": str(shot["path"])}
            )
        return f"PNG, {size // 1024} KB" if size else "Screenshot", blocks
    if name == "browser_evaluate":
        script = {
            "id": "expression",
            "type": "code",
            "language": "javascript",
            "label": "Script",
            "text": str(args.get("expression") or ""),
        }
        value = {
            "id": "value",
            "type": "text",
            "label": "Value",
            "text": _value_text(row.get("value")),
        }
        return "Evaluated", [script, value]
    return "", []


def presenter_for(name: str) -> Presenter:
    """The finished-call presenter of one browser tool."""

    def present(args: Mapping[str, Any], result: Any, structured: Any) -> dict[str, Any]:
        row = _row(result, structured)
        view: dict[str, Any] = {"subject": _subject(name, args, row), "summary": "", "blocks": []}
        if not row:
            return view  # a failed call: the observer adds its typed error block
        summary, blocks = _body(name, args, row)
        view["summary"] = summary
        view["blocks"] = [*blocks, *_notes(row)]
        if isinstance(row.get("navigation_blocked"), Mapping):
            view["status"] = "degraded"
        return view

    present.__name__ = f"present_{name}"
    return present


def starting_for(name: str) -> Callable[[Mapping[str, Any]], dict[str, Any]]:
    """The running row: what the call is doing to what, before it returns."""

    def starting(args: Mapping[str, Any]) -> dict[str, Any]:
        blocks: list[dict[str, Any]] = []
        if name == "browser_evaluate" and args.get("expression"):
            blocks.append(
                {
                    "id": "expression",
                    "type": "code",
                    "language": "javascript",
                    "label": "Script",
                    "text": str(args["expression"]),
                }
            )
        return {"subject": _subject(name, args, {}), "summary": "", "blocks": blocks}

    starting.__name__ = f"starting_{name}"
    return starting
