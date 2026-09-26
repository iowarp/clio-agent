"""Which LM provider the user actually selected -- the boot-time provider decision.

``lm.provider`` has a committed base-layer default (``lm_studio``, from
``config.defaults.yaml``) so that code which *needs* a provider config always gets
a valid one. That default is not a user selection, though: treating it as one made
every server boot try to build an agent against LM Studio, probing
``127.0.0.1:1234`` ten times -- on a headless remote host that has no LM Studio at
all (ares, 2026-09-25). The provider is **unconfigured** until the user picks one
(``PUT /v1/providers/lm``, the config file, or ``CLIO_LM_PROVIDER``).
"""

from __future__ import annotations

import os

from clio_agent import conf

#: Typed reason on the ``lm_provider`` health row while no provider is selected.
LM_PROVIDER_UNCONFIGURED = "lm_provider_unconfigured"


def explicit_lm_provider() -> str:
    """Return the explicitly selected LM provider, or ``""`` when none was chosen.

    Reads the config-file layer and ``CLIO_LM_PROVIDER`` only -- never the committed
    default layer -- so an untouched install reports no selection.

    Returns:
        The provider name from ``lm.provider`` in the user/workspace config file, else
        from ``CLIO_LM_PROVIDER``, else ``""``.
    """
    file_value = conf.store().file_value("lm.provider")
    if file_value is not conf.UNSET and str(file_value).strip():
        return str(file_value).strip()
    return os.environ.get("CLIO_LM_PROVIDER", "").strip()
