"""Serializable provider identity for the per-expert LM path (design §3.1).

An :class:`LMSpec` *fully* names an LM as plain, immutable DATA: provider,
model, endpoint, a **credential reference** (a key, never an inline secret),
transport, and the sampling surface. Because it carries no secret and no live
object, a spec is safe to persist into a stored ``AgentDef``, a checked-in
blueprint frontmatter, or a trace, and safe to ship with a work item to another
node — where its ``credential_ref`` is resolved fresh against node-local
sources (design ``docs/archive/per-expert-provider-lm.md`` §3.1/§3.2).

This module is deliberately additive: nothing consumes ``LMSpec`` yet. It
introduces the data type plus two pure helpers:

* :func:`spec_from_config` — project a live :class:`~clio_agent.config.
  LMProviderConfig` down to a secret-free spec (the resolved ``api_key`` is
  intentionally dropped; the spec keeps only a ``credential_ref``).
* :func:`build_spec` — derive an expert's spec from its ``AgentDef``, inheriting
  each field from a default spec whenever the ``AgentDef`` declares none.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from clio_agent.config import LMProviderConfig
    from clio_agent.gact.types import AgentDef

__all__ = ["LMSpec", "spec_from_config", "build_spec"]


@dataclass(frozen=True)
class LMSpec:
    """Immutable, serializable identity of an LM (design §3.1).

    A spec never carries a secret: authentication is expressed as
    ``credential_ref`` (e.g. ``"argonne:default"``, ``"openai:acctB"``), a KEY
    resolved at the executing boundary by
    :class:`clio_agent.providers.credentials.CredentialResolver`. An all-empty
    spec means "inherit the default profile", which is today's behaviour.

    Attributes:
        provider: Runtime provider kind (``"argonne"``, ``"openai"``,
            ``"lm_studio"``, ...).
        model: Model identifier for the provider.
        api_base: Endpoint base URL. Empty defers to the provider default.
        credential_ref: A credential *reference* (never an inline secret).
            Empty selects the provider's default credential.
        transport: Transport selector for providers that have one
            (``codex`` / ``claude_code``: ``"exec"`` or ``"sdk"``). Empty for
            providers with no transport choice.
        temperature: Sampling temperature (``None`` omits → provider default).
        max_tokens: Per-reply output cap (``None`` omits → resolver default).
        thinking_budget: Reasoning/thinking token budget (``None`` omits).
        top_p: Nucleus-sampling cutoff (``None`` omits).
        top_k: Top-k sampling cutoff (``None`` omits).
        min_p: Minimum-probability cutoff (``None`` omits).
        presence_penalty: Presence penalty (``None`` omits).
    """

    provider: str
    model: str
    api_base: str = ""
    credential_ref: str = ""
    transport: str = ""
    temperature: float | None = None
    max_tokens: int | None = None
    thinking_budget: int | None = None
    thinking_level: str | None = None
    top_p: float | None = None
    top_k: int | None = None
    min_p: float | None = None
    presence_penalty: float | None = None


def spec_from_config(cfg: "LMProviderConfig") -> LMSpec:
    """Project a live :class:`LMProviderConfig` down to a secret-free spec.

    The resolved ``api_key`` on ``cfg`` is intentionally **not** copied — a spec
    carries only a ``credential_ref``. The default config's key was resolved
    from the default (empty) ref, so ``credential_ref`` is left ``""`` here; the
    resolver re-derives the key from that ref at the executing boundary.

    Args:
        cfg: A populated provider config (typically the default/boot config).

    Returns:
        An :class:`LMSpec` naming the same provider/model/endpoint/params, with
        no secret material.
    """
    transport: str
    if cfg.provider == "codex":
        transport = cfg.codex_transport
    elif cfg.provider == "claude_code":
        transport = cfg.claude_code_transport
    else:
        transport = ""
    return LMSpec(
        provider=cfg.provider,
        model=cfg.model,
        api_base=cfg.api_base,
        credential_ref="",
        transport=transport,
        temperature=cfg.temperature,
        max_tokens=cfg.max_tokens,
        thinking_budget=cfg.thinking_budget,
        thinking_level=cfg.thinking_level,
        top_p=cfg.top_p,
        top_k=cfg.top_k,
        min_p=cfg.min_p,
        presence_penalty=cfg.presence_penalty,
    )


def _params_of(agent_def: "AgentDef") -> Mapping[str, Any]:
    """Return the agent's parameter mapping, or an empty mapping."""
    params = getattr(agent_def, "parameters", None)
    return params if isinstance(params, Mapping) else {}


def _opt_float(params: Mapping[str, Any], key: str, default: float | None) -> float | None:
    """Read an optional float override from ``params``, else return ``default``."""
    if key in params and params[key] is not None:
        try:
            return float(params[key])
        except (TypeError, ValueError):
            return default
    return default


def _opt_int(params: Mapping[str, Any], key: str, default: int | None) -> int | None:
    """Read an optional int override from ``params``, else return ``default``."""
    if key in params and params[key] is not None:
        try:
            return int(params[key])
        except (TypeError, ValueError):
            return default
    return default


def _opt_str(params: Mapping[str, Any], key: str, default: str | None) -> str | None:
    """Read an optional string override from ``params``, else return ``default``."""
    if key in params and params[key] is not None:
        value = str(params[key]).strip()
        return value or default
    return default


def build_spec(agent_def: "AgentDef", default_spec: LMSpec) -> LMSpec:
    """Derive an expert's :class:`LMSpec` from its ``AgentDef``.

    Each field is taken from the ``AgentDef`` when the agent declares it, and
    inherited from ``default_spec`` otherwise — so an expert that declares
    nothing resolves to exactly the default profile (today's behaviour). String
    identity fields (``provider``/``model``/``api_base``/``credential_ref``/
    ``transport``) inherit on empty; the sampling params inherit when absent from
    ``AgentDef.parameters``.

    The ``api_base``/``credential_ref``/``transport`` fields are read via
    ``getattr`` so this helper works both before and after those explicit
    ``AgentDef`` fields land (design §9 step 5). Secrets never enter here: only a
    ``credential_ref`` is carried.

    Args:
        agent_def: The expert's agent definition.
        default_spec: The default-profile spec whose fields are inherited when
            the ``AgentDef`` declares none.

    Returns:
        A fully-populated :class:`LMSpec` for this expert.
    """
    params = _params_of(agent_def)
    provider = getattr(agent_def, "default_provider", "") or default_spec.provider
    model = getattr(agent_def, "default_model", "") or default_spec.model
    api_base = getattr(agent_def, "api_base", "") or default_spec.api_base
    credential_ref = getattr(agent_def, "credential_ref", "") or default_spec.credential_ref
    transport = getattr(agent_def, "transport", "") or default_spec.transport
    return LMSpec(
        provider=provider,
        model=model,
        api_base=api_base,
        credential_ref=credential_ref,
        transport=transport,
        temperature=_opt_float(params, "temperature", default_spec.temperature),
        max_tokens=_opt_int(params, "max_tokens", default_spec.max_tokens),
        thinking_budget=_opt_int(params, "thinking_budget", default_spec.thinking_budget),
        thinking_level=_opt_str(params, "thinking_level", default_spec.thinking_level),
        top_p=_opt_float(params, "top_p", default_spec.top_p),
        top_k=_opt_int(params, "top_k", default_spec.top_k),
        min_p=_opt_float(params, "min_p", default_spec.min_p),
        presence_penalty=_opt_float(params, "presence_penalty", default_spec.presence_penalty),
    )
