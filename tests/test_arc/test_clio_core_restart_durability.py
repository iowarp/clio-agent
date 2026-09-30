"""clio-core keeps ARC records across a daemon restart (the portability foundation).

Two child processes on one isolated clio-core config (own ports, own storage, the durable
file tier the harness declares): the first writes a record and stops the daemon cleanly,
the second starts a NEW daemon on the same config and reads the record back. Without a
durable tier the second read is ``None`` (verified against iowarp-core 2.2.1).
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
    sys.path.insert(0, os.environ["REPO_ROOT"])
    from tests._cte_isolation import isolate_cte_env
    isolate_cte_env(Path(os.environ["PROBE_ROOT"]), os.environ)
    from clio_agent.arc import storage
    from clio_agent.arc.runtime_stop import stop_runtime_daemon
    store = storage.make_arc_store(backend="cte", namespace="restart-durability")
    if sys.argv[1] == "write":
        store.put("segments", "s__agent", b"needle=42" * 1000)
        assert store.get("segments", "s__agent") == b"needle=42" * 1000
        outcome = stop_runtime_daemon(store._config_path, "error")
        print("STOPPED", outcome.stopped, outcome.path)
    else:
        got = store.get("segments", "s__agent")
        print("READ", None if got is None else len(got))
        stop_runtime_daemon(store._config_path, "error")
    """
)


def _run(phase: str, root: Path) -> str:
    env = {
        **os.environ,
        "REPO_ROOT": str(Path(__file__).resolve().parents[2]),
        "PROBE_ROOT": str(root),
    }
    done = subprocess.run(
        [sys.executable, "-c", _CHILD, phase],
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-3000:]
    return done.stdout


def test_a_record_survives_a_clean_daemon_restart(tmp_path: Path) -> None:
    root = tmp_path / "clio-core"

    assert "STOPPED True clean_stop" in _run("write", root)
    assert "READ 9000" in _run("read", root)
