"""Independent, durable model choices for CLIO's judges, search assistant, the
long-term memory background pass (``memory_extract``, ``memory_consolidate``), the
fast ``explore`` subagent (``Prefers(role="explore")``), the server-default SPOTTER
watcher model (``spotter``) and the fast model (``fast``) every unset role runs on."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from clio_agent import conf
from clio_agent.user_config_document import (
    USER_CONFIG_LOCK,
    read_document,
    user_config_path,
    write_document,
)

JudgeRole = Literal[
    "goals", "variants", "permissions", "memory_extract", "memory_consolidate", "explore"
]
ROLES: tuple[JudgeRole, ...] = (
    "goals",
    "variants",
    "permissions",
    "memory_extract",
    "memory_consolidate",
    "explore",
)


def fleet_role_configured(role: JudgeRole) -> bool:
    """Distinguish an explicit inherit choice from an older model-only setting."""
    return role in _judge_choices()


def _judge_choices() -> dict[str, Any]:
    value: Any = conf.resolve(
        "fleet.judges",
        env="CLIO_FLEET_JUDGES",
        default={},
        cast=lambda raw: json.loads(raw) if isinstance(raw, str) else raw,
    )
    if not isinstance(value, dict) or value.keys() - set(ROLES):
        raise ValueError(f"Fleet judges must map {', '.join(ROLES)} to a model or null")
    return value


class JudgeModel(BaseModel):
    """A catalog provider identity and model, or null to inherit the caller."""

    model_config = ConfigDict(extra="forbid")
    provider_id: str
    model_id: str


class ExploreNudge(BaseModel):
    """The explore note (:mod:`clio_agent.gact.explore_nudge`): on or off, and after how
    many reading calls. Stored in the reminder ``clio.reminder.explore_nudge``."""

    model_config = ConfigDict(extra="forbid")
    enabled: bool
    after: int = Field(ge=0)


class FleetUpdate(BaseModel):
    """Optimistic settings update, preserving unrelated configuration."""

    model_config = ConfigDict(extra="forbid")
    revision: str
    judges: dict[JudgeRole, JudgeModel | None]
    search: JudgeModel | None = None
    explore_nudge: ExploreNudge | None = None
    #: The model a SPOTTER watcher runs on when its session names none
    #: (``metadata.spotter_model``); null inherits the watched session's model.
    spotter: JudgeModel | None = None
    #: The fast model (:mod:`clio_agent.gact.fleet.fast_model`) every unset helper
    #: role runs on; null uses the caller's account's fast family.
    fast: JudgeModel | None = None


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _model_choice(raw: Any) -> dict[str, Any] | None:
    return JudgeModel.model_validate(raw).model_dump() if raw else None


def _known_account(provider_id: str) -> bool:
    """A built-in provider, or a saved custom server (its own fleet account)."""
    from clio_agent.gact.fleet.entries import account_provider

    return account_provider(provider_id) is not None


def fleet_settings(app: Any = None) -> dict[str, Any]:
    """Read effective choices without exposing credentials (``app``: whose prompt store
    holds the explore reminder; the user's own when omitted)."""
    raw = _judge_choices()
    judges = {
        role: JudgeModel.model_validate(value).model_dump() if value is not None else None
        for role in ROLES
        for value in [raw.get(role)]
    }
    # Literal keys, so the configuration reference finds both knobs.
    search = _model_choice(
        conf.resolve("fleet.search", env="CLIO_FLEET_SEARCH", default=None, cast=_json)
    )
    from clio_agent.gact.explore_nudge import nudge_settings  # noqa: PLC0415

    nudge = nudge_settings(app)
    spotter = _model_choice(
        conf.resolve("fleet.spotter", env="CLIO_FLEET_SPOTTER", default=None, cast=_json)
    )
    fast = _model_choice(
        conf.resolve("fleet.fast", env="CLIO_FLEET_FAST", default=None, cast=_json)
    )
    # An unset SPOTTER or fast model leaves the revision every earlier client computed.
    revision = hashlib.sha256(
        json.dumps(
            {
                "judges": judges,
                "search": search,
                "explore_nudge": nudge,
                **({"spotter": spotter} if spotter else {}),
                **({"fast": fast} if fast else {}),
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    return {
        "revision": revision,
        "judges": judges,
        "search": search,
        "explore_nudge": nudge,
        "spotter": spotter,
        "fast": fast,
    }


def save_fleet_settings(update: FleetUpdate, app: Any = None) -> dict[str, Any]:
    """Save only the changed roles under the shared configuration lock."""
    with USER_CONFIG_LOCK:
        conf.reload()
        if update.revision != fleet_settings(app)["revision"]:
            raise ValueError("Fleet settings changed; reload before saving")
        for choice in [*update.judges.values(), update.search, update.spotter, update.fast]:
            if choice and (not choice.model_id.strip() or not _known_account(choice.provider_id)):
                raise ValueError("Choose a configured provider and a nonempty model")
        path = user_config_path()
        document = read_document(path)
        fleet = dict(document.get("fleet") or {})
        judges = dict(fleet.get("judges") or {})
        judges.update(
            {
                role: choice.model_dump() if choice else None
                for role, choice in update.judges.items()
            }
        )
        fleet["judges"] = judges
        if "search" in update.model_fields_set:
            fleet["search"] = update.search.model_dump() if update.search else None
        if "spotter" in update.model_fields_set:
            fleet["spotter"] = update.spotter.model_dump() if update.spotter else None
        if "fast" in update.model_fields_set:
            fleet["fast"] = update.fast.model_dump() if update.fast else None
        document["fleet"] = fleet
        write_document(path, document)
        if update.explore_nudge is not None:
            from clio_agent.gact.explore_nudge import save_nudge_settings  # noqa: PLC0415

            save_nudge_settings(update.explore_nudge.enabled, update.explore_nudge.after, app)
        conf.reload()
        return fleet_settings(app)


def judge_route(role: JudgeRole, caller: Any, adapter: Any = None) -> tuple[Any, Any]:
    """Resolve a judge using its own provider credentials; an unset role runs on the fast
    model (:mod:`clio_agent.gact.fleet.fast_model`), else the exact caller."""
    raw = fleet_settings()["judges"][role]
    if raw is None:
        from clio_agent.gact.fleet.fast_model import fast_route  # noqa: PLC0415

        return fast_route(caller, adapter)
    choice = JudgeModel.model_validate(raw)
    return model_route(choice.provider_id, choice.model_id)


class ModelRouteUnavailable(ValueError):
    """An explicit route names no provider or model CLIO can reach (typed, fail-fast).

    Raised instead of letting an empty identity resolve to the committed default
    provider (LM Studio on ``127.0.0.1:1234``, retried per call).
    """

    error_type = "model_route_unavailable"


def model_route(provider_id: str, model_id: str) -> tuple[Any, Any]:
    """Construct one explicit identity through the canonical endpoint resolver.

    A saved custom model server (``server-<slug>``) runs at its own address with its
    own key: the config keeps it as ``server_id`` and the spec/resolver carry it.

    Raises:
        ModelRouteUnavailable: ``provider_id`` is empty, or names no catalog
            provider and no saved model server.
    """
    from clio_agent.config import LMProviderConfig, UnknownLMProviderError, create_chat_adapter
    from clio_agent.lm.hooked_lm import create_hooked_lm

    if not provider_id.strip():
        raise ModelRouteUnavailable(f"model route names no provider (model {model_id!r})")
    try:
        config = LMProviderConfig(provider_id=provider_id, model=model_id)
    except UnknownLMProviderError as exc:
        raise ModelRouteUnavailable(str(exc)) from exc
    from clio_agent.providers.lm_spec import spec_from_config
    from clio_agent.providers.resolver import resolve_endpoint_and_handshake

    resolved = resolve_endpoint_and_handshake(
        spec_from_config(config), default_credential=config.api_key
    )
    config = resolved.materialize()
    return create_hooked_lm(config), create_chat_adapter(config)
