"""Structured trace/debug logging for CLIO Agent.

Replaces the ad-hoc ``logging.getLogger("clio_agent").warning("⚑ TAG …")``
instrumentation with two orthogonal axes:

* **severity** — standard ``logging`` levels (the emit/format axis);
* **category** — a small fixed semantic taxonomy gated by one verbosity knob:

  ===========  ================================  ================================
  Category     Meaning                           Tags
  ===========  ================================  ================================
  ``EVENT``    rare anomaly / notable change     SCHEMA-REPAIR, LENIENT-ADAPTER
                                                  RECOVERY, REASONING
  ``ROUTING``  per-hop orchestration decision    RAW-ROUTE, SETTLE-ENTER
  ``HIGH_FREQ``per-call / per-build firehose      PROMPT-BUILD, FWD-*, LM-CALL,
                                                  SIG-BUILD, LM-RESPONSE
  ===========  ================================  ================================

Verbosity (``CLIO_DEBUG`` / ``debug.level``, default ``low``) enables categories
cumulatively: ``off`` → none, ``low`` → EVENT, ``med`` → +ROUTING,
``high`` → +HIGH_FREQ. The surgical ``CLIO_DEBUG_ONLY`` / ``debug.only`` tag
whitelist overrides the level — only listed tags emit, at any level.

**Disable cost.** Python cannot compile-eliminate a disabled call like a C++
macro, so the realistic floor is a module-level boolean short-circuit. HIGH_FREQ
call sites guard with the exported :data:`HF_ON` flag::

    trace.HF_ON and trace.hot("LM-CALL", "sp_len=%d %r", n, payload)

When ``HF_ON`` is ``False`` this is one attribute load + a branch (~10-30 ns) and
**no argument evaluation** — the right operand (including building the args) is
never reached. EVENT/ROUTING fire rarely, so their helpers just early-return
internally (Floor 1, ~hundreds of ns). Keep ``%``-style args (never f-strings)
so formatting stays deferred to the handler.
"""

from __future__ import annotations

import logging
from typing import Any

from clio_agent import conf

# Single canonical logger everything emits through (matches every existing site).
_LOG = logging.getLogger("clio_agent")

# Verbosity level → enabled categories (cumulative).
_LEVELS: dict[str, frozenset[str]] = {
    "off": frozenset(),
    "low": frozenset({"event"}),
    "med": frozenset({"event", "routing"}),
    "high": frozenset({"event", "routing", "high_freq"}),
}
_DEFAULT_LEVEL = "low"

# Module-level gate booleans, (re)computed by configure(). Call sites read these
# via attribute access (``trace.HF_ON``) so reconfiguration takes effect.
EVENT_ON: bool = False
ROUTE_ON: bool = False
HF_ON: bool = False

# Active tag whitelist (normalized slugs) or None when no filter is set.
_ONLY: frozenset[str] | None = None

# Marker so the formatted handler is installed at most once.
_HANDLER_MARK = "_clio_trace_handler"


def _normalize_tag(tag: str) -> str:
    """Normalize a tag to a slug (lowercase, spaces/dashes → underscores)."""
    return tag.strip().lower().replace(" ", "_").replace("-", "_")


def _install_handler() -> None:
    """Attach one formatted StreamHandler to the clio_agent logger (idempotent).

    Sets ``propagate=False`` so trace output is not duplicated by a root handler
    (e.g. under uvicorn), and pins the logger to WARNING when unset so the
    instrumentation (emitted at WARNING) is actually shown.
    """
    if any(getattr(h, _HANDLER_MARK, False) for h in _LOG.handlers):
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s | %(message)s"))
    setattr(handler, _HANDLER_MARK, True)
    _LOG.addHandler(handler)
    _LOG.propagate = False
    if _LOG.level == logging.NOTSET:
        _LOG.setLevel(logging.WARNING)


def configure(
    *,
    level: str | None = None,
    only: list[str] | None = None,
    install_handler: bool = True,
) -> None:
    """(Re)resolve verbosity from config and recompute the category gates.

    Args:
        level: explicit verbosity (``off``/``low``/``med``/``high``); when
            ``None`` it is resolved via ``debug.level`` / ``CLIO_DEBUG``.
        only: explicit tag whitelist; when ``None`` it is resolved via
            ``debug.only`` / ``CLIO_DEBUG_ONLY``.
        install_handler: install the formatted StreamHandler (skip in tests that
            capture records via their own handler).
    """
    global EVENT_ON, ROUTE_ON, HF_ON, _ONLY

    resolved_level = (
        level
        if level is not None
        else conf.resolve("debug.level", env="CLIO_DEBUG", default=_DEFAULT_LEVEL, cast=conf.as_str)
    )
    resolved_level = str(resolved_level).strip().lower()
    if resolved_level not in _LEVELS:
        resolved_level = _DEFAULT_LEVEL

    _no_only: list[str] = []
    only_list = (
        only
        if only is not None
        else conf.resolve("debug.only", env="CLIO_DEBUG_ONLY", default=_no_only, cast=conf.as_csv)
    )
    only_slugs = {_normalize_tag(tag) for tag in only_list}

    # Back-compat: the legacy CLIO_LOG_LM_RESPONSE flag (and a debug.lm_response
    # file key) keep working by whitelisting the lm_response tag.
    if conf.resolve(
        "debug.lm_response", env="CLIO_LOG_LM_RESPONSE", default=False, cast=conf.as_bool
    ):
        only_slugs.add("lm_response")

    _ONLY = frozenset(only_slugs) if only_slugs else None

    if _ONLY is not None:
        # A whitelist must let a listed tag of ANY category reach its helper, so
        # the category short-circuits can't pre-filter — open every gate and let
        # the per-tag check in _emit() do the filtering.
        EVENT_ON = ROUTE_ON = HF_ON = True
    else:
        categories = _LEVELS[resolved_level]
        EVENT_ON = "event" in categories
        ROUTE_ON = "routing" in categories
        HF_ON = "high_freq" in categories

    if install_handler:
        _install_handler()


def _emit(tag: str, msg: str, args: tuple[Any, ...]) -> None:
    """Emit a ``⚑ TAG …`` WARNING, honoring the tag whitelist when active."""
    if _ONLY is not None and _normalize_tag(tag) not in _ONLY:
        return
    # Build the small "⚑ TAG " prefix, but pass *args through so the %-format of
    # the (potentially larger) payload stays deferred to the handler — faithful
    # to the original sites' lazy formatting.
    _LOG.warning("⚑ " + tag + " " + msg, *args)


def event(tag: str, msg: str, *args: Any) -> None:
    """Emit an EVENT trace (rare anomaly / notable state change)."""
    if not EVENT_ON:
        return
    _emit(tag, msg, args)


def route(tag: str, msg: str, *args: Any) -> None:
    """Emit a ROUTING trace (per-hop orchestration decision)."""
    if not ROUTE_ON:
        return
    _emit(tag, msg, args)


def hot(tag: str, msg: str, *args: Any) -> None:
    """Emit a HIGH_FREQ trace (per-call/per-build firehose).

    Guard call sites with the :data:`HF_ON` flag for near-zero disabled cost::

        trace.HF_ON and trace.hot("LM-CALL", "%s", payload)
    """
    if not HF_ON:
        return
    _emit(tag, msg, args)


# Resolve the gates at import so instrumentation is correctly gated even if a
# bootstrap path forgets to call configure(); defer the handler to the explicit
# configure() call at server/CLI startup (the logging "last resort" handler
# still surfaces WARNINGs until then).
configure(install_handler=False)
