"""One ``Content-Disposition`` header builder, safe for any filename.

HTTP header values are latin-1 only. A filename with a non-latin-1 character
(e.g. ``"数据.csv"``) raised deep inside Starlette's header encoding -- a 500
with no typed reason -- and a literal double quote in a filename broke the
quoted-string syntax outright, in every route that built this header with a
raw f-string (#1551 review item 4). Every such route uses
:func:`content_disposition` instead.
"""

from __future__ import annotations

from urllib.parse import quote


def _ascii_fallback(filename: str) -> str:
    """``filename``, with anything outside printable ASCII (and any quote or
    backslash, which would otherwise break the quoted-string) replaced.

    Only the RFC 5987 ``filename*`` form need carry the name losslessly; this
    is purely the RFC 6266 fallback for a client that does not understand it.
    """

    safe = "".join(
        char if 0x20 <= ord(char) < 0x7F and char not in '"\\' else "_" for char in filename
    )
    return safe or "download"


def content_disposition(filename: str, *, disposition: str = "attachment") -> str:
    """One ``Content-Disposition`` header value, safe for any filename.

    Always includes both forms: a quoted ASCII-only ``filename`` (RFC 6266,
    for a client that does not understand ``filename*``) and the RFC 5987
    extended ``filename*=UTF-8''...`` form every modern browser prefers and
    which carries the name losslessly, including non-latin-1 characters.
    """

    ascii_fallback = _ascii_fallback(filename)
    encoded = quote(filename, safe="")
    return f'{disposition}; filename="{ascii_fallback}"; filename*=UTF-8\'\'{encoded}'


__all__ = ["content_disposition"]
