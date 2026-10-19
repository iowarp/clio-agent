"""Human-readable NDP results using the shared, paged tool presentation contract."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import PureWindowsPath
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_SECRET = r"(?:[\w-]*(?:token|password|secret|api[_-]?key|access[_-]?key|signature|credential)|authorization|credentials|sig)"
_JSON_SECRET = re.compile(rf'("{_SECRET}"\s*:\s*)"(?:[^"\\]|\\.)*(?:"|$)', re.I)


def _safe(value: Any) -> str:
    """Keep scientific values and cursors visible while masking credential carriers."""
    text = (
        str(value)
        if not isinstance(value, (Mapping, list))
        else json.dumps(value, indent=2, ensure_ascii=False)
    )
    text = _JSON_SECRET.sub(r'\1"[redacted]"', text)
    text = re.sub(r"\bBearer\s+[\w.+/~=-]+", "Bearer [redacted]", text, flags=re.I)

    def url(match: re.Match[str]) -> str:
        try:
            parsed = urlsplit(match[0])
            query = [
                (key, "[redacted]" if re.fullmatch(_SECRET, key, re.I) else val)
                for key, val in parse_qsl(parsed.query, keep_blank_values=True)
            ]
            return urlunsplit(
                (
                    parsed.scheme,
                    parsed.netloc.rsplit("@", 1)[-1],
                    parsed.path,
                    urlencode(query),
                    parsed.fragment,
                )
            )
        except ValueError:
            return "[invalid URL]"

    return re.sub(r"https?://[^\s\"<>]+", url, text)


def _row(result: Any, structured: Any) -> Mapping[str, Any]:
    value = structured if isinstance(structured, Mapping) else result
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, Mapping) else {}


def _text(identifier: str, label: str, value: Any, **fields: Any) -> dict[str, Any]:
    return {"id": identifier, "type": "text", "label": label, "text": _safe(value), **fields}


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    def cell(value: Any) -> str:
        return _safe(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")

    return "\n".join(
        [
            "| " + " | ".join(headers) + " |",
            "| " + " | ".join("---" for _ in headers) + " |",
            *("| " + " | ".join(cell(value) for value in row) + " |" for row in rows),
        ]
    )


def ndp_presentation(args: Mapping[str, Any], result: Any, structured: Any) -> dict[str, Any]:
    """Show the actual discovery page, its resource identities, warnings and next page."""
    row = _row(result, structured)
    if not ("datasets" in row or "organizations" in row):
        return {
            "summary": "NDP discovery unavailable",
            "blocks": [
                _text(
                    "unavailable",
                    "Result",
                    row.get("message") or "No structured NDP discovery result was recorded.",
                    severity="warning",
                )
            ],
        }
    key = "datasets" if "datasets" in row else "organizations"
    entries = row.get(key, [])
    blocks = []
    filters = {key: value for key, value in args.items() if value != "" and value is not None}
    if filters:
        blocks.append(_text("search", "Search", filters))
    if key == "datasets":
        for index, dataset in enumerate(entries):
            resources = dataset.get("resources", [])
            detail = [
                dataset.get("description", ""),
                f"Organization: {dataset.get('organization') or 'Not specified'}",
                f"Catalog: {dataset.get('endpoint_name') or dataset.get('endpoint_id', '')}",
                f"Dataset ID: {dataset.get('id', '')}",
            ]
            if dataset.get("access"):
                detail.append(f"Access: {dataset['access']}")
            blocks.append(
                {
                    "id": f"dataset-{index}",
                    "type": "item",
                    "label": _safe(
                        dataset.get("title") or dataset.get("name") or dataset.get("id")
                    ),
                    "detail": _safe("\n".join(line for line in detail if line)),
                    "items": [
                        _safe(
                            f"{item.get('name') or item.get('path', '')}"
                            f" ({item.get('format') or 'unspecified format'}, "
                            f"{item.get('size', 0)} bytes)\nResource ID: {item.get('id', '')}"
                        )
                        for item in resources
                    ],
                }
            )
    elif entries:
        blocks.append(
            {
                "id": "organizations",
                "type": "markdown",
                "label": "Organizations",
                "text": _table(
                    ["Organization", "Catalog", "Catalog ID"],
                    [
                        [
                            item.get("id", ""),
                            item.get("endpoint_name", ""),
                            item.get("endpoint_id", ""),
                        ]
                        for item in entries
                    ],
                ),
            }
        )
    if not entries:
        blocks.append(_text("empty", "Results", f"No {key} matched this search."))
    if row.get("total") is not None:
        start = int(args.get("offset") or 0)
        blocks.append(
            _text(
                "page",
                "Result page",
                f"Showing {len(entries)} of {row['total']} datasets (offset {start}).",
            )
        )
    if row.get("next_offset") is not None:
        blocks.append(_text("next-page", "More results", f"Next offset: {row['next_offset']}"))
    blocks.extend(
        _text(f"warning-{i}", "Catalog warning", warning, severity="warning")
        for i, warning in enumerate(row.get("warnings", []))
    )
    return {"summary": f"{len(entries)} NDP {key}", "blocks": blocks}


def _connection(row: Mapping[str, Any], args: Mapping[str, Any]) -> dict[str, Any]:
    name = row.get("endpoint_name") or row.get("name") or args.get("endpoint_id", "")
    url = row.get("endpoint_url") or row.get("url", "")
    route = row.get("ssh_route") or {}
    ssh = row.get("ssh_profile") or route.get("profile") or route.get("host")
    return _text(
        "endpoint",
        "Endpoint",
        "\n".join(str(value) for value in (name, url, f"SSH: {ssh}" if ssh else "") if value),
    )


def _capture(row: Mapping[str, Any], args: Mapping[str, Any]) -> dict[str, Any]:
    blocks = [
        _connection(row, args),
        _text("operation", "Operation", row.get("operation_id") or args.get("operation_id", "")),
    ]
    for key, label in [("parameters", "Parameters"), ("body", "Request body")]:
        value = row.get(key, args.get(key))
        if value:
            blocks.append(
                {
                    "id": key,
                    "type": "code",
                    "label": label,
                    "language": "json",
                    "text": _safe(value),
                }
            )
    receipt = [
        f"HTTP {row.get('status_code', 'unknown')}",
        f"{row.get('bytes_received', 0)} bytes saved",
        f"Content type: {row.get('content_type', 'unknown')}",
        f"Capture ended: {row.get('stop_reason', 'unknown')}",
    ]
    if "text/event-stream" in str(row.get("content_type")):
        receipt.append(f"SSE events: {row.get('events', 0)} (includes control events)")
    blocks.append(_text("receipt", "Response", "\n".join(receipt)))
    if resource_id := row.get("resource_id"):
        name = PureWindowsPath(str(row.get("workspace_path") or "Saved NDP response")).name
        blocks.append(
            {
                "id": "saved-response",
                "type": "link",
                "target": "resource",
                "uri": resource_id,
                "label": name,
            }
        )
    preview = row.get("preview")
    if isinstance(preview, str) and preview:
        language = "text"
        if "json" in str(row.get("content_type")):
            try:
                preview = json.dumps(json.loads(preview), indent=2, ensure_ascii=False)
                language = "json"
            except ValueError:
                pass  # An incomplete preview remains visible exactly as captured.
        blocks.append(
            {
                "id": "response-preview",
                "type": "code",
                "language": language,
                "label": "Captured response",
                "text": _safe(preview),
            }
        )
    else:
        blocks.append(
            _text(
                "response-preview", "Captured response", "No response body in the recorded preview."
            )
        )
    if row.get("preview_truncated") or row.get("stop_reason") not in {None, "completed"}:
        blocks.append(
            _text(
                "capture-limit",
                "Capture limit",
                "This is a bounded capture or preview. Open the saved response for the retained bytes; the remote producer may still be running.",
                severity="warning",
            )
        )
    if row.get("sha256"):
        blocks.append(_text("checksum", "SHA-256", row["sha256"]))
    return {
        "summary": f"{row['bytes_received']} bytes captured, {row.get('stop_reason', 'unknown')}",
        "blocks": blocks,
    }


def endpoint_presentation(args: Mapping[str, Any], result: Any, structured: Any) -> dict[str, Any]:
    """Present connection discovery, API operations, captures and verified artifact uploads."""
    row = _row(result, structured)
    if "bytes_received" in row:
        return _capture(row, args)
    if row.get("state") == "sign_in_required":
        return {
            "summary": "NDP sign-in required",
            "blocks": [
                _text(
                    "sign-in",
                    "Sign-in required",
                    "Sign in to your NDP account in Connected data, then retry this operation.",
                    severity="warning",
                )
            ],
        }
    if row.get("status") == "choose_api_url":
        return {
            "summary": "Choose an NDP API",
            "blocks": [
                _text(
                    "discovery", "Connection discovery", row.get("message", "Choose an API URL.")
                ),
                _text(
                    "candidates",
                    "Discovered API URLs",
                    "\n".join(row.get("candidates", [])) or "No existing NDP API was found.",
                ),
            ],
        }
    if row.get("artifact_id") and "state" in row:
        verified = row["state"] == "verified"
        blocks = [
            _connection(row, args),
            _text(
                "upload",
                "Stored object",
                {
                    "bucket": row.get("bucket"),
                    "key": row.get("key"),
                    "bytes": row.get("size"),
                    "verification": row["state"],
                    "reused": row.get("reused", False),
                    "sha256": row.get("sha256"),
                },
            ),
        ]
        blocks.append(
            {
                "id": "artifact",
                "type": "link",
                "target": "artifact",
                "uri": row["artifact_id"],
                "label": "Uploaded artifact",
            }
        )
        blocks.append(
            _text(
                "publication",
                "Catalog registration",
                "Object storage is separate from dataset registration and federation publication.",
            )
        )
        return {
            "summary": f"{row.get('size', 0)} bytes verified in NDP storage"
            if verified
            else "NDP upload unverified",
            "blocks": blocks,
        }
    if "operations" in row:
        operations = row["operations"]
        total = row.get("operation_count", len(operations))
        blocks = [_connection(row, args)]
        if row.get("api_title"):
            blocks.append(_text("api", "API", row["api_title"]))
        blocks.append(
            _text(
                "inventory",
                "Operations",
                f"{len(operations)} matching operations; {total} in this API."
                + (f" Search: {row['query']}" if row.get("query") else ""),
            )
        )
        if operations:
            blocks.append(
                {
                    "id": "operations",
                    "type": "markdown",
                    "label": "Available operations",
                    "text": _table(
                        ["Operation", "Description", "Parameters"],
                        [
                            [
                                item["id"],
                                item.get("summary", ""),
                                ", ".join(
                                    str(p.get("name", "")) for p in item.get("parameters", [])
                                ),
                            ]
                            for item in operations
                        ],
                    ),
                }
            )
        if row.get("search_note"):
            blocks.append(_text("search-note", "Search result", row["search_note"]))
        return {"summary": f"{len(operations)} Endpoint operations", "blocks": blocks}
    return {
        "summary": "NDP result unavailable",
        "blocks": [
            _text(
                "unavailable",
                "Result",
                row.get("message") or "No structured NDP result was recorded.",
                severity="warning",
            )
        ],
    }
