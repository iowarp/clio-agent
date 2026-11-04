"""The ``browser.*`` configuration keys and the browser's typed errors."""

from __future__ import annotations

from typing import Any

from clio_agent import conf

__all__ = [
    "BACKENDS",
    "BrowserError",
    "action_timeout_s",
    "allow_unsandboxed",
    "backend",
    "cdp_url",
    "frame_quality",
    "frame_width",
    "granted_hosts",
    "idle_minutes",
    "max_pages",
    "person_window_s",
    "profile",
]

BACKENDS = ("managed", "cdp")


class BrowserError(RuntimeError):
    """A browser call could not do what it was asked; ``reason`` is typed.

    The message starts with the reason, so a script's ``ToolError`` carries it too.
    """

    def __init__(self, reason: str, message: str, **detail: Any) -> None:
        super().__init__(f"{reason}: {message}")
        self.reason = reason
        self.message = message
        self.detail = detail


def backend() -> str:
    """``managed`` (CLIO launches Chromium) or ``cdp`` (a Chrome the person runs)."""
    value = conf.resolve("browser.backend", env="CLIO_BROWSER_BACKEND", default="managed")
    value = str(value or "managed").strip().lower()
    if value not in BACKENDS:
        raise ValueError(f"browser.backend must be one of {', '.join(BACKENDS)}, not {value!r}")
    return value


def profile() -> str:
    """The profile name; one profile is one Chromium shared by every session."""
    value = str(conf.resolve("browser.profile", env="CLIO_BROWSER_PROFILE", default="default"))
    name = value.strip() or "default"
    if not name.replace("-", "").replace("_", "").isalnum():
        raise ValueError(f"browser.profile must be letters, digits, - or _, not {name!r}")
    return name


def cdp_url() -> str:
    """The DevTools endpoint of the person's Chrome for the ``cdp`` backend."""
    return str(conf.resolve("browser.cdp_url", env="CLIO_BROWSER_CDP_URL", default="") or "")


def action_timeout_s() -> float:
    """Seconds one browser action may take, time waiting for the person excluded."""
    value = conf.resolve(
        "browser.action_timeout_s",
        env="CLIO_BROWSER_ACTION_TIMEOUT_S",
        default=30.0,
        cast=conf.as_float,
    )
    if not value > 0:
        raise ValueError(f"browser.action_timeout_s must be positive, not {value}")
    return value


def max_pages() -> int:
    """Pages open at once across the server."""
    value = conf.resolve(
        "browser.max_pages", env="CLIO_BROWSER_MAX_PAGES", default=12, cast=conf.as_int
    )
    if value < 1:
        raise ValueError(f"browser.max_pages must be at least 1, not {value}")
    return value


def idle_minutes() -> float:
    """Minutes a profile's browser stays open with no pages before it closes."""
    return conf.resolve(
        "browser.idle_minutes", env="CLIO_BROWSER_IDLE_MINUTES", default=30.0, cast=conf.as_float
    )


def allow_unsandboxed() -> bool:
    """Whether Chromium may run with its sandbox off when the sandbox cannot start."""
    return conf.resolve(
        "browser.allow_unsandboxed",
        env="CLIO_BROWSER_ALLOW_UNSANDBOXED",
        default=False,
        cast=conf.as_bool,
    )


def granted_hosts() -> frozenset[str]:
    """Host patterns the deployment grants to every session (``*.example.org`` works)."""
    raw = conf.resolve(
        "browser.granted_hosts", env="CLIO_BROWSER_GRANTED_HOSTS", default=(), cast=conf.as_csv
    )
    return frozenset(h.strip().lower() for h in raw if h.strip())


def person_window_s() -> float:
    """Seconds after the person's input in which a navigation counts as theirs."""
    return conf.resolve(
        "browser.person_window_s",
        env="CLIO_BROWSER_PERSON_WINDOW_S",
        default=2.0,
        cast=conf.as_float,
    )


def frame_quality() -> int:
    """JPEG quality (1-100) of the live view's frames."""
    value = conf.resolve(
        "browser.frame_quality", env="CLIO_BROWSER_FRAME_QUALITY", default=70, cast=conf.as_int
    )
    if not 1 <= value <= 100:
        raise ValueError(f"browser.frame_quality must be 1 to 100, not {value}")
    return value


def frame_width() -> int:
    """Width in pixels of the live view's frames; the height keeps the page's aspect."""
    value = conf.resolve(
        "browser.frame_width", env="CLIO_BROWSER_FRAME_WIDTH", default=1280, cast=conf.as_int
    )
    if value < 320:
        raise ValueError(f"browser.frame_width must be at least 320, not {value}")
    return value
