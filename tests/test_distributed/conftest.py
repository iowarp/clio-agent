"""Cross-process test harness: a real ``clio_run`` daemon + real worker processes.

These tests share a clio-core runtime across SEPARATE OS processes — the only way to
catch the multi-process bugs (process death, reclaim, claim races, daemon failure)
that single-event-loop tests structurally cannot. Gated by ``CLIO_RUN_CROSS_PROCESS=1``
(they spawn the daemon + subprocesses). The daemon itself is CPU/shared-memory — no
local GPU; real-trace tests add ``CLIO_RUN_LIVE=1`` + ALCF.
"""
from __future__ import annotations

import contextlib
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

CROSS = os.environ.get("CLIO_RUN_CROSS_PROCESS") == "1"
_WORKER_ENTRY = Path(__file__).resolve().parent / "_worker_entry.py"


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "cross_process: real multi-process tests over a clio_run daemon"
    )


def _iowarp_lib_bin() -> tuple[Path, Path]:
    import iowarp_core  # noqa: PLC0415

    base = Path(iowarp_core.__file__).resolve().parent
    return base / "lib", base / "bin"


def _daemon_env() -> dict[str, str]:
    lib, bind = _iowarp_lib_bin()
    env = dict(os.environ)
    env["LD_LIBRARY_PATH"] = f"{lib}:{bind}:" + env.get("LD_LIBRARY_PATH", "")
    env["CTP_LOG_LEVEL"] = "error"
    return env


def _port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


@pytest.fixture(scope="session")
def clio_daemon():
    """Start one shared clio_run daemon for the session; tear it down after."""
    if not CROSS:
        pytest.skip("cross-process test: set CLIO_RUN_CROSS_PROCESS=1")
    env = _daemon_env()
    _, bind = _iowarp_lib_bin()
    runbin = str(bind / "clio_run")
    port = int(os.environ.get("CLIO_CTE_DAEMON_PORT", "9413"))

    # Clear any stale daemon first (a leftover one trips "g_admin already exists").
    with contextlib.suppress(Exception):
        subprocess.run([runbin, "stop"], env=env, capture_output=True, timeout=30)

    proc = subprocess.Popen(
        [runbin, "start"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if _port_open("127.0.0.1", port):
            break
        if proc.poll() is not None:
            raise RuntimeError(f"clio_run daemon exited (code {proc.returncode}) during startup")
        time.sleep(0.2)
    else:
        proc.kill()
        raise RuntimeError("clio_run daemon did not become ready within 30s")

    yield {"host": "127.0.0.1", "port": port, "env": env, "runbin": runbin}

    with contextlib.suppress(Exception):
        subprocess.run([runbin, "stop"], env=env, capture_output=True, timeout=30)
    with contextlib.suppress(Exception):
        proc.terminate()
        proc.wait(timeout=10)
    with contextlib.suppress(Exception):
        proc.kill()


@pytest.fixture
def cross_arc(clio_daemon):
    """A clio-core store in the TEST process, attached to the shared daemon — the
    parent side of a cross-process delegation."""
    os.environ["CLIO_CTE_WITH_RUNTIME"] = "0"
    from clio_agent.arc.storage import make_arc_store  # noqa: PLC0415

    return make_arc_store(backend="cte")


@pytest.fixture
def spawn_worker(clio_daemon, cross_arc):
    """Spawn ``n`` real worker processes attached to the daemon; wait until each
    signals readiness via a clio-core marker. Returns the list of Popen handles."""
    procs: list[subprocess.Popen] = []

    def _spawn(prefix: str, mode: str = "echo", n: int = 1, extra_env: dict | None = None):
        env = dict(os.environ)
        env["CLIO_CTE_WITH_RUNTIME"] = "0"
        if extra_env:
            env.update(extra_env)
        # baseline so multiple spawn calls with the same prefix each wait for THEIR n
        baseline = sum(1 for _ in cross_arc.scan("context", f"{prefix}READY_"))
        launched = []
        for _ in range(n):
            p = subprocess.Popen(
                [sys.executable, str(_WORKER_ENTRY), prefix, mode],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            procs.append(p)
            launched.append(p)
        # wait for n NEW READY markers in the shared store
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            ready = sum(1 for nm, _ in cross_arc.scan("context", f"{prefix}READY_"))
            if ready >= baseline + n:
                return launched
            for p in launched:
                if p.poll() is not None:
                    out = p.stdout.read() if p.stdout else ""
                    raise RuntimeError(f"worker exited early (code {p.returncode}):\n{out}")
            time.sleep(0.2)
        raise RuntimeError(f"workers did not all become ready ({mode}, n={n})")

    yield _spawn

    for p in procs:
        with contextlib.suppress(Exception):
            p.kill()
