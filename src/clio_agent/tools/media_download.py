"""Bounded fetch of one external media file (image, video, audio) by URL.

This is the network half of the ``download_media`` native tool
(:mod:`clio_agent.gact.media_download_tool`). It exists because A2UI viewers
never auto-load an external URL (release 0.9.4.19): to SHOW web media an
agent saves a copy into its workspace, and the producer exports that path as
an artifact every viewer can fetch.

The fetch is bounded by reality checks only, never by guessing what the model
meant:

* the URL is ``http:``/``https:`` with a host and no embedded credentials;
* every host it touches (the first and each redirect hop) must resolve to
  public addresses only -- a model-authored URL must not reach the service's
  loopback, private network or cloud metadata endpoint -- unless config opts
  in with ``tools.media_download.allow_private_hosts``;
* the size cap is enforced from the declared length and again while
  streaming, so an oversized body is never held in memory;
* the declared ``Content-Type`` must match a configured media-type pattern,
  and the leading bytes must not contradict it (an HTML login page served as
  ``image/png`` is refused, not saved).

Every refusal is a :class:`MediaDownloadError` carrying a typed ``reason``, a
session-specific ``detail`` and an actionable ``hint``. The module never
touches the filesystem: where the bytes land is the caller's decision, under
the file policy.
"""

from __future__ import annotations

import hashlib
import ipaddress
import socket
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

#: Default byte cap for one download (config: ``tools.media_download.max_bytes``).
DEFAULT_MAX_BYTES = 50 * 1024 * 1024
#: Default accepted media types (config: ``tools.media_download.media_types``).
DEFAULT_MEDIA_TYPES: tuple[str, ...] = ("image/*", "video/*", "audio/*")
DEFAULT_CONNECT_TIMEOUT_S = 15.0
DEFAULT_READ_TIMEOUT_S = 60.0
#: Redirect hops followed before refusing (each hop is re-validated).
MAX_REDIRECTS = 5
#: Leading bytes sniffed to check the body against its declared type.
_SNIFF_BYTES = 4096
#: Declared types that say nothing about the content; the sniffed type decides.
_GENERIC_TYPES = frozenset({"", "application/octet-stream", "binary/octet-stream"})
#: A sniffed container type that is the same bytes as these declared types.
_CONTAINER_ALIASES = {"application/ogg": ("audio/ogg", "video/ogg", "audio/opus")}

Resolver = Callable[[str, int], list[str]]


