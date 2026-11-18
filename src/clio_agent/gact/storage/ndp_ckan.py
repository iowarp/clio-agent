"""Bounded, paginated metadata reads from an explicitly configured CKAN catalog."""

from __future__ import annotations

import json
import re
import threading
import time
from html.parser import HTMLParser
from typing import Any

import httpx

from clio_agent.gact.storage.ndp_tls import system_tls

PUBLIC_CATALOG_URL = "https://nationaldataplatform.org/catalog"
MAX_METADATA_BYTES = 16 * 1024 * 1024


class _DescriptionText(HTMLParser):
    """Extract readable catalog descriptions without rendering or fetching markup."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self.hidden += 1
        self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def catalog_description(value: str) -> str:
    """Present HTML descriptions as text while preserving ordinary scientific notation."""
    if not re.search(
        r"</?(?:p|div|span|br|a|strong|b|ul|ol|li|h[1-6]|script|style)\b", value, re.I
    ):
        return value
    parser = _DescriptionText()
    parser.feed(value)
    parser.close()
    return " ".join("".join(parser.parts).split())


class NDPCkanCatalog:
    """Read public metadata without forwarding an Endpoint's private account token."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._organizations: dict[str, tuple[float, list[str]]] = {}

    @staticmethod
    def _request(url: str, action: str, parameters: dict[str, Any]) -> Any:
        """Read one bounded page; never retry a slow full-catalog query or follow redirects."""
        with httpx.Client(
            timeout=httpx.Timeout(15, connect=5),
            follow_redirects=False,
            trust_env=False,
            verify=system_tls(),
        ) as client:
            with client.stream(
                "GET", f"{url}/api/3/action/{action}", params=parameters
            ) as response:
                response.raise_for_status()
                body = bytearray()
                for chunk in response.iter_bytes(chunk_size=65536):
                    body.extend(chunk)
                    if len(body) > MAX_METADATA_BYTES:
                        raise ValueError("NDP catalog metadata exceeds the 16 MiB page limit")
        data = json.loads(body)
        if not isinstance(data, dict) or data.get("success") is not True:
            raise ValueError("NDP catalog could not complete this metadata request")
        return data["result"]

    def search(
        self,
        url: str,
        query: str,
        organization: str,
        offset: int,
        limit: int,
        *,
        resource_format: str = "",
    ) -> tuple[list[dict[str, Any]], int]:
        """Search literal word prefixes and page before retrieving dataset metadata.

        ``resource_format`` keeps datasets with a resource of that format (CKAN
        ``res_format``), quoted like the organization so it is a value, never syntax.
        """
        # This is a human text box, not a Solr expression editor. Tokenization
        # keeps operators out and prefix matching supports unfamiliar names.
        tokens = re.findall(r"\w+", query, flags=re.UNICODE)
        parameters: dict[str, Any] = {
            "q": " AND ".join(f"{term}*" for term in tokens) or "*:*",
            "rows": limit,
            "start": offset,
            "sort": "score desc, name asc",
        }
        filters = []
        if organization:
            filters.append("organization:" + json.dumps(organization, ensure_ascii=False))
        if resource_format:
            filters.append("res_format:" + json.dumps(resource_format, ensure_ascii=False))
        if filters:
            parameters["fq"] = " AND ".join(filters)
        result = self._request(url, "package_search", parameters)
        if not isinstance(result, dict) or not isinstance(result.get("results"), list):
            raise ValueError("NDP catalog returned an invalid dataset page")
        rows, count = result["results"], result.get("count")
        if (
            not isinstance(count, int)
            or count < 0
            or any(not isinstance(row, dict) for row in rows)
        ):
            raise ValueError("NDP catalog returned an invalid dataset count or record")
        return rows[:limit], count

    def organizations(self, url: str) -> list[str]:
        """Reuse the publisher list briefly; filtering names needs no new catalog request."""
        with self._lock:
            cached = self._organizations.get(url)
            if cached is not None and time.monotonic() - cached[0] < 60:
                return list(cached[1])
        names = self._request(url, "organization_list", {"all_fields": "false", "limit": 1000})
        if not isinstance(names, list) or any(not isinstance(name, str) for name in names):
            raise ValueError("NDP catalog returned an invalid organization list")
        with self._lock:
            self._organizations[url] = time.monotonic(), names
        return list(names)
