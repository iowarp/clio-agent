"""Capability negotiation projections for GACT 0.3."""

from __future__ import annotations

from typing import Any, Mapping

from clio_agent import __version__ as clio_agent_version
from clio_agent.gact.protocol.v3 import A2UI_V091, GACT_V3, utcnow_iso
from clio_agent.gact.providers.config import _effective_lm_config


def capabilities_to_v3(app: Any, flags: Any, *, replay_retention: int) -> dict[str, Any]:
    """Build the explicit 0.3 negotiation response from live server truth."""

    raw_flags = flags.model_dump() if hasattr(flags, "model_dump") else dict(flags)
    capabilities = {
        key: value
        for key, value in raw_flags.items()
        if isinstance(value, bool) and not key.startswith("x_")
    }
    capabilities.update(
        {
            "a2ui": True,
            "replay": True,
            "workspace_display_names": True,
            "scoped_events": True,
        }
    )
    degradations: list[dict[str, Any]] = []
    for key, value in capabilities.items():
        if not value:
            degradations.append(
                {
                    "code": "capability_unavailable",
                    "reason": f"The server does not provide {key.replace('_', ' ')}.",
                    "capability": key,
                    "recoverable": False,
                }
            )

    lm_config = _effective_lm_config(app)
    lm_status = getattr(app.state, "lm_config_status", {})
    configured = isinstance(lm_config, Mapping) and bool(
        lm_config.get("provider") and lm_config.get("model")
    )
    failed_reason = ""
    if isinstance(lm_status, Mapping) and lm_status.get("state") == "error":
        failed_reason = str(lm_status.get("error") or lm_status.get("status_message") or "")
    observed_at = utcnow_iso()
    model_catalog = {
        "source": "provider" if configured else "unavailable",
        "observed_at": observed_at,
        "stale": bool(failed_reason) or not configured,
        **(
            {"reason": failed_reason}
            if failed_reason
            else (
                {}
                if configured
                else {"reason": "No active provider model catalog has been observed."}
            )
        ),
    }
    if not configured:
        degradations.append(
            {
                "code": "model_catalog_unavailable",
                "reason": str(model_catalog["reason"]),
                "capability": "providers",
                "recoverable": True,
            }
        )
    return {
        "service": {"name": "clio-agent", "version": clio_agent_version},
        "gact_versions": [GACT_V3, "0.2"],
        "a2ui_versions": [A2UI_V091],
        "replay": {"supported": True, "retention": replay_retention},
        "capabilities": capabilities,
        "degradations": degradations,
        "model_catalog": model_catalog,
        **(
            {
                "active_model": {
                    "provider_id": str(lm_config["provider"]),
                    "model_id": str(lm_config["model"]),
                    **(
                        {"effort": str(lm_config["thinking_level"])}
                        if lm_config.get("thinking_level")
                        else {}
                    ),
                }
            }
            if configured
            else {}
        ),
    }
