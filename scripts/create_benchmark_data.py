#!/usr/bin/env python3
"""Create deterministic CLIO stress benchmark datasets.

The generated files are intentionally richer than the small demo fixtures:
multi-group HDF5 data with attributes, Parquet data with row groups and
nullable columns, a dirty Parquet companion, and CSV event data. They are
small enough for local real-provider runs but structured enough to reveal
tool-routing, argument-generation, and tool-result feedback failures.
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
from pathlib import Path
from typing import Any

import h5py
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from PIL import Image


def create_hdf5(filepath: Path) -> dict[str, Any]:
    """Create a multi-group HDF5 file for scientific workflow benchmarks."""
    rng = np.random.default_rng(20260521)
    time_steps = 96
    radial_points = 64
    channels = 12

    time_axis = np.linspace(0.0, 24.0, time_steps, dtype=np.float64)
    radius = np.linspace(0.0, 1.0, radial_points, dtype=np.float64)
    channel_axis = np.arange(channels, dtype=np.int32)

    base_temp = 1_800.0 + 240.0 * np.sin(time_axis[:, None] / 24.0 * 2.0 * np.pi)
    radial_gradient = 360.0 * (1.0 - radius[None, :] ** 2)
    electron_temperature = (
        base_temp + radial_gradient + rng.normal(0.0, 12.0, (time_steps, radial_points))
    )
    density = (
        2.0e19
        + 1.5e18 * np.cos(time_axis[:, None] / 24.0 * 2.0 * np.pi)
        + 2.0e18 * radius[None, :]
        + rng.normal(0.0, 1.0e17, (time_steps, radial_points))
    )
    heat_flux = rng.gamma(shape=2.5, scale=18.0, size=(time_steps, channels))

    with h5py.File(filepath, "w") as h5f:
        h5f.attrs["created_by"] = "clio stress benchmark"
        h5f.attrs["scenario"] = "local real-provider multi-agent workflow"
        h5f.attrs["version"] = "1.0"

        axes = h5f.create_group("axes")
        axes.create_dataset("time_hours", data=time_axis, compression="gzip", chunks=(24,))
        axes.create_dataset("radius_norm", data=radius)
        axes.create_dataset("diagnostic_channel", data=channel_axis)

        plasma = h5f.create_group("plasma")
        temp = plasma.create_dataset(
            "electron_temperature",
            data=electron_temperature.astype(np.float32),
            compression="gzip",
            compression_opts=6,
            chunks=(24, 16),
        )
        temp.attrs["units"] = "eV"
        temp.attrs["description"] = "Synthetic electron temperature over time and radius"

        dens = plasma.create_dataset(
            "density",
            data=density.astype(np.float64),
            compression="gzip",
            compression_opts=4,
            chunks=(24, 16),
        )
        dens.attrs["units"] = "m^-3"
        dens.attrs["description"] = "Synthetic plasma density over time and radius"

        diag = h5f.create_group("diagnostics")
        flux = diag.create_dataset(
            "heat_flux",
            data=heat_flux.astype(np.float32),
            compression="gzip",
            compression_opts=5,
            chunks=(24, 4),
        )
        flux.attrs["units"] = "MW/m^2"
        flux.attrs["description"] = "Synthetic diagnostic channel heat flux"

        quality = h5f.create_group("quality")
        quality.create_dataset(
            "flags",
            data=(heat_flux > np.percentile(heat_flux, 95)).astype(np.int8),
            compression="gzip",
            chunks=(24, 4),
        )

    return {
        "path": str(filepath),
        "datasets": {
            "axes/time_hours": [time_steps],
            "axes/radius_norm": [radial_points],
            "axes/diagnostic_channel": [channels],
            "plasma/electron_temperature": [time_steps, radial_points],
            "plasma/density": [time_steps, radial_points],
            "diagnostics/heat_flux": [time_steps, channels],
            "quality/flags": [time_steps, channels],
        },
        "expected_terms": ["electron_temperature", "density", "heat_flux", "quality/flags"],
    }


def create_parquet(filepath: Path, dirty_filepath: Path) -> dict[str, Any]:
    """Create clean and dirty Parquet files with row groups and nullable columns."""
    rng = np.random.default_rng(20260522)
    n_rows = 3_000
    sites = np.array(["north", "south", "east", "west", "central"])
    run_ids = np.array(["run_001", "run_002", "run_003"])

    sample_id = np.arange(n_rows, dtype=np.int64)
    site_values = [str(sites[i % len(sites)]) for i in range(n_rows)]
    run_values = [str(run_ids[(i // 250) % len(run_ids)]) for i in range(n_rows)]
    temperature_k = rng.normal(294.0, 7.5, n_rows).astype(np.float64)
    pressure_pa = rng.normal(101_250.0, 780.0, n_rows).astype(np.float64)
    humidity_pct = np.clip(rng.normal(45.0, 14.0, n_rows), 4.0, 96.0).astype(np.float64)
    vibration_mm_s = rng.lognormal(mean=0.4, sigma=0.35, size=n_rows).astype(np.float64)
    anomaly_score = (
        np.abs(temperature_k - np.mean(temperature_k)) / np.std(temperature_k)
        + np.abs(pressure_pa - np.mean(pressure_pa)) / np.std(pressure_pa)
    ).astype(np.float64)
    quality_flag = ["warn" if score > 4.0 else "ok" for score in anomaly_score]
    valid = anomaly_score < 5.0

    table = pa.table(
        {
            "sample_id": pa.array(sample_id, type=pa.int64()),
            "run_id": pa.array(run_values, type=pa.string()),
            "site": pa.array(site_values, type=pa.string()),
            "temperature_k": pa.array(temperature_k, type=pa.float64()),
            "pressure_pa": pa.array(pressure_pa, type=pa.float64()),
            "humidity_pct": pa.array(humidity_pct, type=pa.float64()),
            "vibration_mm_s": pa.array(vibration_mm_s, type=pa.float64()),
            "anomaly_score": pa.array(anomaly_score, type=pa.float64()),
            "quality_flag": pa.array(quality_flag, type=pa.string()),
            "valid": pa.array(valid, type=pa.bool_()),
        },
        metadata={b"scenario": b"clio-stress-benchmark", b"source": b"deterministic"},
    )
    pq.write_table(table, filepath, row_group_size=375, compression="zstd")

    dirty_temperature = temperature_k.astype(object).tolist()
    dirty_pressure = pressure_pa.astype(object).tolist()
    dirty_site = list(site_values)
    for idx in range(0, n_rows, 173):
        dirty_temperature[idx] = None
    for idx in range(89, n_rows, 211):
        dirty_pressure[idx] = None
    for idx in range(47, n_rows, 307):
        dirty_site[idx] = "unknown"

    dirty_table = pa.table(
        {
            "sample_id": pa.array(sample_id, type=pa.int64()),
            "site": pa.array(dirty_site, type=pa.string()),
            "temperature_k": pa.array(dirty_temperature, type=pa.float64()),
            "pressure_pa": pa.array(dirty_pressure, type=pa.float64()),
            "quality_flag": pa.array(quality_flag, type=pa.string()),
            "valid": pa.array(valid, type=pa.bool_()),
        },
        metadata={b"scenario": b"clio-stress-benchmark-dirty"},
    )
    pq.write_table(dirty_table, dirty_filepath, row_group_size=300, compression="zstd")

    return {
        "path": str(filepath),
        "dirty_path": str(dirty_filepath),
        "rows": n_rows,
        "row_group_size": 375,
        "columns": list(table.column_names),
        "expected_terms": [
            "temperature_k",
            "pressure_pa",
            "humidity_pct",
            "anomaly_score",
            "quality_flag",
        ],
    }


def create_csv(filepath: Path) -> dict[str, Any]:
    """Create a CSV event stream with numeric and categorical sensor fields."""
    rng = np.random.default_rng(20260523)
    sites = ["north", "south", "east", "west", "central"]
    statuses = ["ok", "ok", "ok", "warn", "maintenance"]
    rows: list[dict[str, str]] = []
    for idx in range(420):
        site = sites[idx % len(sites)]
        rows.append(
            {
                "event_id": f"evt_{idx:04d}",
                "timestamp": f"2026-05-{1 + (idx // 24):02d}T{idx % 24:02d}:00:00Z",
                "site": site,
                "temperature_k": f"{rng.normal(294.0, 8.0):.3f}",
                "pressure_pa": f"{rng.normal(101_250.0, 700.0):.3f}",
                "status": statuses[idx % len(statuses)],
                "operator_note": "calibration" if idx % 97 == 0 else "",
            }
        )

    with filepath.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    return {
        "path": str(filepath),
        "rows": len(rows),
        "columns": list(rows[0]),
        "expected_terms": ["event_id", "timestamp", "temperature_k", "pressure_pa", "status"],
    }


def create_genomics(output_dir: Path) -> dict[str, Any]:
    """Create small FASTA and VCF files for genomics benchmark workflows."""
    fasta_path = output_dir / "pathogen_reference.fasta"
    vcf_path = output_dir / "pathogen_sample_variants.vcf"

    rng = np.random.default_rng(20260524)
    bases = np.array(list("ACGT"))
    contigs: dict[str, str] = {}
    for contig, length, gc_bias in (
        ("chrA", 4800, [0.21, 0.29, 0.29, 0.21]),
        ("plasmidB", 1250, [0.33, 0.17, 0.17, 0.33]),
    ):
        sequence = "".join(rng.choice(bases, size=length, p=gc_bias).tolist())
        contigs[contig] = sequence

    with fasta_path.open("w", encoding="utf-8") as handle:
        for contig, sequence in contigs.items():
            handle.write(f">{contig} synthetic pathogen benchmark reference\n")
            for start in range(0, len(sequence), 80):
                handle.write(sequence[start : start + 80] + "\n")

    variants = [
        ("chrA", 128, "var001", "A", "G", 72.0, "PASS", "GENE=repA;EFFECT=missense"),
        ("chrA", 790, "var002", "CT", "C", 54.5, "PASS", "GENE=membrane;EFFECT=frameshift"),
        ("chrA", 1432, "var003", "G", "GA", 61.2, "PASS", "GENE=polymerase;EFFECT=insertion"),
        ("chrA", 3104, "var004", "T", "C", 22.1, "LowQual", "GENE=hypothetical;EFFECT=synonymous"),
        ("plasmidB", 217, "var005", "C", "T", 88.4, "PASS", "GENE=resistance;EFFECT=stop_gained"),
        ("plasmidB", 904, "var006", "GTA", "G", 47.3, "PASS", "GENE=mobility;EFFECT=deletion"),
    ]
    with vcf_path.open("w", encoding="utf-8") as handle:
        handle.write("##fileformat=VCFv4.2\n")
        handle.write("##source=clio-benchmark\n")
        handle.write('##INFO=<ID=GENE,Number=1,Type=String,Description="Synthetic gene label">\n')
        handle.write('##INFO=<ID=EFFECT,Number=1,Type=String,Description="Synthetic effect label">\n')
        handle.write("#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tsample_A\n")
        for row in variants:
            chrom, pos, var_id, ref, alt, qual, filter_value, info = row
            handle.write(
                f"{chrom}\t{pos}\t{var_id}\t{ref}\t{alt}\t{qual:.1f}\t"
                f"{filter_value}\t{info}\tGT:DP\t0/1:42\n"
            )

    return {
        "fasta_path": str(fasta_path),
        "vcf_path": str(vcf_path),
        "records": len(contigs),
        "variants": len(variants),
        "expected_terms": ["chrA", "plasmidB", "missense", "frameshift", "stop_gained"],
    }


def create_materials(output_dir: Path) -> dict[str, Any]:
    """Create a small CIF file for materials/crystallography benchmark workflows."""
    cif_path = output_dir / "strontium_titanate.cif"
    cif_path.write_text(
        "data_SrTiO3_benchmark\n"
        "_chemical_formula_sum 'Sr1 Ti1 O3'\n"
        "_chemical_formula_structural 'SrTiO3'\n"
        "_symmetry_space_group_name_H-M 'P m -3 m'\n"
        "_cell_length_a 3.905\n"
        "_cell_length_b 3.905\n"
        "_cell_length_c 3.905\n"
        "_cell_angle_alpha 90\n"
        "_cell_angle_beta 90\n"
        "_cell_angle_gamma 90\n"
        "\n"
        "loop_\n"
        "_atom_site_label\n"
        "_atom_site_type_symbol\n"
        "_atom_site_fract_x\n"
        "_atom_site_fract_y\n"
        "_atom_site_fract_z\n"
        "_atom_site_occupancy\n"
        "Sr1 Sr 0.000 0.000 0.000 1.0\n"
        "Ti1 Ti 0.500 0.500 0.500 1.0\n"
        "O1 O 0.500 0.500 0.000 1.0\n"
        "O2 O 0.500 0.000 0.500 1.0\n"
        "O3 O 0.000 0.500 0.500 1.0\n",
        encoding="utf-8",
    )
    return {
        "cif_path": str(cif_path),
        "formula": "SrTiO3",
        "space_group": "P m -3 m",
        "atom_sites": 5,
        "expected_terms": ["SrTiO3", "P m -3 m", "Sr", "Ti", "O"],
    }


def create_geospatial(output_dir: Path) -> dict[str, Any]:
    """Create a GeoJSON file for geospatial benchmark workflows."""
    geojson_path = output_dir / "field_sites.geojson"
    payload = {
        "type": "FeatureCollection",
        "name": "clio_benchmark_field_sites",
        "features": [
            {
                "type": "Feature",
                "properties": {"site_id": "north_ridge", "kind": "sensor", "status": "active"},
                "geometry": {"type": "Point", "coordinates": [-105.2705, 40.015]},
            },
            {
                "type": "Feature",
                "properties": {"site_id": "south_valley", "kind": "sensor", "status": "maintenance"},
                "geometry": {"type": "Point", "coordinates": [-105.251, 39.991]},
            },
            {
                "type": "Feature",
                "properties": {"site_id": "access_transect", "kind": "transect", "status": "active"},
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[-105.281, 40.004], [-105.268, 40.012], [-105.252, 40.019]],
                },
            },
            {
                "type": "Feature",
                "properties": {"site_id": "study_boundary", "kind": "boundary", "status": "active"},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [
                            [-105.292, 39.982],
                            [-105.238, 39.982],
                            [-105.238, 40.026],
                            [-105.292, 40.026],
                            [-105.292, 39.982],
                        ]
                    ],
                },
            },
        ],
    }
    geojson_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return {
        "geojson_path": str(geojson_path),
        "features": len(payload["features"]),
        "expected_terms": ["north_ridge", "south_valley", "study_boundary", "Point", "Polygon"],
    }


def create_imaging(output_dir: Path) -> dict[str, Any]:
    """Create a PNG microscopy-style fixture for scientific image workflows."""
    png_path = output_dir / "microscopy_cells.png"
    height = 96
    width = 128
    yy, xx = np.mgrid[:height, :width]
    image = np.zeros((height, width), dtype=np.uint8)
    image += np.linspace(4, 16, width, dtype=np.uint8)[None, :]

    cells = [
        ("cell_alpha", 34, 31, 15, 10, 168),
        ("cell_beta", 82, 42, 18, 12, 214),
        ("cell_gamma", 56, 70, 13, 9, 188),
    ]
    for _name, cx, cy, rx, ry, intensity in cells:
        mask = ((xx - cx) / rx) ** 2 + ((yy - cy) / ry) ** 2 <= 1.0
        image[mask] = np.maximum(image[mask], intensity)
        core = ((xx - cx) / max(rx // 3, 1)) ** 2 + ((yy - cy) / max(ry // 3, 1)) ** 2 <= 1.0
        image[core] = 245

    Image.fromarray(image, mode="L").save(png_path)
    return {
        "png_path": str(png_path),
        "width": width,
        "height": height,
        "objects": len(cells),
        "expected_terms": ["cell_alpha", "cell_beta", "cell_gamma", "foreground", "intensity"],
    }


def create_mass_spec(output_dir: Path) -> dict[str, Any]:
    """Create a small mzML-style XML fixture for mass spectrometry workflows."""
    mzml_path = output_dir / "proteomics_qc.mzML"
    spectra = [
        ("scan=1", 1, 0.12, [401.2, 455.8, 512.3, 609.4], [1200.0, 5400.0, 2100.0, 800.0]),
        ("scan=2", 2, 0.18, [522.2, 701.4, 884.6], [2300.0, 890.0, 440.0]),
        ("scan=3", 1, 0.25, [399.8, 455.8, 612.1, 777.7], [980.0, 5100.0, 1600.0, 1200.0]),
        ("scan=4", 2, 0.31, [488.9, 650.2, 933.5], [1800.0, 720.0, 610.0]),
    ]
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<mzML xmlns="http://psi.hupo.org/ms/mzml" id="clio_proteomics_qc">',
        '  <run id="run_benchmark">',
        f'    <spectrumList count="{len(spectra)}">',
    ]
    for spectrum_id, ms_level, scan_time, mz_values, intensities in spectra:
        tic = sum(intensities)
        lines.extend(
            [
                f'      <spectrum id="{spectrum_id}" defaultArrayLength="{len(mz_values)}">',
                f'        <cvParam name="ms level" value="{ms_level}"/>',
                f'        <cvParam name="total ion current" value="{tic:.1f}"/>',
                f'        <scanList count="1"><scan><cvParam name="scan start time" value="{scan_time:.2f}" unitName="minute"/></scan></scanList>',
                "        <binaryDataArrayList count=\"2\">",
                "          <binaryDataArray>",
                '            <cvParam name="m/z array"/>',
                "            <binary>" + " ".join(f"{value:.4f}" for value in mz_values) + "</binary>",
                "          </binaryDataArray>",
                "          <binaryDataArray>",
                '            <cvParam name="intensity array"/>',
                "            <binary>" + " ".join(f"{value:.1f}" for value in intensities) + "</binary>",
                "          </binaryDataArray>",
                "        </binaryDataArrayList>",
                "      </spectrum>",
            ]
        )
    lines.extend(["    </spectrumList>", "  </run>", "</mzML>", ""])
    mzml_path.write_text("\n".join(lines), encoding="utf-8")
    return {
        "mzml_path": str(mzml_path),
        "spectra": len(spectra),
        "ms_levels": {"1": 2, "2": 2},
        "expected_terms": ["scan=1", "scan=2", "ms level", "total ion current", "m/z"],
    }


def create_lfq(output_dir: Path) -> dict[str, Any]:
    """Create a compact LFQ intensity matrix with known condition shifts."""
    lfq_path = output_dir / "proteinGroups_lfq_benchmark.tsv"
    rows = [
        ("spike_UP_A", "SPIKEUP", "", "", 8200, 7900, 8400, 32100, 33700, 31950),
        ("spike_UP_B", "SPIKEUPB", "", "", 6100, 6400, 6200, 24600, 25100, 23800),
        ("stable_CTRL", "CTRL", "", "", 12000, 11800, 12150, 11950, 12250, 12050),
        ("missing_case", "MISS", "", "", 9000, "", 8800, 14000, "", 15000),
        ("down_shift", "DOWN", "", "", 22000, 21800, 22500, 6100, 6400, 5900),
        ("CON__keratin", "KRT", "+", "", 40000, 41000, 40500, 39900, 40200, 40100),
        ("REV__decoy", "DECOY", "", "+", 7000, 7100, 6900, 7100, 7200, 7050),
    ]
    columns = [
        "Protein IDs",
        "Gene names",
        "Potential contaminant",
        "Reverse",
        "LFQ intensity Control_1",
        "LFQ intensity Control_2",
        "LFQ intensity Control_3",
        "LFQ intensity Treatment_1",
        "LFQ intensity Treatment_2",
        "LFQ intensity Treatment_3",
    ]
    with lfq_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t")
        writer.writerow(columns)
        writer.writerows(rows)
    return {
        "lfq_path": str(lfq_path),
        "control_prefix": "Control",
        "treatment_prefix": "Treatment",
        "spike_terms": "SPIKEUP,SPIKEUPB",
        "expected_spike_log2fc": 2.0,
        "expected_terms": ["SPIKEUP", "DOWN", "median", "missing"],
    }


def create_hpc_traces(output_dir: Path) -> dict[str, Any]:
    """Create paired Darshan-style text traces with a planted write regression."""
    baseline_path = output_dir / "baseline_darshan.txt"
    candidate_path = output_dir / "candidate_darshan.txt"
    baseline_path.write_text(
        "\n".join(
            [
                "# CLIO benchmark baseline Darshan-style trace",
                "runtime: 100.0 s",
                "nprocs: 128",
                "POSIX 0 shared POSIX_BYTES_WRITTEN 104857600",
                "POSIX 0 shared POSIX_BYTES_READ 52428800",
                "POSIX 0 shared POSIX_F_WRITE_TIME 12.0",
                "POSIX 0 shared POSIX_F_READ_TIME 4.5",
                "POSIX 0 shared POSIX_F_META_TIME 1.2",
                "MPIIO 0 shared MPIIO_COLL_WRITES 96",
                "MPIIO 0 shared MPIIO_INDEP_WRITES 8",
                "write transfer sizes: 1048576 1048576 2097152",
                "",
            ]
        ),
        encoding="utf-8",
    )
    candidate_path.write_text(
        "\n".join(
            [
                "# CLIO benchmark candidate Darshan-style trace",
                "runtime: 118.0 s",
                "nprocs: 128",
                "POSIX 0 shared POSIX_BYTES_WRITTEN 104857600",
                "POSIX 0 shared POSIX_BYTES_READ 52428800",
                "POSIX 0 shared POSIX_F_WRITE_TIME 28.0",
                "POSIX 0 shared POSIX_F_READ_TIME 4.7",
                "POSIX 0 shared POSIX_F_META_TIME 5.8",
                "MPIIO 0 shared MPIIO_COLL_WRITES 24",
                "MPIIO 0 shared MPIIO_INDEP_WRITES 88",
                "write transfer sizes: 131072 262144 524288",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return {
        "baseline_path": str(baseline_path),
        "candidate_path": str(candidate_path),
        "expected_terms": ["write_time", "metadata_time", "independent_writes", "regression"],
    }


def create_format_bridge(output_dir: Path) -> dict[str, Any]:
    """Create an HDF5 source with conversion-safe and policy-risk columns."""
    source_path = output_dir / "format_bridge_source.h5"
    output_path = output_dir / "format_bridge_converted.parquet"
    with h5py.File(source_path, "w") as handle:
        values = np.linspace(0.0, 1.0, 32, dtype=np.float64)
        values[7] = np.nan
        handle.create_dataset("safe_float", data=values, compression="gzip")
        handle.create_dataset("labels", data=np.array(["alpha", "beta"] * 16, dtype=h5py.string_dtype()))
        handle.create_dataset("float16_policy", data=np.linspace(0.0, 1.0, 32, dtype=np.float16))
        handle.create_dataset("complex_signal", data=np.arange(32, dtype=np.float64) + 1j)
        datetime = handle.create_dataset("time_ns", data=np.arange(32, dtype=np.int64))
        datetime.attrs["logical_type"] = "datetime64[ns]"
    return {
        "source_path": str(source_path),
        "output_path": str(output_path),
        "expected_terms": ["safe_float", "float16", "complex", "datetime", "checksum"],
    }


def create_terrain(output_dir: Path) -> dict[str, Any]:
    """Create DEM and point-cloud terrain fixtures for site-suitability workflows."""
    dem_path = output_dir / "terrain_dem.csv"
    pointcloud_path = output_dir / "terrain_points.csv"
    gridded_path = output_dir / "terrain_points_gridded.csv"
    dem = np.array(
        [
            [100.0, 101.0, 102.0, 103.0],
            [100.5, 101.5, 102.5, 103.5],
            [101.0, 102.0, 103.0, 104.0],
            [102.0, 103.0, 104.0, 105.0],
        ],
        dtype=float,
    )
    np.savetxt(dem_path, dem, delimiter=",", fmt="%.3f")
    with pointcloud_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["x", "y", "z"])
        for y in range(4):
            for x in range(4):
                writer.writerow([x, y, float(dem[y, x])])
    return {
        "dem_path": str(dem_path),
        "pointcloud_path": str(pointcloud_path),
        "gridded_path": str(gridded_path),
        "expected_terms": ["slope", "suitable", "point", "grid", "elevation"],
    }


def create_adios_bp5(output_dir: Path) -> dict[str, Any]:
    """Copy a real BP5 sample when present, otherwise create a BP-like container."""
    destination = output_dir / "gray scott noise 0.01 data.bp5"
    source = (
        Path(__file__).resolve().parents[2]
        / "bp5-dataset-collection"
        / "Gray-Scott"
        / "dataset 1"
        / "noise=0.01"
        / "data.bp5"
    )
    if source.exists():
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(source, destination)
        source_kind = "bp5-dataset-collection"
    else:
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "data.0").write_bytes(b"x" * 1024)
        (destination / "md.0").write_bytes(b"m" * 256)
        (destination / "md.idx").write_bytes(b"i" * 64)
        (destination / "mmd.0").write_bytes(b"q" * 128)
        profiling = [
            {
                "rank": 0,
                "transport_0": {
                    "type": "File_POSIX",
                    "wbytes": 1024,
                    "write": {"nCalls": 4},
                    "open": {"nCalls": 1},
                    "close": {"nCalls": 1},
                },
            }
        ]
        (destination / "profiling.json").write_text(json.dumps(profiling), encoding="utf-8")
        source_kind = "synthetic-bp5-container"

    return {
        "path": str(destination),
        "source": source_kind,
        "expected_terms": ["BP5", "profiling", "transport", "ADIOS2"],
    }


def create_benchmark_data(output_dir: Path) -> dict[str, Any]:
    """Create all benchmark files and return a manifest dictionary."""
    output_dir.mkdir(parents=True, exist_ok=True)
    hdf5 = create_hdf5(output_dir / "fusion_run.h5")
    parquet = create_parquet(
        output_dir / "facility_measurements.parquet",
        output_dir / "facility_measurements_dirty.parquet",
    )
    csv_info = create_csv(output_dir / "sensor_events.csv")
    genomics = create_genomics(output_dir)
    materials = create_materials(output_dir)
    geospatial = create_geospatial(output_dir)
    imaging = create_imaging(output_dir)
    mass_spec = create_mass_spec(output_dir)
    lfq = create_lfq(output_dir)
    hpc = create_hpc_traces(output_dir)
    format_bridge = create_format_bridge(output_dir)
    terrain = create_terrain(output_dir)
    adios = create_adios_bp5(output_dir)

    manifest: dict[str, Any] = {
        "version": 1,
        "description": "Deterministic local datasets for CLIO stress benchmarks.",
        "hdf5": hdf5,
        "parquet": parquet,
        "csv": csv_info,
        "genomics": genomics,
        "materials": materials,
        "geospatial": geospatial,
        "imaging": imaging,
        "mass_spec": mass_spec,
        "lfq": lfq,
        "hpc": hpc,
        "format_bridge": format_bridge,
        "terrain": terrain,
        "adios": adios,
    }
    manifest_path = output_dir / "manifest.json"
    manifest["manifest_path"] = str(manifest_path)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return manifest


def main() -> None:
    """CLI entry point for benchmark data generation."""
    parser = argparse.ArgumentParser(description="Create CLIO stress benchmark datasets")
    parser.add_argument(
        "--output-dir",
        default="tmp/clio-benchmark-data",
        help="Directory where benchmark files will be written",
    )
    args = parser.parse_args()

    output_dir = Path(args.output_dir).expanduser().resolve()
    manifest = create_benchmark_data(output_dir)
    print(json.dumps(manifest, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
