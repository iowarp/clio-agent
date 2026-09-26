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

from __future__ import annotations

from clio_agent.gact.desktop_boot import start_desktop_boot_heartbeat
from clio_agent.gact.runtime_bytecode_repair import repair_bundled_runtime_bytecode
from clio_agent.runtime.console_encoding import ensure_utf8_console

if __name__ == "__main__":
    # First: the sidecar's stdout/stderr are pipes opened in the locale code
    # page on Windows; every later print/log line must encode as UTF-8.
    ensure_utf8_console()
    start_desktop_boot_heartbeat()
    # Before ANY dependency import: an in-place-upgraded bundled runtime can
    # still hold the previous release's unchecked bytecode.
    repair_bundled_runtime_bytecode()
    from clio_agent.gact.app import main  # noqa: PLC0415 - entry-point-only import

    main()
