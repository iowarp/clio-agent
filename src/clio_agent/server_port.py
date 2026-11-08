"""The CLIO server's default port: one key in CLIO's configuration that every surface reads.

The key is ``gact.port`` in CLIO's layered configuration (:mod:`clio_agent.conf`): the
workspace then user ``config.yaml`` (on macOS ``~/Library/Application Support/clio-agent/
config/config.yaml``), then the ``CLIO_PORT`` environment variable, then 17800. The
server entry points use it when no ``--port`` is given, the ``clio`` launcher asks for
it (``python -m clio_agent.server_port``), and the Desktop and web UI read the same file
(``branding/clio/brand.json`` ``backend.portConfig`` names the key and variable).

This module stays import-light: the launcher runs it before every command.
"""

from __future__ import annotations

from clio_agent import conf

DEFAULT_PORT = 17800
PORT_KEY = "gact.port"
PORT_ENV = "CLIO_PORT"


def configured_port() -> int:
    """The port a CLIO server listens on and its clients attach to, unless told otherwise."""
    port = conf.resolve("gact.port", env="CLIO_PORT", default=DEFAULT_PORT, cast=conf.as_int)
    if not 1 <= port <= 65535:
        raise ValueError(f"{PORT_KEY} (or {PORT_ENV}) must be a TCP port, got {port}")
    return port


def main() -> None:
    print(configured_port())


if __name__ == "__main__":
    main()
