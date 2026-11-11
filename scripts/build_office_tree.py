"""Lay this host's Office as ``ensure_office`` does and pack it as one release asset.

Linux only. TDF's packages are unpacked and pruned, the private userland is laid in
(``office_compat``), and the result is packed as ``office.prepared_tree_name()``, a
``.tar.zst``. ``install.sh`` downloads it from the release (its install plan's
``office`` line), so an install needs neither TDF's mirrors nor Ubuntu's archives, and
``ensure_office`` only unpacks it (``installer_prepared_tree``). The tree is then run
once, as every install does before publishing it.

    uv run --no-project --python 3.14 --with zstandard --with filelock --with truststore \
        python scripts/build_office_tree.py --output DIR [--release-url URL]

Writes ``DIR/<asset>`` and ``DIR/office-tree.pack.json`` (``asset``, ``sha256``,
``archive_bytes``, and ``url`` when ``--release-url`` names the release's download
folder), which ``scripts/release_install_plan.py --office-pack`` reads.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src/clio_agent/runtime/document_stack"))

import office  # noqa: E402
import zstandard  # noqa: E402


def _members(image: Path) -> list[Path]:
    found: list[Path] = []
    for directory, names, files in os.walk(image):
        names.sort()
        path = Path(directory)
        found.extend(path / name for name in names)
        found.extend(path / name for name in sorted(files))
    return found


def _owned_by_root(member: tarfile.TarInfo) -> tarfile.TarInfo:
    member.uid = member.gid = 0
    member.uname = member.gname = ""
    return member


def pack(image: Path, target: Path) -> None:
    compressor = zstandard.ZstdCompressor(level=19, threads=-1, write_content_size=True)
    with (
        target.open("wb") as raw,
        compressor.stream_writer(raw) as stream,
        tarfile.open(fileobj=stream, mode="w|", format=tarfile.PAX_FORMAT) as archive,
    ):
        for path in _members(image):
            archive.add(
                path, path.relative_to(image).as_posix(), recursive=False, filter=_owned_by_root
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--release-url", help="the release's download folder URL")
    args = parser.parse_args(argv)
    name = office.prepared_tree_name()
    if name is None:
        raise SystemExit("no prepared Office tree for this host (Linux x86_64 or aarch64 only)")
    args.output.mkdir(parents=True, exist_ok=True)
    target = args.output / name
    with tempfile.TemporaryDirectory(prefix="office-tree-", dir=args.output) as temporary:
        staging = Path(temporary)
        image = staging / "image"
        image.mkdir()
        url, archive_name, checksum = office.asset()
        archive = staging / archive_name

        def source() -> Path:
            office._download(url, archive, checksum, (office.asset_fallback(),))
            return archive

        office._lay_image(image, staging, source)
        archive.unlink(missing_ok=True)
        pack(image, target)
        executable = office.executable(image)
        if executable is None:
            raise SystemExit("the laid Office has no executable")
        office._verify_office(executable, cwd=image)
    with target.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    manifest = {"asset": name, "sha256": digest, "archive_bytes": target.stat().st_size}
    if args.release_url:
        manifest["url"] = f"{args.release_url.rstrip('/')}/{name}"
    (args.output / "office-tree.pack.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest))
    return 0


if __name__ == "__main__":
    sys.exit(main())
