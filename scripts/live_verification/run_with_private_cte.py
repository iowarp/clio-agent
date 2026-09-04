"""Run a live-verification leg under a PRIVATE, REAL clio-core CTE daemon.

Why this exists (found live 2026-09-03, leg C2): the leg scripts boot their
gact ``run_server`` from the ambient shell env, so ARC resolves the USER-level
CTE config (``AppData/Local/clio-agent/cte/cte.yaml``) — whose 50GB file-tier
allocation cannot preflight on this box (27.5GB free on C:, no pre-allocated
``storage.bin``). The server then degrades LOUDLY to ``LocalFSStore``
(``clio_core_file_capacity_unavailable``) — typed and surfaced exactly as
designed, but a live GATE must hold the real CTE daemon (owner doctrine:
ARC-local is unit-test-only, never gate evidence).

The pytest real-case suite is immune because ``tests/conftest.py`` builds a
session-private real daemon via :mod:`tests._cte_isolation` (512MB file tier,
reserved port block, hermetic ``chi_*_segment_${{USER}}`` shm namespace). This
wrapper reuses that exact machinery for the standalone leg scripts:

1. write the private config + set the isolation env quintet
   (``CLIO_RUNTIME_STATE_DIR``/``CLIO_ARC_STORE_CONFIG``/``CLIO_SERVER_CONF``/
   ``CLIO_CORE_PORT``/``USER``) in THIS process's env,
2. eagerly spawn+attach the private daemon and **hard-fail (exit 2) if it does
   not come up** — the wrapper's whole point is that the leg never runs
   degraded,
3. exec the leg command as a child (it inherits the env, so the gact
   ``run_server`` subprocess composes the same private tiers),
4. deterministically release this process's client, then reap the daemon.

The isolation root (config + 512MB ``storage.bin`` + daemon logs) is kept
under ``out/live-verification/`` as run evidence, NOT auto-deleted.

Usage::

    uv run python scripts/live_verification/run_with_private_cte.py \
        scripts/live_verification/leg_bd_stress.py --provider claude_code --model sonnet
"""

from __future__ import annotations

import datetime as _dt
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))  # tests.* is an importable package from the repo root

from tests._cte_isolation import (  # noqa: E402
    cte_isolation_available,
    eagerly_attach_private_daemon,
    isolate_cte_env,
    reap_private_daemon,
)
from tests._process_hygiene import release_this_process_client  # noqa: E402


def main(argv: list[str]) -> int:
    """Isolate, attach, run ``argv`` as a child, release, reap."""
    if not argv:
        print("usage: run_with_private_cte.py <leg-script.py> [leg args ...]", file=sys.stderr)
        return 2
    if not cte_isolation_available():
        print(
            "FATAL: clio-core CTE bindings/launcher unavailable on this box; "
            "a real-CTE gate run is impossible (do NOT fall back to ARC-local).",
            file=sys.stderr,
        )
        return 2

    stamp = _dt.datetime.now(tz=_dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    root = REPO_ROOT / "out" / "live-verification" / f"cte-private-{stamp}"
    root.mkdir(parents=True, exist_ok=True)
    isolation = isolate_cte_env(root, os.environ)
    print(f"[private-cte] root={root} port={isolation.port} user={os.environ['USER']}")

    try:
        if not eagerly_attach_private_daemon():
            print(
                "FATAL: private clio-core daemon did not come up (see the ARC "
                "init-degradation log above); refusing to run the leg degraded.",
                file=sys.stderr,
            )
            return 2
        print("[private-cte] real clio-core backend attached; launching leg")
        child = [sys.executable, *argv]
        return subprocess.call(child, cwd=str(REPO_ROOT), env=os.environ.copy())
    finally:
        release_this_process_client()
        reap_private_daemon(isolation.state_dir)
        print(f"[private-cte] released client + reaped daemon (evidence kept at {root})")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
