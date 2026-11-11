"""Compress the desktop runtime's tar into ``gact-runtime.tar.zst``, small enough to download.

``tar --zstd`` uses Zstandard's default level 3: the 0.9.5.1 Windows runtime packed to
506 MB. Level 19 with long-distance matching over a 128 MB window packs the same tar to
about 363 MB and decompresses as fast or faster. 128 MB (window log 27) is the largest
window a default decoder accepts, including the desktop's (``zstd`` crate,
``runtime_unpack.rs``), so the archive needs no decoder change.

Run with the runtime's own Python (3.14 or later: ``compression.zstd`` is in its
standard library)::

    python -I -B install/compress_runtime.py --tar runtime.tar --out gact-runtime.tar.zst
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from compression import zstd
from compression.zstd import CompressionParameter

LEVEL = 19
WINDOW_LOG = 27  # 128 MB: the default decoder limit (ZSTD_WINDOWLOG_LIMIT_DEFAULT)
CHUNK = 4 << 20


def compress(tar: Path, out: Path, level: int = LEVEL, window_log: int = WINDOW_LOG) -> str:
    """Write ``out`` from ``tar``; return how many workers compressed it."""
    options = {
        CompressionParameter.compression_level: level,
        CompressionParameter.window_log: window_log,
        CompressionParameter.enable_long_distance_matching: 1,
    }
    _, max_workers = CompressionParameter.nb_workers.bounds()
    workers = min(os.cpu_count() or 1, max_workers)
    if workers > 0:
        options[CompressionParameter.nb_workers] = workers
    compressor = zstd.ZstdCompressor(options=options)
    with tar.open("rb") as src, out.open("wb") as dst:
        while chunk := src.read(CHUNK):
            dst.write(compressor.compress(chunk))
        dst.write(compressor.flush())
    return f"{workers} workers" if workers > 0 else "1 thread (this zstd has no worker support)"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--tar", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--level", type=int, default=LEVEL)
    args = parser.parse_args()
    started = time.monotonic()
    how = compress(args.tar, args.out, args.level)
    size_in, size_out = args.tar.stat().st_size, args.out.stat().st_size
    print(
        f"[compress-runtime] {size_in / 2**20:.1f} MiB -> {size_out / 2**20:.1f} MiB, "
        f"level {args.level}, window 2^{WINDOW_LOG}, {how}, {time.monotonic() - started:.0f}s"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
