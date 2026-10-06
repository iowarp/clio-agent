#!/usr/bin/env python3
"""Regenerate CLIO branding from the two approved SVG masters.

Run ``pnpm --dir site install --frozen-lockfile`` once, then
``uv run python -m scripts.gen_brand_assets``. Sharp is already a site dependency;
Pillow is already a Python dependency. No tracing or external fonts are used
for the owl or serif wordmark.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageOps

from scripts.gen_installer_art import write_art

REPO_ROOT = Path(__file__).resolve().parents[1]
BRAND = Path("branding/clio")

# Compatibility exports remain available at their existing paths. Only these
# product assets are regenerated; screenshots and Clio Coder are not inputs.
MARK_EXPORTS = {
    "branding/clio/mark.png": 2048,
    "branding/logo_cropeed.png": 1024,
    "docs/images/logo-small.png": 1024,
    "site/src/assets/brand/clio-mark.png": 1024,
    "site/public/favicon.png": 256,
    "branding/clio/icons/32x32.png": 32,
    "branding/clio/icons/128x128.png": 128,
    "branding/clio/icons/128x128@2x.png": 256,
    "branding/clio/icons/icon.png": 1024,
}
WORDMARK_EXPORTS = {
    "branding/clio/wordmark.png": 2048,
    "branding/logo.png": 1024,
    "docs/images/logo-large.png": 1024,
}
MARK_COPIES = (
    "branding/clio/logo.svg",
    "site/src/assets/brand/clio-mark.svg",
    "site/public/favicon.svg",
)

RENDER_SVG = """
const { createRequire } = require('node:module');
const fs = require('node:fs');
const path = require('node:path');
const jobs = JSON.parse(fs.readFileSync(0, 'utf8'));
const sharp = createRequire(path.resolve('site/package.json'))('sharp');
(async () => {
  for (const job of jobs) {
    await sharp(job.source, { density: 192 })
      .resize(job.size, job.size).png().toFile(job.output);
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
"""


def write_native_icons(mark_path: Path, out_dir: Path) -> None:
    """Write Windows and macOS icons from a freshly rendered SVG export."""
    out_dir.mkdir(parents=True, exist_ok=True)
    with Image.open(mark_path) as source:
        image = source.convert("RGBA")
    image.save(
        out_dir / "icon.ico",
        format="ICO",
        sizes=[(size, size) for size in (16, 24, 32, 48, 64, 128, 256)],
    )
    image.save(out_dir / "icon.icns", format="ICNS")


def generate_assets(root: Path = REPO_ROOT) -> None:
    """Refresh website, UI, native icons, installer art and legacy exports."""
    jobs = []
    for master, exports in (("mark.svg", MARK_EXPORTS), ("wordmark.svg", WORDMARK_EXPORTS)):
        source = root / BRAND / master
        if not source.is_file():
            raise FileNotFoundError(source)
        for relative, size in exports.items():
            output = root / relative
            output.parent.mkdir(parents=True, exist_ok=True)
            jobs.append({"source": str(source), "output": str(output), "size": size})
    subprocess.run(
        ["node", "-e", RENDER_SVG],
        input=json.dumps(jobs),
        text=True,
        cwd=root,
        check=True,
    )
    for relative in MARK_COPIES:
        shutil.copyfile(root / BRAND / "mark.svg", root / relative)
    write_native_icons(root / BRAND / "mark.png", root / BRAND / "icons")
    # Keep the legacy wide banner URL usable. The README uses wordmark.svg.
    with Image.open(root / BRAND / "wordmark.png") as logo:
        banner = ImageOps.pad(logo, (2172, 724), color=(0, 0, 0, 0))
        banner.save(root / "docs/images/banner.png")
    write_art(root / BRAND / "installer", root / BRAND / "mark.png", root / BRAND / "wordmark.png")
    print("Regenerated CLIO SVG copies, PNG exports, native icons and installer art.")


if __name__ == "__main__":
    generate_assets()
