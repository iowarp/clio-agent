"""Recapture the search fixtures a rate-limited network could not record.

Run from a normal (not shared cloud) network, keyless, a few polite calls per
service. It rewrites ``tests/test_search/fixtures/<api>/<case>.json`` in the format
``tests/test_search/_fakes.py`` replays, trimming bodies to five results and long
text to 4,000 characters.

    uv run python scripts/search/recapture_fixtures.py            # every missing case
    uv run python scripts/search/recapture_fixtures.py exa parallel  # only these
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[2] / "tests" / "test_search" / "fixtures"
UA = "clio-agent-tests/0.1 (+https://github.com/iowarp/clio-agent)"
ACCEPT = "application/json, text/event-stream"
KEEP = ("content-type", "retry-after", "mcp-session-id")


def trim(value: Any) -> Any:
    """Keep the structure, five results per list and 4,000 characters per string."""

    if isinstance(value, str):
        return value if len(value) <= 4000 else value[:4000] + "…[trimmed]"
    if isinstance(value, list):
        return [trim(v) for v in value[:5]]
    if isinstance(value, dict):
        return {k: trim(v) for k, v in value.items()}
    return value


def save(api: str, case: str, request: dict[str, Any], response: httpx.Response) -> None:
    """Write one fixture and print its status."""

    ctype = response.headers.get("content-type", "")
    if "event-stream" in ctype:
        body: Any = "\n".join(
            "data: " + json.dumps(trim(json.loads(line[6:]))) if line.startswith("data: ") else line
            for line in response.text.splitlines()
        )
    else:
        try:
            body = trim(response.json())
        except ValueError:
            body = trim(response.text)
    headers = {
        k: v for k, v in response.headers.items() if k.lower() in KEEP or "ratelimit" in k.lower()
    }
    target = ROOT / api / f"{case}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    record = {"request": request, "status": response.status_code, "headers": headers, "body": body}
    target.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{api}/{case}: HTTP {response.status_code}")
    time.sleep(2)


def mcp(
    client: httpx.Client, api: str, url: str, calls: list[tuple[str, str, dict[str, Any]]]
) -> None:
    """Open an MCP session and record each ``(case, tool, arguments)`` call."""

    init = {
        "jsonrpc": "2.0",
        "id": 0,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "clio-agent-tests", "version": "1"},
        },
    }
    first = client.post(url, json=init, headers={"accept": ACCEPT})
    session = first.headers.get("mcp-session-id")
    headers = {"accept": ACCEPT, **({"mcp-session-id": session} if session else {})}
    client.post(
        url, json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=headers
    )
    for index, (case, tool, args) in enumerate(calls, 1):
        body = {"jsonrpc": "2.0", "id": index, "method": "tools/call"}
        body["params"] = {"name": tool, "arguments": args}
        save(
            api,
            case,
            {"method": "POST", "url": url, "json": body},
            client.post(url, json=body, headers=headers),
        )


def capture(client: httpx.Client, api: str) -> None:
    """Record the cases one service is missing."""

    if api == "exa":
        mcp(client, "exa", "https://mcp.exa.ai/mcp?tools=web_search_advanced_exa,web_fetch_exa", [
            ("search_ok", "web_search_advanced_exa", {"query": "ADIOS2 BP5 engine documentation", "numResults": 5, "textMaxCharacters": 2000}),
            ("search_filtered", "web_search_advanced_exa", {"query": "lossy compression scientific data", "numResults": 5, "includeDomains": ["arxiv.org"], "startPublishedDate": "2025-01-01", "textMaxCharacters": 2000}),
            ("fetch_ok", "web_fetch_exa", {"urls": ["https://slurm.schedmd.com/job_array.html"], "maxCharacters": 3000}),
        ])  # fmt: skip
    elif api == "parallel":
        mcp(client, "parallel", "https://search.parallel.ai/mcp", [
            ("search_ok", "web_search", {"objective": "ADIOS2 BP5 engine documentation", "search_queries": ["ADIOS2 BP5 engine"]}),
            ("fetch_ok", "web_fetch", {"urls": ["https://slurm.schedmd.com/job_array.html"], "objective": "job array syntax"}),
        ])  # fmt: skip
    elif api == "firecrawl":
        url = "https://api.firecrawl.dev/v2/search"
        for case, body in (
            ("search_ok", {"query": "ADIOS2 BP5 engine documentation", "limit": 5}),
            ("search_research", {"query": "wildfire smoke PM2.5 satellite", "limit": 3, "categories": ["research"], "tbs": "cdr:1,cd_min:1/1/2025,cd_max:11/1/2026"}),
        ):  # fmt: skip
            save(
                api, case, {"method": "POST", "url": url, "json": body}, client.post(url, json=body)
            )
    elif api == "openalex":
        for case, url in (
            ("filter_search", "https://api.openalex.org/works?search=lossy%20compression%20scientific%20data&per_page=5"),
            ("work_by_doi", "https://api.openalex.org/works/doi:10.1038/s41586-025-09482-1"),
        ):  # fmt: skip
            save(api, case, {"method": "GET", "url": url}, client.get(url))
    else:
        raise SystemExit(f"unknown api {api!r}; choose exa, parallel, firecrawl, openalex")


def main(apis: list[str]) -> None:
    """Recapture the named services (all four by default)."""

    with httpx.Client(timeout=90, headers={"User-Agent": UA}, follow_redirects=True) as client:
        for api in apis or ["exa", "parallel", "firecrawl", "openalex"]:
            capture(client, api)


if __name__ == "__main__":
    main(sys.argv[1:])
