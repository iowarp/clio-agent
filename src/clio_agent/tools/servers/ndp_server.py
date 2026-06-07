"""National Data Platform tools backed by clio-kit MCP."""

from __future__ import annotations

import csv
import json
import math
import os
import re
import shlex
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StdioTransport

from clio_agent.tools.file_policy import FilePolicyError, validate_write_path

ndp_server = FastMCP("ndp")

_GLOBAL_CKAN_API = "https://nationaldataplatform.org/catalog/api/3/action"
_MAX_STAGE_BYTES = 50 * 1024 * 1024
_WEBGET_CONNECT_TIMEOUT_S = 8
_WEBGET_MAX_TIME_S = 45
_WEBGET_RETRY_COUNT = 1
_WEBGET_RETRY_DELAY_S = 1
_WEBGET_SUBPROCESS_TIMEOUT_S = 60
_MAX_ARCGIS_FEATURES = 200
_MAX_CSV_PROFILE_ROWS = 250_000
_CLIO_KIT_SKIP_UVX_REASON = (
    "no configured/local/PATH clio-kit launcher; using public CKAN fallback "
    "instead of uvx package launch"
)
_SIZE_UNITS = {
    "b": 1,
    "byte": 1,
    "bytes": 1,
    "kb": 1024,
    "kib": 1024,
    "mb": 1024 * 1024,
    "mib": 1024 * 1024,
    "gb": 1024 * 1024 * 1024,
    "gib": 1024 * 1024 * 1024,
    "tb": 1024 * 1024 * 1024 * 1024,
    "tib": 1024 * 1024 * 1024 * 1024,
}


def _clio_kit_transport(server_name: str = "ndp") -> StdioTransport:
    """Return a stdio transport for a local clio-kit checkout or package command."""
    configured = os.environ.get("CLIO_KIT_PATH", "").strip()
    local_path = Path(configured).expanduser() if configured else Path("../clio-kit")
    local_path = local_path.resolve()
    if local_path.exists():
        return StdioTransport(
            command="uv",
            args=[
                "--directory",
                str(local_path),
                "run",
                "clio-kit",
                "mcp-server",
                server_name,
            ],
        )
    configured_command = os.environ.get("CLIO_KIT_COMMAND", "").strip()
    if configured_command:
        parts = shlex.split(configured_command)
        if parts:
            return StdioTransport(
                command=parts[0],
                args=[*parts[1:], "mcp-server", server_name],
            )

    path_command = shutil.which("clio-kit")
    if path_command:
        return StdioTransport(command=path_command, args=["mcp-server", server_name])

    return StdioTransport(
        command="uvx",
        args=["--from", "clio-kit", "clio-kit", "mcp-server", server_name],
    )


def _clio_kit_launcher_source() -> str:
    """Return how CLIO would launch clio-kit, without starting it."""

    configured = os.environ.get("CLIO_KIT_PATH", "").strip()
    local_path = Path(configured).expanduser() if configured else Path("../clio-kit")
    if local_path.resolve().exists():
        return "local_path"
    if os.environ.get("CLIO_KIT_COMMAND", "").strip():
        return "explicit_command"
    if shutil.which("clio-kit"):
        return "path_command"
    if os.environ.get("CLIO_KIT_ALLOW_UVX", "").strip().lower() in {"1", "true", "yes", "on"}:
        return "uvx"
    return ""


def _should_try_clio_kit(server: str) -> bool:
    """Return whether an NDP call should start clio-kit for this request."""

    if server != "global":
        return True
    return bool(_clio_kit_launcher_source())


def _annotate_ckan_skip(payload: dict[str, Any]) -> dict[str, Any]:
    """Record that public CKAN was used without probing the broken uvx path."""

    meta = payload.setdefault("_meta", {})
    if isinstance(meta, dict):
        meta.setdefault("clio_kit_skipped", True)
        meta.setdefault("clio_kit_skip_reason", _CLIO_KIT_SKIP_UVX_REASON)
    return payload


def _decode_text_content(result: Any) -> dict[str, Any]:
    """Decode a clio-kit FastMCP tool result into a plain dictionary."""
    data = getattr(result, "data", None)
    if isinstance(data, dict):
        return data
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        return structured
    for part in getattr(result, "content", []) or []:
        text = getattr(part, "text", "")
        if not text:
            continue
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            return {"text": text}
        if isinstance(decoded, dict):
            return decoded
        return {"data": decoded}
    return {}


