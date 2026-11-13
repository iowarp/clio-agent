"""The CLIO server's port: one machine-wide key in CLIO's user configuration.

Precedence, highest first:

1. an explicit ``--port`` given to the server or the ``clio`` launcher;
2. ``gact.port`` in the USER ``config.yaml`` (:func:`clio_agent.paths.user_config_dir`;
   on macOS ``~/Library/Application Support/clio-agent/config/config.yaml``);
3. the ``CLIO_PORT`` environment variable;
4. 17800.

There is one CLIO server per machine, so a workspace ``.clio-agent/shared/config.yaml``
does NOT set it: the port, pidfile and attach target never depend on the directory a
command runs from. The ``clio`` launcher and the Desktop/web UI ask for the resolved
value with ``python -I -m clio_agent.server_port``, which prints only the port on one
line and reads nothing from the working directory (``branding/clio/brand.json``
``backend.portConfig`` names the key, file and variable). ``docs/ENVIRONMENT.md``
documents the full contract.

This module stays import-light: the launcher runs it before every command.
"""

from __future__ import annotations

import os

from clio_agent import conf

DEFAULT_PORT = 17800
PORT_KEY = "gact.port"
PORT_ENV = "CLIO_PORT"
_CLAIMED: int | None = None


def configured_port() -> int:
    """The port a CLIO server listens on and its clients attach to, unless told otherwise."""
    port = conf.resolve(
        "gact.port", env="CLIO_PORT", default=DEFAULT_PORT, cast=conf.as_int, user_only=True
    )
    if not 1 <= port <= 65535:
        raise ValueError(f"{PORT_KEY} (or {PORT_ENV}) must be a TCP port, got {port}")
    return port


def claim_api_port(port: int | None) -> int:
    """Resolve the API port for a server in this process and keep it from clio-core.

    ``CLIO_PORT`` names this HTTP API, but clio-core reads the same generic name for its
    RPC port, both in this process (the client) and in the daemon it spawns. Once the API
    port is resolved it must not leak there (the ``clio`` launcher unsets it likewise).
    """
    global _CLAIMED
    resolved = configured_port() if port is None else port
    os.environ.pop(PORT_ENV, None)
    _CLAIMED = resolved
    return resolved


def claimed_port() -> int | None:
    """The API port this process's server claimed, or ``None`` before one did."""
    return _CLAIMED


def main() -> None:
    print(configured_port())


if __name__ == "__main__":
    main()
