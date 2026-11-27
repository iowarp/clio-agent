"""The person's interface language, passed to a plugin server in request ``_meta``.

A client names the language it shows CLIO in with the ``X-Clio-Locale`` request header
(a BCP 47 tag such as ``en``, ``es`` or ``pt-BR``). While that request is handled,
:func:`scope` holds the tag, and every ``resources/read`` and ``tools/call`` CLIO makes
to a plugin's MCP server for it (opening or reading a plugin page or tab, and an A2UI
action on a server's surface) carries ``_meta["io.iowarp/locale"] = <tag>``. A server
may word its surfaces in that language; one that ignores the key keeps working.

A missing or malformed header sends no key. Nothing here names a particular server.
"""

from __future__ import annotations

import contextvars
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

#: The request header a client names its interface language in.
LOCALE_HEADER = "X-Clio-Locale"
#: The request ``_meta`` key a plugin server reads the language from.
LOCALE_META_KEY = "io.iowarp/locale"
#: The longest tag accepted; a longer header is ignored.
MAX_LENGTH = 35

_TAG = re.compile(r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*")

_LOCALE: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "clio_request_locale", default=None
)


def normalize(value: Any) -> str | None:
    """``value`` as a locale tag, or ``None`` when it is not a BCP 47-like tag."""

    if not isinstance(value, str):
        return None
    tag = value.strip()
    if not tag or len(tag) > MAX_LENGTH or _TAG.fullmatch(tag) is None:
        return None
    return tag


@contextmanager
def scope(tag: str | None) -> Iterator[None]:
    """Hold ``tag`` (already normalized, or ``None``) for the calls made in this block."""

    token = _LOCALE.set(tag)
    try:
        yield
    finally:
        _LOCALE.reset(token)


def current() -> str | None:
    """The locale of the request being handled, or ``None``."""

    return _LOCALE.get()


def localized(texts: Any, default: str, tag: str | None = None) -> str:
    """``texts[tag]`` for the held (or given) locale, else ``default``.

    ``texts`` maps locale tags to text. An exact tag wins, matched without case, then
    the tag with its last subtags dropped in turn (``pt-BR`` falls back to ``pt``).
    """

    wanted = normalize(tag) if tag is not None else current()
    if not wanted or not isinstance(texts, Mapping) or not texts:
        return default
    by_tag = {str(key).lower(): value for key, value in texts.items()}
    parts = wanted.lower().split("-")
    while parts:
        text = by_tag.get("-".join(parts))
        if isinstance(text, str) and text.strip():
            return text.strip()
        parts.pop()
    return default


def locale_meta() -> dict[str, Any] | None:
    """The ``_meta`` entry naming the locale, or ``None`` when no locale is held."""

    tag = current()
    return {LOCALE_META_KEY: tag} if tag else None


def with_locale(meta: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """``meta`` with the held locale added, or ``meta`` itself when none is held."""

    extra = locale_meta()
    if extra is None:
        return dict(meta) if meta is not None else None
    return {**(meta or {}), **extra}


class LocaleMiddleware:
    """Pure ASGI middleware: hold each HTTP request's ``X-Clio-Locale`` while it runs."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope_: Any, receive: Any, send: Any) -> None:
        if scope_.get("type") != "http":
            await self.app(scope_, receive, send)
            return
        wanted = LOCALE_HEADER.lower().encode("latin-1")
        raw = next(
            (value for name, value in scope_.get("headers") or () if name.lower() == wanted),
            None,
        )
        tag = normalize(raw.decode("latin-1")) if raw is not None else None
        with scope(tag):
            await self.app(scope_, receive, send)


def install(app: Any) -> None:
    """Add :class:`LocaleMiddleware` to ``app``."""

    app.add_middleware(LocaleMiddleware)


__all__ = [
    "LOCALE_HEADER",
    "LOCALE_META_KEY",
    "LocaleMiddleware",
    "current",
    "install",
    "locale_meta",
    "localized",
    "normalize",
    "scope",
    "with_locale",
]
