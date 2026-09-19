# CLIO installer art

The two bitmaps NSIS draws in the Windows installer wizard, referenced by
`branding/clio/tauri.clio.conf.json` as `bundle.windows.nsis.headerImage` and
`sidebarImage`:

| File | Size | Where it appears |
| --- | --- | --- |
| `header.bmp` | 150x57 | The band at the top of every interior wizard page |
| `sidebar.bmp` | 164x314 | The full-height panel on the Welcome and Finish pages |

Both must be **uncompressed 24-bit BMPs at exactly those sizes**. NSIS does not
reject a wrong-format bitmap and neither does the Tauri build — it only ever
surfaces as a broken or blank page in an installer that has already shipped, so
`external/gact-tui/desktop/scripts/check-installer-art.mjs` validates them and
`clio-bundles.yml` runs that check before every branded desktop build.

## Provenance

Generated from the tracked CLIO mark (`branding/logo_cropeed.png`, the same
raster `branding/clio/logo.svg` embeds) by `scripts/gen_installer_art.py`:
the mark on white with the wordmark and an accent rule for the header, and a
dark gradient panel with the mark, wordmark and the Gnosis Research Center
attribution for the sidebar. The accent is `brand.json`'s `accent` (`#ea7b2a`).

Regenerate after a brand change with:

```bash
uv run scripts/gen_installer_art.py
```

Replacing the art by hand is equally valid — drop in two BMPs that satisfy the
checker (`node external/gact-tui/desktop/scripts/check-installer-art.mjs
branding/clio/installer`) and the generator becomes the record of the pair they
replaced.

## Local branded builds

`clio-bundles.yml` copies this directory to
`external/gact-tui/desktop/src-tauri/installer/` before the Tauri build, the
same way it copies `branding/clio/icons`, so the overlay's relative
`installer/...` paths resolve. A local branded build needs the same copy:

```bash
mkdir -p external/gact-tui/desktop/src-tauri/installer
cp -f branding/clio/installer/*.bmp external/gact-tui/desktop/src-tauri/installer/
```

Like the icon copy, this leaves files behind in the submodule checkout; remove
them before committing a submodule pin.
