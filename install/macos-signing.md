# Signing and notarizing the macOS bundle

The signing configuration itself lives in gact-tui (Tauri). This page is the
contract the bundled backend, `Contents/Resources/gact-runtime`, needs from it.

## What to sign

Every Mach-O inside `gact-runtime`, not just the files Tauri knows about:
the interpreter, every `.so`/`.dylib` in the standard library and site-packages,
`codex`, `uv`/`uvx`, `clio_run` and other helper binaries. Find them by magic
number, not by name or extension:

```bash
uv run --no-project --python 3.14 scripts/smoke_macos_bundle.py \
  "CLIO Desktop.app" --bundled --macho-list gact-runtime-macho.json
```

`files` lists paths relative to `gact-runtime`. The release workflow publishes
the same list as the `gact-runtime-macho-<os>` artifact. Sign inside-out
(libraries before the executables that load them, the app last) with
`codesign --force --options runtime --timestamp --sign "$IDENTITY"`. Do not
rely on `--deep`.

`archived` lists Mach-O members of wheels in `document-wheels/`. `codesign`
cannot reach them, and the notary service may inspect archives. If
notarization rejects them, sign the members before the wheels are packed in
`install/build_document_wheels.py`, which also regenerates the hashes that
`uv pip sync --require-hashes` checks.

## Hardened-runtime entitlements

| Executable | Entitlements | Why |
| --- | --- | --- |
| `python/bin/python3.14` | [`gact-runtime.entitlements`](macos/gact-runtime.entitlements): `disable-library-validation`, `allow-unsigned-executable-memory` | Loads third-party extension modules installed under `~/Library/Caches/clio-agent`; ctypes/cffi trampolines |
| `iowarp_core/bin/clio_run` | [`clio-run.entitlements`](macos/clio-run.entitlements): `allow-dyld-environment-variables`, `disable-library-validation` | CLIO passes `DYLD_LIBRARY_PATH` (`arc/runtime_spawn.py`); its link-time libraries already resolve via `@loader_path` |
| Everything else (`codex`, `uv`, `uvx`, `rg`, `gh`, libraries) | none | |

`allow-jit` is not needed: CPython's JIT is off by default and the Node.js and
LibreOffice runtimes are downloaded, separately signed programs outside the
bundle. Verify `clio_run`'s need for `allow-dyld-environment-variables` on the
first signed build by starting a session that uses ARC memory.

## Nothing writes into the bundle

A write into `Contents/` breaks the seal. The runtime guarantees this:

- `runtime.json` runs the interpreter with `-I -B` and
  `PYTHONDONTWRITEBYTECODE=1`, and `protect_bundled_runtime()` re-applies it in
  the server and document/Office preparation, so no child writes `.pyc`.
- Bytecode ships precompiled (`install/precompile_runtime.py`): the startup
  import graph plus every module of `clio_agent`, `clio_kit` and
  `clio_schemas`.
- Package environments, Office and caches live under `~/Library/Caches` and
  `~/Library/Application Support`.

`scripts/smoke_macos_bundle.py` fails if a run adds or changes any file in the
app, and re-runs `codesign --verify --deep --strict`.

## Gatekeeper

`--quarantine` marks the relocated copy as downloaded, prints the
`spctl --assess` verdict and boots the quarantined app when Gatekeeper accepts
it. Add `--require-gatekeeper` to the release workflow once DMGs are notarized
and stapled, so a rejection fails the release.

## Symlinks

The runtime keeps `python` and `python3` as symlinks to `python3.14`. Copying
resources must preserve them; a dereferenced copy ships three 19 MB copies of
the same signed binary.
