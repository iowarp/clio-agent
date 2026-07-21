"""Defer litellm's eager ``cl100k_base`` load (~40 MB RSS) until first real use.

Why this exists
---------------
``litellm==1.91.3`` (pinned in #961 for Windows-wheel stability) imports its
``litellm.compression`` submodule at ``import litellm`` time, which chains into
``litellm.litellm_core_utils.default_encoding`` whose *module body* eagerly calls
``tiktoken.get_encoding("cl100k_base")``. That materialises tiktoken's native
CoreBPE rank map — ~40 MB of resident RSS that is almost entirely on the Rust
side and therefore invisible to ``tracemalloc``/gc heap tooling. Because the
GACT server imports ``litellm`` lazily (on the first LM turn, via
``lm.factory.create_lm``), the cost shows up as a ~40 MB *post-idle* growth of
``server-main`` that never returns — exactly the #930 memory-gate regression
(``final`` 0.72 → 0.78; the budget was recorded against ``litellm==1.78.5``,
which did not pay this cost).

The subscription-CLI providers the gate exercises (``claude_code``/``codex``)
never actually encode with ``cl100k_base``: generation runs through the provider
SDK which reports its own token usage, and clio's own context-budget estimate
counts the provider-prefixed model (which litellm tokenises without cl100k) or
falls back to a ~4-chars/token heuristic. So that 40 MB is pure resident waste
for those providers.

What this does
--------------
Installs a lazy proxy for ``cl100k_base`` so litellm's eager import-time
``get_encoding("cl100k_base")`` returns immediately and the real rank map is
built ONLY when something first calls a method on it (``.encode``/``.decode``).
OpenAI models still get exact, correct counts — the encoding just materialises
on first use instead of at import. This is a pure lazy-loading optimisation with
no change to any tokenisation result.

Contract
--------
* Idempotent (safe to call from several boot paths).
* MUST run before the first ``import litellm`` in the process to take effect.
  ``lm.factory.create_lm`` calls it as its very first step — before the provider
  registration that performs that import — and ``create_lm`` is the sole path by
  which litellm enters the GACT server (nothing imports it at app import or during
  agent construction beforehand; verified by the #930 memory gate).
* Best-effort: if tiktoken is unavailable or already patched, it no-ops and
  returns a typed status the caller logs — never raises.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_installed = False


class _LazyEncoding:
    """A stand-in for a tiktoken ``Encoding`` that builds the real one on first use.

    Only attribute/method access other than the cheap ``name`` triggers the real
    (~40 MB) load, so holding a reference costs nothing until a caller actually
    encodes/decodes. litellm only ever calls ``.encode(...)`` on the default
    encoding (no ``isinstance`` checks), so transparent ``__getattr__`` delegation
    is sufficient.
    """

    __slots__ = ("_name", "_real", "_real_get")

    def __init__(self, name: str, real_get: Any) -> None:
        self._name = name
        self._real: Any = None
        self._real_get = real_get

    def _load(self) -> Any:
        if self._real is None:
            self._real = self._real_get(self._name)
        return self._real

    @property
    def name(self) -> str:
        return self._name

    def __getattr__(self, item: str) -> Any:
        # __getattr__ only fires for names not found normally; the slots above are
        # always set in __init__, so there is no materialise-on-init recursion.
        return getattr(self._load(), item)


def install_lazy_cl100k() -> bool:
    """Patch ``tiktoken.get_encoding`` so ``cl100k_base`` loads lazily.

    Returns ``True`` when the lazy proxy is in place (installed now or already),
    ``False`` if tiktoken could not be imported. Never raises.
    """

    global _installed
    if _installed:
        return True
    try:
        import tiktoken
        from tiktoken import registry as _registry
    except Exception:  # noqa: BLE001 - tiktoken optional; nothing to defer without it
        return False

    real_get = tiktoken.get_encoding
    if getattr(real_get, "_clio_lazy", False):
        _installed = True
        return True

    def lazy_get(name: str, *args: Any, **kwargs: Any) -> Any:
        # Defer ONLY the plain ``cl100k_base`` lookup (litellm's eager import-time
        # call). Any explicit-args call or other encoding passes straight through.
        if name == "cl100k_base" and not args and not kwargs:
            return _LazyEncoding(name, real_get)
        return real_get(name, *args, **kwargs)

    lazy_get._clio_lazy = True  # type: ignore[attr-defined]
    tiktoken.get_encoding = lazy_get  # type: ignore[assignment]
    # Some callers reach the registry binding directly; keep them consistent.
    try:
        _registry.get_encoding = lazy_get  # type: ignore[assignment]
    except Exception:  # noqa: BLE001,S110 - registry shape is best-effort
        pass

    _installed = True
    logger.info(
        "installed lazy cl100k_base tiktoken proxy "
        "reason=defer_litellm_default_encoding (~40MB deferred until first encode)"
    )
    return True


_cost_recount_disabled = False


def disable_litellm_cost_recount() -> bool:
    """Stop litellm's response-cost calc from materialising the ~40 MB cl100k vocab.

    litellm computes a ``response_cost`` after every completion. For clio's CLI
    providers (``claude_code``/``codex``) the streamed response is not a
    usage-bearing object at cost time, so litellm's ``completion_cost`` falls into
    its RE-COUNT branch and tokenises the text with its default ``gpt-3.5-turbo``
    tokenizer — i.e. ``cl100k_base`` — materialising the full ~40 MB rank map on
    the first turn (fires ~60×/session; #930). clio never consumes litellm's
    ``response_cost`` (its own usage/cost come from the provider SDK via
    ``emit_call_usage``), so this recount is pure resident waste.

    We wrap ``litellm.response_cost_calculator`` to return ``None`` (litellm's own
    on-error value, already tolerated everywhere it is read) so the recount — and
    thus the cl100k load — never runs. clio's *own* token counting
    (``context_tokens`` → ``litellm.token_counter`` for OpenAI models) is left
    untouched, so auto-compaction accuracy for tiktoken-native models is preserved.

    Must run AFTER litellm is imported (unlike the tiktoken proxy above). Idempotent;
    never raises. Returns ``True`` when the wrapper is in place.
    """

    global _cost_recount_disabled
    if _cost_recount_disabled:
        return True
    try:
        import litellm
    except Exception:  # noqa: BLE001 - litellm optional; nothing to wrap without it
        return False
    if getattr(getattr(litellm, "response_cost_calculator", None), "_clio_no_recount", False):
        _cost_recount_disabled = True
        return True

    def _no_recount(*_args: Any, **_kwargs: Any) -> None:
        return None

    _no_recount._clio_no_recount = True  # type: ignore[attr-defined]
    try:
        litellm.response_cost_calculator = _no_recount  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - litellm shape is best-effort
        return False

    _cost_recount_disabled = True
    logger.info(
        "disabled litellm response-cost recount "
        "reason=defer_litellm_cost_tokenizer (clio uses emit_call_usage; keeps ~40MB cl100k out)"
    )
    return True
