"""Per-test config-file layer helpers (#985 move 3 residual).

The autouse ``allow_pytest_tmp_path`` fixture no longer leans on ambient process
env for the four test-default knobs (``agents.disable_default_registry_bootstrap``,
``tools.file_policy.allowed_roots``, ``lm.model``, ``arc.store``). It writes them
to the per-test *user* ``config.yaml`` under ``XDG_CONFIG_HOME`` — the config-FILE
layer :mod:`clio_agent.conf` resolves ABOVE the environment. Tests that need to
override one of those knobs therefore cannot win with ``monkeypatch.setenv`` (the
file shadows the env); they mutate the file layer instead, through these helpers.

Every mutator calls :func:`clio_agent.conf.reload` so the process-wide store drops
its cached file layer and the next ``resolve`` re-reads from disk. The helpers key
off ``XDG_CONFIG_HOME`` (set by the fixture to a per-test tmp dir), so they target
exactly the file the fixture wrote — never a developer's real user config.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from clio_agent import conf


def user_config_path() -> Path:
    """The per-test user ``config.yaml`` under the fixture's ``XDG_CONFIG_HOME``."""
    xdg = os.environ["XDG_CONFIG_HOME"]  # set by the autouse fixture to a tmp dir
    return Path(xdg) / "clio-agent" / "config.yaml"


def read_config() -> dict[str, Any]:
    """Read the per-test user config.yaml into a dict (``{}`` when absent)."""
    path = user_config_path()
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return dict(data) if isinstance(data, dict) else {}


def write_config(data: dict[str, Any]) -> None:
    """Overwrite the per-test user config.yaml with ``data`` and reload the store."""
    path = user_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    conf.reload()


def set_config(dotted_key: str, value: Any) -> None:
    """Deep-set ``dotted_key`` in the per-test user config.yaml and reload.

    Reads the current file (preserving the fixture's other knobs), sets the nested
    key, writes it back, and reloads the process store. Use for the "explicit
    override wins" contracts that previously reached for ``monkeypatch.setenv`` on
    a file-layer knob (``arc.store`` / ``lm.model`` / ``tools.file_policy.*``).
    """
    data = read_config()
    node = data
    parts = dotted_key.split(".")
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[parts[-1]] = value
    write_config(data)


def delete_config(dotted_key: str) -> None:
    """Delete ``dotted_key`` from the per-test user config.yaml and reload.

    Drops the fixture-provided value so the env (or the in-code default) resolves
    instead — the file-layer analogue of ``monkeypatch.delenv``. Used by the
    discovery/no-explicit-override contracts (e.g. dropping ``lm.model`` so LM
    discovery triggers, or ``arc.store`` so an env value is the sole source).
    """
    data = read_config()
    node = data
    parts = dotted_key.split(".")
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            return  # nothing to delete; leave the file untouched
        node = child
    node.pop(parts[-1], None)
    write_config(data)
