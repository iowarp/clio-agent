"""Write a desktop release's install plan: what the first launch would download.

``install.sh --desktop`` and ``install.ps1 -Desktop`` fetch these files while the app
downloads and hand them to ``clio-desktop --prepare-runtime`` (``CLIO_INSTALL_DOWNLOADS``), which uses each one
only when its bytes match the app's own pin. One line per file, readable by bash 3.2:

    kind sha256 bytes name url

A first ``capability WORD`` line says the app waits for a download still in progress
(``office.INSTALLER_PLAN_CAPABILITY``), so the installer may prepare before its downloads
finish; readers that predate it skip lines without five fields.

The pins are read from the source (``office.py``, ``document_runtime.py``) without
importing it, so this runs on any CI Python. Sizes come from the servers and only feed
the installer's progress; 0 means unknown. The release lists the plan in its
SHA256SUMS, which is what authenticates it to the installer.

    python scripts/release_install_plan.py --target aarch64-apple-darwin --output PLAN
"""

from __future__ import annotations

import argparse
import ast
import http.client
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OFFICE = ROOT / "src/clio_agent/runtime/document_stack/office.py"
DOCUMENT_RUNTIME = ROOT / "src/clio_agent/runtime/document_runtime.py"
TARGETS = {
    "aarch64-apple-darwin": (("Darwin", "aarch64"), "darwin-arm64"),
    "x86_64-apple-darwin": (("Darwin", "x86_64"), "darwin-x64"),
    "x86_64-unknown-linux-gnu": (("Linux", "x86_64"), "linux-x64"),
    "aarch64-unknown-linux-gnu": (("Linux", "aarch64"), "linux-arm64"),
    # The bundled Windows setup is x64 only (it runs under emulation on ARM).
    "x86_64-pc-windows-msvc": (("Windows", "x86_64"), "win32-x64"),
}


def _constants(path: Path, *names: str) -> dict[str, object]:
    found: dict[str, object] = {}
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id in names:
                found[target.id] = ast.literal_eval(node.value)
    missing = set(names) - set(found)
    if missing:
        raise SystemExit(f"{path.name} no longer defines {sorted(missing)}")
    return found


def capability() -> str:
    """What this release's app does with the installer's downloads (one word)."""
    return str(_constants(OFFICE, "INSTALLER_PLAN_CAPABILITY")["INSTALLER_PLAN_CAPABILITY"])


def _named_asset(manifest: dict[str, object], what: str) -> tuple[str, str, str]:
    if not all(isinstance(manifest.get(field), str) for field in ("asset", "url", "sha256")):
        raise SystemExit(f"the {what} manifest names no release asset")
    return str(manifest["sha256"]), str(manifest["asset"]), str(manifest["url"])


def entries(
    target: str,
    pack: dict[str, object] | None = None,
    office_pack: dict[str, object] | None = None,
) -> list[tuple[str, str, str, str]]:
    """``(kind, sha256, name, url)`` for each file the target's install fetches.

    ``pack`` is a ``gact-runtime.pack.json`` that names the runtime archive as its own
    release asset. It comes first. The Windows setup unpacks it while it downloads, and
    that archive already holds LibreOffice (``install/lay_office.py``). The Linux archive
    holds no Office: ``install.sh`` installs the agent from it instead of from PyPI, and
    ``--desktop`` skips it, since the deb and rpm carry their own copy.

    ``office_pack`` (``scripts/build_office_tree.py``'s ``office-tree.pack.json``) names
    a Linux release's prepared Office tree, its own release asset: the ``office`` line
    then fetches it from the release instead of TDF's package archive, and the app
    unpacks it instead of laying Office from TDF's and Ubuntu's packages.
    """
    if target not in TARGETS:
        raise SystemExit(f"no install plan for {target}")
    key, pnpm_host = TARGETS[target]
    office = _constants(OFFICE, "VERSION", "ASSETS")
    pnpm = _constants(DOCUMENT_RUNTIME, "PNPM_VERSION", "PNPM_PACKAGES")
    version = office["VERSION"]
    directory, suffix, checksum = office["ASSETS"][key]  # type: ignore[index]
    name = f"LibreOffice_{version}_{suffix}"
    if pack is not None:
        plan = [("runtime", *_named_asset(pack, "runtime pack"))]
    else:
        plan = []
    if office_pack is not None:
        if key[0] != "Linux":
            raise SystemExit("a prepared Office tree is a Linux release asset")
        plan.append(("office", *_named_asset(office_pack, "Office tree")))
    elif pack is None or key[0] != "Windows":
        plan.append(
            (
                "office",
                checksum,
                name,
                f"https://download.documentfoundation.org/libreoffice/stable/{version}/{directory}/{name}",
            )
        )
    packages: dict[str, str] = pnpm["PNPM_PACKAGES"]  # type: ignore[assignment]
    for package in ("pnpm", f"@pnpm/exe.{pnpm_host}"):
        if package in packages:
            tarball = f"{package.rsplit('/', 1)[-1]}-{pnpm['PNPM_VERSION']}.tgz"
            url = f"https://registry.npmjs.org/{package}/-/{tarball}"
            plan.append(("npm", packages[package], tarball, url))
    return plan


def remote_size(url: str) -> int:
    request = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - https pins
            size = int(response.headers.get("Content-Length") or 0)
        if size:
            return size
        # The npm registry answers HEAD without a length; a one-byte range reports the total.
        request = urllib.request.Request(url, headers={"Range": "bytes=0-0"})
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - https pins
            return int((response.headers.get("Content-Range") or "/0").rsplit("/", 1)[1])
    except (OSError, ValueError, http.client.HTTPException):
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--target", required=True, choices=sorted(TARGETS))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--no-sizes", action="store_true", help="write 0 instead of asking")
    parser.add_argument(
        "--runtime-pack", type=Path, help="the setup's gact-runtime.pack.json, if it names an asset"
    )
    parser.add_argument(
        "--office-pack", type=Path, help="build_office_tree.py's office-tree.pack.json (Linux)"
    )
    args = parser.parse_args(argv)
    pack = office_pack = None
    if args.runtime_pack is not None:
        pack = json.loads(args.runtime_pack.read_text(encoding="utf-8"))
    if args.office_pack is not None:
        office_pack = json.loads(args.office_pack.read_text(encoding="utf-8"))

    def size(kind: str, url: str) -> int:
        if kind == "runtime" and pack is not None:
            return int(pack["archive_bytes"])
        if kind == "office" and office_pack is not None:
            return int(office_pack["archive_bytes"])
        return 0 if args.no_sizes else remote_size(url)

    lines = [f"capability {capability()}"] + [
        f"{kind} {sha} {size(kind, url)} {name} {url}"
        for kind, sha, name, url in entries(args.target, pack, office_pack)
    ]
    args.output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
