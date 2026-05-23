"""
CLIO Agent Test Fixtures

Provides shared fixtures for all test modules, including synthetic
HDF5 and Parquet test data for MCP server testing.
"""

import os

import pytest

import clio_agent  # noqa: F401


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
