"""
CLIO Agent Test Fixtures

Provides shared fixtures for all test modules, including synthetic
HDF5 and Parquet test data for MCP server testing.
"""

import contextlib
import os
import tempfile
from pathlib import Path

import pytest
import yaml

import clio_agent  # noqa: F401
from tests._cte_isolation import (
    cte_isolation_available,
    eagerly_attach_private_daemon,
    isolate_cte_env,
    reap_private_daemon,
    remove_private_cte_root,
)
from tests._process_hygiene import (
    SKIP_ENV,
    ProcessHygieneAudit,
    child_snapshot,
    daemon_pid,
    release_this_process_client,
)


@pytest.fixture
def floor_sandbox(monkeypatch):
    """Pin the OS write-confinement backend to the honest FLOOR for a passthrough test (#976 B2).

    Environment-conformance (house rule: *isolated tests never see the real box config — vary
    CONFIG, not ambient env*). On a Landlock-capable Linux host the confinement ladder ACTIVATES
    for real, so :func:`clio_agent.runtime.sandbox.wrap_confined` prepends the fence shim and
    every passthrough / argv-pinning assertion breaks (``'<venv>/bin/python' == 'mytool'``,
    ``'landlock' == 'none'``). This fixture pins ``sandbox._STATE`` to a floor
    :class:`~clio_agent.runtime.sandbox.SandboxResult` (mechanism none, active False, typed
    reason) AND neutralizes ``install_sandbox`` / ``_resolve_backend`` so a mid-test re-resolve
    can never reactivate.

    Activation tests (the B2 ladder matrix, which inject explicit probes / ``state=``) must NOT
    use this fixture — they pin their own state and would be masked by it.
    """
    import sys as _sys

    from clio_agent.runtime import sandbox as _sandbox

    floor = _sandbox.SandboxResult(
        mechanism=_sandbox.MECHANISM_NONE,
        active=False,
        reason=_sandbox.REASON_NOT_INSTALLED,
        details={"platform": _sys.platform},
    )
    monkeypatch.setattr(_sandbox, "_STATE", floor)
    monkeypatch.setattr(_sandbox, "_resolve_backend", lambda *a, **k: floor)

    def _floor_install(*_a, **_k):
        _sandbox._STATE = floor
        return floor

    monkeypatch.setattr(_sandbox, "install_sandbox", _floor_install)
    return floor


@pytest.fixture(scope="session", autouse=True)
def _clio_private_cte_daemon():
    """Point this suite run's cte-leg tests at a PRIVATE clio-core daemon.

    The host-shared daemon (port 9413) serves live CLIO servers; suites attaching to it
    both accrete state into it (the 12.3 GiB daemon) and flake on cross-instance writes
    (BM25 corpus perturbation, size-then-read truncation). This fixture redirects the
    runtime coordination state (``CLIO_RUNTIME_STATE_DIR``) and the CTE config/port to a
    session-private topology BEFORE any test can attach, so the suite's daemon is its
    own: spawned lazily by the first cte attach, stopped by the session-end release
    (last one out), force-reaped here as belt-and-suspenders. A binding- or launcher-free
    environment leaves the env untouched (the cte legs skip on their own importorskip).
    See :mod:`tests._cte_isolation`.
    """
    if not cte_isolation_available():
        yield None
        return
    isolation_root = Path(tempfile.mkdtemp(prefix=f"clio-agent-cte-{os.getpid()}-"))
    isolation = isolate_cte_env(isolation_root, os.environ)
    try:
        # Eager spawn+attach: boot the private daemon deterministically at session
        # start (not mid-suite under load) and hold a client so it stays up all
        # session. A failure is already recorded loudly by the init-degradation path.
        if not eagerly_attach_private_daemon():
            import warnings  # noqa: PLC0415

            warnings.warn(
                "private clio-core daemon failed to come up at session start; "
                "cte-leg tests will run degraded (see the ARC init-degradation log)",
                stacklevel=1,
            )
        yield isolation
    finally:
        reap_private_daemon(isolation.state_dir)
        remove_private_cte_root(isolation.root)


