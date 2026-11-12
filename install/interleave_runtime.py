"""Write the Windows runtime archive's member order, spreading every part of the tree through it.

``install.ps1`` unpacks the archive while it downloads, with several ``tar.exe`` processes
that each write one part of the tree (``Start-ClioUnpackers``). Defender scans each new
file as it is closed, so writing the files, not downloading them, sets the pace. In path
order the documents tree (LibreOffice, Node packages and the document Python: about
12,700 files) is the second half of the archive: its unpackers wait out the download and
then write all of it after the last byte arrives.

This lists the directories first, then the files in runs of neighbours (one directory,
up to ``RUN_FILES`` files and ``RUN_BYTES`` bytes, which keeps similar files together for
the compressor) dealt in a fixed pseudo-random order. Every part of the tree, however the
unpackers divide it, then arrives evenly over the download and is written as it arrives.
The desktop setup deals each entry to its least-loaded writer (gact-tui
``runtime_unpack.rs``) and does not depend on the order.

    python -I -B install/interleave_runtime.py --runtime <dir>\\gact-runtime --out members.txt

then ``tar.exe -cf runtime.tar -C <dir> -n -T members.txt``.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path

RUN_FILES = 32
RUN_BYTES = 4 << 20


def members(runtime: Path) -> list[str]:
    """The archive's member names under ``runtime``'s leaf: directories, then dealt runs."""
    leaf = runtime.name
    directories = [leaf]
    runs: list[list[str]] = []
    for current, names, files in os.walk(runtime):
        names.sort()
        here = Path(current)
        prefix = leaf + "/" + here.relative_to(runtime).as_posix()
        prefix = leaf if prefix == leaf + "/." else prefix
        directories += [f"{prefix}/{name}" for name in names]
        run: list[str] = []
        size = 0
        for name in sorted(files):
            length = (here / name).stat().st_size
            if run and (len(run) == RUN_FILES or size + length > RUN_BYTES):
                runs.append(run)
                run, size = [], 0
            run.append(f"{prefix}/{name}")
            size += length
        if run:
            runs.append(run)
    runs.sort(key=lambda run: hashlib.sha256(run[0].encode()).digest())
    names = directories + [name for run in runs for name in run]
    # Windows tar.exe reads -T in the ANSI code page; an ASCII list reads the same in any.
    unreadable = [name for name in names if not name.isascii()]
    if unreadable:
        raise ValueError(f"non-ASCII runtime paths tar -T may misread: {unreadable[:5]}")
    return names


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    names = members(args.runtime.resolve())
    args.out.write_text("".join(f"{name}\n" for name in names), encoding="utf-8", newline="\n")
    print(f"[interleave-runtime] {len(names)} members", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
