"""The ``X-Clio-Locale`` header becomes request ``_meta["io.iowarp/locale"]``."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from clio_agent.tools import request_locale


@pytest.mark.parametrize("tag", ["en", "es", "pt-BR", "zh-Hant-TW", "  fr  "])
def test_a_bcp47_like_tag_is_accepted(tag: str) -> None:
    assert request_locale.normalize(tag) == tag.strip()


@pytest.mark.parametrize("tag", ["", "x", "en_US", "en-", "-en", "e n", "en-" + "a" * 40, None, 7])
def test_a_malformed_tag_is_refused(tag: object) -> None:
    assert request_locale.normalize(tag) is None


def test_meta_carries_the_held_locale_only_inside_its_scope() -> None:
    assert request_locale.locale_meta() is None
    assert request_locale.with_locale({"a": 1}) == {"a": 1}
    assert request_locale.with_locale(None) is None
    with request_locale.scope("es"):
        assert request_locale.current() == "es"
        assert request_locale.locale_meta() == {"io.iowarp/locale": "es"}
        assert request_locale.with_locale({"a": 1}) == {"a": 1, "io.iowarp/locale": "es"}
        assert request_locale.with_locale(None) == {"io.iowarp/locale": "es"}
    assert request_locale.current() is None


def _run(headers: list[tuple[bytes, bytes]], kind: str = "http") -> list[str | None]:
    seen: list[str | None] = []

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        seen.append(request_locale.current())

    middleware = request_locale.LocaleMiddleware(inner)
    asyncio.run(middleware({"type": kind, "headers": headers}, None, None))
    return seen


def test_the_middleware_holds_the_header_while_the_request_runs() -> None:
    assert _run([(b"x-clio-locale", b"pt-BR")]) == ["pt-BR"]
    assert _run([(b"X-Clio-Locale", b"es")]) == ["es"]
    assert _run([(b"x-clio-locale", b"not a tag")]) == [None]
    assert _run([]) == [None]
    assert _run([(b"x-clio-locale", b"es")], kind="websocket") == [None]
    assert request_locale.current() is None
