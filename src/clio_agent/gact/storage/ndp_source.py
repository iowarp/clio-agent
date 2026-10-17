"""Read selected NDP resources through their existing HTTP, Pelican or S3 routes."""

from __future__ import annotations

import io
from collections.abc import Buffer
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, Callable, Iterator, cast
from urllib.parse import quote, unquote, urljoin, urlsplit

import httpx
from clio_schemas.connected_resources import SourceCapabilities

from clio_agent.gact.storage.models import FileEntry
from clio_agent.gact.storage.ndp_catalog import NDPDataset, NDPEndpoint, NDPResource

if TYPE_CHECKING:
    from clio_agent.gact.storage.models import SourceRecord
    from clio_agent.gact.storage.store import SourceStore

MAX_READ_BYTES = 512 * 1024 * 1024


def public_selection(store: SourceStore, record: SourceRecord) -> bool:
    """Offer anonymous access only for selected HTTP resources outside an authenticated Endpoint."""
    from clio_agent.gact.storage.ndp_catalog import NDPSelection

    try:
        selection = store.get("ndp-selection", record.configuration.ndp_selection_id, NDPSelection)
    except KeyError:
        return False
    return not selection.dataset.endpoint.authenticated and all(
        urlsplit(item.url).scheme in {"http", "https"} for item in selection.dataset.resources
    )


class _ResourceReader(io.RawIOBase):
    def __init__(self, response: httpx.Response, client: httpx.Client) -> None:
        self.response, self.client = response, client
        self.chunks: Iterator[bytes] = response.iter_bytes(chunk_size=65536)
        self.pending = b""
        self.total = 0

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Buffer) -> int:
        view = memoryview(buffer).cast("B")
        if not self.pending:
            try:
                self.pending = next(self.chunks, b"")
            except httpx.HTTPError as exc:
                raise ValueError("NDP stopped streaming this resource; retry the read") from exc
            self.total += len(self.pending)
            if self.total > MAX_READ_BYTES:
                raise ValueError("This NDP resource exceeds the 512 MB read limit")
        size = min(len(view), len(self.pending))
        view[:size], self.pending = self.pending[:size], self.pending[size:]
        return size

    def close(self) -> None:
        try:
            self.response.close()
        finally:
            self.client.close()
            super().close()


def credential_target(endpoint: NDPEndpoint, url: str) -> bool:
    """Send a user's bearer only beneath their explicitly configured HTTPS Endpoint."""
    target, base = urlsplit(url), urlsplit(endpoint.url)
    return bool(
        endpoint.authenticated
        and target.scheme == base.scheme == "https"
        and target.netloc == base.netloc
        and (target.path == base.path or target.path.startswith(base.path.rstrip("/") + "/"))
    )


def resource_route(endpoint: NDPEndpoint, resource: NDPResource) -> tuple[str, dict[str, str]]:
    """Dispatch selected catalog URLs to documented read routes, never mutation routes."""
    parsed = urlsplit(resource.url)
    if parsed.username or parsed.password or parsed.fragment or not parsed.hostname:
        raise ValueError("NDP returned an invalid resource address")
    if parsed.scheme in {"https", "http"}:
        return resource.url, {}
    if parsed.scheme in {"pelican", "osdf"}:
        if parsed.scheme == "osdf" or parsed.hostname == "osg-htc.org":
            federation = "osdf"
        elif parsed.hostname == "path-cc.io":
            federation = "path-cc"
        else:
            # Full URLs are accepted by the Endpoint's Pelican client. Do not
            # silently route an unknown federation through its osdf default.
            raise ValueError(
                "Configure this Pelican federation on the NDP Endpoint before reading it"
            )
        return endpoint.url + "/pelican/download", {
            "path": unquote(parsed.path),
            "federation": federation,
            "stream": "true",
        }
    if parsed.scheme == "s3":
        bucket = quote(parsed.netloc, safe="")
        key = quote(unquote(parsed.path.lstrip("/")), safe="/")
        if not key or any(part in {".", ".."} for part in unquote(parsed.path).split("/")):
            raise ValueError("Choose an S3 object, not a bucket or relative path")
        return endpoint.url + f"/s3/objects/{bucket}/{key}", {}
    raise ValueError("This NDP resource transport is not supported; choose HTTP(S), Pelican or S3")


