"""Download the ERA5 files the Helene walkthrough uses (public NSF NCAR bucket, anonymous).

    python scripts/demo/helene/fetch_era5.py DEST

Writes ``DEST/msl_2024-09.nc`` and ``DEST/sst_2024-09.nc`` (September 2024 surface fields)
and the cloud liquid, cloud ice, rain and snow water pressure-level files for 24-27
September under their own names: 18 files, about 4.4 GB. A file already present with the
same size is kept.
"""

from __future__ import annotations

import sys
from pathlib import Path

BUCKET = "nsf-ncar-era5"
SURFACE = {
    "msl_2024-09.nc": "e5.oper.an.sfc/202409/e5.oper.an.sfc.128_151_msl.ll025sc.2024090100_2024093023.nc",
    "sst_2024-09.nc": "e5.oper.an.sfc/202409/e5.oper.an.sfc.128_034_sstk.ll025sc.2024090100_2024093023.nc",
}
WATER = ("128_246_clwc", "128_247_ciwc", "128_075_crwc", "128_076_cswc")
DAYS = ("24", "25", "26", "27")


def keys() -> dict[str, str]:
    """Local file name -> bucket key, for every file the walkthrough reads."""
    out = dict(SURFACE)
    for code in WATER:
        for day in DAYS:
            name = f"e5.oper.an.pl.{code}.ll025sc.202409{day}00_202409{day}23.nc"
            out[name] = f"e5.oper.an.pl/202409/{name}"
    return out


def main(dest: Path) -> None:
    import boto3
    from botocore import UNSIGNED
    from botocore.config import Config

    s3 = boto3.client("s3", region_name="us-west-2", config=Config(signature_version=UNSIGNED))
    dest.mkdir(parents=True, exist_ok=True)
    for name, key in keys().items():
        target = dest / name
        size = s3.head_object(Bucket=BUCKET, Key=key)["ContentLength"]
        if target.exists() and target.stat().st_size == size:
            print(f"kept {name}")
            continue
        print(f"fetching {name} ({size / 1e6:.0f} MB)", flush=True)
        s3.download_file(BUCKET, key, str(target))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    main(Path(sys.argv[1]))
