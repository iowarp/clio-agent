"""clio-core keeps the agent's context across restarts only on a durable tier.

A clio-core storage tier left at the default ``volatile`` persistence level keeps
nothing across a daemon restart (verified against iowarp-core 2.2.1: a record written,
the daemon stopped cleanly, a new daemon on the same config -> the record is gone;
with the file tier at ``persistence_level: "temporary"`` it is read back, and after a
crash too). Since iowarp-core 2.5 a core that resolves volatile also *deletes* an
existing snapshot and metadata log at start, so the check runs before every spawn
(:func:`refuse_volatile_spawn`). So:

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
#: CLIO's own addition for a seed that still carries a volatile (RAM) data tier.
_CORE_MODULE = "  - mod_name: clio_cte_core\n"
DURABILITY_LINE = "    durability: persistent\n"
_REMEDY = (
    'mark the clio-core file tier durable: add `persistence_level: "temporary"` to its '
    "storage entry (a volatile tier keeps nothing across a daemon restart). From "
    "iowarp-core 2.5 one volatile tier (a RAM data tier) also turns the whole core's "
    "metadata log off unless the clio_cte_core module states `durability: persistent`"
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


def _core_persistent(config: dict[str, Any], tiers: list[dict[str, Any]]) -> bool:
    """clio-core 2.5's own rule (``Config::ResolveDurability``): an explicit
    ``durability`` wins; unstated, the core keeps a WAL only when it has devices and
    every one is labelled durable. One volatile tier makes the whole core volatile."""
    stated = {
        str(module.get("durability", "")).lower()
        for module in config.get("compose") or []
        if isinstance(module, dict) and module.get("mod_name") == "clio_cte_core"
    } - {""}
    if stated:
        return stated == {"persistent"}
    return bool(tiers) and all(
        str(tier.get("persistence_level", "volatile")).lower() in DURABLE_LEVELS for tier in tiers
    )


def _file_tier_durable(config: dict[str, Any]) -> bool:
    return any(
        str(tier.get("bdev_type", "")).lower() != "ram"
        and str(tier.get("persistence_level", "volatile")).lower() in DURABLE_LEVELS
        for tier in _tiers(config)
    )


def _metadata_logged(config: dict[str, Any]) -> bool:
    """Every ``clio_cte_core`` names a metadata log (its WAL; without one nothing restores)."""
    cores = [
        module
        for module in config.get("compose") or []
        if isinstance(module, dict) and module.get("mod_name") == "clio_cte_core"
    ]
    return bool(cores) and all(
        str(((core.get("performance") or {}).get("metadata_log_path")) or "").strip()
        for core in cores
    )


def config_is_durable(text: str) -> bool:
    """Whether a clio-core config keeps records across a restart.

    It needs a non-RAM tier at a durable persistence level, a metadata log, and the core
    itself must be persistent under clio-core's rule (:func:`_core_persistent`).
    """
    config = yaml.safe_load(text) or {}
    return (
        _file_tier_durable(config)
        and _metadata_logged(config)
        and _core_persistent(config, _tiers(config))
    )


def refuse_volatile_spawn(config_path: str) -> None:
    """Refuse to start a daemon on a config clio-core would treat as volatile.

    iowarp-core 2.5 does not only skip the restore for a volatile core: it deletes the
    existing snapshot and metadata-log shards when it starts (``core_runtime.cc:862``,
    ``:8726-8752``), so one start with an unlabelled tier loses the store for good. This
    runs at the single spawn point, before every daemon start.

    Raises:
        ClioCoreNotDurableError: The config is not durable (or cannot be read).
    """
    try:
        text = Path(config_path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ClioCoreNotDurableError(f"{config_path}: unreadable ({exc})") from exc
    if not config_is_durable(text):
        raise ClioCoreNotDurableError(
            f"{config_path}: refusing to start clio-core, which would delete this store's "
            f"metadata log on a volatile start. {_REMEDY}; and name a metadata_log_path"
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
    upgraded = text
    if not _file_tier_durable(yaml.safe_load(text) or {}):
        upgraded = text[:line_end] + _PERSISTENCE_LINE + text[line_end:]
    if not config_is_durable(upgraded) and upgraded.count(_CORE_MODULE) == 1:
        # An older seed's RAM data tier: from iowarp-core 2.5 one volatile tier makes
        # the whole core volatile (no metadata log) unless durability is stated.
        at = upgraded.index(_CORE_MODULE) + len(_CORE_MODULE)
        upgraded = upgraded[:at] + DURABILITY_LINE + upgraded[at:]
    if not config_is_durable(upgraded):
        raise ClioCoreNotDurableError(f"{path}: upgrade did not make it durable; {_REMEDY}")
    path.write_text(upgraded, encoding="utf-8")
    from clio_agent.arc.clio_core_growth import rekey_managed_capacity  # noqa: PLC0415 - cycle

    # Growth stays enabled: this is CLIO's own edit, not a person's.
    rekey_managed_capacity(path, text, upgraded)
    logger.warning(
        "clio-core config %s upgraded in place: its file tier is now durable "
        '(persistence_level: "temporary"%s); before this, a daemon restart lost all ARC data',
        path,
        ", durability: persistent" if DURABILITY_LINE in upgraded else "",
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
