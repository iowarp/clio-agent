"""Independent, durable model choices for CLIO's judges, search assistant and the
long-term memory background pass (``memory_extract``, ``memory_consolidate``)."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from clio_agent import conf
from clio_agent.user_config_document import (
    USER_CONFIG_LOCK,
    read_document,
    user_config_path,
    write_document,
)

JudgeRole = Literal["goals", "variants", "permissions", "memory_extract", "memory_consolidate"]
ROLES: tuple[JudgeRole, ...] = (
    "goals",
    "variants",
    "permissions",
    "memory_extract",
    "memory_consolidate",
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


class FleetUpdate(BaseModel):
    """Optimistic settings update, preserving unrelated configuration."""

    model_config = ConfigDict(extra="forbid")
    revision: str
    judges: dict[JudgeRole, JudgeModel | None]
    search: JudgeModel | None = None


def fleet_settings() -> dict[str, Any]:
    """Read effective choices without exposing credentials."""
    raw = _judge_choices()
    judges = {
        role: JudgeModel.model_validate(value).model_dump() if value is not None else None
        for role in ROLES
        for value in [raw.get(role)]
    }
    search_raw = conf.resolve(
        "fleet.search",
        env="CLIO_FLEET_SEARCH",
        default=None,
        cast=lambda value: json.loads(value) if isinstance(value, str) else value,
    )
    search = JudgeModel.model_validate(search_raw).model_dump() if search_raw else None
    revision = hashlib.sha256(
        json.dumps({"judges": judges, "search": search}, sort_keys=True).encode()
    ).hexdigest()
    return {"revision": revision, "judges": judges, "search": search}


def save_fleet_settings(update: FleetUpdate) -> dict[str, Any]:
    """Save only the changed roles under the shared configuration lock."""
    from clio_agent.providers.catalog import get_provider

    with USER_CONFIG_LOCK:
        conf.reload()
        if update.revision != fleet_settings()["revision"]:
            raise ValueError("Fleet settings changed; reload before saving")
        for choice in [*update.judges.values(), update.search]:
            if choice and (not choice.model_id.strip() or get_provider(choice.provider_id) is None):
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
        document["fleet"] = fleet
        write_document(path, document)
        conf.reload()
        return fleet_settings()


def judge_route(role: JudgeRole, caller: Any, adapter: Any = None) -> tuple[Any, Any]:
    """Resolve a judge using its own provider credentials; inherit the exact caller by default."""
    raw = fleet_settings()["judges"][role]
    if raw is None:
        return caller, adapter
    choice = JudgeModel.model_validate(raw)
    return model_route(choice.provider_id, choice.model_id)


def model_route(provider_id: str, model_id: str) -> tuple[Any, Any]:
    """Construct one explicit identity through the canonical endpoint resolver."""
    from clio_agent.config import LMProviderConfig, create_chat_adapter
    from clio_agent.lm.hooked_lm import create_hooked_lm

    config = LMProviderConfig(provider_id=provider_id, model=model_id)
    from clio_agent.providers.lm_spec import spec_from_config
    from clio_agent.providers.resolver import resolve_endpoint_and_handshake

    resolved = resolve_endpoint_and_handshake(
        spec_from_config(config), default_credential=config.api_key
    )
    config = resolved.materialize()
    return create_hooked_lm(config), create_chat_adapter(config)
