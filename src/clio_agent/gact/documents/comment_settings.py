"""The ``comments.*`` configuration keys (comments plan, defaults table; addendum A)."""

from __future__ import annotations

from clio_agent import conf

__all__ = ["context_chars", "html_excerpt_chars", "max_image_side", "under_mark_chars"]


def _bounded(key: str, env: str, default: int, low: int, high: int) -> int:
    value = conf.resolve(key, env=env, default=default, cast=conf.as_int)
    if not low <= value <= high:
        raise ValueError(f"{key} must be between {low} and {high}, not {value}")
    return value


def under_mark_chars() -> int:
    """Characters of what lies under one mark that its entry carries; the rest goes to a
    session file the entry names (addendum A, section 2)."""
    return _bounded(
        "comments.under_mark_chars", "CLIO_COMMENTS_UNDER_MARK_CHARS", 1_200, 100, 20_000
    )


def html_excerpt_chars() -> int:
    """Characters of a picked element's ``outerHTML`` kept on its anchor."""
    return _bounded(
        "comments.html_excerpt_chars", "CLIO_COMMENTS_HTML_EXCERPT_CHARS", 400, 50, 8_000
    )


def context_chars() -> int:
    """Characters of text kept before and after a text selection."""
    return _bounded("comments.context_chars", "CLIO_COMMENTS_CONTEXT_CHARS", 64, 0, 1_000)


def max_image_side() -> int:
    """Longest side, in pixels, of an overview or crop sent to the agent."""
    return _bounded("comments.max_image_side", "CLIO_COMMENTS_MAX_IMAGE_SIDE", 2_048, 256, 8_192)
