"""The clio-core daemon's runtime thread count (``arc.core_threads``).

CLIO's seeded ``cte.yaml`` carried a literal ``runtime: num_threads: 4``. clio-core
splits its runtime threads between scheduling and I/O, and four gave one I/O worker:
every put of 4 KB or more (all of CLIO's, because of the size-class floor) queued on
it, from every server on the machine (``work_orchestrator.cc``, ``default_sched.cc``).
Measured on P7 (2026-11-02), puts waited on the daemon on more than a third of the
server's active stacks.

The count is now ``arc.core_threads`` (default 8). A seed CLIO wrote with the old
literal is upgraded in place, once, like the durability upgrade
(:mod:`clio_agent.arc.clio_core_durability`); any other value is someone's explicit
edit and is left alone. The daemon reads it when it starts.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from clio_agent import conf

__all__ = ["SEEDED_THREADS", "core_threads", "ensure_seeded_threads"]

logger = logging.getLogger(__name__)

#: The thread count every CLIO seed carried before ``arc.core_threads``.
SEEDED_THREADS = 4

_LINE = re.compile(r"^(  num_threads: )(\d+)$", re.MULTILINE)


def core_threads() -> int:
    """The daemon's runtime thread count (``arc.core_threads``, at least 2).

    Raises:
        ValueError: The configured count is below 2 (one scheduler, one I/O worker).
    """
    value = conf.resolve(
        "arc.core_threads", env="CLIO_ARC_CORE_THREADS", default=8, cast=conf.as_int
    )
    if value < 2:
        raise ValueError(f"arc.core_threads must be at least 2, not {value}")
    return value


def ensure_seeded_threads(path: Path) -> None:
    """Move a CLIO seed still at :data:`SEEDED_THREADS` to the configured count."""
    text = path.read_text(encoding="utf-8")
    found = _LINE.findall(text)
    wanted = core_threads()
    if len(found) != 1 or int(found[0][1]) != SEEDED_THREADS or wanted == SEEDED_THREADS:
        return
    upgraded = _LINE.sub(rf"\g<1>{wanted}", text)
    path.write_text(upgraded, encoding="utf-8")
    from clio_agent.arc.clio_core_growth import rekey_managed_capacity  # noqa: PLC0415 - cycle

    # CLIO's own edit: it keeps owning the config's growth.
    rekey_managed_capacity(path, text, upgraded)
    logger.warning(
        "clio-core config %s upgraded in place: runtime num_threads %d -> %d "
        "(arc.core_threads); the daemon uses it from its next start",
        path,
        SEEDED_THREADS,
        wanted,
    )
