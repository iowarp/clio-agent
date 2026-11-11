"""Lay the pinned LibreOffice application into a Windows runtime's ``documents\\office``.

The MSI is a CAB archive that Windows' CAB API expands one file at a time, about
415 files/s under Defender. Expanded once at release time, the same tree arrives in
the runtime pack, which installs it with parallel tar writers and authenticates it
with the pack's own hash. ``document_runtime`` uses ``documents\\office`` when it
holds an executable, so prepare only verifies it.

Run with the runtime's own Python on Windows::

    <runtime>\\python\\python.exe -I -B install/lay_office.py --runtime <runtime> [--msi FILE]
"""

from __future__ import annotations

import argparse
import hashlib
import platform
import shutil
import sys
import tempfile
from pathlib import Path

from clio_agent.runtime.document_stack import office, windows_msi


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--msi", type=Path, help="the pinned MSI, if already downloaded")
    args = parser.parse_args()
    if platform.system() != "Windows":
        raise SystemExit("lay_office.py expands the MSI with Windows' CAB API")
    target = args.runtime.resolve() / "documents" / "office"
    if target.exists():
        raise SystemExit(f"{target} already exists")
    url, msi_name, checksum = office.asset()
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="office-lay-", dir=target.parent) as temporary:
        work = Path(temporary)
        msi = args.msi
        if msi is None:
            msi = work / msi_name
            office._download(url, msi, checksum)
        with msi.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != checksum:
                raise SystemExit(f"{msi} is not the pinned LibreOffice {office.VERSION} MSI")
        tree = work / "office"
        tree.mkdir()
        windows_msi.extract_application(msi, tree)
        if office.executable(tree) is None:
            raise SystemExit("the expanded MSI has no soffice executable")
        shutil.move(str(tree), target)
    files = sum(1 for item in target.rglob("*") if item.is_file())
    print(f"[lay-office] LibreOffice {office.VERSION}: {files} files in {target}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
