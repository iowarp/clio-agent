"""
CLIO Agent Test Fixtures

Provides shared fixtures for all test modules, including synthetic
HDF5 test data for MCP server testing.
"""

import h5py
import numpy as np
import pytest


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