@pytest.fixture(scope="session", autouse=True)
def _clio_process_hygiene_audit(request, _clio_private_cte_daemon):
    """Guarantee the suite releases every clio-core client + helper child it attaches.

    Tests that build ``make_arc_store(backend="cte")`` attach a clio-core client to the
    suite's private daemon (see ``_clio_private_cte_daemon``); a hard-killed run skips
    the ``atexit`` release and leaves a ghost registration whose daemon accretes state
    (the 12.3 GiB the owner observed on the previously-shared daemon). This session
    fixture (a) snapshots the client registry + this process's child tree at session
    start, (b) deterministically releases THIS process's client at session end (not
    relying on ``atexit``) — which, as the last client out, also stops the private
    daemon — and (c) FAILS the run — naming culprit tests — if a client this process
    family registered died without deregistering, or a helper child is left running.
    See :mod:`tests._process_hygiene`. Opt out with ``CLIO_TEST_SKIP_CLIENT_AUDIT=1``
    (emergencies only). Depends on the isolation fixture so setup/teardown order is
    pinned: env first, audit second; audit (release/stop) first, daemon reap last.
    """
    audit = ProcessHygieneAudit(
        root_pid=os.getpid(),
        _snapshot_children=lambda root: child_snapshot(root, exclude_subtree=daemon_pid()),
    )
    request.session._clio_hygiene_audit = audit
    yield
    # Deterministic release first, THEN audit what remains.
    release_this_process_client()
    # Reap the PRIVATE daemon before auditing: an eager attach that fails
    # mid-spawn (a port race under xdist workers) leaves a clio_run child with
    # ZERO registered clients and NO pidfile — last-one-out never stops it and
    # the snapshot's daemon_pid() exclusion never saw it, so the audit would
    # flag our own session-scoped daemon as a leak (the #912 flake-hunt hit).
    # It is ours by construction (private state dir); killing it here is always
    # safe, and the daemon fixture's later reap becomes a no-op.
    if _clio_private_cte_daemon is not None:
        reap_private_daemon(_clio_private_cte_daemon.state_dir)
    # Census sweep: a spawn that failed BEFORE writing its pidfile leaves a
    # clio_run child the pidfile reap above cannot see (the #913 flake-hunt
    # recurrence). With the isolation env active every clio_run child of this
    # process family is OURS (private state dir), so terminate residuals and
    # WARN — daemon lifecycle is this fixture's responsibility, not a test
    # leak; the audit below still fails on any OTHER leaked resource.
    if _clio_private_cte_daemon is not None:
        import psutil  # noqa: PLC0415

        try:
            for child in psutil.Process(os.getpid()).children(recursive=True):
                try:
                    if "clio_run" in (child.name() or ""):
                        import warnings  # noqa: PLC0415

                        warnings.warn(
                            f"reaping orphan private clio_run pid={child.pid} "
                            "(spawn failed before pidfile write)",
                            stacklevel=1,
                        )
                        child.terminate()
                        child.wait(timeout=5.0)
                except (psutil.NoSuchProcess, psutil.TimeoutExpired):
                    with contextlib.suppress(psutil.NoSuchProcess):
                        child.kill()
        except psutil.NoSuchProcess:
            pass
    if os.environ.get(SKIP_ENV):
        return
    result = audit.finalize(own_pid=os.getpid())
    if not result.clean:
        raise AssertionError(result.format_failure())


@pytest.fixture(autouse=True)
def _attribute_process_leaks(request):
    """Attribute freshly-appeared clio-core client registrations to the running test.

    Runs a sub-millisecond registry ``listdir`` at each test teardown so the session-end
    audit can name WHICH test introduced a leaked client (only descendants of this pytest
    process are tracked, so a parallel CLIO instance is never blamed).
    """
    yield
    audit = getattr(request.session, "_clio_hygiene_audit", None)
    if audit is not None:
        audit.observe_test(request.node.nodeid)


@pytest.fixture(autouse=True)
def _restore_clio_logger():
    """Snapshot + restore the process-global ``clio_agent`` logger state (suite-wide).

    :func:`clio_agent.runtime.trace.configure` (triggered by any test that boots the CLI /
    serve path) sets ``propagate=False`` and installs a handler on the shared ``clio_agent``
    logger with no teardown. Left leaked into a later-collected test in the same xdist worker,
    that breaks ``caplog``-based assertions (caplog captures via root-logger propagation) — an
    order-dependent isolation flake surfaced when a new test module shifts the distribution.
    Restoring propagate/level/handlers around EVERY test kills the whole class (was scoped to
    tests/test_ui/conftest.py; promoted here so it protects the full suite).
    """
    import logging  # noqa: PLC0415 - only needed by this fixture

    lg = logging.getLogger("clio_agent")
    saved_propagate = lg.propagate
    saved_level = lg.level
    saved_handlers = list(lg.handlers)
    try:
        yield
    finally:
        lg.propagate = saved_propagate
        lg.level = saved_level
        lg.handlers[:] = saved_handlers


