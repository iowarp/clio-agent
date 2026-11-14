"""Answer keys for the HDF5 benchmark tasks, computed from the ERA5 files with h5py/numpy.

Independent of the agent's tools: plain h5py reads of the same local files the local
arm uses (byte-identical to the S3 objects).

    uv run --frozen --no-sync python -m scripts.eval.hdf5_bench.ground_truth ERA5_DIR
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import h5py
import numpy as np

MSL_FILE = "e5.oper.an.sfc.128_151_msl.ll025sc.2024090100_2024093023.nc"
CLWC_FILE = "e5.oper.an.pl.128_246_clwc.ll025sc.2024092600_2024092623.nc"
#: The search box: 15–35°N, 260–290°E (100–70°W).
LAT_RANGE, LON_RANGE = (15.0, 35.0), (260.0, 290.0)
T1_TIMES = [dt.datetime(2024, 9, 24) + dt.timedelta(hours=6 * k) for k in range(16)]
T2_HOURS = [0, 6, 12, 18]
FILL = 9.0e20


def _box(f: h5py.File) -> tuple[slice, slice, np.ndarray, np.ndarray]:
    lat, lon = f["latitude"][()], f["longitude"][()]
    la = np.where((lat >= LAT_RANGE[0]) & (lat <= LAT_RANGE[1]))[0]
    lo = np.where((lon >= LON_RANGE[0]) & (lon <= LON_RANGE[1]))[0]
    return slice(la[0], la[-1] + 1), slice(lo[0], lo[-1] + 1), lat, lon


def _hours(f: h5py.File) -> list[dt.datetime]:
    base = dt.datetime(1900, 1, 1)
    return [base + dt.timedelta(hours=int(h)) for h in f["time"][()]]


def track(era5: Path) -> list[dict]:
    """T1: the minimum mean sea level pressure in the box every 6 hours, 24–27 Sep."""
    with h5py.File(era5 / MSL_FILE) as f:
        la, lo, lat, lon = _box(f)
        times = _hours(f)
        rows = []
        for when in T1_TIMES:
            t = times.index(when)
            field = f["MSL"][t, la, lo].astype("f8")
            field[field > FILL] = np.nan
            i, j = np.unravel_index(np.nanargmin(field), field.shape)
            rows.append(
                {
                    "time": when.strftime("%Y-%m-%dT%H:%MZ"),
                    "lat": float(lat[la][i]),
                    "lon": float(lon[lo][j]),
                    "msl_hpa": round(float(field[i, j]) / 100.0, 2),
                }
            )
    return rows


def cloud_peaks(era5: Path) -> list[dict]:
    """T2: per 6-hourly step on 26 Sep, the largest cloud liquid water in the box and where."""
    with h5py.File(era5 / CLWC_FILE) as f:
        la, lo, lat, lon = _box(f)
        level = f["level"][()]
        times = _hours(f)
        rows = []
        for hour in T2_HOURS:
            t = times.index(dt.datetime(2024, 9, 26, hour))
            field = f["CLWC"][t, :, la, lo].astype("f8")
            field[field > FILL] = np.nan
            k, i, j = np.unravel_index(np.nanargmax(field), field.shape)
            rows.append(
                {
                    "time": f"2024-09-26T{hour:02d}:00Z",
                    "level_hpa": float(level[k]),
                    "lat": float(lat[la][i]),
                    "lon": float(lon[lo][j]),
                    "clwc_g_per_kg": round(float(field[k, i, j]) * 1000.0, 4),
                }
            )
    return rows


def main(argv: list[str] | None = None) -> int:
    era5 = Path((argv or sys.argv[1:])[0])
    print(json.dumps({"T1": track(era5), "T2": cloud_peaks(era5)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