class MediaDownloadError(Exception):
    """A typed refusal the model can read and act on."""

    def __init__(
        self,
        reason: str,
        detail: str,
        *,
        hint: str = "",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail
        self.hint = hint
        self.details = details or {}

    def to_result(self) -> dict[str, Any]:
        """Return the tool-result dict ``{ok: False, reason, detail, hint, **details}``."""

        return {
            "ok": False,
            "reason": self.reason,
            "detail": self.detail,
            "hint": self.hint,
            **self.details,
        }


def _csv(value: Any) -> tuple[str, ...]:
    if isinstance(value, (list, tuple)):
        items = [str(item) for item in value]
    else:
        items = str(value).split(",")
    return tuple(item.strip().lower() for item in items if item.strip())


@dataclass(frozen=True)
class MediaDownloadLimits:
    """Config-resolved bounds for one download."""

    max_bytes: int = DEFAULT_MAX_BYTES
    allowed_media_types: tuple[str, ...] = DEFAULT_MEDIA_TYPES
    connect_timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S
    read_timeout_s: float = DEFAULT_READ_TIMEOUT_S
    allow_private_hosts: bool = False

    @classmethod
    def from_config(cls) -> MediaDownloadLimits:
        """Resolve every bound through ``clio_agent.conf`` (file, then env, then default)."""

        from clio_agent import conf  # noqa: PLC0415 - keep module import light
        from clio_agent.tools.file_policy import _coerce_size_bytes  # noqa: PLC0415

        return cls(
            max_bytes=conf.resolve(
                "tools.media_download.max_bytes",
                env="CLIO_MEDIA_DOWNLOAD_MAX_BYTES",
                default=DEFAULT_MAX_BYTES,
                cast=_coerce_size_bytes("CLIO_MEDIA_DOWNLOAD_MAX_BYTES"),
            ),
            allowed_media_types=conf.resolve(
                "tools.media_download.media_types",
                env="CLIO_MEDIA_DOWNLOAD_MEDIA_TYPES",
                default=DEFAULT_MEDIA_TYPES,
                cast=_csv,
            ),
            connect_timeout_s=conf.resolve(
                "tools.media_download.connect_timeout_s",
                env="CLIO_MEDIA_DOWNLOAD_CONNECT_TIMEOUT_S",
                default=DEFAULT_CONNECT_TIMEOUT_S,
                cast=conf.as_float,
            ),
            read_timeout_s=conf.resolve(
                "tools.media_download.read_timeout_s",
                env="CLIO_MEDIA_DOWNLOAD_READ_TIMEOUT_S",
                default=DEFAULT_READ_TIMEOUT_S,
                cast=conf.as_float,
            ),
            allow_private_hosts=conf.resolve(
                "tools.media_download.allow_private_hosts",
                env="CLIO_MEDIA_DOWNLOAD_ALLOW_PRIVATE_HOSTS",
                default=False,
                cast=conf.as_bool,
            ),
        )


@dataclass(frozen=True)
class FetchedMedia:
    """One downloaded media body, verified against its declared type."""

    body: bytes
    media_type: str
    final_url: str
    host: str
    sha256: str = field(default="")


def media_type_allowed(media_type: str, patterns: tuple[str, ...]) -> bool:
    """Whether ``media_type`` matches one ``type/subtype`` or ``type/*`` pattern."""

    kind = media_type.strip().lower()
    if "/" not in kind:
        return False
    family = kind.split("/", 1)[0]
    return any(p == kind or (p.endswith("/*") and p[:-2] == family) for p in patterns)


def default_resolve(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


def _host_addresses(host: str, port: int, resolve: Resolver) -> list[str]:
    try:
        ipaddress.ip_address(host)
        return [host]
    except ValueError:
        pass
    try:
        addresses = resolve(host, port)
    except OSError as exc:
        raise MediaDownloadError(
            "media_download_network_error",
            f"the host {host!r} could not be resolved: {exc}",
            hint="check the URL's host name; the file cannot be fetched from here",
        ) from exc
    if not addresses:
        raise MediaDownloadError(
            "media_download_network_error",
            f"the host {host!r} resolved to no address",
            hint="check the URL's host name; the file cannot be fetched from here",
        )
    return addresses


def validate_media_url(url: str, limits: MediaDownloadLimits, *, resolve: Resolver) -> str:
    """Return the URL's host after checking scheme, credentials and host reachability.

    Raises:
        MediaDownloadError: ``media_download_invalid_url`` or
            ``media_download_host_not_allowed`` or ``media_download_network_error``.
    """

    raw = (url or "").strip()
    parts = urlsplit(raw)
    invalid_hint = "pass an absolute http: or https: URL of the media file itself"
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise MediaDownloadError(
            "media_download_invalid_url",
            f"{raw[:200]!r} is not an absolute http: or https: URL with a host",
            hint=invalid_hint,
        )
    if parts.username or parts.password:
        raise MediaDownloadError(
            "media_download_invalid_url",
            "the URL embeds credentials, which this tool never sends",
            hint="use a public URL that needs no credentials",
        )
    host = parts.hostname
    port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
    if not limits.allow_private_hosts:
        for address in _host_addresses(host, port, resolve):
            ip = ipaddress.ip_address(address.split("%", 1)[0])
            if not ip.is_global:
                raise MediaDownloadError(
                    "media_download_host_not_allowed",
                    f"{host!r} resolves to the non-public address {address}; a download "
                    "may only reach public hosts",
                    hint=(
                        "use a public URL; private-network hosts are reachable only when "
                        "tools.media_download.allow_private_hosts is enabled in config"
                    ),
                )
    return host


def _check_declared_type(declared: str, url: str, limits: MediaDownloadLimits) -> None:
    if declared in _GENERIC_TYPES or media_type_allowed(declared, limits.allowed_media_types):
        return
    allowed = ", ".join(limits.allowed_media_types)
    raise MediaDownloadError(
        "media_download_content_type_not_allowed",
        f"{url} answered with Content-Type {declared!r}; this tool saves only {allowed}",
        hint=(
            "pass the URL of the media file itself (often the page links to it), "
            "not a web page about it"
        ),
    )


def _verified_type(declared: str, body: bytes, url: str, limits: MediaDownloadLimits) -> str:
    """Check the leading bytes against the declared type; return the effective type."""

    from clio_agent.gact.resource_mime import detect_media_type  # noqa: PLC0415

    name = urlsplit(url).path.rsplit("/", 1)[-1]
    head = body[:_SNIFF_BYTES]
    sniffed, source = detect_media_type(name, head)
    mismatch_hint = "the URL does not serve the media it claims; find the file's direct URL"
    if declared == "image/svg+xml" and b"<svg" in head:
        # SVG is XML text by construction: the only media type whose body sniffs as text.
        return declared
    if source in {"utf8", "utf8_and_extension"}:
        raise MediaDownloadError(
            "media_download_content_mismatch",
            f"{url} declared {declared or 'no type'!r} but returned text ({sniffed})",
            hint=mismatch_hint,
        )
    if source == "signature":
        if sniffed in _CONTAINER_ALIASES and declared in _CONTAINER_ALIASES[sniffed]:
            return declared
        if not media_type_allowed(sniffed, limits.allowed_media_types):
            raise MediaDownloadError(
                "media_download_content_mismatch",
                f"{url} declared {declared or 'no type'!r} but its bytes are {sniffed}",
                hint=mismatch_hint,
            )
        if declared in _GENERIC_TYPES:
            return sniffed
    if declared in _GENERIC_TYPES:
        if media_type_allowed(sniffed, limits.allowed_media_types):
            return sniffed
        raise MediaDownloadError(
            "media_download_content_type_not_allowed",
            f"{url} sent no specific Content-Type and its bytes are not a recognised media file",
            hint="pass the URL of the media file itself",
        )
    return declared


def _too_large(url: str, size: int | None, limits: MediaDownloadLimits) -> MediaDownloadError:
    measured = f"{size} bytes" if size is not None else "more bytes"
    return MediaDownloadError(
        "media_download_too_large",
        f"{url} is {measured}, over the {limits.max_bytes}-byte download limit",
        hint="use a smaller rendition of the file, or raise tools.media_download.max_bytes",
        details={"max_bytes": limits.max_bytes},
    )


def _user_agent() -> str:
    from clio_agent import __version__  # noqa: PLC0415

    return f"clio-agent/{__version__} (+https://github.com/iowarp/clio-agent) media-download"


def _read_body(response: httpx.Response, url: str, limits: MediaDownloadLimits) -> bytes:
    declared_length = response.headers.get("content-length", "")
    if declared_length.isdigit() and int(declared_length) > limits.max_bytes:
        raise _too_large(url, int(declared_length), limits)
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_bytes():
        total += len(chunk)
        if total > limits.max_bytes:
            raise _too_large(url, None, limits)
        chunks.append(chunk)
    return b"".join(chunks)


def fetch_media(
    url: str,
    limits: MediaDownloadLimits,
    *,
    transport: httpx.BaseTransport | None = None,
    resolve: Resolver | None = None,
) -> FetchedMedia:
    """Download one media file within ``limits`` and verify its type.

    Args:
        url: The external ``http(s)`` URL of the media file.
        limits: Config-resolved bounds (:meth:`MediaDownloadLimits.from_config`).
        transport: An ``httpx`` transport override (tests use a mock transport).
        resolve: A ``(host, port) -> [address]`` resolver override.

    Returns:
        The verified body with its effective media type, final URL and hash.

    Raises:
        MediaDownloadError: Every refusal, typed (see the module docstring).
    """

    resolver = resolve or default_resolve
    current = (url or "").strip()
    timeout = httpx.Timeout(limits.read_timeout_s, connect=limits.connect_timeout_s)
    headers = {"User-Agent": _user_agent(), "Accept": "image/*,video/*,audio/*;q=0.9,*/*;q=0.1"}
    with httpx.Client(
        transport=transport, timeout=timeout, follow_redirects=False, headers=headers
    ) as client:
        for _hop in range(MAX_REDIRECTS + 1):
            host = validate_media_url(current, limits, resolve=resolver)
            try:
                with client.stream("GET", current) as response:
                    if response.is_redirect and response.headers.get("location"):
                        current = urljoin(current, response.headers["location"])
                        continue
                    if not 200 <= response.status_code < 300:
                        raise MediaDownloadError(
                            "media_download_http_error",
                            f"{current} answered HTTP {response.status_code}",
                            hint="check that the URL is current and publicly reachable",
                            details={"status": response.status_code},
                        )
                    declared = response.headers.get("content-type", "").split(";", 1)[0]
                    declared = declared.strip().lower()
                    _check_declared_type(declared, current, limits)
                    body = _read_body(response, current, limits)
            except httpx.HTTPError as exc:
                raise MediaDownloadError(
                    "media_download_network_error",
                    f"fetching {current} failed: {type(exc).__name__}: {exc}",
                    hint="the host could not be reached or stalled; retry later or use "
                    "another source",
                ) from exc
            media_type = _verified_type(declared, body, current, limits)
            return FetchedMedia(
                body=body,
                media_type=media_type,
                final_url=current,
                host=host,
                sha256=hashlib.sha256(body).hexdigest(),
            )
    raise MediaDownloadError(
        "media_download_too_many_redirects",
        f"{url} redirected more than {MAX_REDIRECTS} times",
        hint="pass the final URL of the media file directly",
    )


__all__ = [
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MEDIA_TYPES",
    "FetchedMedia",
    "MediaDownloadError",
    "MediaDownloadLimits",
    "default_resolve",
    "fetch_media",
    "media_type_allowed",
    "validate_media_url",
]
