"""Module entry point: ``python -m clio_agent.gact``.

The bundled desktop runtime invokes the GACT server this way (via the
generic ``runtime.json`` manifest, iowarp/gact-tui#311) because
console-script shims embed absolute build-host paths and break when the
runtime is relocated into an installer (#909). ``-m`` needs only a
working interpreter + site-packages, which the portable runtime ships.

Same CLI as ``clio-agent serve`` / the ``clio-agent-gact`` console
script: ``--host``, ``--port``, ``--reload``, ``--no-agent``, ``--cwd``.
"""

from clio_agent.gact.app import main

if __name__ == "__main__":
    main()
