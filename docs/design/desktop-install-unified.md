# Desktop install: one interface, fast macOS backend

Status: proposal (2026-11-09). Measured on a clean Mac mini M4 (10 cores, macOS 26.5.1, no
Homebrew, no Command Line Tools) against release v0.9.5.1, main, and PR #1966
(`jaimecernuda-clio-agent-mac-fixes`). Diagram: [desktop-install-unified-diagram.md](desktop-install-unified-diagram.md).
Charts and the chart script: [`evidence/desktop-install-mac-2026-11-09/`](evidence/desktop-install-mac-2026-11-09/).

## Problem

`install/install.sh --desktop` took about 20 minutes on a user's Mac, and the output was
silent while each step ran one after another. The Windows path is a separate, already-optimized
design. The per-OS scripts share no phases, progress format or budgets, so an improvement on one
OS doesn't reach the others.

## What was measured

All local numbers are warm-cache, scratch `CLIO_DESKTOP_DIR` under `/private/tmp`, never
`/Applications`.

### Where a macOS install spends time

| Phase | main | PR #1966 | Notes |
|---|---:|---:|---|
| download, 577 MB DMG, one curl stream | 69–92 s | same | link ran at 6.3–8.3 MB/s |
| sha256 against the release | 1.2 s | 1.1 s | |
| `hdiutil verify` (reads the whole image) | 4.0 s | — | #1966 uses `attach -noverify` |
| attach | 0.2 s | 0.1 s | |
| `codesign --deep --strict` on the mounted image | 8.1 s | — | |
| `ditto` to the destination (1.4 GB, 29,323 files) | 10.5 s | 11.5–11.8 s | |
| `codesign --deep --strict` on the copy | 2.7 s | 2.7 s | |
| **local subtotal** | **≈27 s** | **≈15.5 s** | |
| `--prepare-runtime`, cold HOME | 58.9 s | 58.9 s | see below |
| **total at 8.3 MB/s** | **≈156 s** | **≈144 s** | chart: `phases.png` |

`--prepare-runtime` (58.9 s cold) is mostly network:

- gh CLI (38 MB) download: ready at 17 s.
- Python document packages: ready at 22.3 s (offline wheels, fast).
- pnpm installed from the npm registry: 32 s here (2 s on CI). This is the critical path.
- LibreOffice 26.2.6 (298 MB) from a random mirror, single urllib stream, in parallel: done at
  58.5 s. Locally, `hdiutil attach` with verify takes 3.1 s (0.1 s with `-noverify`), and
  `copytree` takes 5.1 s for 801 MB and 17.6k files.

**The local work is not the 20 minutes.** Even on main it's about 27 s plus about 10 s of local
prepare. The first install downloads about 943 MB (577 MB app + 298 MB Office + 38 MB gh + pnpm
and smaller pieces), mostly one stream at a time. The model
`T ≈ 577/B + 27 + max(336/B + 8, 59)` matches the measured 156 s at B = 8.3 MB/s and gives
about 20 min at B ≈ 0.8 MB/s (chart: `time-vs-link.png`). I could not reproduce 0.8 MB/s
today (GitHub ran at 6.3–8.3 MB/s, PyPI at 1.7, Cloudflare at 2.6). So the user's run is
consistent with a slow link plus serial, silent steps. **Bytes and serialization are the
levers; on fast links local work matters too.**

### Download strategies

| Strategy, 577 MB from GitHub | Time |
|---|---:|
| single stream | 69.4, 88.7, 92.1 s |
| 4 byte ranges in parallel | 85.0, 92.4 s |
| 8 byte ranges in parallel | 73.5, 85.1 s |
| 16 byte ranges in parallel | 100.8 s |

Ranged downloads gave no gain: this link is the bottleneck, not the per-stream rate. Range
parallelism only helps when one stream is throttled (some CDNs or proxies) and it adds
complexity (part files, a final concatenation, 0.5 s). Concurrency across *different* assets
(app + Office + gh) is the useful form, because today those wait on each other.

### Archive formats for the app

| Format | Size | Copy or extract | Notes |
|---|---:|---:|---|
| DMG UDZO (today, zlib, HFS+) | 577 MB | `ditto` 12.8–13.6 s | |
| DMG ULFO (lzfse) | 561 MB | 12.4–13.3 s | |
| DMG ULMO (lzma) | 452 MB | 48–51 s | 4× slower copy |
| DMG APFS + ULFO | 711 MB | 11.6 s | bigger download |
| tar.gz | 578 MB | 13 s | macOS bsdtar has no zstd |
| Apple Archive `.aar`, lzfse | 553 MB | 2.9–3.2 s | build 6 s |
| **Apple Archive `.aar`, lzma** | **428 MB** | **4.2–5.3 s** | build 45 s |

