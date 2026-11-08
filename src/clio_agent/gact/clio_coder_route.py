"""Which model a delegated Clio Coder instance runs on.

Resolution, first match wins:

1. the call's own ``model`` (and optional ``provider``),
2. the Clio Coder agent binding: ``fleet.agents.clio-coder`` in CLIO's settings
   (``CLIO_FLEET_AGENTS``), else the operator's ``CLIO_AGENT_CLIO_CODER_MODEL`` route or
   ``CLIO_AGENT_CLIO_CODER_HOME_TEMPLATE`` profile,
3. the fleet default: ``fleet.default`` (``CLIO_FLEET_DEFAULT``),
4. the dispatching conversation's own model.

With one configured target every layer is unset and the delegation runs on the conversation's
model; with a large catalog each layer can name any catalog provider and model. A CLIO route is
rendered as a Clio Coder target whose settings name an environment variable, never a key.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from clio_agent import conf
from clio_agent.runtime.clio_coder import ClioCoderTarget

AGENT_ID = "clio-coder"
HOME_TEMPLATE_ENV = "CLIO_AGENT_CLIO_CODER_HOME_TEMPLATE"
MODEL_ENV = "CLIO_AGENT_CLIO_CODER_MODEL"
URL_ENV = "CLIO_AGENT_CLIO_CODER_URL"
KEY_ENV_ENV = "CLIO_AGENT_CLIO_CODER_API_KEY_ENV"
#: The child-only variable that carries a key CLIO resolved from a store rather than the env.
DELEGATED_KEY_ENV = "CLIO_DELEGATED_MODEL_API_KEY"

SOURCE_CALL = "call"
SOURCE_BINDING = "agent-binding"
SOURCE_FLEET_DEFAULT = "fleet-default"
SOURCE_MAIN = "conversation"

#: CLIO provider kinds Clio Coder can reach with an endpoint URL and an API key.
_RUNTIME_BY_KIND = {"openai": "openai-compat", "lm_studio": "openai-compat", "ollama": "ollama"}
#: Providers whose own runtime in Clio Coder signs in by itself.
_RUNTIME_BY_PROVIDER = {
    "anthropic": "anthropic",
    "claude_code": "claude-code",
    "codex": "codex-cli",
    "openrouter": "openrouter",
}
#: OpenAI-kind providers whose auth is not a bearer key on an OpenAI-shaped endpoint.
_UNMAPPED_PROVIDERS = frozenset({"azure_openai", "vertex_ai", "bedrock"})
_NO_URL_RUNTIMES = frozenset({"claude-code", "codex-cli"})


@dataclass(frozen=True)
class DelegatedRoute:
    """The resolved route: where it came from and how the instance is configured.

    ``target`` is written into the instance home and selected for this run; ``None`` means the
    run uses the home's own configuration (a template profile). ``env`` holds what the child
    process alone receives.
    """

    source: str
    provider_id: str = ""
    model_id: str = ""
    target: ClioCoderTarget | None = None
    home_template: Path | None = None
    env: Mapping[str, str] = field(default_factory=dict)

    def describe(self) -> dict[str, str]:
        row = {"source": self.source, "provider_id": self.provider_id, "model": self.model_id}
        if self.target is not None:
            row["clio_coder_target"] = self.target.target_id
            row["clio_coder_runtime"] = self.target.runtime
        if self.home_template is not None:
            row["home_template"] = str(self.home_template)
        return row


def _choice(key: str, env: str) -> Any:
    return conf.resolve(
        key,
        env=env,
        default=None,
        cast=lambda raw: json.loads(raw) if isinstance(raw, str) else raw,
    )


def _model_ref(raw: Any, where: str) -> tuple[str, str] | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ValueError(f"{where} must be {{provider_id, model_id}}")
    provider_id = str(raw.get("provider_id") or "").strip()
    model_id = str(raw.get("model_id") or "").strip()
    if not provider_id or not model_id:
        raise ValueError(f"{where} must name both provider_id and model_id")
    return provider_id, model_id


def agent_binding() -> tuple[str, str] | None:
    agents = _choice("fleet.agents", "CLIO_FLEET_AGENTS")
    if agents is None:
        return None
    if not isinstance(agents, Mapping):
        raise ValueError("fleet.agents must map an agent id to {provider_id, model_id}")
    return _model_ref(agents.get(AGENT_ID), f"fleet.agents.{AGENT_ID}")


def fleet_default() -> tuple[str, str] | None:
    return _model_ref(_choice("fleet.default", "CLIO_FLEET_DEFAULT"), "fleet.default")


def _target_id(provider_id: str) -> str:
    return "clio-" + re.sub(r"[^a-z0-9-]+", "-", provider_id.lower()).strip("-")


def _provider_endpoint(app: Any, provider_id: str, model_id: str) -> tuple[str, str]:
    """The provider's endpoint and key for this model, as the conversation itself would use."""
    from clio_agent.config import LMProviderConfig  # noqa: PLC0415
    from clio_agent.gact.providers.config import _active_lm_model_ref  # noqa: PLC0415

    live = getattr(getattr(app.state, "agent", None), "_provider_config", None)
    active = _active_lm_model_ref(app)
    if live is not None and active["provider_id"] == provider_id:
        # The running conversation's provider: reuse its endpoint (it may come from boot env).
        return str(getattr(live, "api_base", "") or ""), str(getattr(live, "api_key", "") or "")
    config = LMProviderConfig(provider_id=provider_id, model=model_id)
    return str(config.api_base or ""), str(config.api_key or "")