class NDPSource:
    """A selected read-only dataset; metadata linking never downloads its contents."""

    capabilities = SourceCapabilities(
        link_folder=True,
        search=True,
        revision_check=True,
        supported_modes=["read_only"],
        unavailable_reasons={
            "working_copy": "NDP connections are read-only.",
            "write_enabled": "NDP connections are read-only.",
        },
    )

    def __init__(self, dataset: NDPDataset, token: Callable[[], str] | None = None) -> None:
        self.dataset, self.token = dataset, token

    def entries(
        self,
        *,
        progress: Callable[[int], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
        **_: object,
    ) -> list[FileEntry]:
        """Expose only approved resources under stable, portable names."""
        if cancelled and cancelled():
            raise InterruptedError("NDP indexing cancelled")
        return [
            FileEntry(
                path=item.path, kind="file", size=item.size, revision=item.revision, locator=item.id
            )
            for item in self.dataset.resources
        ]

    def open_read(self, entry: FileEntry) -> BinaryIO:
        """Stream a selected object with bounded reads and explicit response ownership."""
        selected = next(
            (
                item
                for item in self.dataset.resources
                if item.path == entry.path and item.id == entry.locator
            ),
            None,
        )
        if selected is None:
            raise PermissionError("Choose a resource attached to this NDP source")
        if selected.size > MAX_READ_BYTES:
            raise ValueError("This NDP resource exceeds the 512 MB read limit")
        url, params = resource_route(self.dataset.endpoint, selected)
        protected = urlsplit(selected.url).scheme in {"pelican", "osdf", "s3"}
        if protected and not credential_target(self.dataset.endpoint, url):
            raise PermissionError(
                "Configure an HTTPS NDP Endpoint for authenticated Pelican or S3 reads"
            )
        client = httpx.Client(timeout=httpx.Timeout(60, connect=15), follow_redirects=False)
        response: httpx.Response | None = None
        try:
            for attempt in range(6):
                headers = {}
                if credential_target(self.dataset.endpoint, url):
                    if self.token is None:
                        raise PermissionError("Sign in to NDP to read this Endpoint's resources")
                    headers["Authorization"] = "Bearer " + self.token()
                request = client.build_request("GET", url, params=params, headers=headers)
                response = client.send(request, stream=True)
                if response.is_redirect:
                    location = response.headers.get("location", "")
                    response.close()
                    url, params = urljoin(url, location), {}
                    parsed = urlsplit(url)
                    if (
                        parsed.scheme not in {"http", "https"}
                        or parsed.username
                        or parsed.password
                        or (protected and parsed.scheme != "https")
                    ):
                        raise ValueError("NDP returned an unsafe download redirect")
                    if attempt == 5:
                        raise ValueError("NDP returned too many download redirects")
                    continue
                if response.status_code in {401, 403}:
                    suffix = (
                        " The Endpoint currently requires writer access even for S3 downloads."
                        if urlsplit(selected.url).scheme == "s3"
                        else ""
                    )
                    raise PermissionError(
                        "NDP denied this resource. Check your sign-in and live Endpoint/group permissions."
                        + suffix
                    )
                if not response.is_success:
                    raise ValueError(
                        f"NDP could not read this resource (HTTP {response.status_code})"
                    )
                if int(response.headers.get("content-length", "0")) > MAX_READ_BYTES:
                    raise ValueError("This NDP resource exceeds the 512 MB read limit")
                return cast(BinaryIO, io.BufferedReader(_ResourceReader(response, client)))
            raise ValueError("NDP download could not be resolved")
        except Exception as exc:
            if response is not None:
                response.close()
            client.close()
            if isinstance(exc, httpx.HTTPError):
                raise ValueError("NDP could not be reached while reading this resource") from exc
            raise

    def apply(self, path: str, content: Path | None, expected: str | None) -> str | None:
        """Refuse every upstream write even when the account has a writer/admin role."""
        raise PermissionError("NDP sources are read-only")
