"""Module entry point: ``python -m clio_agent.gact``.

The bundled desktop runtime invokes the GACT server this way (via the
generic ``runtime.json`` manifest, iowarp/gact-tui#311) because
console-script shims embed absolute build-host paths and break when the
runtime is relocated into an installer (#909). ``-m`` needs only a
working interpreter + site-packages, which the portable runtime ships.

Same CLI as ``clio-agent serve`` / the ``clio-agent-gact`` console
script: ``--host``, ``--port``, ``--reload``, ``--no-agent``, ``--cwd``.

The ``app`` import is lazy (inside the guard) per the gact decomposition
no-cycle invariant: siblings never import ``clio_agent.gact.app`` at
module load (``tests/test_gact/test_decomposition_guardrails.py``).
"""

if __name__ == "__main__":
    from clio_agent.gact.app import main  # noqa: PLC0415 - entry-point-only import

    main()
