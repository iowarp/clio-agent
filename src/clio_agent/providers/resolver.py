"""Pure spec → :class:`LMProviderConfig` resolution with cached handshake (design §3.3).

This is the owner module for turning a serializable :class:`~clio_agent.providers.
lm_spec.LMSpec` into a concrete, ready-to-run
:class:`~clio_agent.config.LMProviderConfig` **without** touching process-global
state. It closes the ``builders.py`` ``same_provider`` gap: a cross-provider
expert gets its own endpoint, its own handshake-discovered ``context_window`` /
context-aware ``max_tokens`` / reasoning + tool flags, and — at ``forward()`` —
its own freshly resolved credential.

The resolution is deliberately split in two, per Candidate 3's *credential
freshness* refinement (design §3.3):

* :func:`resolve_endpoint_and_handshake` — the **cheap, cache-backed** half:
  fill ``PROVIDER_DEFAULTS`` (endpoint / model / caps) exactly as
  :meth:`LMProviderConfig.__post_init__` does today but **without reading
  ``os.environ`` for the credential**, then fold a per-``(provider, model,
  api_base)`` handshake through the existing TTL cache
  (:func:`clio_agent.providers.handshake.run_handshake_sync`, which routes
  through :func:`handshake.cache.cached_or_run`). Safe to compute once at module
  ``__init__``. Returns a :class:`ResolvedLMSpec` holding a **key-less** config
  skeleton. On a failed / absent / model-unresolved handshake it falls back to
  the static ``PROVIDER_DEFAULTS`` caps **with a structured reason** attached to
  the result (no silent degradation — design's no-silent-fallback ground rule).

* :meth:`ResolvedLMSpec.materialize` — the **per-call** half: resolve the
  ``api_key`` *fresh* through a :class:`~clio_agent.providers.credentials.
  CredentialResolver` (tokens rotate mid-session) and return the fully-populated
  config. This is what the ``dspy.context(lm=create_lm(cfg))`` boundary consumes.

Both halves are pure and idempotent: no ``os.environ`` writes, no
``dspy.configure``, no shared-LM mutation. The only cross-call shared state
touched is the read-mostly, idempotent handshake TTL cache.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from clio_agent.providers.credentials import CredentialResolver
from clio_agent.providers.handshake import cache as _handshake_cache
from clio_agent.providers.handshake import run_handshake_sync

if TYPE_CHECKING:  # pragma: no cover - typing only
    from clio_agent.config import LMProviderConfig
    from clio_agent.providers.handshake.model import HandshakeReport, ModelProfile
    from clio_agent.providers.lm_spec import LMSpec

__all__ = [
    "ResolvedLMSpec",
    "resolve_endpoint_and_handshake",
    "HANDSHAKE_FALLBACK_REASONS",
    "handshake_fallback_payload",
]

# Sentinel written into the skeleton's ``api_key`` at construction time so
# ``LMProviderConfig.__post_init__`` sees a non-empty key and SKIPS its
# credential resolution (which would read ``os.environ`` / mint a token). The
# skeleton is meant to be key-less; :meth:`ResolvedLMSpec.materialize` supplies
# the real credential fresh per call. Non-empty and unmistakable.
_CRED_DEFERRED_SENTINEL = "\x00clio-cred-deferred\x00"


# Audited catalog of reasons an expert's LM config fell back to the static
# ``PROVIDER_DEFAULTS`` caps instead of handshake-discovered ones. Same shape /
# discipline as ``gact.runtime.capabilities._STREAM_FALLBACK_REASON_DEFINITIONS``:
# a typed reason with a category, recovery actions, and a human description, so a
# degradation is queryable structured data rather than an invisible downgrade.
HANDSHAKE_FALLBACK_REASONS: dict[str, dict[str, Any]] = {
    "handshake_unreachable": {
        "category": "provider_handshake_failure",
        "degraded": True,
        "recovery_actions": ["retry", "reconfigure", "continue_with_static_caps"],
        "description": (
            "The provider handshake could not connect or authenticate, so the "
            "expert LM config uses the static PROVIDER_DEFAULTS caps "
            "(context_window unknown, provider-default max_tokens)."
        ),
    },
    "handshake_model_unresolved": {
        "category": "provider_handshake_limitation",
        "degraded": True,
        "recovery_actions": ["reconfigure", "continue_with_static_caps"],
        "description": (
            "The handshake connected but reported no matching model profile for "
            "the requested model, so the expert LM config uses the static "
            "PROVIDER_DEFAULTS caps instead of discovered ones."
        ),
    },
    "handshake_error": {
        "category": "provider_handshake_error",
        "degraded": True,
        "recovery_actions": ["retry", "reconfigure", "continue_with_static_caps"],
        "description": (
            "The provider handshake raised unexpectedly; the expert LM config "
            "falls back to the static PROVIDER_DEFAULTS caps."
        ),
    },
}


def handshake_fallback_payload(reason: str, message: str = "") -> dict[str, Any]:
    """Build a structured handshake-fallback reason payload (catalog-style).

    Mirrors :func:`clio_agent.gact.streaming._stream_fallback_payload`: looks the
    ``reason`` up in :data:`HANDSHAKE_FALLBACK_REASONS`, copies its audited
    metadata, and appends an optional free-text ``message``. Raises ``ValueError``
    on an unknown reason so a typo cannot silently produce an empty reason.

    Args:
        reason: A key of :data:`HANDSHAKE_FALLBACK_REASONS`.
        message: Optional context (e.g. the provider/model and the handshake error).

    Returns:
        A JSON-serializable reason payload.
    """
    definition = HANDSHAKE_FALLBACK_REASONS.get(reason)
    if definition is None:
        raise ValueError(f"Unknown handshake fallback reason: {reason}")
    payload: dict[str, Any] = {
        "reason": reason,
        **{
            key: list(value) if isinstance(value, list) else value
            for key, value in definition.items()
        },
    }
    if message:
        payload["message"] = message
    return payload


def _matched_profile(report: "HandshakeReport", model: str) -> "ModelProfile | None":
    """Return the profile a handshake ``report`` would fold for ``model``.

    Mirrors the matching order in :meth:`LMProviderConfig.apply_handshake` (exact
    id → vendor-prefix basename → the sole model when only one is served) so the
    resolver can decide whether a real fold happened and, if not, record a
    structured fallback reason.
    """
    models = getattr(report, "models", None) or ()
    if not models:
        return None
    profile = report.model(model) if hasattr(report, "model") else None
    if profile is not None:
        return profile
    want = model.rsplit("/", 1)[-1].lower()
    profile = next((m for m in models if m.id.rsplit("/", 1)[-1].lower() == want), None)
    if profile is not None:
        return profile
    if len(models) == 1:
        return models[0]
    return None


@dataclass(frozen=True)
class ResolvedLMSpec:
    """A spec resolved to an endpoint + handshake-populated, key-less config skeleton.

    Produced by :func:`resolve_endpoint_and_handshake` (the cache-backed half) and
    consumed by :meth:`materialize` (the per-call credential half). Holds no
    secret: ``config_skeleton.api_key`` is empty; the real credential is resolved
    fresh at :meth:`materialize` time so a rotated token is always current.

    Attributes:
        spec: The originating :class:`LMSpec`.
        config_skeleton: A :class:`LMProviderConfig` with endpoint/model/caps
            filled and the handshake folded, but **no** ``api_key`` (empty). Never
            mutate it in place — :meth:`materialize` copies it.
        handshake_fallback: A structured reason payload when the handshake did not
            fold (unreachable / model-unresolved / error), else ``None``. Its
            presence means the skeleton carries static ``PROVIDER_DEFAULTS`` caps.
        placeholder_key: The provider's default ``api_key`` placeholder (e.g.
            ``"lm-studio"`` for a local provider, ``""`` for a cloud provider),
            used by :meth:`materialize` only for the default credential ref so the
            single-default-LM baseline stays byte-identical.
    """

    spec: "LMSpec"
    config_skeleton: "LMProviderConfig"
    handshake_fallback: dict[str, Any] | None = None
    placeholder_key: str = ""
    _default_ref: bool = field(default=True, repr=False)

    def materialize(self, cred_resolver: CredentialResolver | None = None) -> "LMProviderConfig":
        """Resolve the credential fresh and return the fully-populated config.

        The ``api_key`` is resolved *here* (not at
        :func:`resolve_endpoint_and_handshake` time) because tokens rotate
        mid-session — this is the per-call half of the split. A shallow copy of
        the key-less skeleton is made so the cached skeleton stays clean and two
        concurrent ``materialize`` calls never share the returned object.

        Credential precedence:

        * A non-empty resolved key wins.
        * For the **default** credential ref (empty / ``"<provider>:default"``) an
          empty resolution falls back to ``placeholder_key`` — this is the
          local-provider placeholder (e.g. ``"lm-studio"``), a provider default
          rather than a credential, so the single-default-LM baseline is
          preserved byte-for-byte.
        * For an **explicit / named** ref, an empty resolution stays empty: the
          downstream LM call surfaces an actionable auth error rather than
          silently falling back to a different account's credential.

        Args:
            cred_resolver: The read-only credential resolver. Defaults to a fresh
                :class:`CredentialResolver` (the process-env-backed default).

        Returns:
            A fully-populated :class:`LMProviderConfig` carrying the freshly
            resolved credential; its ``api_key`` lives only for this config's
            lifetime.
        """
        resolver = cred_resolver if cred_resolver is not None else CredentialResolver()
        key = resolver.resolve(self.spec.provider, self.spec.credential_ref)
        config = copy.copy(self.config_skeleton)
        if key:
            config.api_key = key
        elif self._default_ref:
            config.api_key = self.placeholder_key
        else:
            config.api_key = ""
        return config


def _build_key_less_skeleton(spec: "LMSpec") -> tuple["LMProviderConfig", str]:
    """Construct a ``PROVIDER_DEFAULTS``-filled config with **no** credential.

    Passes the deferred-credential sentinel so ``__post_init__`` skips its
    ``os.environ`` / token read (the credential is resolved fresh in
    :meth:`ResolvedLMSpec.materialize`), then blanks it. Returns the skeleton plus
    the provider's default ``api_key`` placeholder for the default-ref path.
    """
    from clio_agent.config import PROVIDER_DEFAULTS, LMProviderConfig  # noqa: PLC0415

    provider = spec.provider
    kwargs: dict[str, Any] = {
        "provider": provider,
        "api_base": spec.api_base,
        "model": spec.model,
        "api_key": _CRED_DEFERRED_SENTINEL,
        "temperature": spec.temperature if spec.temperature is not None else 0.0,
        "max_tokens": spec.max_tokens or 0,
        "thinking_budget": spec.thinking_budget or 0,
        "top_p": spec.top_p,
        "top_k": spec.top_k,
        "min_p": spec.min_p,
        "presence_penalty": spec.presence_penalty,
    }
    if provider == "codex" and spec.transport:
        kwargs["codex_transport"] = spec.transport
    if provider == "claude_code" and spec.transport:
        kwargs["claude_code_transport"] = spec.transport
    config = LMProviderConfig(**kwargs)  # type: ignore[arg-type]
    # Drop the sentinel: the skeleton is key-less by construction.
    config.api_key = ""
    defaults = PROVIDER_DEFAULTS.get(provider, PROVIDER_DEFAULTS["lm_studio"])
    placeholder = str(defaults.get("api_key", "") or "")
    return config, placeholder


def _fold_handshake(
    config: "LMProviderConfig",
    spec: "LMSpec",
    *,
    ttl_s: float,
    force: bool,
) -> dict[str, Any] | None:
    """Fold a cached handshake into ``config`` in place; return a fallback reason or None.

    Runs the per-``(provider, model, api_base)`` handshake through the existing
    TTL cache (``run_handshake_sync`` → ``handshake.cache.cached_or_run``) with a
    passive, key-less context (the credential is not yet resolved on this half).
    On a usable report the discovered fields (``context_window``, context-aware
    ``max_tokens``, reasoning + tool flags) are folded via
    :meth:`LMProviderConfig.apply_handshake`; otherwise the static
    ``PROVIDER_DEFAULTS`` caps already on ``config`` are kept and a structured
    reason is returned.
    """
    from clio_agent.providers.handshake import HandshakeContext  # noqa: PLC0415

    detail = (
        f"provider={spec.provider} model={spec.model} api_base={config.api_base or '<default>'}"
    )
    try:
        report = run_handshake_sync(
            HandshakeContext(
                provider_id=spec.provider,
                provider_kind=spec.provider,
                api_base=config.api_base,
                api_key="",
                target_model=spec.model,
                # Passive + no runtime mutation: this half runs without a
                # credential and must not trigger an interactive flow or an LM
                # Studio reload from a pure resolver.
                auth_mode="passive",
                mutate_runtime=False,
            ),
            ttl_s=ttl_s,
            force=force,
        )
    except Exception as exc:  # run_handshake_sync should never raise, but be safe.
        return handshake_fallback_payload("handshake_error", f"{detail}: {exc}")

    if report is None or not getattr(report, "ok", False):
        err = getattr(report, "error", None) if report is not None else "no report"
        return handshake_fallback_payload("handshake_unreachable", f"{detail}: {err}")

    if _matched_profile(report, spec.model) is None:
        return handshake_fallback_payload("handshake_model_unresolved", detail)

    config.apply_handshake(report, user_set_max_tokens=(spec.max_tokens or 0) > 0)
    return None


def resolve_endpoint_and_handshake(
    spec: "LMSpec",
    *,
    ttl_s: float = _handshake_cache.DEFAULT_TTL_S,
    force: bool = False,
) -> ResolvedLMSpec:
    """Resolve a spec's endpoint + handshake into a key-less config skeleton (design §3.3).

    The cache-backed half of the split. Fills ``PROVIDER_DEFAULTS`` exactly as
    :meth:`LMProviderConfig.__post_init__` does today but without reading
    ``os.environ`` for the credential, then folds a per-``(provider, model,
    api_base)`` handshake via the existing TTL cache. A failed / absent /
    model-unresolved handshake keeps the static caps and attaches a structured
    :data:`HANDSHAKE_FALLBACK_REASONS` payload (no silent degradation).

    Pure and idempotent: no ``os.environ`` writes, no ``dspy`` mutation. The only
    shared state touched is the read-mostly, idempotent handshake TTL cache, so
    two calls with the same spec return equivalent results.

    Args:
        spec: The fully-named :class:`LMSpec` to resolve.
        ttl_s: Handshake cache freshness window (seconds).
        force: Bypass the handshake cache and re-probe.

    Returns:
        A :class:`ResolvedLMSpec` holding the key-less, handshake-populated config
        skeleton, plus any structured handshake-fallback reason. Call
        :meth:`ResolvedLMSpec.materialize` to resolve the credential and get the
        runnable config.
    """
    from clio_agent.providers.credentials import _account_of  # noqa: PLC0415

    skeleton, placeholder = _build_key_less_skeleton(spec)
    fallback = _fold_handshake(skeleton, spec, ttl_s=ttl_s, force=force)
    default_ref = _account_of(spec.credential_ref) == ""
    return ResolvedLMSpec(
        spec=spec,
        config_skeleton=skeleton,
        handshake_fallback=fallback,
        placeholder_key=placeholder,
        _default_ref=default_ref,
    )
