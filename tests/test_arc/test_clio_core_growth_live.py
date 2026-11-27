"""A capacity target added at runtime is accepted by the real daemon (iowarp-core 2.5).

2.5.0 resolves an unlabelled ``AsyncRegisterTarget`` only against the devices the
daemon loaded at start, so the extra file CLIO adds when the store is full counted as
volatile and the persistent core refused it (rc 16). ``clio_core_growth._register``
now labels it with the planned tier's level; this drives it against a private daemon.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytest.importorskip("clio_cte_core_ext")

_CHILD = textwrap.dedent(
    """
    import os, sys
    from pathlib import Path
    import yaml
    sys.path.insert(0, os.environ["REPO_ROOT"])
    from tests._cte_isolation import isolate_cte_env
    isolate_cte_env(Path(os.environ["PROBE_ROOT"]), os.environ)
    from clio_agent.arc import clio_core_growth, storage
    from clio_agent.arc.runtime_stop import stop_runtime_daemon
    store = storage.make_arc_store(backend="cte", namespace="growth-live")
    config = Path(store._config_path)
    document = yaml.safe_load(config.read_text(encoding="utf-8"))
    tiers = document["compose"][1]["storage"]
    extra = config.parent / "storage-extra-001.bin"
    tiers.append({**tiers[0], "path": extra.as_posix(), "capacity_limit": "16777216B"})
    plan = {
        "index": len(tiers) - 1,
        "path": extra.as_posix(),
        "capacity": 16 << 20,
        "config_text": yaml.safe_dump(document, sort_keys=False),
    }
    try:
        clio_core_growth._register(store, plan)
        print("REGISTERED")
    finally:
        stop_runtime_daemon(store._config_path, "error")
    """
)


def test_a_runtime_capacity_target_is_accepted(tmp_path: Path) -> None:
    env = {
        **os.environ,
        "REPO_ROOT": str(Path(__file__).resolve().parents[2]),
        "PROBE_ROOT": str(tmp_path / "clio-core"),
    }
    done = subprocess.run(
        [sys.executable, "-c", _CHILD],
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-3000:]
    assert "REGISTERED" in done.stdout