@pytest.fixture(autouse=True)
def _reset_runtime_context():
    """Isolate each test from the single GACT runtime contextvar (#714).

    Several tests establish runtime state via tokenless bare sets
    (``set_turn_identity`` / ``set_turn_id`` / ``set_trace_id`` /
    ``install_trajectory_cell`` / ``set_react_context_window``), mirroring the
    turn-scoped leaks of the original contextvars. Snapshot-and-reset the one
    ``_RUNTIME`` var around every test (token-balanced) so those tokenless sets
    cannot bleed into the next test -- the hygiene the original tests achieved
    via explicit per-var token resets, now centralized on the single var.
    """
    from clio_agent.gact import context as ctx

    token = ctx._RUNTIME.set(ctx.RuntimeContext())
    try:
        yield
    finally:
        ctx._RUNTIME.reset(token)


@pytest.fixture(autouse=True)
def _reset_process_arc():
    """Isolate each test from the one per-process ARC singleton (#714).

    ``_set_app_arc`` publishes ``clio_agent.gact.runtime.globals._PROCESS_ARC`` (#714:
    the funnel + ARC singleton now live in ``runtime.globals``, re-exported as a NAME
    from ``gact.app``; the LIVE owner is ``runtime.globals``) so deep/threaded emit
    contexts that cannot reach the request app still route through the SAME ARC (ARC is
    the source of the highway; ``_emit_semantic_event`` fails loud when no ARC is
    reachable). Because it is a module global it persists across tests: a later no-arc
    emit could silently resolve a *prior* test's ARC, making the fail-loud behavior
    order-dependent. Reset it to ``None`` around every test so a test that exercises the
    no-arc path sees a truly absent ARC regardless of run order.
    """
    from clio_agent.gact.runtime import globals as gact_globals

    saved = gact_globals._PROCESS_ARC
    gact_globals._PROCESS_ARC = None
    try:
        yield
    finally:
        gact_globals._PROCESS_ARC = saved


