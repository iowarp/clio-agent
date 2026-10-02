"""Confined persistence helpers for embedded document-editor callbacks."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, build_opener
from urllib.request import Request as UrlRequest

_MAX_EDITOR_SAVE_BYTES = 512 * 1024 * 1024


def write_working_copy(path: Path, payload: bytes) -> None:
    """Atomically replace a working copy after enforcing its size limit."""

    if len(payload) > _MAX_EDITOR_SAVE_BYTES:
        raise ValueError("editor save exceeds the configured size limit")
    # A short sibling name: a long one (name + uuid) pushed deep workspace paths past
    # the Windows 260-character limit. Same directory, so the replace stays atomic.
    handle, name = tempfile.mkstemp(dir=path.parent, prefix=".", suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(payload)
        os.replace(name, path)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise


def exact_http_origin(left: str, right: str) -> bool:
    """Return whether two HTTP URLs have the same normalized exact origin."""

    try:
        source = urlparse(left)
        allowed = urlparse(right)
        source_port = source.port or (443 if source.scheme == "https" else 80)
        allowed_port = allowed.port or (443 if allowed.scheme == "https" else 80)
    except ValueError:
        return False
    return (
        source.scheme in {"http", "https"}
        and source.scheme == allowed.scheme
        and source.hostname is not None
        and source.hostname.lower() == (allowed.hostname or "").lower()
        and source_port == allowed_port
        and source.username is None
        and source.password is None
    )


class _RejectEditorRedirects(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: UrlRequest,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        raise ValueError("editor callback redirects are not allowed")


def download_editor_save(url: str, provider_base: str) -> bytes:
    """Download one editor save from its configured exact origin without redirects."""

    if not exact_http_origin(url, provider_base):
        raise ValueError("editor callback download URL is outside the configured origin")
    request = UrlRequest(url, headers={"User-Agent": "clio-agent"})
    opener = build_opener(_RejectEditorRedirects())
    with opener.open(request, timeout=30.0) as response:  # noqa: S310 - exact origin checked
        length = int(response.headers.get("Content-Length", "0") or "0")
        if length > _MAX_EDITOR_SAVE_BYTES:
            raise ValueError("editor save exceeds the configured size limit")
        payload = response.read(_MAX_EDITOR_SAVE_BYTES + 1)
    if len(payload) > _MAX_EDITOR_SAVE_BYTES:
        raise ValueError("editor save exceeds the configured size limit")
    return payload


__all__ = ["download_editor_save", "exact_http_origin", "write_working_copy"]
