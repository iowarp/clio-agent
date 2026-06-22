"""
CLIO Agent Test Fixtures

Provides shared fixtures for all test modules, including synthetic
HDF5 and Parquet test data for MCP server testing.
"""

import os
from pathlib import Path

import pytest

import clio_agent  # noqa: F401


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
def allow_pytest_tmp_path(tmp_path, monkeypatch):
    """Isolate tests from developer shell defaults.

    Developer shells often set CLIO_ALLOWED_ROOTS narrowly for manual
    use. Tests should not inherit that and then reject their own
    tmp_path fixtures.

    Unit tests also should not depend on a live LM Studio server for
    model discovery. Tests that need discovery unset CLIO_LM_MODEL
    explicitly.
    """

    existing = os.environ.get("CLIO_ALLOWED_ROOTS", "")
    roots = [str(tmp_path)]
    if existing.strip():
        roots.extend(item for item in existing.split(os.pathsep) if item)
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", os.pathsep.join(roots))

    if "CLIO_LM_MODEL" not in os.environ:
        monkeypatch.setenv("CLIO_LM_MODEL", "ibm/granite-4-h-tiny")

    monkeypatch.setenv("CLIO_AGENT_DISABLE_DEFAULT_REGISTRY_BOOTSTRAP", "1")
    monkeypatch.setenv("CLIO_AGENT_ENABLE_LEGACY_NATIVE_EXPERTS", "1")
    # Tests use the fast, isolated LocalFS ARC store by default; production
    # defaults to clio-core CTE. The CTE integration tests override via an
    # explicit backend="cte" arg, so they are unaffected.
    monkeypatch.setenv("CLIO_ARC_STORE", "local")
    xdg_root = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_root))
    _write_test_default_registry_blueprint(xdg_root)


def _write_test_default_registry_blueprint(xdg_root: Path) -> None:
    root = xdg_root / "clio-agent" / "agent-blueprints" / "data-semantics"
    experts = root / "experts"
    experts.mkdir(parents=True, exist_ok=True)
    root.joinpath("AGENT.md").write_text(
        """---
id: data-semantics
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