@pytest.fixture(autouse=True)
def allow_pytest_tmp_path(tmp_path, monkeypatch):
    """Isolate tests from developer shell defaults, config-file-first (#985).

    Developer shells often set ``CLIO_ALLOWED_ROOTS`` narrowly for manual use, and
    unit tests must not depend on a live LM Studio server for model discovery. This
    fixture establishes the four test-default knobs so neither leaks in.

    #985 move 3 (+ residual): the autouse *env soup* is fully dissolved. The only
    env this fixture sets is ``XDG_CONFIG_HOME`` (legitimate isolation — it repoints
    the user-config + skill discovery at a per-test tmp dir). All four test-default
    knobs are written to the per-test user ``config.yaml`` — the config-FILE layer
    :mod:`clio_agent.conf` resolves ABOVE the environment:

    - ``agents.disable_default_registry_bootstrap: true`` — no network git clone in
      unit tests; ``_write_test_default_registry_blueprint`` writes the default
      blueprint on disk so discovery still finds it. No env-direct reader.
    - ``tools.file_policy.allowed_roots`` — ``tmp_path`` unioned with any dev-shell
      ``CLIO_ALLOWED_ROOTS`` (the union that previously lived in the env value now
      builds the file list). The stale env var is *deleted* so the file is the sole
      source of truth.
    - ``lm.model: ibm/granite-4-h-tiny`` — a pinned unit-test model that suppresses
      LM-Studio discovery via ``has_explicit_model_override`` (which reads the file
      layer).
    - ``arc.store: local`` — the fast, isolated LocalFS backend (production defaults
      to clio-core; the cte integration tests override via an explicit
      ``backend="cte"`` arg, unaffected).

    Because the file wins over the env, a test that needs to override one of these
    knobs mutates the file layer (``tests._config_layer.set_config`` /
    ``delete_config`` + the reload they perform) rather than ``monkeypatch.setenv``:
    the subdir-restriction allowed_roots tests write a narrower list; the LM
    discovery tests delete ``lm.model``; the ``arc.store=banana`` fail-loud test
    writes ``banana`` into the file. See the #985 residual report.
    """
    from clio_agent import conf  # noqa: PLC0415 - avoid import cost at collection

    xdg_root = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_root))
    # CLIO_USER_DIR is the only override platformdirs honors on EVERY OS —
    # XDG alone is a no-op on Windows, which let route-level writes (the
    # blueprint-sources registry) land in the real %LOCALAPPDATA% (~100 dead
    # pytest fixture rows found 2026-08-13). Point it at the same tree the XDG
    # layout resolves to so both resolution paths agree.
    monkeypatch.setenv("CLIO_USER_DIR", str(xdg_root / "clio-agent"))

    # Union tmp_path with any dev-shell CLIO_ALLOWED_ROOTS, then build the FILE
    # value from it and DELETE the stale env var so the file is authoritative.
    existing = os.environ.get("CLIO_ALLOWED_ROOTS", "")
    allowed_roots = [str(tmp_path)]
    if existing.strip():
        allowed_roots.extend(item for item in existing.split(os.pathsep) if item)
    monkeypatch.delenv("CLIO_ALLOWED_ROOTS", raising=False)

    # #948 S4b: the legacy native-expert runtime (the deleted Tier-1 planner) is
    # gone, and its ``CLIO_AGENT_ENABLE_LEGACY_NATIVE_EXPERTS`` knob is retired, so
    # a default/main session MUST resolve an Agent Blueprint react main to run. The
    # blueprint is written on disk below; keep the network git bootstrap DISABLED.
    config_dir = xdg_root / "clio-agent"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_dir.joinpath("config.yaml").write_text(
        yaml.safe_dump(
            {
                "agents": {"disable_default_registry_bootstrap": True},
                "tools": {"file_policy": {"allowed_roots": allowed_roots}},
                "lm": {"model": "ibm/granite-4-h-tiny"},
                "arc": {"store": "local"},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    # The process-wide store caches its file layer; drop it so this test's
    # freshly-written user config.yaml (and XDG) take effect.
    conf.reload()
    _write_test_default_registry_blueprint(xdg_root)

    # Isolate on-disk skill discovery (gact.skills owns the scanner, #917;
    # catalog.py consumes it). Without this, the repo's OWN .claude/skills (e.g.
    # release-clio, grind-clio-case) and a developer's global skills leak into the
    # expert catalog and shift the deterministic agent list the tests assert.
    # Keep only roots under tmp_path, so discovery tests that create skills under
    # their tmp_path (and chdir there) still work, while ambient repo/home skills
    # are dropped. No cwd change (registry-snapshot tests need the repo cwd).
    import clio_agent.gact.skills as _skills  # noqa: PLC0415

    _orig_skill_roots = _skills._skill_search_roots

    def _isolated_skill_roots(home: Path, cwd: Path) -> list[tuple[Path, str, str]]:
        roots = _orig_skill_roots(home, cwd)
        # Keep roots under tmp_path (per-test skills) AND clio's SHIPPED built-in root
        # (the deterministic ``planning`` entry-skill, P1.5 #1067) — the latter is clio's
        # own surface, not the ambient repo/home leakage this isolation exists to drop.
        return [
            (r, s, scope)
            for (r, s, scope) in roots
            if _path_under(r, tmp_path) or _path_under(r, _skills._BUILTIN_SKILLS_ROOT)
        ]

    monkeypatch.setattr(_skills, "_skill_search_roots", _isolated_skill_roots)


def _path_under(path: Path, base: Path) -> bool:
    try:
        Path(path).resolve().relative_to(Path(base).resolve())
        return True
    except (ValueError, OSError):
        return False


def _write_test_default_registry_blueprint(xdg_root: Path) -> None:
    # Bind the fixture blueprint id to the loader's DEFAULT_AGENT_BLUEPRINT_ID so
    # the two can never drift again: every explicit
    # ``POST /v1/sessions/{sid}/agent-blueprint`` activation test that names
    # DEFAULT_AGENT_BLUEPRINT_ID resolves THIS fixture's rows via
    # load_agent_blueprints(blueprint_id=...), so a mismatch breaks every
    # activated-session agent-catalog/expert-pack test. (Commit 3bf695b changed
    # the constant to "earthscope-gnss-region" for the demo default registry but
    # left this fixture on the old "data-semantics" id.) NOTE: this snapshot is
    # no longer implicitly loaded for a BARE/unactivated session --
    # catalog._builtin_agents() (owner ruling 2026-08-05) always returns just the
    # code-shipped builtin main; only an EXPLICIT activation resolves this
    # fixture's rows.
    from clio_agent.gact.agent_blueprints import DEFAULT_AGENT_BLUEPRINT_ID  # noqa: PLC0415

    root = xdg_root / "clio-agent" / "agent-blueprints" / DEFAULT_AGENT_BLUEPRINT_ID
    experts = root / "experts"
    experts.mkdir(parents=True, exist_ok=True)
    root.joinpath("AGENT.md").write_text(
        f"""---
id: {DEFAULT_AGENT_BLUEPRINT_ID}
version: 0.1.0
title: Data Semantics Agent
description: Test default registry data semantics agent.
root_expert: main
---
Default test registry Agent Blueprint.
""",
        encoding="utf-8",
    )
    rows = {
        "main": """---
id: main
title: Main Agent
description: Tier-1 orchestrator.
tier: 1
module:
  kind: react
specialization: orchestrator
prompt_id: clio.main.planner
---
You are CLIO's agent planner.
""",
        "data": """---
id: data
title: Data Expert
description: Specializes in scientific data files and discovery.
parent_id: main
tier: 2
module:
  kind: react
specialization: data_analysis
keywords:
  - hdf5
  - adios
  - bp5
  - data
tools:
  - hdf5_list_datasets
  - hdf5_analyze_dataset
  - hdf5_check_compression
  - hdf5_optimize_chunking
  - hdf5_analyze_file
  - adios_inspect_file
  - adios_inspect_variables
  - adios_inspect_profiling
prompt_id: clio.expert.data
---
You are the CLIO Data Expert.
""",
        "analysis": """---
id: analysis
title: Analysis Expert
description: Specializes in statistical analysis and data quality.
parent_id: main
tier: 2
module:
  kind: react
specialization: data_analysis
keywords:
  - parquet
  - csv
  - statistics
tools:
  - parquet_analyze_schema
  - parquet_query_data
  - parquet_compute_statistics
  - csv_read_table
prompt_id: clio.expert.analysis
---
You are the CLIO Analysis Expert.
""",
        "visualization": """---
id: visualization
title: Visualization Expert
description: Produces scientific data visualizations.
parent_id: main
tier: 2
specialization: data_visualization
keywords:
  - visualization
  - plot
tools:
  - plot_histogram
  - plot_bar_chart
  - plot_scatter
  - plot_summary
prompt_id: clio.expert.visualization
---
You are the CLIO Visualization Expert.
""",
        "ndp_catalog": """---
id: ndp_catalog
title: NDP Catalog Expert
description: Nested data expert for dataset discovery and staging.
parent_id: data
tier: 3
specialization: knowledge_retrieval
keywords:
  - ndp
  - catalog
tools:
  - ndp_list_organizations
  - ndp_search_datasets
  - ndp_get_dataset_details
  - ndp_stage_resource
prompt_id: clio.expert.data
---
You are the CLIO NDP Catalog Expert.
""",
        "sac_format": """---
id: sac_format
title: SAC Format Expert
description: Nested format expert for SAC waveform archives.
parent_id: analysis
tier: 3
specialization: data_analysis
keywords:
  - sac
  - waveform
tools:
  - sac_inspect_archive
  - sac_discover_earthscope_region_waveform
  - sac_fetch_earthscope_waveform
  - sac_compute_trace_statistics
  - sac_plot_traces
prompt_id: clio.expert.analysis
---
You are the CLIO SAC Format Expert.
""",
        "utility": """---
id: utility
title: Utility Expert
description: Exposes local permission-gated utility tools.
parent_id: main
tier: 2
specialization: utility
keywords:
  - shell
  - bash
  - terminal
  - command
tools:
  - shell_bash
  - fs_propose_edit
prompt_id: clio.chat
---
You are the CLIO Utility Expert.
""",
    }
    for name, text in rows.items():
        experts.joinpath(f"{name}.md").write_text(text, encoding="utf-8")
    root.joinpath(".clio-install.md").write_text(
        """# CLIO Agent Blueprint install metadata

source: git@github.com:JaimeCernuda/clio-agent-marketplace.git
source_kind: git
ref: main
commit: 908e013d68a80b1e13d5e7d633309d1f6813d970
pinned_commit: 908e013d68a80b1e13d5e7d633309d1f6813d970
scope: global
""",
        encoding="utf-8",
    )


@pytest.fixture
def host_agent_executor(monkeypatch):
    """Make a default session's react ``main`` execute the ``build_app`` host fake.

    Opt-in seam (#948 S4b) for turn-engine tests (test_gact / test_sdk / test_ui)
    that hand ``build_app`` a fake agent with a canned-``Prediction`` ``forward``
    and assert the turn produced it. Since the legacy fall-through planner is
    deleted, a default session now resolves the default-registry blueprint react
    ``main``; this fixture routes that root's ONE build seam back to the host
    fake's ``forward`` (see :mod:`tests._harness`). Request it via
    ``pytestmark = pytest.mark.usefixtures("host_agent_executor")`` at module
    scope, or per test.
    """

    from tests._harness import install_host_agent_executor

    install_host_agent_executor(monkeypatch)


@pytest.fixture
def sample_hdf5(tmp_path):
    """Create a synthetic HDF5 file for testing.

    Structure:
        /simulation/temperature  - 100x100 float64, gzip compressed, chunked (10,10)
        /simulation/pressure     - 100x100 float64, not compressed, chunked (25,25)
        /timestamps              - 1000 int64, contiguous (no chunks, no compression)

    Attributes:
        /simulation/temperature.units = "Kelvin"
        /simulation/temperature.description = "Surface temperature"
        /.created_by = "clio-agent-test"
        /.version = "1.0"

    Returns:
        str: Path to the temporary HDF5 file
    """
    import h5py
    import numpy as np

    filepath = tmp_path / "test_data.h5"
    rng = np.random.default_rng(42)  # Deterministic for reproducibility

    with h5py.File(filepath, "w") as f:
        # Group: /simulation
        sim = f.create_group("simulation")

        # Dataset: temperature (100x100 float64, gzip compressed)
        temp = sim.create_dataset(
            "temperature",
            data=rng.standard_normal((100, 100)),
            compression="gzip",
            compression_opts=6,
            chunks=(10, 10),
        )
        temp.attrs["units"] = "Kelvin"
        temp.attrs["description"] = "Surface temperature"

        # Dataset: pressure (100x100 float64, no compression, chunked)
        sim.create_dataset(
            "pressure",
            data=rng.standard_normal((100, 100)),
            chunks=(25, 25),
        )

        # Dataset: timestamps (1000 int64, contiguous)
        f.create_dataset("timestamps", data=np.arange(1000))

        # Root attributes
        f.attrs["created_by"] = "clio-agent-test"
        f.attrs["version"] = "1.0"

    return str(filepath)


@pytest.fixture
def sample_parquet(tmp_path):
    """Create a synthetic Parquet file for testing.

    Structure:
        id          - int64, sequential 0-99
        temperature - float64, random 15.0-35.0
        city        - string, random from ["NYC", "LA", "Chicago", "Houston", "Phoenix"]

    100 rows total.

    Returns:
        str: Path to the temporary Parquet file
    """
    import numpy as np
    import pyarrow as pa
    import pyarrow.parquet as pq

    rng = np.random.default_rng(42)  # Deterministic for reproducibility

    cities = ["NYC", "LA", "Chicago", "Houston", "Phoenix"]
    n_rows = 100

    table = pa.table(
        {
            "id": pa.array(range(n_rows), type=pa.int64()),
            "temperature": pa.array(rng.uniform(15.0, 35.0, size=n_rows), type=pa.float64()),
            "city": pa.array(
                [cities[i % len(cities)] for i in rng.integers(0, len(cities), size=n_rows)],
                type=pa.string(),
            ),
        }
    )

    filepath = tmp_path / "test_data.parquet"
    pq.write_table(table, filepath)

    return str(filepath)
