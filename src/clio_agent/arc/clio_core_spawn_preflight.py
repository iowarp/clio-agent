"""Checks that must pass before CLIO starts a clio-core daemon (the single spawn point).

Each refuses typed, before ``clio-run start``, a start that would fail or do harm:

* the IPC socket path must fit ``sun_path`` and the memfd dir must be private
  (:mod:`clio_agent.arc.clio_core_ipc_path`): otherwise clio-core 2.5 exits 1;
* the config must be durable (:func:`clio_agent.arc.clio_core_durability.refuse_volatile_spawn`):
  otherwise clio-core 2.5 deletes the store's metadata log as it starts.
"""

from __future__ import annotations

from collections.abc import Mapping

from clio_agent.arc.clio_core_durability import refuse_volatile_spawn
from clio_agent.arc.clio_core_ipc_path import require_ipc_path_fits


def require_spawnable(env: Mapping[str, str], config_path: str, port: int) -> None:
    """Raise the typed refusal for a daemon start that must not happen."""
    require_ipc_path_fits(env, port)
    if config_path:  # "" = clio-core's own default config, which CLIO does not seed
        refuse_volatile_spawn(config_path)


__all__ = ["require_spawnable"]