- Apple Archive (`aa`, built into macOS) extracts the app in 3–5 s, against about 12 s for any
  DMG copy. The lzma variant is also the smallest: 149 MB (26%) less to download.
- It streams. `curl … | tee >(shasum -a 256) | aa extract -d DIR` at 8.3 MB/s took 51.8 s,
  against 51.6 s for the download alone. Extraction and hashing are fully hidden behind the
  network.
- Code signatures survive `aa` extraction (`codesign --verify --deep --strict` passes, 3.0 s).
- DMG format choice barely moves copy time, except ULMO, which is much slower. APFS images are
  larger. A DMG stays useful only for manual drag-installs; for that, ULFO is a free small win.

### The wheelhouse + uv option

I rebuilt the bundled Python environment as 219 wheels (from the dist-info RECORD files; 352 MB
deflated, every file except README.txt) and installed them with the bundled uv 0.13.6
(`--offline --no-index --no-deps`):

| | Time |
|---|---:|
| uv install, cold cache, clonefile | 2.4–2.7 s |
| uv install, copy mode | 6.8–8.6 s |
| uv install, warm cache | 0.9 s |
| runtime as `.aar` lzma: expanded tree | 403 MB |
| runtime as `.aar` lzma: wheelhouse | 513 MB |
| runtime as `.aar` lzfse: wheelhouse | 535 MB |

The installed environment imports `clio_agent.gact`, `numpy` and `iowarp_core`. But the
wheelhouse **makes the download about 110 MB larger**, because deflated wheels defeat solid
compression. It gains nothing over `aa` extract of the expanded tree (about 4 s for the whole
app). It also adds a first-run step that can fail. **Not recommended.** If file count matters,
a runtime pack (option B below) gives the same benefit.

Notarization and library-validation caveats don't apply today. The app is ad-hoc signed
(`TeamIdentifier=not set`; `clio-desktop` flags `adhoc,runtime`) and not notarized. They will
apply after a move to Developer ID signing, and they favor shipping signed files in place over
installing native code at first launch.

Shipped bytecode is already small: 2,944 `.pyc` files, and the runtime runs with `-I -B` and
`PYTHONDONTWRITEBYTECODE=1`, so there's no compile step to speed up.

### Why Windows is fast

- NSIS ships the Python runtime as one `gact-runtime.tar.zst` plus `gact-runtime.pack.json`
  (`desktop/src-tauri/tauri.bundled.windows-pack.conf.json`), so the installer writes a few
  files, not about 29k.
- `NSIS_HOOK_POSTINSTALL` (`installer-hooks.nsh`) runs `clio-desktop.exe --prepare-runtime`
  through `nsExec::ExecToLog`. That streams staged progress lines ("Installer checkpoint: …",
  "Unpacking the bundled CLIO runtime...", "Bundled CLIO runtime is ready (N files, Xs)").
- `runtime_unpack.rs` decodes the zstd tar with 8 parallel writers into a staging directory,
  renames it atomically, writes a `pack.sha256` receipt and deletes the archive.
- The same `--prepare-runtime` then runs the shared document prepare. On macOS,
  `prepare_bundled_runtime` just returns the in-bundle directory, so none of this applies.

CI (install-matrix run 42574071708): Windows `desktop: install (NSIS /S)` took 73.8 s,
**excluding** the download. macOS `install.sh --desktop (dmg)` took 177.7 s against a 180 s
budget, **including** the download (about 9 s on the runner), roughly 52 s of
attach/codesign/ditto on the VM disk, and 111.5 s of prepare (about 100 s of it LibreOffice).
The two numbers aren't comparable.

## Design: one install interface

### Shared phases

Every OS runs the same five phases in the same order, with the same progress output. Only the
`install` step has a per-OS backend.

| Phase | Work | Per-OS? |
|---|---|---|
| 1 resolve | Fetch `install-plan.json` from the release: per-asset URL, size, sha256, kind (`app`, `office`, `gh`, `pnpm`) and OS/arch. | no |
| 2 download | Fetch every asset of the plan **concurrently**, with retries and resume (`curl -C -`). A byte-range split is only a fallback when one stream is under 2 MB/s. | no |
| 3 verify | One sha256 per asset, computed **inline** while downloading, against the plan. No second full pass. | no |
| 4 install | Unpack to a staging path, then rename atomically to the destination. | **yes** |
| 5 prepare | `clio-desktop --prepare-runtime`, given the already-downloaded asset paths, so it doesn't fetch them again. | no (already shared) |

On Windows, phase 4 is NSIS plus the existing zstd unpack. On macOS it's `aa extract` (below).
On Linux it stays the AppImage/deb.

### Progress protocol

Both scripts and `--prepare-runtime` print one line per event, human-readable by default and
JSON with `--progress=json`:

