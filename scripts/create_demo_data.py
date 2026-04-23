#!/usr/bin/env python3
"""Create deterministic CLIO demo datasets.

The script writes one HDF5 file and one Parquet file with small scientific-style
tables that are safe to inspect in demos.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import h5py
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq


def create_hdf5(filepath: Path) -> None:
    """Create an HDF5 file with compressed and uncompressed datasets."""
    rng = np.random.default_rng(42)

    with h5py.File(filepath, "w") as h5f:
        sim = h5f.create_group("simulation")
        temp = sim.create_dataset(
            "temperature",
            data=rng.normal(loc=290.0, scale=8.0, size=(120, 80)),
            compression="gzip",
            compression_opts=6,
            chunks=(20, 20),
        )
        temp.attrs["units"] = "Kelvin"
        temp.attrs["description"] = "Synthetic atmospheric temperature"

        sim.create_dataset(
            "pressure",
            data=rng.normal(loc=101_325.0, scale=500.0, size=(120, 80)),
            chunks=(30, 20),
        )
        h5f.create_dataset("time_step", data=np.arange(120, dtype=np.int64))
        h5f.attrs["created_by"] = "clio-agent demo"
        h5f.attrs["version"] = "1.0"


def create_parquet(filepath: Path) -> None:
    """Create a Parquet file with numeric and categorical columns."""
    rng = np.random.default_rng(43)
    n_rows = 200
    sites = ["north", "south", "east", "west"]

    table = pa.table(
        {
            "sample_id": pa.array(range(n_rows), type=pa.int64()),
            "temperature": pa.array(rng.uniform(273.0, 315.0, n_rows), type=pa.float64()),
            "pressure": pa.array(rng.normal(101_325.0, 650.0, n_rows), type=pa.float64()),
            "site": pa.array([sites[i % len(sites)] for i in range(n_rows)], type=pa.string()),
        }
    )
    pq.write_table(table, filepath)


def main() -> None:
    parser = argparse.ArgumentParser(description="Create CLIO demo HDF5 and Parquet files")
    parser.add_argument(
        "--output-dir",
        default="/tmp/clio-agent-demo",
        help="Directory where demo files will be written",
    )
    args = parser.parse_args()

    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    hdf5_path = output_dir / "clio_demo.h5"
    parquet_path = output_dir / "clio_demo.parquet"

    create_hdf5(hdf5_path)
    create_parquet(parquet_path)

    print(f"HDF5: {hdf5_path}")
    print(f"Parquet: {parquet_path}")


if __name__ == "__main__":
    main()
