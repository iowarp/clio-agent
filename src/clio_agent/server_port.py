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


def claim_api_port(port: int | None) -> int:
    """Resolve the API port for a server in this process and keep it from clio-core.

    ``CLIO_PORT`` names this HTTP API, but clio-core reads the same generic name for its
    RPC port, both in this process (the client) and in the daemon it spawns. Once the API
    port is resolved it must not leak there (the ``clio`` launcher unsets it likewise).
    """
    import os  # noqa: PLC0415 - keep the launcher's per-command import light

    resolved = configured_port() if port is None else port
    os.environ.pop(PORT_ENV, None)
    return resolved


def main() -> None:
    print(configured_port())


if __name__ == "__main__":
    main()