```json
{"phase":"download","asset":"app","bytes":214000000,"total":428000000,"rate_mbps":8.1,"t":26.4}
{"phase":"install","state":"done","seconds":0.4,"files":29323}
{"phase":"summary","seconds":{"resolve":0.5,"download":92.6,"verify":0,"install":0.4,"prepare":12.0},"bytes":769000000}
```

- The human form prints a line at each phase start and end, plus download progress at least
  every 5 s with rate and ETA. The user always sees which phase is running and why.
- The final `summary` line is the timing record. CI parses it instead of wrapping steps in
  shell timers, and the same record is what a user pastes into a bug report.
- Windows already emits checkpoint lines through `nsExec`. Map them onto the same phase names
  rather than inventing a second format.

### Per-phase budgets

| Budget | Value | Why |
|---|---:|---|
| local: install + prepare, excluding network wait | ≤ 60 s on CI runners, ≤ 20 s on Apple silicon | catches regressions in our code |
| network bytes for a first install | ≤ 800 MB (≤ 500 MB if Office is deferred) | the lever on slow links |
| total, CI runner | ≤ 180 s | keeps the existing ceiling |

The bytes budget is deterministic, so it can't flake on network speed.

## macOS backend changes

Each row is cumulative, with the expected first-install total at the measured 8.3 MB/s and at
1 MB/s. The 8.3 MB/s numbers come from measured parts; see `phases.png`.

| Change | Where | 8.3 MB/s | 1 MB/s |
|---|---|---:|---:|
| main today | | 156 s | 15.8 min |
| PR #1966: no redundant verify/codesign, progress lines | `install.sh` | 144 s | 15.6 min |
| **A.** Ship the app as `.aar` (lzma), stream download → sha256 → `aa extract` into staging, then rename | release pipeline + `install.sh` | 114 s | 12.9 min |
| **B.** Fetch app, Office and gh concurrently from the install plan; bundle pnpm | `install.sh`, `document_runtime.py`, gact-tui bundle | 108 s | 13.1 min |
| **C.** Prefetch LibreOffice only on demand (first document use) or as a background job after launch | `document_install.py` | 72 s | 8.1 min |

- **A** removes about 12 s of copy on Apple silicon (more on slow disks: CI spent about 52 s
  there) and 149 MB of download. Keep one `codesign --verify` on the result, which costs 3 s.
  Keep the DMG asset for manual installs, as ULFO.
- **B** turns the first-install wall time into "total bytes ÷ link rate" instead of a sum of
  waits. Bundling pnpm removes the 32 s npm registry step that sat on the critical path. It
  helps on fast links only: on a 1 MB/s link the bytes are the same, so the time doesn't drop.
- **C** is the largest lever on slow links: 298 MB of the 769 MB are LibreOffice. Hosting the
  LibreOffice DMG as a GitHub release asset (pinned and hashed) would also give it the same,
  predictable CDN as the app.
- Smaller fixes in the same area: `office.py` `_extract` should use `hdiutil attach -noverify`
  (−3 s) and `ditto` or `aa` instead of `copytree`.
- Option **B′, a runtime pack on macOS too** (the Windows `tar.zst` and Rust unpacker): one
  code path for all OSes, but the pack can't be deleted from inside a signed `.app`, so it costs
  about 400 MB of extra disk. A is cheaper and gets the same install time.
- **Size diet** (independent of the above): the largest compressed pieces are `codex_cli_bin`
  83 MB, `document-wheels` 107 MB (56 MB of it Node), `pyarrow` 24 MB and `googleapiclient`
  8 MB. Making `codex_cli_bin` an on-demand download alone would cut about 20% of the app
  download.

## CI changes

- The macOS desktop job runs only on schedule or dispatch (`install-matrix.yml`:
  `if: github.event_name != 'pull_request'`). A budget a PR can't fail isn't held. Run a
  desktop smoke on PRs that touch `install/`, the desktop bundle pins or `document_*`, and add
  `required_in` for that entry in `scripts/ci/install_budgets.json`.
- Split the single 180 s entry into the three budgets above, read from the `summary` line.
  The macOS run is at 177.7 s today, so the current budget fails on noise, not on regressions.
- Make the Windows job include its download, so the two OSes report the same phases.

## Open questions

1. The 20 minutes wasn't reproduced. The model needs about 0.8 MB/s effective. The user's
   `/Applications` copy had been patched in place several times, so forensics were
   inconclusive. A `summary` line from the next user install would settle it.
2. Defer LibreOffice (C) or keep "everything ready at first launch"? C halves slow-link time,
   but the first document task then waits for the 298 MB download.
3. Developer ID signing and notarization: once adopted, `aa` keeps signatures intact, but the
   wheelhouse and any first-run native install get harder. This favors A over the wheelhouse.

## Side finding

During `--prepare-runtime` with an isolated `HOME`, the store migration reported
`store_unavailable`: it detected the user's running clio-core daemon through machine-wide
core-hosts state. Prepare isn't fully isolated by `HOME`.
