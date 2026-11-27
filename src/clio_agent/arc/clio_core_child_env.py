"""Settings a clio-core child must never inherit from CLIO's own environment.

CLIO owns its daemon's storage layout and its network surface, so these are forced for
every clio-core child (:func:`clio_agent.arc.clio_core_config.pin_core_child_env`),
whatever the parent process carries:

* ``CLIO_VIZ_ENABLE=0``: iowarp-core 2.5's dashboard has no authentication and no
  Origin check, so any web page can POST ``/api/pools/<pool>/destroy`` while it is up
  (a cross-origin form destroyed a pool, live, 2.5.0). A parent value of ``1`` is
  overridden, loudly.
* no ``CLIO_BDEV_STRIPE_FILES`` / ``CLIO_BDEV_STRIPE_UNIT``: a file bdev reopened with a
  different stripe layout opens new empty stripe files and reads every block back wrong,
  with no error (live, 2.5.0: 100 of 100 records). CLIO's store is never striped.

Each override is logged with a typed reason (``clio_core_child_env_overridden``).
"""

from __future__ import annotations

import logging
from collections.abc import MutableMapping

logger = logging.getLogger(__name__)

CHILD_ENV_OVERRIDDEN = "clio_core_child_env_overridden"
VIZ_ENABLE_ENV = "CLIO_VIZ_ENABLE"
#: Layout settings of the file bdev that must match the store on disk.
STRIPE_ENVS = ("CLIO_BDEV_STRIPE_FILES", "CLIO_BDEV_STRIPE_UNIT")


def force_safe_core_env(env: MutableMapping[str, str]) -> None:
    """Turn the dashboard off and drop striping from a clio-core child's ``env``."""
    previous = env.get(VIZ_ENABLE_ENV, "")
    env[VIZ_ENABLE_ENV] = "0"
    if previous.strip() not in ("", "0"):
        logger.warning(
            "reason=%s var=%s was=%r now='0': the clio-core dashboard has no "
            "authentication, so CLIO never starts it",
            CHILD_ENV_OVERRIDDEN,
            VIZ_ENABLE_ENV,
            previous,
        )
    for name in STRIPE_ENVS:
        if name in env:
            logger.warning(
                "reason=%s var=%s was=%r removed: a stripe layout that differs from the "
                "store on disk reads every block back wrong",
                CHILD_ENV_OVERRIDDEN,
                name,
                env.pop(name),
            )


__all__ = ["CHILD_ENV_OVERRIDDEN", "STRIPE_ENVS", "VIZ_ENABLE_ENV", "force_safe_core_env"]
