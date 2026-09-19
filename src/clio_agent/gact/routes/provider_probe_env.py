"""Runtime provider inputs for the shared doctor probe."""

from __future__ import annotations

import os
from collections.abc import Mapping


def runtime_provider_probe_env(live_lm: object) -> dict[str, str]:
    """Overlay the active runtime provider onto the process environment."""

    env = dict(os.environ)
    if not isinstance(live_lm, Mapping):
        return env
    for key, value in (
        ("CLIO_LM_PROVIDER", live_lm.get("provider")),
        ("CLIO_LM_API_BASE", live_lm.get("api_base")),
        ("CLIO_LM_MODEL", live_lm.get("model")),
    ):
        if value is not None:
            env[key] = str(value)
    return env
