# CLIO B4 branding

`mark.svg` (owl) and `wordmark.svg` (owl with serif CLIO lettering) are the
approved smooth vector masters. Both use the same geometry and palette on
light and dark backgrounds: navy `#143B5D`, blue `#2F7599`, orange `#E87C2F`,
ivory `#F2EAD9`. The owl's outer strokes are 13 units; the beak stroke is 6.5.
The wordmark is paths and needs no installed font. Preserve transparent areas
and ordinary antialiasing; do not use pixelated or crisp-edges rendering.

The web workspace and Desktop select these files through `brand.json`. Compact
marks use the full-color `logoImage` fallback rather than a theme-colored mask,
so the approved palette and outline are identical in both themes. The browser
favicon also uses `mark.svg`. `logo.svg` is a compatibility copy of the mark.
The separately supplied monochrome `ui-icons/` remain available for custom
icon use, but are not selected for CLIO product branding.

The website header, footer and favicon use exact copies of `mark.svg`.
The README and Desktop startup screen use `wordmark.svg`. Native Windows,
Linux and macOS icons and Windows installer bitmaps are derived from these
masters. Historical screenshots and demo recordings are evidence, not brand
sources. Clio Coder has separate branding.

Regenerate all derivatives after changing either master:

```sh
pnpm --dir site install --frozen-lockfile
uv run python -m scripts.gen_brand_assets
node external/gact-tui/desktop/scripts/check-brand-icons.mjs branding/clio/icons --require-icns
node external/gact-tui/desktop/scripts/check-installer-art.mjs branding/clio/installer
```

The generator uses the existing Sharp and Pillow dependencies. It refreshes
legacy PNG paths as well, so downstream references cannot retain stale art.
SVG remains the source used directly by the website and workspace. Published
release installers retain the assets they were built with; regenerated native
icons take effect in the next installer build.
