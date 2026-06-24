"""Real-cases tier: live, end-to-end CLIO blueprint case tests.

This tier sits a level above unit and integration. A real case runs an actual
CLIO session through a marketplace Agent Blueprint against a live provider, then
asserts on the normalized trace. These tests are slow and hit external services,
so they are skipped unless ``CLIO_RUN_LIVE=1`` is set.

Provider/model are not hardcoded: the SUT discovers cells from the live provider
registry (or ``CLIO_AGENTTEST_CELLS``); pin one with ``pytest --provider/--model``
or fan out with ``pytest --matrix``.

Reproducible harness (replaces the throwaway ``/tmp`` grind shell): the gact
server lifecycle is the ``gact_server`` fixture below (start CLEAN before every
cell, kill after), and the per-model LM bind config lives in
``clio_sut.MODEL_PROFILES``. So a live grind is one committed ``pytest`` command,
not a shell incantation that gets wiped — the exact run semantics are in git.

Run one San Diego positive cell on qwopus, for example::

    CLIO_RUN_LIVE=1 uv run pytest \
      tests/test_real_cases/test_earthscope_case.py::test_earthscope_gnss_region \
      -k sandiego_1 --provider lm_studio --model qwopus3.5-9b-v3 \
      -o addopts="" -p no:cacheprovider -q
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generator

import httpx
import pytest

# The real-cases tier drives live provider runs through the `agent-test` harness,
# an unpublished local checkout that isn't installed in CI (and the tier needs a
# live provider anyway). Skip the whole directory cleanly when it's absent so
# pytest collection doesn't error — locally, where agent-test is installed, the
# tier collects and runs as before.
pytest.importorskip("agent_test")

from . import clio_sut  # noqa: F401,E402  — subclassing SUT registers it for agent-test

# --------------------------------------------------------------------------- #
# Committed server-harness config (was /tmp shell env). Each is env-overridable
# for a different box, but the committed defaults make a run reproducible here.
# --------------------------------------------------------------------------- #
REPO_ROOT = Path(__file__).resolve().parents[2]
# Dedicated port for the fixture-managed server, distinct from any ad-hoc server.
GACT_PORT = int(os.environ.get("CLIO_GACT_FIXTURE_PORT", "17960"))
GACT_URL = f"http://127.0.0.1:{GACT_PORT}"
KIT_PATH = os.environ.get("CLIO_KIT_PATH", str(Path.home() / "clio-kit"))
ALLOWED_ROOTS = os.environ.get("CLIO_ALLOWED_ROOTS", f"{Path.home()}:/tmp")
# Durable (NOT /tmp) home for per-cell server logs + full semantic traces, so a
# run stays inspectable/reproducible later. Gitignored (run evidence, not source).
GRIND_ROOT = Path(os.environ.get("CLIO_GRIND_ROOT", str(REPO_ROOT / ".grind")))
SERVER_HEALTH_TIMEOUT_S = float(os.environ.get("CLIO_GACT_HEALTH_TIMEOUT_S", "180"))


def pytest_collection_modifyitems(config, items) -> None:
    """Skip live real-case tests unless explicitly enabled."""
    if os.environ.get("CLIO_RUN_LIVE"):
        return
    skip_live = pytest.mark.skip(reason="real-case live test; set CLIO_RUN_LIVE=1 to run")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)


# --------------------------------------------------------------------------- #
# gact server lifecycle (init/cleanup) — replaces grind_matrix.sh start/kill
# --------------------------------------------------------------------------- #
@dataclass
class GactServer:
    """A live gact server managed by the ``gact_server`` fixture."""

    url: str
    port: int
    trace_dir: Path
    server_log: Path
    process: subprocess.Popen[bytes]


def _kill_port(port: int) -> None:
    """Kill whatever process is listening on ``port`` (best-effort).

    Mirrors the shell harness's ``kill_server``: a prior cell's provider_timeout
    can leave executor work running server-side, which poisons the next bind, so
    every cell starts from a guaranteed-clean port.
    """
    try:
        out = subprocess.run(["ss", "-ltnp"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return
    for line in out.splitlines():
        if f":{port} " not in line and f":{port}\t" not in line:
            continue
        marker = "pid="
        idx = line.find(marker)
        if idx < 0:
            continue
        pid_str = ""
        for ch in line[idx + len(marker) :]:
            if ch.isdigit():
                pid_str += ch
            else:
                break
        if pid_str:
            try:
                os.kill(int(pid_str), 9)
            except (ProcessLookupError, PermissionError):
                pass
    time.sleep(2)


def _reap_process_group(process: "subprocess.Popen[bytes]", *, timeout: float = 10.0) -> None:
    """Terminate the server AND every process it spawned (its MCP stdio children).

    The server is launched with ``start_new_session=True`` so it leads its own
    process group; signalling the whole group (``killpg`` on the negative PID)
    reaps the uvx/MCP subprocess children too, instead of orphaning them to init.
    Plain ``process.terminate()`` signalled only the top PID and leaked the MCP
    children across cells — the recurring gact/MCP process pile-up. SIGTERM first
    (graceful), then SIGKILL if it doesn't exit; falls back to a per-process
    signal if the group is already gone.
    """
    if process.poll() is not None:
        return
    try:
        pgid = os.getpgid(process.pid)
    except ProcessLookupError:
        return
    for sig, wait_s in ((signal.SIGTERM, timeout), (signal.SIGKILL, 5.0)):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=wait_s)
            return
        except subprocess.TimeoutExpired:
            continue


def _wait_healthy(url: str, deadline: float) -> bool:
    """Poll ``GET /v1/health`` until the server answers or ``deadline`` passes."""
    while time.monotonic() < deadline:
        try:
            with httpx.Client(timeout=3.0) as http:
                http.get(f"{url}/v1/health")
            return True
        except (httpx.HTTPError, OSError):
            time.sleep(2)
    return False


def _slug(name: str) -> str:
    """Filesystem-safe slug from a pytest node name (keeps the cell id readable)."""
    return "".join(ch if ch.isalnum() or ch in "-._" else "_" for ch in name).strip("_")


@pytest.fixture
def gact_server(request: pytest.FixtureRequest) -> Generator[GactServer, None, None]:
    """Start a CLEAN gact server for this cell and tear it down afterwards.

    Per-cell isolation (function scope): the server is killed and restarted fresh
    before every test, each with its own durable semantic-trace dir, so one hung
    tool / timed-out run can never cascade into the next cell. This is the pytest
    home for what ``grind_matrix.sh`` did in shell — committed and reproducible.
    """
    if "uv" not in (shutil.which("uv") or ""):
        pytest.skip("uv not on PATH; cannot launch the gact server")

    slug = _slug(request.node.name)
    trace_dir = GRIND_ROOT / "traces" / slug
    server_log = GRIND_ROOT / "logs" / f"{slug}.gact.log"
    if trace_dir.exists():
        shutil.rmtree(trace_dir, ignore_errors=True)
    trace_dir.mkdir(parents=True, exist_ok=True)
    server_log.parent.mkdir(parents=True, exist_ok=True)

    _kill_port(GACT_PORT)

    env = {
        **os.environ,
        "CLIO_DEBUG": os.environ.get("CLIO_DEBUG", "med"),
        "CLIO_KIT_PATH": KIT_PATH,
        "CLIO_ALLOWED_ROOTS": ALLOWED_ROOTS,
        # Single canonical recorder: full, live, durable semantic trace per cell.
        "CLIO_SEMANTIC_TRACE_BACKEND": "file",
        "CLIO_SEMANTIC_TRACE_PATH": str(trace_dir),
    }
    # CRITICAL: the repo-root autouse fixture (tests/conftest.py) injects
    # UNIT-test-isolation env into os.environ, which a live grind must NOT inherit
    # — the live server has to run like real CLIO (production runtime), not a unit
    # test. Strip each:
    #   - XDG_CONFIG_HOME: points the server at a throwaway tmp config root, so it
    #     can't see the user's installed blueprint -> assignment 404s "agent
    #     blueprint not found" (TRAP 1/5).
    #   - CLIO_AGENT_ENABLE_LEGACY_NATIVE_EXPERTS: switches off the Agent Blueprint
    #     runtime (app.py _agent_definition_uses_blueprint_runtime), so the
    #     orchestrator runs as a legacy USER agent (route_source=user_agent) that
    #     never settles its expert_handoffs -> the turn ends after main's first
    #     call with no delegation (steps=[], end_turn). THIS is the agent-driven
    #     routing path the grind exists to exercise.
    #   - CLIO_LM_MODEL: a unit-test default model; the bind PUT sets the real one.
    for _k in (
        "XDG_CONFIG_HOME",
        "CLIO_AGENT_ENABLE_LEGACY_NATIVE_EXPERTS",
        "CLIO_LM_MODEL",
    ):
        env.pop(_k, None)
    log_fh = server_log.open("wb")
    process = subprocess.Popen(
        ["uv", "run", "clio-agent-gact", "--host", "127.0.0.1", "--port", str(GACT_PORT)],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        # Own session/process group so teardown can group-kill the server AND every
        # MCP stdio child it spawns (uvx geo/pandas/plot/ndp). Without this, teardown
        # signalled only the top PID and orphaned the MCP children to init — the
        # cross-cell process leak that piled up gact + MCP servers on the box.
        start_new_session=True,
    )

    # The SUT connects to CLIO_GACT_URL; point it at this fixture's server.
    prev_url = os.environ.get("CLIO_GACT_URL")
    os.environ["CLIO_GACT_URL"] = GACT_URL
    clio_sut.DEFAULT_BASE_URL = GACT_URL  # module constant read at SUT __init__

    healthy = _wait_healthy(GACT_URL, time.monotonic() + SERVER_HEALTH_TIMEOUT_S)
    if not healthy:
        _reap_process_group(process)
        log_fh.close()
        _kill_port(GACT_PORT)
        if prev_url is None:
            os.environ.pop("CLIO_GACT_URL", None)
        else:
            os.environ["CLIO_GACT_URL"] = prev_url
        pytest.fail(
            f"gact server on :{GACT_PORT} did not become healthy in "
            f"{SERVER_HEALTH_TIMEOUT_S:g}s (see {server_log})"
        )

    try:
        yield GactServer(
            url=GACT_URL,
            port=GACT_PORT,
            trace_dir=trace_dir,
            server_log=server_log,
            process=process,
        )
    finally:
        # Kill the server AND its MCP child process group so neither lingering
        # executor work nor an orphaned uvx/MCP subprocess bleeds into the next
        # cell's bind (or piles up on the box).
        _reap_process_group(process)
        log_fh.close()
        _kill_port(GACT_PORT)
        if prev_url is None:
            os.environ.pop("CLIO_GACT_URL", None)
        else:
            os.environ["CLIO_GACT_URL"] = prev_url


# --------------------------------------------------------------------------- #
# agent fixture override: bind only AFTER the fixture server is healthy
# --------------------------------------------------------------------------- #
@pytest.fixture
def agent(request: pytest.FixtureRequest, gact_server: GactServer) -> Any:
    """Same contract as ``agent_test``'s ``agent`` fixture, but depends on
    ``gact_server`` so the live server is guaranteed up (and the SUT's base URL
    repointed) before the model binds. Cell + overrides come from the plugin's
    indirect parametrization / ``--override`` exactly as upstream.
    """
    from agent_test.config import resolve_active_sut
    from agent_test.plugin import CELL_KEY, _parse_overrides

    provider, model = request.param
    config = request.config
    sut = resolve_active_sut(config)
    request.node.stash[CELL_KEY] = (provider, model)
    if not sut.available(provider, model):
        pytest.skip(f"{provider}/{model} not available here")
    overrides = _parse_overrides(config)
    return sut.bind(provider, model, overrides=overrides)