def _tool_error(
    *,
    code: str,
    message: str,
    next_action: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return CLIO's structured tool error shape."""
    error: dict[str, Any] = {
        "type": "tool_error",
        "code": code,
        "message": message,
        "next_action": next_action,
    }
    if details:
        error["details"] = details
    return {"error": error}


def _clean_optional_text(value: str | None) -> str | None:
    """Normalize planner-produced empty strings to absent optional values."""
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def _clean_server(value: str | None, *, allowed: set[str]) -> str:
    """Normalize NDP server selector while preserving explicit valid choices."""
    cleaned = _clean_optional_text(value)
    if cleaned in allowed:
        return cleaned
    return "global"


def _clean_string_list(value: list[str] | None) -> list[str] | None:
    """Remove empty planner placeholders from optional string lists."""
    if not value:
        return None
    cleaned = [str(item).strip() for item in value if str(item).strip()]
    return cleaned or None


def _clean_limit(value: int | str | None) -> int | None:
    """Normalize optional result limit from planner/tool arguments."""
    if value is None or value == "":
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return max(1, min(parsed, 20))


def _clean_max_bytes(value: int | str | None) -> int:
    """Normalize optional byte limit for resource staging."""
    if value is None or value == "":
        return _MAX_STAGE_BYTES
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return _MAX_STAGE_BYTES
    return max(1, parsed)


def _compact_resource_formats(resources: Any) -> tuple[list[str], int, list[str], list[str]]:
    """Return compact resource format/name/url summaries for an NDP dataset row."""
    if not isinstance(resources, list):
        return [], 0, [], []
    formats = sorted(
        {
            str(resource.get("format")).upper()
            for resource in resources
            if isinstance(resource, dict) and resource.get("format")
        }
    )
    names = [
        str(resource.get("name") or resource.get("url") or "").strip()
        for resource in resources
        if isinstance(resource, dict) and (resource.get("name") or resource.get("url"))
    ]
    urls = [
        str(resource.get("url") or "").strip()
        for resource in resources
        if isinstance(resource, dict) and resource.get("url")
    ]
    return formats[:5], len(resources), names[:30], urls[:8]


def _compact_resource_summaries(resources: Any) -> list[dict[str, Any]]:
    """Return bounded resource summaries with enough detail for selection."""

    if not isinstance(resources, list):
        return []
    summaries: list[dict[str, Any]] = []
    for index, resource in enumerate(resources[:30]):
        if not isinstance(resource, dict):
            continue
        summary = {
            "index": index,
            "name": resource.get("name"),
            "format": resource.get("format"),
            "size": resource.get("size") or resource.get("resSize"),
            "url": resource.get("url"),
        }
        summaries.append({key: value for key, value in summary.items() if value})
    return summaries


def _compact_dataset(row: Any) -> Any:
    """Keep catalog rows useful for LLM synthesis without flooding context."""
    if not isinstance(row, dict):
        return row
    formats, resource_count, resource_names, resource_urls = _compact_resource_formats(
        row.get("resources")
    )
    notes = str(row.get("notes") or "").strip()
    if len(notes) > 120:
        notes = notes[:117] + "..."
    compacted = {
        "id": row.get("id"),
        "name": row.get("name"),
        "title": row.get("title"),
        "owner_org": row.get("owner_org"),
        "notes": notes,
        "resource_count": resource_count,
        "resource_formats": formats,
        "resource_names": resource_names,
    }
    resource_summaries = _compact_resource_summaries(row.get("resources"))
    if resource_summaries:
        compacted["resource_summaries"] = resource_summaries
        compacted["resource_summaries_truncated"] = resource_count > len(resource_summaries)
    if resource_urls:
        compacted["resource_urls"] = resource_urls
    return compacted


def _global_ckan_package_show(dataset_identifier: str) -> dict[str, Any]:
    """Fetch one dataset directly from the public NDP CKAN API."""
    response = requests.get(
        f"{_GLOBAL_CKAN_API}/package_show",
        params={"id": dataset_identifier},
        timeout=20,
    )
    response.raise_for_status()
    decoded = response.json()
    if not decoded.get("success") or not isinstance(decoded.get("result"), dict):
        raise ValueError(f"CKAN package_show returned unsuccessful response: {decoded!r}")
    return decoded["result"]


def _global_ckan_organization_list(name_filter: str | None) -> dict[str, Any]:
    """Fetch organizations directly from public NDP CKAN."""
    response = requests.get(
        f"{_GLOBAL_CKAN_API}/organization_list",
        params={"all_fields": "true"},
        timeout=20,
    )
    response.raise_for_status()
    decoded = response.json()
    result = decoded.get("result")
    if not decoded.get("success") or not isinstance(result, list):
        raise ValueError(f"CKAN organization_list returned unsuccessful response: {decoded!r}")

    needle = (name_filter or "").strip().lower()
    organizations: list[Any] = []
    for row in result:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        title = str(row.get("title") or row.get("display_name") or "").strip()
        if needle and needle not in name.lower() and needle not in title.lower():
            continue
        organizations.append(
            {
                "id": row.get("id"),
                "name": name,
                "title": title,
                "package_count": row.get("package_count"),
            }
        )
    return {
        "organizations": organizations[:8],
        "count": len(organizations),
        "server": "global",
        "name_filter": name_filter,
        "_meta": {
            "tool": "list_organizations",
            "status": "success",
            "source": "ckan_organization_list",
        },
        "organizations_truncated": len(organizations) > 8,
    }


def _global_ckan_package_search(args: dict[str, Any]) -> dict[str, Any]:
    """Search datasets directly from public NDP CKAN."""
    terms: list[str] = []
    for value in args.get("search_terms") or []:
        if str(value).strip():
            terms.append(str(value).strip())
    for key in (
        "search_term",
        "dataset_name",
        "dataset_title",
        "dataset_description",
        "resource_name",
        "resource_description",
        "resource_url",
    ):
        value = args.get(key)
        if value and str(value).strip():
            terms.append(str(value).strip())
    query = " ".join(terms).strip() or "*:*"

    filters: list[str] = []
    owner_org = args.get("owner_org")
    if owner_org:
        filters.append(f"organization:{owner_org}")
    resource_format = args.get("resource_format")
    if resource_format:
        filters.append(f"res_format:{resource_format}")

    limit = args.get("limit") or 10
    params: dict[str, Any] = {"q": query, "rows": min(int(limit), 20)}
    if filters:
        params["fq"] = " ".join(filters)
    response = requests.get(
        f"{_GLOBAL_CKAN_API}/package_search",
        params=params,
        timeout=20,
    )
    response.raise_for_status()
    decoded = response.json()
    result = decoded.get("result")
    if not decoded.get("success") or not isinstance(result, dict):
        raise ValueError(f"CKAN package_search returned unsuccessful response: {decoded!r}")
    rows = result.get("results") or []
    if not isinstance(rows, list):
        rows = []
    compacted = [_compact_dataset(row) for row in rows[: min(len(rows), 20)]]
    compacted, station_filter = _filter_earthscope_station_resource_results(
        compacted,
        args,
    )
    visible_rows = compacted[:4]
    return _annotate_search_coverage(
        {
            "datasets": visible_rows,
            "count": len(visible_rows),
            "total_found": len(compacted) if station_filter else result.get("count", len(compacted)),
            "server": "global",
            "_meta": {
                "tool": "search_datasets",
                "status": "success",
                "source": "ckan_package_search",
                "clio_kit_fallback": True,
                **({"station_resource_filter": station_filter} if station_filter else {}),
            },
            "datasets_truncated": len(compacted) > 4,
        },
        args,
    )


def _search_term_text(args: dict[str, Any]) -> str:
    terms: list[str] = []
    for key in (
        "search_terms",
        "search_keys",
        "filter_list",
    ):
        value = args.get(key)
        if isinstance(value, list):
            terms.extend(str(item) for item in value if str(item).strip())
    for key in (
        "search_term",
        "dataset_name",
        "dataset_title",
        "dataset_description",
        "resource_name",
        "resource_description",
        "resource_url",
        "resource_format",
        "owner_org",
    ):
        value = args.get(key)
        if value and str(value).strip():
            terms.append(str(value).strip())
    return " ".join(terms).casefold()


def _earthscope_station_resource_code(args: dict[str, Any]) -> str:
    """Return a requested EarthScope station code for precise resource searches."""

    resource_name = str(args.get("resource_name") or "").strip()
    if str(args.get("resource_format") or "").upper() not in {"", "CSV"}:
        return ""
    candidate_texts = [resource_name]
    for key in ("search_terms", "filter_list"):
        values = args.get(key)
        if isinstance(values, list):
            candidate_texts.extend(str(value) for value in values if value)
    for key in ("search_term", "dataset_name", "dataset_title"):
        value = args.get(key)
        if value:
            candidate_texts.append(str(value))
    excluded = {
        "CSV",
        "DATA",
        "GNSS",
        "GPS",
        "HTML",
        "LIST",
        "PBO",
        "PNG",
        "RAW",
        "SITE",
        "TIME",
    }

    def station_code(value: object) -> str:
        token = str(value).split(".", 1)[0].strip().upper()
        if re.fullmatch(r"[A-Z0-9]{3,5}", token) and token not in excluded:
            return token
        return ""

    resource_station = station_code(resource_name)
    if resource_station:
        return resource_station

    text = _search_term_text(args)
    if "earthscope" not in text and "gnss" not in text and "gps" not in text:
        return ""
    for value in candidate_texts:
        if code := station_code(value):
            return code
    return ""


def _dataset_is_earthscope_station_resource(dataset: dict[str, Any]) -> bool:
    """Return whether a compact CKAN row looks like a single-station GNSS resource."""

    fields: list[str] = []
    for key in ("name", "title"):
        value = dataset.get(key)
        if value:
            fields.append(str(value))
    for key in ("resource_names", "resource_urls"):
        values = dataset.get(key)
        if isinstance(values, list):
            fields.extend(str(value) for value in values if value)
    for summary in dataset.get("resource_summaries") or []:
        if isinstance(summary, dict):
            fields.extend(
                str(summary.get(key) or "")
                for key in ("name", "url")
                if summary.get(key)
            )
    station_resource_re = re.compile(
        r"(^|[/_.-])[A-Z0-9]{3,5}[._-][A-Z0-9]{2}[._-]LY_?[._-][A-Z0-9]{2}"
        r"(?:[._-]|$)",
        re.IGNORECASE,
    )
    return any(station_resource_re.search(field) for field in fields)


def _dataset_matches_earthscope_station_resource(
    dataset: dict[str, Any],
    station_code: str,
) -> bool:
    """Return whether a compact CKAN row belongs to the requested station."""

    station = re.escape(station_code.upper())
    pattern = re.compile(rf"(^|[/_.-]){station}([._-]|$)", re.IGNORECASE)
    fields: list[str] = []
    for key in ("name", "title"):
        value = dataset.get(key)
        if value:
            fields.append(str(value))
    for key in ("resource_names", "resource_urls"):
        values = dataset.get(key)
        if isinstance(values, list):
            fields.extend(str(value) for value in values if value)
    for summary in dataset.get("resource_summaries") or []:
        if isinstance(summary, dict):
            fields.extend(
                str(summary.get(key) or "")
                for key in ("name", "url")
                if summary.get(key)
            )
    return any(pattern.search(field) for field in fields)


def _filter_earthscope_station_resource_results(
    datasets: list[dict[str, Any]],
    args: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Filter station-specific EarthScope searches to matching station resources."""

    station_code = _earthscope_station_resource_code(args)
    if not station_code:
        text = _search_term_text(args)
        if (
            ("gnss" not in text and "gps" not in text)
            or "csv" not in text
        ):
            return datasets, None
        filtered = [
            dataset
            for dataset in datasets
            if not _dataset_is_earthscope_station_resource(dataset)
        ]
        if len(filtered) == len(datasets):
            return datasets, None
        return filtered, {
            "input_count": len(datasets),
            "output_count": len(filtered),
            "status": "broad_station_resources_suppressed",
        }
    filtered = [
        dataset
        for dataset in datasets
        if _dataset_matches_earthscope_station_resource(dataset, station_code)
    ]
    return filtered, {
        "station_code": station_code,
        "input_count": len(datasets),
        "output_count": len(filtered),
        "status": "applied",
    }


def _earthscope_search_coverage(args: dict[str, Any]) -> dict[str, Any]:
    text = _search_term_text(args)
    station_code = _earthscope_station_resource_code(args)
    has_earthscope = "earthscope" in text
    has_gnss_or_gps = "gnss" in text or "gps" in text
    has_raw_csv = (
        "raw_csv" in text
        or "csv" in text
        or str(args.get("resource_format") or "").upper() == "CSV"
    )
    broad_catalog_searched = has_earthscope and has_gnss_or_gps and has_raw_csv
    station_resource_search = has_raw_csv and (
        bool(station_code)
        or (
            has_earthscope
            and (
                "station" in text
                or "site" in text
                or "time" in text
                or "timeseries" in text
            )
        )
    )
    return {
        "domain": "earthscope_gnss",
        "broad_station_catalog_searched": broad_catalog_searched,
        "station_resource_search": station_resource_search,
        "search_terms": args.get("search_terms") or [],
        "resource_name": args.get("resource_name"),
        "resource_format": args.get("resource_format"),
        "station_code": station_code or None,
        "status": "covered" if broad_catalog_searched or station_resource_search else "incomplete",
        "next_action": (
            "Search with EarthScope, GNSS/GPS, and CSV/raw_csv terms before "
            "claiming no EarthScope station metadata exists."
            if not broad_catalog_searched and not station_resource_search
            else ""
        ),
    }


def _annotate_search_coverage(payload: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    payload["search_coverage"] = _earthscope_search_coverage(args)
    return payload


def _resource_matches(resource: dict[str, Any], resource_name: str | None) -> bool:
    """Return whether a resource row matches an optional name/id/url selector."""
    if not resource_name:
        return True
    needle = resource_name.strip().lower()
    haystack = " ".join(
        str(resource.get(key) or "") for key in ("id", "name", "url", "description", "format")
    ).lower()
    return needle in haystack


def _select_resource(
    dataset: dict[str, Any],
    *,
    resource_name: str | None,
    resource_index: int | str | None,
) -> dict[str, Any] | None:
    """Choose one resource from a CKAN dataset row."""
    resources = dataset.get("resources")
    if not isinstance(resources, list):
        return None
    if resource_name:
        for resource in resources:
            if isinstance(resource, dict) and _resource_matches(resource, resource_name):
                return resource
        return None
    try:
        if isinstance(resource_index, int) and not isinstance(resource_index, bool):
            index = resource_index
        elif isinstance(resource_index, str) and resource_index.strip():
            index = int(resource_index)
        else:
            index = 0
    except ValueError:
        index = 0
    if index < 0 or index >= len(resources):
        return None
    resource = resources[index]
    return resource if isinstance(resource, dict) else None


def _safe_filename(value: str, *, default: str) -> str:
    """Return a conservative filesystem name for staged catalog resources."""
    cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value.strip())
    cleaned = cleaned.strip("._-")
    return cleaned[:120] or default


def _parse_resource_size_bytes(value: Any) -> int | None:
    """Parse common CKAN resource size strings such as ``1.4 GB``."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value if value >= 0 else None
    if not isinstance(value, str) or not value.strip():
        return None
    parts = value.strip().replace(",", "").split()
    if not parts:
        return None
    try:
        number = float(parts[0])
    except ValueError:
        return None
    unit = parts[1].lower() if len(parts) > 1 else "bytes"
    multiplier = _SIZE_UNITS.get(unit)
    if multiplier is None:
        return None
    return int(number * multiplier)


def _stage_error(
    *,
    code: str,
    message: str,
    next_action: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a structured staging failure."""
    return _tool_error(code=code, message=message, next_action=next_action, details=details)


def _stage_http_resource(
    *,
    url: str,
    output_path: Path,
    max_bytes: int,
) -> dict[str, Any]:
    """Download a small HTTP resource under CLIO file policy."""
    curl = shutil.which("curl")
    if curl:
        return _stage_curl_resource(
            curl=curl,
            url=url,
            output_path=output_path,
            max_bytes=max_bytes,
        )
    return _stage_requests_resource(url=url, output_path=output_path, max_bytes=max_bytes)


def _existing_staged_resource(output_path: Path, *, url: str) -> dict[str, Any] | None:
    """Return an existing staged artifact for the exact validated output path."""

    if not output_path.is_file():
        return None
    size = output_path.stat().st_size
    if size <= 0:
        return None
    return {
        "staged": True,
        "path": str(output_path),
        "size_bytes": size,
        "url": url,
        "source_url": url,
        "method": "existing_file",
        "cache_hit": True,
    }


def _stage_curl_resource(
    *,
    curl: str,
    url: str,
    output_path: Path,
    max_bytes: int,
) -> dict[str, Any]:
    """Stage an HTTP(S) resource through curl/webget-style semantics."""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    partial_path = output_path.with_name(f"{output_path.name}.part")
    partial_path.unlink(missing_ok=True)
    command = [
        curl,
        "--location",
        "--fail",
        "--show-error",
        "--silent",
        "--connect-timeout",
        str(_WEBGET_CONNECT_TIMEOUT_S),
        "--max-time",
        str(_WEBGET_MAX_TIME_S),
        "--retry",
        str(_WEBGET_RETRY_COUNT),
        "--retry-delay",
        str(_WEBGET_RETRY_DELAY_S),
        "--max-filesize",
        str(max_bytes),
        "--output",
        str(partial_path),
        url,
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=_WEBGET_SUBPROCESS_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        partial_path.unlink(missing_ok=True)
        return _stage_error(
            code="webget_timeout",
            message="curl timed out before the NDP resource could be staged.",
            next_action="Retry later, select a smaller mirror, or stage the URL manually.",
            details={
                "url": url,
                "method": "curl",
                "timeout_s": _WEBGET_SUBPROCESS_TIMEOUT_S,
                "command": _redacted_command(command),
            },
        )

    if completed.returncode != 0:
        partial_path.unlink(missing_ok=True)
        stderr = (completed.stderr or completed.stdout or "").strip()
        return _stage_error(
            code="webget_failed",
            message="curl could not stage the selected NDP resource.",
            next_action="Inspect the URL, retry later, or select another concrete resource.",
            details={
                "url": url,
                "method": "curl",
                "returncode": completed.returncode,
                "stderr": stderr[-1200:],
                "command": _redacted_command(command),
            },
        )

    if not partial_path.exists():
        return _stage_error(
            code="webget_output_missing",
            message="curl exited successfully but did not create the staged resource.",
            next_action="Inspect the output directory and retry the resource staging.",
            details={"url": url, "method": "curl", "output_path": str(partial_path)},
        )

    size = partial_path.stat().st_size
    if size > max_bytes:
        partial_path.unlink(missing_ok=True)
        return _stage_error(
            code="resource_too_large",
            message=(
                f"NDP resource is {size} bytes, which exceeds the staging "
                f"limit of {max_bytes} bytes."
            ),
            next_action="Increase max_bytes intentionally or select a smaller resource.",
            details={"url": url, "method": "curl", "size_bytes": size, "max_bytes": max_bytes},
        )

    partial_path.replace(output_path)
    return {
        "staged": True,
        "path": str(output_path),
        "size_bytes": size,
        "url": url,
        "method": "curl",
        "command": _redacted_command(command),
    }


def _redacted_command(command: list[str]) -> list[str]:
    """Return a non-shell command vector safe for provenance metadata."""

    return [str(part) for part in command]


def _stage_requests_resource(
    *,
    url: str,
    output_path: Path,
    max_bytes: int,
) -> dict[str, Any]:
    """Download a small HTTP resource with Python streaming when curl is absent."""
    try:
        with requests.get(url, stream=True, timeout=(10, 60)) as response:
            response.raise_for_status()
            content_length = response.headers.get("content-length")
            if content_length:
                try:
                    size = int(content_length)
                except ValueError:
                    size = 0
                if size > max_bytes:
                    return _stage_error(
                        code="resource_too_large",
                        message=(
                            f"NDP resource is {size} bytes, which exceeds the "
                            f"staging limit of {max_bytes} bytes."
                        ),
                        next_action=(
                            "Increase max_bytes for an intentional large download, or "
                            "stage the resource manually with a domain-specific tool."
                        ),
                        details={"url": url, "size_bytes": size, "max_bytes": max_bytes},
                    )
            total = 0
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with output_path.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > max_bytes:
                        handle.close()
                        output_path.unlink(missing_ok=True)
                        return _stage_error(
                            code="resource_too_large",
                            message=(
                                f"NDP resource exceeded staging limit of {max_bytes} "
                                "bytes while downloading."
                            ),
                            next_action=(
                                "Increase max_bytes for an intentional large download, "
                                "or stage the resource manually with a streaming tool."
                            ),
                            details={"url": url, "bytes_read": total, "max_bytes": max_bytes},
                        )
                    handle.write(chunk)
    except requests.RequestException as exc:
        return _stage_error(
            code="resource_download_failed",
            message=f"Could not download NDP resource: {exc}",
            next_action="Retry later or inspect the resource URL manually.",
            details={"url": url},
        )

    return {
        "staged": True,
        "path": str(output_path),
        "size_bytes": output_path.stat().st_size,
        "url": url,
        "method": "python_requests",
    }


def _arcgis_layer_query_url(feature_service_url: str, layer_id: int | str | None) -> str:
    """Return an ArcGIS FeatureServer query URL for a service or layer URL."""

    base = feature_service_url.strip().rstrip("/")
    if not base.lower().startswith(("http://", "https://")):
        raise ValueError("feature_service_url must be HTTP(S)")
    if base.lower().endswith("/query"):
        return base
    tail = base.rsplit("/", 1)[-1]
    if tail.isdigit():
        return f"{base}/query"
    selected_layer = str(layer_id if layer_id not in (None, "") else 0).strip()
    if not selected_layer.isdigit():
        raise ValueError("layer_id must be numeric when querying a FeatureServer root")
    return f"{base}/{selected_layer}/query"


def _arcgis_bbox_geometry(
    *,
    min_lon: float | str | None,
    min_lat: float | str | None,
    max_lon: float | str | None,
    max_lat: float | str | None,
) -> dict[str, Any]:
    values = [min_lon, min_lat, max_lon, max_lat]
    if any(value in (None, "") for value in values):
        return {}
    parsed_values = [str(value) for value in values]
    try:
        xmin, ymin, xmax, ymax = (float(value) for value in parsed_values)
    except (TypeError, ValueError) as exc:
        raise ValueError("bbox values must be numeric longitude/latitude values") from exc
    if xmin >= xmax or ymin >= ymax:
        raise ValueError("bbox must satisfy min_lon < max_lon and min_lat < max_lat")
    return {
        "geometry": json.dumps(
            {"xmin": xmin, "ymin": ymin, "xmax": xmax, "ymax": ymax, "spatialReference": {"wkid": 4326}}
        ),
        "geometryType": "esriGeometryEnvelope",
        "inSR": 4326,
        "spatialRel": "esriSpatialRelIntersects",
    }


def _compact_arcgis_geometry(geometry: Any) -> dict[str, Any]:
    if not isinstance(geometry, dict):
        return {}
    if "x" in geometry and "y" in geometry:
        return {"x": geometry.get("x"), "y": geometry.get("y")}
    rings = geometry.get("rings")
    if isinstance(rings, list):
        xs: list[float] = []
        ys: list[float] = []
        for ring in rings[:3]:
            if not isinstance(ring, list):
                continue
            for point in ring[:250]:
                if isinstance(point, list | tuple) and len(point) >= 2:
                    try:
                        xs.append(float(point[0]))
                        ys.append(float(point[1]))
                    except (TypeError, ValueError):
                        continue
        if xs and ys:
            return {
                "bbox": [min(xs), min(ys), max(xs), max(ys)],
                "point_count_sampled": len(xs),
            }
    return {"geometry_keys": sorted(str(key) for key in geometry)[:8]}


def _arcgis_epoch_to_iso(value: Any) -> str | None:
    """Return an ISO UTC timestamp for plausible ArcGIS epoch values."""

    if isinstance(value, bool):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if numeric <= 0:
        return None
    seconds = numeric / 1000 if numeric > 10_000_000_000 else numeric
    if not 946_684_800 <= seconds <= 4_102_444_800:
        return None
    return datetime.fromtimestamp(seconds, tz=UTC).isoformat().replace("+00:00", "Z")


def _is_arcgis_date_field(name: str) -> bool:
    lowered = name.lower()
    return any(token in lowered for token in ("date", "time", "start", "end", "updated", "expires"))


def _normalize_arcgis_attributes(attributes: Any) -> dict[str, Any]:
    """Add ISO companions for ArcGIS date/time fields while preserving raw values."""

    if not isinstance(attributes, dict):
        return {}
    normalized = dict(attributes)
    for key, value in attributes.items():
        key_text = str(key)
        if not _is_arcgis_date_field(key_text):
            continue
        iso_value = _arcgis_epoch_to_iso(value)
        if iso_value:
            normalized[f"{key_text}_iso"] = iso_value
    return normalized


def _arcgis_feature_collection(features: list[dict[str, Any]]) -> dict[str, Any]:
    """Convert ArcGIS feature rows into a compact GeoJSON-like feature collection."""

    rows: list[dict[str, Any]] = []
    for feature in features:
        if not isinstance(feature, dict):
            continue
        rows.append(
            {
                "type": "Feature",
                "properties": _normalize_arcgis_attributes(feature.get("attributes")),
                "geometry": _compact_arcgis_geometry(feature.get("geometry")),
            }
        )
    return {"type": "FeatureCollection", "features": rows}


def _write_json_output(output_path: str | None, payload: dict[str, Any]) -> dict[str, Any]:
    if not output_path:
        return {}
    try:
        path = validate_write_path(output_path, field="output_path", create_parent=True)
    except FilePolicyError as exc:
        return exc.to_result()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True, indent=2, default=str), encoding="utf-8")
    return {"output_path": str(path), "output_size_bytes": path.stat().st_size}


def _read_csv_rows(path: Path, *, max_rows: int) -> tuple[list[str], list[dict[str, str]], int]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        columns = list(reader.fieldnames or [])
        rows: list[dict[str, str]] = []
        total = 0
        for row in reader:
            total += 1
            if len(rows) < max_rows:
                rows.append({str(key): str(value or "") for key, value in row.items()})
            if total >= _MAX_CSV_PROFILE_ROWS:
                break
    return columns, rows, total


def _to_float(value: Any) -> float | None:
    try:
        text = str(value).strip()
        if not text:
            return None
        return float(text)
    except (TypeError, ValueError):
        return None


def _parse_datetime_text(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is not None:
            return parsed.astimezone(UTC).replace(tzinfo=None)
        return parsed
    except ValueError:
        pass
    for fmt in ("%m/%d/%Y", "%Y/%m/%d", "%Y-%m-%d", "%m/%d/%Y %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)  # noqa: DTZ007
        except ValueError:
            continue
    return None


def _infer_csv_plot_x_axis(values: list[str]) -> dict[str, Any]:
    """Infer plot-ready x values and trace metadata from a CSV x column."""

    if not values:
        return {
            "kind": "row_index",
            "values": [],
            "label": "row index",
            "parse_success_ratio": 0.0,
        }

    numeric: list[float | None] = [_to_float(value) for value in values]
    numeric_values = [value for value in numeric if value is not None and math.isfinite(value)]
    numeric_ratio = len(numeric_values) / len(values)
    if numeric_ratio >= 0.8 and numeric_values:
        median_abs = sorted(abs(value) for value in numeric_values)[len(numeric_values) // 2]
        if median_abs >= 1_000_000_000_000:
            datetimes = [
                datetime.fromtimestamp(value / 1000, UTC).replace(tzinfo=None)
                if value is not None and math.isfinite(value)
                else None
                for value in numeric
            ]
            return {
                "kind": "epoch_milliseconds",
                "values": datetimes,
                "label": "time (UTC)",
                "parse_success_ratio": numeric_ratio,
            }
        if median_abs >= 1_000_000_000:
            datetimes = [
                datetime.fromtimestamp(value, UTC).replace(tzinfo=None)
                if value is not None and math.isfinite(value)
                else None
                for value in numeric
            ]
            return {
                "kind": "epoch_seconds",
                "values": datetimes,
                "label": "time (UTC)",
                "parse_success_ratio": numeric_ratio,
            }

    parsed_datetimes = [_parse_datetime_text(value) for value in values]
    parsed_count = sum(value is not None for value in parsed_datetimes)
    parsed_ratio = parsed_count / len(values)
    if parsed_ratio >= 0.8 and parsed_count:
        return {
            "kind": "datetime",
            "values": parsed_datetimes,
            "label": "time",
            "parse_success_ratio": parsed_ratio,
        }

    return {
        "kind": "categorical",
        "values": list(range(len(values))),
        "labels": values,
        "label": "row index",
        "parse_success_ratio": max(numeric_ratio, parsed_ratio),
    }


def _csv_numeric_summary(rows: list[dict[str, str]], columns: list[str]) -> dict[str, dict[str, Any]]:
    summary: dict[str, dict[str, Any]] = {}
    for column in columns:
        values = [_to_float(row.get(column)) for row in rows]
        numeric = [value for value in values if value is not None]
        if not numeric:
            continue
        summary[column] = {
            "count": len(numeric),
            "min": min(numeric),
            "max": max(numeric),
            "mean": sum(numeric) / len(numeric),
        }
    return summary


def _csv_missing_summary(rows: list[dict[str, str]], columns: list[str]) -> dict[str, int]:
    return {
        column: sum(1 for row in rows if not str(row.get(column) or "").strip())
        for column in columns
    }


def _csv_sample_rows(rows: list[dict[str, str]], limit: int = 3) -> list[dict[str, str]]:
    return rows[: max(0, limit)]


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius_km = 6371.0088
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return radius_km * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _earthscope_station_rows(path: Path, *, latitude: float, longitude: float) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.reader(handle)
        header = next(reader, None) or []
        normalized_header = [str(column).strip().casefold() for column in header]
        header_text = " ".join(normalized_header)
        if not (
            normalized_header
            and normalized_header[0] == "site"
            and "latitude" in header_text
            and "longitude" in header_text
            and "status" in header_text
        ):
            raise ValueError(
                "not an EarthScope station metadata catalog; expected Site, Latitude, "
                "Longitude, network, and Status columns"
            )
        for raw in reader:
            if len(raw) < 10:
                continue
            station = raw[0].strip()
            lat = _to_float(raw[1])
            lon = _to_float(raw[2])
            if not station or lat is None or lon is None:
                continue
            network = raw[8].strip() if len(raw) > 8 else ""
            status = raw[9].strip() if len(raw) > 9 else ""
            distance_km = _haversine_km(latitude, longitude, lat, lon)
            station_upper = station.upper()
            station_lower = station.lower()
            rows.append(
                {
                    "station": station_upper,
                    "latitude": lat,
                    "longitude": lon,
                    "network": network,
                    "status": status,
                    "distance_km": round(distance_km, 3),
                    "suggested_search_terms": [
                        station_upper,
                        station_lower,
                        f"{station_upper} EarthScope GNSS CSV",
                        f"{station_lower}-ci-ly",
                    ],
                    "resource_discovery": {
                        "status": "search_required",
                        "reason": (
                            "Station metadata identifies a nearby station but does "
                            "not prove a concrete station time-series resource URL."
                        ),
                        "search_terms": [
                            station_upper,
                            f"{station_upper} EarthScope GNSS CSV",
                            f"{station_upper}.CI.LY",
                            f"{station_upper} raw_csv",
                        ],
                    },
                }
            )
    return sorted(rows, key=lambda row: (row["distance_km"], row["station"]))


def _station_resource_queries(stations: list[dict[str, Any]], *, limit: int = 5) -> list[dict[str, Any]]:
    queries: list[dict[str, Any]] = []
    for row in stations[:limit]:
        station = str(row.get("station") or "").strip().upper()
        if not station:
            continue
        queries.append(
            {
                "station": station,
                "preferred_calls": [
                    {
                        "tool": "ndp_search_datasets",
                        "arguments": {
                            "resource_name": station,
                            "resource_format": "CSV",
                            "server": "global",
                            "limit": 20,
                        },
                    },
                ],
            }
        )
    return queries


def _stage_pelican_resource(
    *,
    url: str,
    output_path: Path,
    max_bytes: int,
    resource_size_bytes: int | None,
) -> dict[str, Any]:
    """Stage an OSDF/Pelican resource with the local Pelican CLI when available."""
    if resource_size_bytes is not None and resource_size_bytes > max_bytes:
        return _stage_error(
            code="resource_too_large",
            message=(
                f"NDP resource is advertised as {resource_size_bytes} bytes, which "
                f"exceeds the staging limit of {max_bytes} bytes."
            ),
            next_action=(
                "Increase max_bytes intentionally, select a smaller concrete object, "
                "or stage the resource manually with Pelican."
            ),
            details={"url": url, "size_bytes": resource_size_bytes, "max_bytes": max_bytes},
        )

    pelican = shutil.which("pelican")
    if pelican is None:
        return _stage_error(
            code="pelican_unavailable",
            message=(
                "The selected NDP resource uses OSDF/Pelican transport, but the "
                "`pelican` CLI was not found on PATH."
            ),
            next_action=(
                "Install the Pelican client, verify `pelican --version`, then retry "
                "ndp_stage_resource."
            ),
            details={"url": url, "transport": "osdf"},
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = [pelican, "object", "get", url, str(output_path)]
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=900,
        )
    except subprocess.TimeoutExpired:
        return _stage_error(
            code="pelican_timeout",
            message="Pelican staging timed out before the resource was downloaded.",
            next_action="Retry with a smaller concrete object or stage the resource manually.",
            details={"url": url, "timeout_s": 900},
        )

    if completed.returncode != 0:
        return _stage_error(
            code="pelican_stage_failed",
            message="Pelican failed to stage the selected NDP resource.",
            next_action=(
                "Inspect Pelican stderr/stdout, select a concrete object if the URL is "
                "a namespace, or stage manually."
            ),
            details={
                "url": url,
                "exit_code": completed.returncode,
                "stdout": completed.stdout[-2000:],
                "stderr": completed.stderr[-2000:],
            },
        )

    if not output_path.exists():
        return _stage_error(
            code="pelican_output_missing",
            message="Pelican exited successfully but the expected staged file was not found.",
            next_action="Inspect the output directory and Pelican logs.",
            details={"url": url, "output_path": str(output_path)},
        )

    size = output_path.stat().st_size if output_path.is_file() else 0
    if size > max_bytes:
        return _stage_error(
            code="resource_too_large",
            message=(
                f"Staged file is {size} bytes, which exceeds the staging limit of "
                f"{max_bytes} bytes."
            ),
            next_action="Delete the staged file or raise max_bytes for intentional large staging.",
            details={"url": url, "path": str(output_path), "size_bytes": size},
        )

    return {
        "staged": True,
        "path": str(output_path),
        "size_bytes": size,
        "url": url,
        "transport": "osdf",
    }


def _compact_ndp_result(tool_name: str, decoded: dict[str, Any]) -> dict[str, Any]:
    """Compact successful clio-kit NDP payloads before they enter CLIO traces."""
    compacted = dict(decoded)
    if tool_name == "list_organizations":
        organizations = compacted.get("organizations")
        if isinstance(organizations, list) and len(organizations) > 8:
            compacted["organizations"] = organizations[:8]
            compacted["organizations_truncated"] = True
        return compacted
    if tool_name == "search_datasets":
        compacted.pop("search_parameters", None)
        datasets = compacted.get("datasets")
        if isinstance(datasets, list):
            compacted["datasets"] = [_compact_dataset(row) for row in datasets[:4]]
            compacted["datasets_truncated"] = len(datasets) > 4
        return compacted
    if tool_name == "get_dataset_details":
        return _compact_dataset(compacted)
    return compacted


async def _call_clio_kit_ndp_tool(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Call one clio-kit NDP MCP tool and surface upstream errors explicitly."""
    try:
        async with Client(_clio_kit_transport("ndp")) as client:
            result = await client.call_tool(tool_name, args)
    except Exception as exc:
        return _tool_error(
            code="clio_kit_ndp_unavailable",
            message=f"Could not call clio-kit NDP MCP tool {tool_name!r}: {exc}",
            next_action=(
                "Install clio-kit, set CLIO_KIT_PATH to a local checkout, "
                "set CLIO_KIT_COMMAND to a launcher command, or ensure uvx can "
                "resolve the clio-kit package."
            ),
            details={"tool": tool_name, "args": args},
        )

    if bool(getattr(result, "is_error", False)):
        return _tool_error(
            code="clio_kit_ndp_error",
            message=f"clio-kit NDP MCP tool {tool_name!r} returned an error.",
            next_action="Inspect the returned MCP content and retry with corrected arguments.",
            details={"tool": tool_name, "content": _decode_text_content(result)},
        )

    decoded = _decode_text_content(result)
    if not decoded:
        return _tool_error(
            code="empty_tool_result",
            message=f"clio-kit NDP MCP tool {tool_name!r} returned no structured content.",
            next_action="Retry the call or inspect clio-kit NDP server logs.",
            details={"tool": tool_name},
        )
    return _compact_ndp_result(tool_name, decoded)


async def _dataset_details(
    dataset_identifier: str,
    *,
    identifier_type: str,
    server: str,
    compact: bool,
) -> dict[str, Any]:
    """Return dataset details, falling back to public CKAN when clio-kit is brittle."""
    if server == "global" and identifier_type in {"id", "name"}:
        try:
            direct = _global_ckan_package_show(dataset_identifier)
        except Exception as exc:
            if not _should_try_clio_kit(server):
                return _tool_error(
                    code="ndp_dataset_details_unavailable",
                    message=f"Could not retrieve NDP dataset details from public CKAN: {exc}",
                    next_action=(
                        "Retry later, set CLIO_KIT_COMMAND or CLIO_KIT_PATH to a "
                        "working clio-kit launcher, or use a dataset identifier from "
                        "NDP search results."
                    ),
                    details={
                        "dataset_identifier": dataset_identifier,
                        "identifier_type": identifier_type,
                        "clio_kit_skipped": True,
                        "clio_kit_skip_reason": _CLIO_KIT_SKIP_UVX_REASON,
                    },
                )
        else:
            if compact:
                compacted = _compact_dataset(direct)
                compacted["_meta"] = {
                    "tool": "get_dataset_details",
                    "status": "success",
                    "source": "ckan_package_show",
                }
                if not _should_try_clio_kit(server):
                    _annotate_ckan_skip(compacted)
                return compacted
            return direct

    args = {
        "dataset_identifier": dataset_identifier,
        "identifier_type": identifier_type,
        "server": server,
    }
    clio_kit_result = await _call_clio_kit_ndp_tool("get_dataset_details", args)
    if not clio_kit_result.get("error") and any(
        clio_kit_result.get(key) for key in ("id", "name", "title", "resources", "resource_urls")
    ):
        return clio_kit_result if compact else clio_kit_result

    if server != "global" or identifier_type not in {"id", "name"}:
        return clio_kit_result

    try:
        direct = _global_ckan_package_show(dataset_identifier)
    except Exception as exc:
        if clio_kit_result.get("error"):
            return clio_kit_result
        return _tool_error(
            code="ndp_dataset_details_unavailable",
            message=f"Could not retrieve NDP dataset details: {exc}",
            next_action="Retry later or use a dataset identifier from NDP search results.",
            details={"dataset_identifier": dataset_identifier, "identifier_type": identifier_type},
        )
    if compact:
        compacted = _compact_dataset(direct)
        compacted["_meta"] = {
            "tool": "get_dataset_details",
            "status": "success",
            "source": "ckan_package_show",
        }
        return compacted
    return direct


@ndp_server.tool()
async def list_organizations(
    name_filter: str | None = None,
    server: str = "global",
) -> dict[str, Any]:
    """List organizations available in the National Data Platform."""
    args: dict[str, Any] = {
        "name_filter": _clean_optional_text(name_filter),
        "server": _clean_server(server, allowed={"local", "global", "pre_ckan"}),
    }
    if not _should_try_clio_kit(args["server"]):
        return _annotate_ckan_skip(_global_ckan_organization_list(args["name_filter"]))
    clio_kit_result = await _call_clio_kit_ndp_tool("list_organizations", args)
    if not clio_kit_result.get("error") or args["server"] != "global":
        return clio_kit_result
    try:
        direct = _global_ckan_organization_list(args["name_filter"])
    except Exception:
        return clio_kit_result
    direct["_meta"]["clio_kit_error"] = clio_kit_result["error"]
    return direct


@ndp_server.tool()
async def search_datasets(
    search_terms: list[str] | None = None,
    search_keys: list[str] | None = None,
    dataset_name: str | None = None,
    dataset_title: str | None = None,
    owner_org: str | None = None,
    resource_url: str | None = None,
    resource_name: str | None = None,
    dataset_description: str | None = None,
    resource_description: str | None = None,
    resource_format: str | None = None,
    search_term: str | None = None,
    filter_list: list[str] | None = None,
    timestamp: str | None = None,
    server: str = "global",
    limit: int | str | None = None,
) -> dict[str, Any]:
    """Search for datasets in the NDP using terms or field-specific criteria."""
    args: dict[str, Any] = {
        "search_terms": _clean_string_list(search_terms),
        "search_keys": _clean_string_list(search_keys),
        "dataset_name": _clean_optional_text(dataset_name),
        "dataset_title": _clean_optional_text(dataset_title),
        "owner_org": _clean_optional_text(owner_org),
        "resource_url": _clean_optional_text(resource_url),
        "resource_name": _clean_optional_text(resource_name),
        "dataset_description": _clean_optional_text(dataset_description),
        "resource_description": _clean_optional_text(resource_description),
        "resource_format": _clean_optional_text(resource_format),
        "search_term": _clean_optional_text(search_term),
        "filter_list": _clean_string_list(filter_list),
        "timestamp": _clean_optional_text(timestamp),
        "server": _clean_server(server, allowed={"local", "global"}),
        "limit": _clean_limit(limit),
    }
    cleaned_args = {key: value for key, value in args.items() if value is not None}
    if cleaned_args.get("server") == "global":
        try:
            direct = _global_ckan_package_search(cleaned_args)
        except Exception as exc:
            if not _should_try_clio_kit(cleaned_args.get("server", "global")):
                return _tool_error(
                    code="ndp_dataset_search_unavailable",
                    message=f"Could not search global NDP CKAN: {exc}",
                    next_action=(
                        "Retry later, set CLIO_KIT_COMMAND or CLIO_KIT_PATH to a "
                        "working clio-kit launcher, or simplify the search terms."
                    ),
                    details={"args": cleaned_args, "clio_kit_skipped": True},
                )
        else:
            meta = direct.get("_meta")
            if isinstance(meta, dict):
                meta.pop("clio_kit_fallback", None)
                meta["ckan_direct"] = True
            if not _should_try_clio_kit(cleaned_args.get("server", "global")):
                _annotate_ckan_skip(direct)
            return direct
    if not _should_try_clio_kit(cleaned_args.get("server", "global")):
        return _annotate_ckan_skip(_global_ckan_package_search(cleaned_args))
    clio_kit_result = await _call_clio_kit_ndp_tool(
        "search_datasets",
        cleaned_args,
    )
    if not clio_kit_result.get("error") or cleaned_args.get("server") != "global":
        return _annotate_search_coverage(clio_kit_result, cleaned_args)
    try:
        direct = _global_ckan_package_search(cleaned_args)
    except Exception:
        return _annotate_search_coverage(clio_kit_result, cleaned_args)
    direct["_meta"]["clio_kit_error"] = clio_kit_result["error"]
    return direct


@ndp_server.tool()
async def get_dataset_details(
    dataset_identifier: str,
    identifier_type: str = "id",
    server: str = "global",
) -> dict[str, Any]:
    """Retrieve detailed metadata for a specific NDP dataset by ID or name."""
    return await _dataset_details(
        dataset_identifier,
        identifier_type=identifier_type,
        server=server,
        compact=True,
    )


@ndp_server.tool()
async def stage_resource(
    dataset_identifier: str,
    identifier_type: str = "id",
    resource_name: str | None = None,
    resource_index: int | str | None = 0,
    output_dir: str | None = None,
    max_bytes: int | str | None = None,
    server: str = "global",
) -> dict[str, Any]:
    """Stage a downloadable NDP resource under CLIO's file policy.

    HTTP(S) resources are downloaded directly with a size cap. OSDF/Pelican
    resources are reported as unsupported unless a future Pelican-backed staging
    tool is added; CLIO must not pretend those bytes were staged.
    """

    direct_url = dataset_identifier.strip()
    if direct_url.lower().startswith(("http://", "https://")):
        try:
            max_stage_bytes = _clean_max_bytes(max_bytes)
            destination_dir = Path(output_dir or Path.cwd() / "tmp" / "clio-ndp-staging")
            destination_dir.mkdir(parents=True, exist_ok=True)
            filename_source = resource_name or Path(direct_url.split("?", 1)[0]).name
            filename = _safe_filename(filename_source, default="ndp-resource")
            output_path = validate_write_path(
                str(destination_dir / filename),
                field="output_path",
            )
        except FilePolicyError as exc:
            return exc.to_result()

        existing = _existing_staged_resource(output_path, url=direct_url)
        if existing is not None:
            existing.update(
                {
                    "dataset_id": "",
                    "dataset_name": "",
                    "dataset_title": "",
                    "resource_name": filename,
                    "selected_resource_name": filename,
                    "selected_resource_url": direct_url,
                    "_meta": {
                        "tool": "stage_resource",
                        "status": "success",
                        "source": "direct_url",
                        "cache_hit": True,
                    },
                }
            )
            return existing

        result = _stage_http_resource(
            url=direct_url,
            output_path=output_path,
            max_bytes=max_stage_bytes,
        )
        if result.get("error"):
            return result
        result.update(
            {
                "dataset_id": "",
                "dataset_name": "",
                "dataset_title": "",
                "resource_name": filename,
                "selected_resource_name": filename,
                "selected_resource_url": direct_url,
                "source_url": direct_url,
                "_meta": {"tool": "stage_resource", "status": "success", "source": "direct_url"},
            }
        )
        return result

    dataset = await _dataset_details(
        dataset_identifier,
        identifier_type=identifier_type,
        server=server,
        compact=False,
    )
    if dataset.get("error"):
        return dataset

    resource = _select_resource(
        dataset,
        resource_name=resource_name,
        resource_index=resource_index,
    )
    if resource is None:
        return _stage_error(
            code="resource_not_found",
            message="No matching resource was found in the NDP dataset details.",
            next_action="Use a resource name or index from ndp_get_dataset_details.",
            details={
                "dataset_identifier": dataset_identifier,
                "resource_name": resource_name,
                "resource_index": resource_index,
            },
        )

    url = str(resource.get("url") or "").strip()
    if not url:
        return _stage_error(
            code="resource_url_missing",
            message="The selected NDP resource does not include a URL.",
            next_action="Choose a different resource or inspect the dataset in the NDP catalog.",
            details={"dataset_identifier": dataset_identifier, "resource": resource},
        )

    is_osdf = url.lower().startswith("osdf://")
    if not is_osdf and not url.lower().startswith(("http://", "https://")):
        return _stage_error(
            code="unsupported_resource_transport",
            message=f"Unsupported NDP resource URL scheme: {url}",
            next_action="Use an HTTP(S) resource or add a staging tool for this transport.",
            details={"dataset_identifier": dataset_identifier, "url": url},
        )

    try:
        max_stage_bytes = _clean_max_bytes(max_bytes)
        destination_dir = Path(output_dir or Path.cwd() / "tmp" / "clio-ndp-staging")
        destination_dir.mkdir(parents=True, exist_ok=True)
        filename_source = (
            str(resource.get("name") or "")
            or Path(url.split("?", 1)[0]).name
            or str(dataset.get("name") or dataset_identifier)
        )
        filename = _safe_filename(filename_source, default="ndp-resource")
        output_path = validate_write_path(str(destination_dir / filename), field="output_path")
    except FilePolicyError as exc:
        return exc.to_result()

    existing = _existing_staged_resource(output_path, url=url)
    if existing is not None:
        existing.update(
            {
                "dataset_id": dataset.get("id"),
                "dataset_name": dataset.get("name"),
                "dataset_title": dataset.get("title"),
                "resource_name": resource.get("name"),
                "selected_resource_name": resource.get("name"),
                "selected_resource_url": url,
                "_meta": {"tool": "stage_resource", "status": "success", "cache_hit": True},
            }
        )
        return existing

    resource_size_bytes = _parse_resource_size_bytes(
        resource.get("size") or resource.get("resSize")
    )
    if is_osdf:
        result = _stage_pelican_resource(
            url=url,
            output_path=output_path,
            max_bytes=max_stage_bytes,
            resource_size_bytes=resource_size_bytes,
        )
    else:
        result = _stage_http_resource(
            url=url,
            output_path=output_path,
            max_bytes=max_stage_bytes,
        )
    if result.get("error"):
        return result
    result.update(
        {
            "dataset_id": dataset.get("id"),
            "dataset_name": dataset.get("name"),
            "dataset_title": dataset.get("title"),
            "resource_name": resource.get("name"),
            "selected_resource_name": resource.get("name"),
            "selected_resource_url": url,
            "source_url": url,
            "_meta": {"tool": "stage_resource", "status": "success"},
        }
    )
    return result


@ndp_server.tool()
def query_arcgis_features(
    feature_service_url: str,
    layer_id: int | str | None = None,
    where: str = "1=1",
    out_fields: str = "*",
    max_features: int | str | None = 25,
    min_lon: float | str | None = None,
    min_lat: float | str | None = None,
    max_lon: float | str | None = None,
    max_lat: float | str | None = None,
    output_path: str | None = None,
) -> dict[str, Any]:
    """Query an NDP ArcGIS FeatureServer resource with optional lon/lat bbox.

    This is a generic bridge for catalog resources advertised as ``Esri REST``.
    It keeps payloads compact for agent traces and can persist a GeoJSON-like
    feature collection for later artifact inspection.
    """

    if not output_path and feature_service_url:
        # Auto-persist every layer query so downstream bbox/render/overlap always
        # have a file to resolve — removes the fragile "the model must remember
        # to pass output_path" link that breaks region/map steps on small models.
        # Name it from the service so keyword resolution finds it (fire ->
        # ...perimeters, smoke -> ...smokeforecast, air -> ...air...).
        try:
            segments = [s for s in str(feature_service_url).split("/") if s]
            service_name = ""
            for index, segment in enumerate(segments):
                if segment.lower() == "featureserver" and index > 0:
                    service_name = segments[index - 1]
                    break
            if not service_name:
                service_name = next(
                    (s for s in reversed(segments) if s and not s.isdigit() and "." not in s),
                    "features",
                )
            safe = re.sub(r"[^a-zA-Z0-9]+", "_", service_name).strip("_").lower() or "features"
            output_path = f"{safe}.geojson"
        except Exception:  # noqa: BLE001 - never let auto-naming break the query
            output_path = None

    if output_path:
        # Relocate the saved FeatureCollection into a writable artifact dir so
        # models that pass invented/unwritable paths (e.g. /workspace/x.geojson)
        # still produce a real file the renderer can read.
        _root = Path(os.environ.get("CLIO_ARTIFACTS_ROOT") or (Path.cwd() / ".clio" / "artifacts" / "geo"))
        _root.mkdir(parents=True, exist_ok=True)
        _name = Path(str(output_path)).name or "features.geojson"
        if not _name.lower().endswith((".geojson", ".json")):
            _name += ".geojson"
        output_path = str((_root / _name).resolve())

    try:
        limit = max(1, min(int(max_features or 25), _MAX_ARCGIS_FEATURES))
        params: dict[str, Any] = {
            "f": "json",
            "where": _clean_optional_text(where) or "1=1",
            "outFields": _clean_optional_text(out_fields) or "*",
            "returnGeometry": "true",
            "outSR": 4326,
            "resultRecordCount": limit,
        }
        params.update(
            _arcgis_bbox_geometry(
                min_lon=min_lon,
                min_lat=min_lat,
                max_lon=max_lon,
                max_lat=max_lat,
            )
        )
        query_url = _arcgis_layer_query_url(feature_service_url, layer_id)
    except ValueError as exc:
        return _tool_error(
            code="arcgis_query_invalid_arguments",
            message=str(exc),
            next_action="Provide a valid FeatureServer URL, numeric layer id, and optional bbox.",
        )

    try:
        response = requests.get(query_url, params=params, timeout=30)
        response.raise_for_status()
        decoded = response.json()
    except (requests.RequestException, ValueError) as exc:
        return _tool_error(
            code="arcgis_query_failed",
            message=f"Could not query ArcGIS FeatureServer resource: {exc}",
            next_action="Inspect the resource URL, layer id, where clause, and bbox.",
            details={"url": feature_service_url, "query_url": query_url},
        )

    if isinstance(decoded, dict) and decoded.get("error"):
        return _tool_error(
            code="arcgis_query_error",
            message="ArcGIS returned an error for the requested feature query.",
            next_action="Adjust the layer id, where clause, out fields, or geometry filter.",
            details={"url": feature_service_url, "arcgis_error": decoded.get("error")},
        )
    features = decoded.get("features") if isinstance(decoded, dict) else None
    if not isinstance(features, list):
        return _tool_error(
            code="arcgis_query_unexpected_payload",
            message="ArcGIS returned no feature list for the requested resource.",
            next_action="Inspect the service metadata and retry with a concrete FeatureServer layer.",
            details={"url": feature_service_url, "payload_keys": sorted(decoded) if isinstance(decoded, dict) else []},
        )

    collection = _arcgis_feature_collection(features)
    # The compact collection above is for agent traces (geometry is summarized).
    # For a saved file, fetch native GeoJSON so downstream renderers get real
    # geometry instead of bbox/point summaries.
    output: dict[str, Any] = {}
    if output_path:
        geo_collection = collection
        try:
            geo_resp = requests.get(query_url, params={**params, "f": "geojson"}, timeout=30)
            geo_resp.raise_for_status()
            candidate = geo_resp.json()
            if isinstance(candidate, dict) and candidate.get("type") == "FeatureCollection":
                geo_collection = candidate
        except (requests.RequestException, ValueError):
            geo_collection = collection
        output = _write_json_output(output_path, geo_collection)
    if output.get("error"):
        return output
    raw_fields = decoded.get("fields") if isinstance(decoded, dict) else []
    fields = raw_fields if isinstance(raw_fields, list) else []
    return {
        "ok": True,
        "source_url": feature_service_url,
        "query_url": response.url,
        "feature_count": len(collection["features"]),
        "geometry_type": decoded.get("geometryType") if isinstance(decoded, dict) else None,
        "fields": [
            str(field.get("name"))
            for field in fields
            if isinstance(field, dict) and field.get("name")
        ][:24],
        "features": collection["features"][: min(10, len(collection["features"]))],
        "features_truncated": len(collection["features"]) > 10,
        **output,
        "_meta": {"tool": "query_arcgis_features", "status": "success"},
    }


@ndp_server.tool()
def profile_csv_resource(
    filepath: str,
    max_rows: int | str | None = 5000,
) -> dict[str, Any]:
    """Profile a staged NDP CSV resource with columns, samples, and numeric stats."""

    path = Path(filepath).expanduser()
    if not path.exists() or not path.is_file():
        return _tool_error(
            code="csv_resource_missing",
            message="The staged CSV resource path does not exist or is not a file.",
            next_action="Run ndp_stage_resource first or provide an existing CSV path.",
            details={"filepath": filepath},
        )
    try:
        limit = max(1, min(int(max_rows or 5000), _MAX_CSV_PROFILE_ROWS))
        columns, rows, row_count = _read_csv_rows(path, max_rows=limit)
    except (OSError, csv.Error, UnicodeDecodeError, ValueError) as exc:
        return _tool_error(
            code="csv_profile_failed",
            message=f"Could not profile the staged CSV resource: {exc}",
            next_action="Verify the resource is a UTF-8 compatible CSV or select another resource.",
            details={"filepath": filepath},
        )
    numeric = _csv_numeric_summary(rows, columns)
    missing = _csv_missing_summary(rows, columns)
    rows_profiled = len(rows)
    return {
        "ok": True,
        "path": str(path),
        "size_bytes": path.stat().st_size,
        "columns": columns,
        "column_count": len(columns),
        "rows_examined": row_count,
        "rows_profiled": rows_profiled,
        "rows_scanned": row_count,
        "numeric_summary_rows": rows_profiled,
        "row_scan_cap": _MAX_CSV_PROFILE_ROWS,
        "scan_limited": row_count >= _MAX_CSV_PROFILE_ROWS,
        "profile_limited": row_count > rows_profiled,
        "numeric_summary": numeric,
        "missing_values": missing,
        "missing_values_rows": rows_profiled,
        "missing_values_scope": "profiled_rows",
        "sample_rows": _csv_sample_rows(rows),
        "_meta": {"tool": "profile_csv_resource", "status": "success"},
    }


@ndp_server.tool()
def filter_earthscope_station_catalog(
    filepath: str,
    latitude: float | str,
    longitude: float | str,
    radius_km: float | str = 50.0,
    limit: int | str = 10,
) -> dict[str, Any]:
    """Find nearby EarthScope GNSS stations from a staged station metadata CSV."""

    path = Path(filepath).expanduser()
    if not path.exists() or not path.is_file():
        return _tool_error(
            code="station_catalog_missing",
            message="The staged EarthScope station metadata CSV path does not exist.",
            next_action="Run ndp_stage_resource for the EarthScope station metadata CSV first.",
            details={"filepath": filepath},
        )
    lat = _to_float(latitude)
    lon = _to_float(longitude)
    radius = _to_float(radius_km)
    if lat is None or lon is None or radius is None:
        return _tool_error(
            code="station_catalog_invalid_geometry",
            message="Latitude, longitude, and radius_km must be numeric.",
            next_action="Use the geospatial expert's typed center coordinates.",
            details={"latitude": latitude, "longitude": longitude, "radius_km": radius_km},
        )
    try:
        row_limit = max(1, min(int(limit or 10), 50))
    except (TypeError, ValueError):
        row_limit = 10
    try:
        rows = _earthscope_station_rows(path, latitude=lat, longitude=lon)
    except (OSError, csv.Error, UnicodeDecodeError, ValueError) as exc:
        if "not an earthscope station metadata catalog" in str(exc).casefold():
            return {
                "ok": True,
                "path": str(path),
                "center": {"latitude": lat, "longitude": lon},
                "radius_km": radius,
                "catalog_applicable": False,
                "resource_kind": "station_timeseries_csv",
                "station_count": 0,
                "within_radius_count": 0,
                "stations": [],
                "analysis_ready": True,
                "next_action": (
                    "Do not filter this file as station metadata. Treat it as a "
                    "staged station time-series CSV and continue with profiling or plotting."
                ),
                "_meta": {
                    "tool": "filter_earthscope_station_catalog",
                    "status": "not_applicable",
                },
            }
        return _tool_error(
            code="station_catalog_parse_failed",
            message=f"Could not parse the EarthScope station metadata CSV: {exc}",
            next_action="Verify the resource is the EarthScope station metadata CSV.",
            details={"filepath": filepath},
        )
    within_radius = [row for row in rows if row["distance_km"] <= radius]
    selected = within_radius[:row_limit]
    resource_queries = _station_resource_queries(selected)
    return {
        "ok": True,
        "path": str(path),
        "center": {"latitude": lat, "longitude": lon},
        "radius_km": radius,
        "station_count": len(rows),
        "within_radius_count": len(within_radius),
        "stations": selected,
        "nearest_station": selected[0] if selected else (rows[0] if rows else None),
        "analysis_ready": False,
        "resource_discovery": {
            "status": "search_required" if selected else "no_station_candidates",
            "station_resource_queries": resource_queries,
            "search_terms": list(
                dict.fromkeys(
                    term
                    for row in selected[:5]
                    for term in row.get("resource_discovery", {}).get("search_terms", [])
                    if str(term).strip()
                )
            ),
            "reason": (
                "Nearby station metadata was found, but station-specific GNSS "
                "time-series CSV resources still need live NDP discovery."
                if selected
                else "No nearby stations were returned by the station metadata filter."
            ),
        },
        "next_action": (
            "Search NDP for a concrete station time-series CSV using the returned "
            "station_resource_queries preferred_calls, then stage that station-specific resource."
        ),
        "_meta": {"tool": "filter_earthscope_station_catalog", "status": "success"},
    }


@ndp_server.tool()
def plot_csv_timeseries(
    filepath: str,
    x_column: str,
    y_columns: list[str] | str,
    output_path: str | None = None,
    max_rows: int | str | None = 2000,
    title: str | None = None,
) -> dict[str, Any]:
    """Create a PNG line plot from staged NDP CSV columns."""

    path = Path(filepath).expanduser()
    if not path.exists() or not path.is_file():
        return _tool_error(
            code="csv_resource_missing",
            message="The staged CSV resource path does not exist or is not a file.",
            next_action="Run ndp_stage_resource first or provide an existing CSV path.",
            details={"filepath": filepath},
        )
    if isinstance(y_columns, str):
        selected_y = [part.strip() for part in y_columns.split(",") if part.strip()]
    else:
        selected_y = [str(part).strip() for part in y_columns if str(part).strip()]
    if not selected_y:
        return _tool_error(
            code="csv_plot_missing_columns",
            message="At least one y column is required for a CSV plot.",
            next_action="Inspect the CSV columns, then call plot_csv_timeseries with numeric y columns.",
        )
    default_output = path.with_name(f"{path.stem}_plot.png")
    output_path_corrected = False
    output_path_warning = ""
    candidate_output = Path(output_path).expanduser() if output_path else default_output
    if ".clio" in path.parts and candidate_output.parent != path.parent:
        output_path_corrected = True
        output_path_warning = (
            "Output path was kept beside the staged source CSV to preserve workspace "
            "artifact provenance."
        )
        candidate_output = default_output
    try:
        output = validate_write_path(str(candidate_output), field="output_path", create_parent=True)
    except FilePolicyError:
        if not output_path:
            raise
        output_path_corrected = True
        output_path_warning = (
            "Requested output path was outside allowed roots; wrote beside the staged source CSV "
            "instead."
        )
        output = validate_write_path(str(default_output), field="output_path", create_parent=True)
    try:
        row_limit = max(1, min(int(max_rows or 2000), _MAX_CSV_PROFILE_ROWS))
        columns, rows, _ = _read_csv_rows(path, max_rows=row_limit)
    except (OSError, csv.Error, UnicodeDecodeError, ValueError) as exc:
        return _tool_error(
            code="csv_plot_read_failed",
            message=f"Could not read the staged CSV resource for plotting: {exc}",
            next_action="Verify the resource is a UTF-8 compatible CSV or select another resource.",
            details={"filepath": filepath},
        )
    missing = [column for column in [x_column, *selected_y] if column not in columns]
    if missing:
        return _tool_error(
            code="csv_plot_unknown_columns",
            message="Requested plot columns are not present in the CSV resource.",
            next_action="Use ndp_profile_csv_resource to inspect available columns.",
            details={"missing_columns": missing, "available_columns": columns},
        )

    x_values = [row.get(x_column, "") for row in rows]
    x_axis = _infer_csv_plot_x_axis(x_values)
    series: dict[str, list[float | None]] = {
        column: [_to_float(row.get(column)) for row in rows] for column in selected_y
    }
    plotted = {column: values for column, values in series.items() if any(v is not None for v in values)}
    if not plotted:
        return _tool_error(
            code="csv_plot_no_numeric_values",
            message="None of the requested y columns contained numeric values in the scanned rows.",
            next_action="Choose numeric columns from ndp_profile_csv_resource output.",
            details={"y_columns": selected_y},
        )

    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.dates as mdates  # noqa: PLC0415
        import matplotlib.pyplot as plt  # noqa: PLC0415

        output.parent.mkdir(parents=True, exist_ok=True)
        fig, ax = plt.subplots(figsize=(10, 4.8))
        x_plot_values = x_axis["values"]
        valid_x = [value is not None for value in x_plot_values]
        for column, values in plotted.items():
            xy = [
                (x_value, value)
                for x_value, value, ok in zip(x_plot_values, values, valid_x, strict=False)
                if ok
            ]
            x_series = [item[0] for item in xy]
            y_series = [float("nan") if item[1] is None else item[1] for item in xy]
            ax.plot(x_series, y_series, linewidth=1.2, label=column)
        if x_axis["kind"] in {"epoch_milliseconds", "epoch_seconds", "datetime"}:
            locator = mdates.AutoDateLocator(minticks=4, maxticks=8)
            ax.xaxis.set_major_locator(locator)
            ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
            fig.autofmt_xdate(rotation=30, ha="right")
        else:
            tick_count = min(8, len(x_values))
            if tick_count:
                step = max(1, len(x_values) // tick_count)
                tick_positions = list(range(0, len(x_values), step))[:tick_count]
                tick_labels = x_axis.get("labels", x_values)
                ax.set_xticks(tick_positions)
                ax.set_xticklabels(
                    [tick_labels[index] for index in tick_positions], rotation=35, ha="right"
                )
        ax.set_xlabel(f"{x_column} ({x_axis['label']})")
        ax.set_ylabel(", ".join(plotted))
        ax.set_title(title or path.name)
        ax.grid(alpha=0.25)
        ax.legend(loc="best")
        fig.tight_layout()
        fig.savefig(output, dpi=140)
        plt.close(fig)
    except Exception as exc:  # pragma: no cover - dependency/runtime specific
        return _tool_error(
            code="csv_plot_failed",
            message=f"Could not create CSV plot artifact: {exc}",
            next_action="Inspect the selected columns and output path, then retry.",
            details={"filepath": filepath, "output_path": str(output)},
        )

    result = {
        "ok": True,
        "path": str(path),
        "output_path": str(output),
        "output_size_bytes": output.stat().st_size,
        "x_column": x_column,
        "x_axis": {
            "kind": x_axis["kind"],
            "label": x_axis["label"],
            "parse_success_ratio": x_axis["parse_success_ratio"],
        },
        "y_columns": sorted(plotted),
        "rows_plotted": len(x_values),
        "_meta": {"tool": "plot_csv_timeseries", "status": "success"},
    }
    if output_path_corrected:
        result["output_path_corrected"] = True
        result["requested_output_path"] = output_path
        result["warning"] = output_path_warning
    return result