def target_for(app: Any, provider_id: str, model_id: str) -> tuple[ClioCoderTarget, dict[str, str]]:
    """Render a CLIO catalog route as a Clio Coder target and the child-only environment."""
    from clio_agent.providers.catalog import as_cloud_api_key_env, get_provider  # noqa: PLC0415

    preset = get_provider(provider_id)
    if preset is None:
        raise ValueError(f"Unknown provider {provider_id!r}; choose one from the fleet catalog")
    runtime = _RUNTIME_BY_PROVIDER.get(provider_id)
    if runtime is None and provider_id not in _UNMAPPED_PROVIDERS:
        runtime = _RUNTIME_BY_KIND.get(preset.provider_kind)
    if runtime is None:
        raise ValueError(
            f"Clio Coder cannot use CLIO provider {provider_id!r} directly; bind it a route with "
            f"fleet.agents.{AGENT_ID}, {MODEL_ENV} or {HOME_TEMPLATE_ENV}"
        )
    target_id = _target_id(provider_id)
    if runtime in _NO_URL_RUNTIMES:
        return ClioCoderTarget(model=model_id, target_id=target_id, runtime=runtime), {}
    url, key = _provider_endpoint(app, provider_id, model_id)
    known = as_cloud_api_key_env().get(provider_id)
    if known and key and os.environ.get(known) == key:
        key_env, env = known, {}
    elif key:
        key_env, env = DELEGATED_KEY_ENV, {DELEGATED_KEY_ENV: key}
    else:
        key_env, env = None, {}
    target = ClioCoderTarget(
        model=model_id, target_id=target_id, runtime=runtime, url=url or None, api_key_env=key_env
    )
    return target, env


def _operator_route() -> DelegatedRoute | None:
    model = os.environ.get(MODEL_ENV, "").strip()
    if model:
        target = ClioCoderTarget(
            model=model,
            target_id="operator",
            url=os.environ.get(URL_ENV) or None,
            api_key_env=os.environ.get(KEY_ENV_ENV) or None,
        )
        return DelegatedRoute(SOURCE_BINDING, "", model, target=target)
    template = os.environ.get(HOME_TEMPLATE_ENV, "").strip()
    if template:
        path = Path(template).expanduser().resolve()
        if not (path / "config" / "settings.yaml").is_file():
            raise ValueError(
                f"{HOME_TEMPLATE_ENV}={template!r} has no config/settings.yaml; "
                "point it at a prepared Clio Coder home or unset it to inherit "
                "the conversation's model"
            )
        return DelegatedRoute(SOURCE_BINDING, home_template=path)
    return None


def resolve_route(app: Any, session: Any, *, model: str = "", provider: str = "") -> DelegatedRoute:
    """Pick the delegated model by precedence: call, agent binding, fleet default, conversation."""
    from clio_agent.gact.fleet.child_model import caller_model  # noqa: PLC0415

    main = caller_model(app, session)
    model, provider = model.strip(), provider.strip()
    if provider and not model:
        raise ValueError("provider needs a model; name the model to run on that provider")
    chosen: tuple[str, tuple[str, str]] | None = None
    if model:
        chosen = (SOURCE_CALL, (provider or main["provider_id"], model))
    elif (bound := agent_binding()) is not None:
        chosen = (SOURCE_BINDING, bound)
    elif (operator := _operator_route()) is not None:
        return operator
    elif (default := fleet_default()) is not None:
        chosen = (SOURCE_FLEET_DEFAULT, default)
    elif main["provider_id"] and main["model_id"]:
        chosen = (SOURCE_MAIN, (main["provider_id"], main["model_id"]))
    if chosen is None:
        raise ValueError(
            "Clio Coder has no model: select a model for this CLIO conversation "
            "(delegation inherits it without extra configuration), or set "
            f"fleet.agents.{AGENT_ID} or fleet.default to {{provider_id, model_id}}"
        )
    source, (provider_id, model_id) = chosen
    target, env = target_for(app, provider_id, model_id)
    return DelegatedRoute(source, provider_id, model_id, target=target, env=env)
