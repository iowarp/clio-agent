"""National Data Platform tools backed by clio-kit MCP."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

import requests
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StdioTransport

from clio_agent.tools.file_policy import FilePolicyError, validate_write_path

ndp_server = FastMCP("ndp")

_GLOBAL_CKAN_API = "https://nationaldataplatform.org/catalog/api/3/action"
_MAX_STAGE_BYTES = 50 * 1024 * 1024
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
    return formats[:5], len(resources), names[:1], urls[:3]


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
        "20",
        "--max-time",
        "600",
        "--retry",
        "2",
        "--retry-delay",
        "2",
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
            timeout=660,
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
                "timeout_s": 660,
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
        except Exception:
            pass
        else:
            if compact:
                compacted = _compact_dataset(direct)
                compacted["_meta"] = {
                    "tool": "get_dataset_details",
                    "status": "success",
                    "source": "ckan_package_show",
                }
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
    return await _call_clio_kit_ndp_tool("list_organizations", args)


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
    return await _call_clio_kit_ndp_tool(
        "search_datasets",
        {key: value for key, value in args.items() if value is not None},
    )


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
            "_meta": {"tool": "stage_resource", "status": "success"},
        }
    )
    return result
