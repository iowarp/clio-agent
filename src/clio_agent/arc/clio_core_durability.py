"""clio-core keeps the agent's context across restarts only on a durable tier.

A clio-core storage tier left at the default ``volatile`` persistence level keeps
nothing across a daemon restart (verified against iowarp-core 2.2.1: a record written,
the daemon stopped cleanly, a new daemon on the same config -> the record is gone;
with the file tier at ``persistence_level: "temporary"`` it is read back, and after a
crash too once clio-core's periodic flush has run). So:

* the config CLIO seeds marks its file tier durable (``clio_core_config`` template);
* a seeded config from before that is upgraded in place, once, logged
  (:func:`ensure_seeded_config_durable`) -- it is CLIO's own file;
* any other config that is not durable is a typed
  :class:`~clio_agent.arc.init_degradation.ArcStoreUnavailableError`
  (``clio_core_not_durable``, :func:`require_durable_config`) -- never a store that
  forgets everything on restart.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

CLIO_CORE_NOT_DURABLE = "clio_core_not_durable"
DURABLE_LEVELS = frozenset({"temporary", "long_term"})
_PERSISTENCE_LINE = '        persistence_level: "temporary"\n'
_REMEDY = (
    'mark the clio-core file tier durable: add `persistence_level: "temporary"` to its '
    "storage entry (a volatile tier keeps nothing across a daemon restart)"
)


class ClioCoreNotDurableError(RuntimeError):
    """The clio-core config has no durable storage tier."""

    degradation_reason = CLIO_CORE_NOT_DURABLE


def _tiers(config: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        tier
        for module in config.get("compose") or []
        if isinstance(module, dict) and module.get("mod_name") == "clio_cte_core"
        for tier in module.get("storage") or []
        if isinstance(tier, dict)
    ]


def config_is_durable(text: str) -> bool:
    """Whether a clio-core config text has a non-RAM tier at a durable persistence level."""
    config = yaml.safe_load(text) or {}
    return any(
        str(tier.get("bdev_type", "")).lower() != "ram"
        and str(tier.get("persistence_level", "volatile")).lower() in DURABLE_LEVELS
        for tier in _tiers(config)
    )


def ensure_seeded_config_durable(path: Path) -> None:
    """Upgrade CLIO's own seeded config in place so its file tier is durable (once)."""
    text = path.read_text(encoding="utf-8")
    if config_is_durable(text):
        return
    anchor = '        bdev_type: "file"\n'
    if text.count(anchor) != 1:
        raise ClioCoreNotDurableError(f"{path}: cannot upgrade in place; {_REMEDY}")
    start = text.index(anchor)
    score_at = text.index("        score:", start)
    line_end = text.index("\n", score_at) + 1
    upgraded = text[:line_end] + _PERSISTENCE_LINE + text[line_end:]
    if not config_is_durable(upgraded):
        raise ClioCoreNotDurableError(f"{path}: upgrade did not make it durable; {_REMEDY}")
    path.write_text(upgraded, encoding="utf-8")
    logger.warning(
        "clio-core config %s upgraded in place: its file tier is now durable "
        '(persistence_level: "temporary"); before this, a daemon restart lost all ARC data',
        path,
    )


def require_durable_config(config_path: str) -> None:
    """Raise a typed store error when ``config_path`` would forget everything on restart."""
    from clio_agent.arc.init_degradation import ArcStoreUnavailableError  # noqa: PLC0415

    try:
        text = Path(config_path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ArcStoreUnavailableError(error=exc, config_path=config_path) from exc
    if not config_is_durable(text):
        raise ArcStoreUnavailableError(
            error=ClioCoreNotDurableError(f"{config_path}: {_REMEDY}"), config_path=config_path
        )
