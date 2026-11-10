# Desktop install: one interface, fast macOS backend

Status: shared steps, A and B implemented; C declined (2026-11-09). Measured on a clean Mac
mini M4 (10 cores, macOS 26.5.1, no Homebrew, no Command Line Tools) against release
v0.9.5.1, main, and PR #1966 (`jaimecernuda-clio-agent-mac-fixes`). The implementation is
stacked on #1966 with #1978 (Developer ID signing) merged in; see
[Implementation and measured results](#implementation-and-measured-results).
Diagram: [desktop-install-unified-diagram.md](desktop-install-unified-diagram.md).
Charts and the chart scripts: [`evidence/desktop-install-mac-2026-11-09/`](evidence/desktop-install-mac-2026-11-09/).

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

This section is the proposal. Where the implementation differs (a text plan instead of
`install-plan.json`, pnpm prefetched instead of bundled, no option C), see
[Implementation and measured results](#implementation-and-measured-results).

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

Both scripts print human-readable lines by default. With `--progress json` (`-Progress json`
in PowerShell, or `CLIO_INSTALL_PROGRESS=json`), stdout carries only JSON lines and the
readable messages move to stderr. As implemented (a real run on this Mac):

```json
{"event":"step","step":"download","state":"begin"}
{"event":"progress","step":"download","bytes":214000000,"total":747076163}
{"event":"step","step":"download","state":"end","seconds":36.7}
{"event":"summary","ok":true,"seconds":{"resolve":0.1,"download":36.7,"verify":0.0,"install":2.6,"prepare":17.3},"total":56.7,"bytes":747076163}
```

A failure ends with `{"event":"summary","ok":false,"step":"verify","error":"…",…}`.
`--prepare-runtime` keeps its own checkpoint lines; they go to stderr inside the `prepare`
step.

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

As implemented, `scripts/ci/install_budgets.json` holds one entry per step for the macOS
`desktop-install` job, read from the summary line by `check_budgets.py --install-events`:
resolve 30 s, download 120 s, verify 30 s, install 60 s, prepare 150 s, total 180 s. A failed
install fails its step's row and the total. The bytes budget isn't implemented yet.

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

As implemented: a new `desktop-install` job (macos-15) in `install-matrix.yml` runs
`install.sh --desktop --progress json` into `$RUNNER_TEMP/Applications` and enforces the step
budgets. On pull requests it runs only when the PR touches the installers (`install/`, the
budget files, `document_runtime.py`, `office.py`); it always runs on schedule and dispatch.
The existing `desktop:` jobs are unchanged, so the Windows job still excludes its download.

## Open questions

1. The 20 minutes wasn't reproduced. The model needs about 0.8 MB/s effective. The user's
   `/Applications` copy had been patched in place several times, so forensics were
   inconclusive. A `summary` line from the next user install would settle it.
2. Defer LibreOffice (C) or keep "everything ready at first launch"? C halves slow-link time,
   but the first document task then waits for the 298 MB download.
3. Developer ID signing and notarization: once adopted, `aa` keeps signatures intact, but the
   wheelhouse and any first-run native install get harder. This favors A over the wheelhouse.
   Answered below: an `.aar` of the stapled app keeps the ticket.

## Implementation and measured results

Decided while the user was away: shared steps plus A and B; not C (LibreOffice stays
mandatory and is installed up front, but downloads alongside the app). Windows changes only
its output.

### What changed

- **Shared steps, both scripts.** `install.sh` and `install.ps1` run resolve, download,
  verify, install (and on macOS/Linux, prepare). Each step is timed. Both scripts print
  `Done in X s (resolve …, download …)` and support `--progress json`. The Windows download
  and NSIS setup are unchanged.
- **A, streamed `.aar`.** The release pipeline archives the stapled app with
  `aa archive -a lzma` after notarization (`scripts/macos_release_notarize.sh archive-app`),
  and `verify` unpacks it again and checks the app. It's published as
  `CLIO.Desktop-<triple>-bundled.app.aar`. `install.sh` streams it:
  `curl | tee (sha256, part file) | aa extract` into a staging folder beside the
  destination. Then it checks the SHA-256 against the release manifest, runs one
  `codesign --verify --deep --strict`, and swaps the app in, keeping the old one until the
  new one is in place. Asset order: `.app.aar`, then the updater's `.app.tar.gz` (which
  v0.9.5.1 already has), then the DMG. The DMG stays the fallback and is what
  `--download-only` keeps.
- **Notarization.** An `.aar` can't be notarized or stapled itself, and doesn't need to be:
  the stapled ticket is stored inside the app bundle (`Contents/CodeResources`), and `aa`
  preserves it. Checked by round-tripping a stapled app through `aa archive`/`aa extract`
  and running `spctl -a -vv` on the result. Files fetched by `curl` carry no quarantine
  attribute, as with the updater's `.tar.gz`.
- **B, concurrent fetch.** The release publishes `install-plan.<triple>.bundled.txt`
  (`scripts/release_install_plan.py`, listed in that triple's `SHA256SUMS`). It names
  LibreOffice and pnpm (the JS package and its native binary) with SHA-256, size and URL.
  `install.sh` fetches them at the same time as the app into a downloads folder, and passes
  it to `--prepare-runtime` as `CLIO_INSTALL_DOWNLOADS`. `office.py` and
  `document_runtime.py` use a file there only when its bytes match the pin; otherwise they
  download as before.
- **pnpm isn't bundled in the app.** pnpm 12 runs as a native binary
  (`@pnpm/exe.<os>-<arch>`), so bundling it would mean another signed executable inside
  the app. Prefetching both tarballs through the plan gives the same result offline.
- **`office.py`**: `hdiutil attach -noverify` for the LibreOffice DMG; its SHA-256 is already
  checked.
- **CI**: the `desktop-install` job and per-step budgets above.

### Measured on this Mac

Scratch `CLIO_DESKTOP_DIR`, isolated `HOME`, v0.9.5.1 (chart: `before-after.png`).

| Run | download | verify | install | prepare | total |
|---|---:|---:|---:|---:|---:|
| #1966 DMG, session 1 (10.5 MB/s) | 52.6 | 0.1 | 14.5 | 40.7 | **108.0 s** |
| new script, v0.9.5.1 `.app.tar.gz`, no plan, session 1 | 54.9 | 0.0 | 2.4 | 45.1 | **102.9 s** |
| #1966 DMG, session 2 (17.2 MB/s) | 31.9 | 0.1 | 14.7 | 45.2 | **92.0 s** |
| new: streamed `.aar` + plan prefetch, session 2 (two runs) | 36.7 | 0.0 | 2.6 | 17.3–17.7 | **56.7–57.1 s** |

- **Install step**: 14.5 s → 2.4–2.6 s. It's only the final codesign check; the unpack
  overlaps the download. Unpacking the 428 MB `.aar` from a local file takes about 4 s.
- **Download**: the `.aar` is 428 MB against 576 MB for the `.tar.gz` and 549 MB for the
  DMG. Fetching the app, LibreOffice (298 MB) and pnpm (21 MB) at the same time took 47.9 s,
  the same as the app alone: GitHub limits one stream (about 12 MB/s) below this link's
  capacity, so LibreOffice arrives for free.
- **Prepare** with the plan's files present: 14.0–14.6 s against 31.3–31.8 s cold, on the
  same app, alternating runs. Office goes from about 31 s to 8.7 s, and pnpm from about
  10 s (npm registry) to 0.6 s.
- **How the after run was made**: v0.9.5.1 has no `.aar`, no plan, and a `--prepare-runtime`
  that doesn't read `CLIO_INSTALL_DOWNLOADS`. So the measured "after" app is v0.9.5.1 with
  this branch's `document_runtime.py`/`office.py` changes applied, ad-hoc re-signed, and
  archived as an `.aar`. A wrapper `curl` served it with the plan from 127.0.0.1, held to
  11.5 MiB/s (12 MB/s, the measured GitHub single-stream rate). LibreOffice and npm came
  from the real network. Session 2's DMG arrived at 17.2 MB/s, so the 12 MB/s hold
  understates the gain.
- **Expected from the next release** (it ships the `.aar` and the plan): at 12 MB/s per
  stream, about 57 s as measured. At session 2's 17.2 MB/s, the 428 MB `.aar` takes about
  25 s and LibreOffice finishes within it, so about 0.1 + 25 + 2.6 + 15 ≈ 43 s. Compare
  92–108 s for #1966 and about 156 s for main. With v0.9.5.1 the script streams the
  `.tar.gz` and saves only the copy.
- **Windows**: not measured; its behaviour is unchanged.

### Launch after install

The `.aar` doesn't change the first launch. Measured with `scripts/smoke_desktop_package.py
--quit`, alternating DMG and `.aar` installs. Each run used its own `HOME`, `TMPDIR` and
`CLIO_DESKTOP_DIR`, with `--agent-home` pointing at the data that install's prepare wrote.
"Ready" is launch to an authenticated `/v1/health` with clio-core attached and ARC ready.
"Quit" is the time from quit to the whole process tree exiting. Each launch attached its own
private clio-core; no other CLIO's core ran during a run.

**Same app on disk.** The DMG install (`ditto`) and `aa extract` produce identical trees:
33,548 entries with matching mode, size, symlink target, flags, owner and mtime. Both carry
only `com.apple.provenance` xattrs, with no `com.apple.quarantine`. `CodeResources` is
byte-identical, and `codesign --verify --deep --strict` passes on both (2.6 s each). `spctl`
gives the same verdict for both: it rejects v0.9.5.1 because the release is ad-hoc signed
(`flags=adhoc,runtime`), and #1978's Developer ID build replaces that. With no quarantine
attribute there's no Gatekeeper first-launch scan. syspolicyd and amfid logged no assessments
in the launch window, so library validation costs the same for both.

**v0.9.5.1 with #1966's runtime fixes** (4dbb7e16d, a307eb71c and 3f12cb7d3 applied to the
bundled `clio_agent`, then re-signed ad hoc). SearXNG syncs against CLIO's own Python with
`--no-python-downloads`. A 0.2 s process sampler saw no `/usr/bin` developer-tool stub during
these launches. The bare `python3 -c` helpers resolve to the bundled interpreter, because
`local_environment()` puts it first on `PATH`.

| Run | first: ready / quit (s) | second: ready / quit (s) |
|---|---|---|
| 5 DMG | 11.4 / 4.5 | 12.5 / 4.4 |
| 6 `.aar` | 12.0 / 3.9 | 12.1 / 4.0 |
| 7 `.aar` | 11.4 / 3.9 | 13.3 / 4.8 |
| 8 DMG | harness failure¹ | 13.3 / 5.2 |
| 9 DMG | 12.9 / 4.4 | 12.1 / 4.0 |
| 10 `.aar` | 12.5 / 4.6 | 13.0 / 4.0 |

Medians: DMG first 12.2 s, second 12.5 s; `.aar` first 12.0 s, second 13.0 s. Runs 9 and 10
were sampled the same way, with no stub, no CPython download and no new CLT dialog, and no
clio-core left running after any quit.

¹ The smoke script read `cmdline()` of a short-lived helper macOS wouldn't expose and stopped
with `psutil.AccessDenied`. The Desktop log shows that launch reused the prepared runtime and
reached ready. Fixed in 5d448d3d5: an unreadable descendant is skipped, since it can't be the
backend.

**v0.9.5.1 as released.** Labelled separately: on each first launch, SearXNG's
`uv sync --python 3.12` downloaded a CPython 3.12 into the service cache. That raised the
user's "command line developer tools" dialogs for `python3` and `install_name_tool`. #1966
fixes this.

| Run | first: ready / quit (s) | second: ready / quit (s) |
|---|---|---|
| 1 DMG | 12.4 / 3.9 | 12.9 / 3.0 |
| 2 `.aar` | 12.1 / 3.7 | 12.7 / 4.1 |
| 3 `.aar` | 12.1 / 3.7 | 12.4 / 4.3 |
| 4 DMG | 12.9 / 4.0 | 13.3 / 5.3 |

- **Ready is about 11.4–13.3 s for both formats**, first or second launch. The spread within
  a format is wider than the gap between formats. A second launch isn't faster because
  prepare has already done the one-time work; the 12 s is backend start, clio-core attach and
  ARC.
- **Install-time prepare is what keeps the first launch clean.** Launched without
  `--agent-home` (an empty agent home, as CI did before), "ready" still came at 14.8 s (`.aar`)
  and 11.8 s (DMG). But the first-run setup (about 1.8 GB: LibreOffice, gh, Python document
  packages, pnpm) kept running after ready, so quit took 42.6 s and 39.4 s. The ready metric
  misses this; quit exposes it. A quit budget (warm quits are 3.0–5.3 s) would catch a prepare
  that silently stopped running at install time. Not added yet, because Linux quit times
  aren't measured.
- **CI** (3db792f8a): the macOS `desktop-install` job now launches the app it just installed,
  first and second, with `--agent-home` pointing at that install's prepared data, and
  `check_budgets.py --desktop` holds it to the 60 s `backend_ready_seconds` budget. The macOS
  launches in the release `desktop` job get the same `--agent-home`. Before this, they
  measured a cold home and redid the whole prepare.

## Under 60 s: overlap and trims (follow-up to the 71.1 s live run)

The live run of the merged #1985 took 71.1 s: download 45.2, install 2.6, prepare 23.0. Three
changes cut what still ran in series.

- **(g) Prepare while first-launch files download.** The plan's first line is now
  `capability waits-for-partial-downloads`. When the installer sees it, it no longer waits
  for LibreOffice and pnpm before verify, install and prepare; it waits for them after
  prepare. The app (`office.installer_download`, also used for pnpm) waits while
  `NAME.part` grows and takes `NAME` once the installer has verified and renamed it. A
  partial file that stops growing for 60 s is abandoned and the app fetches it. Prefetch
  curls stop after 60 s below 1 B/s. An older app has no such wait and would download the
  file a second time, so a plan without the line keeps the old order. Progress events for
  those files stay `"step":"download"` while later steps run; the download step's seconds
  then cover the app only, and prepare includes any wait for Office.
- **(d) Signature check during prepare.** For a release install, `codesign --verify --deep
  --strict` runs in the background once the app is in place, and its result is collected
  after prepare. If it fails, or the run stops first, the new app is removed and the
  previous app restored. The archive already matched the release's SHA-256, and arm64
  enforces code signatures at exec, so the gap is a prepare run by a signature-invalid app
  whose bytes matched the release. `--desktop-package` test builds keep the check before
  the swap.
- **(e1) Drop Codex's unused helpers on macOS.** `codex-exec-server` and
  `codex-code-mode-host` (126 MB raw, 31 MB LZMA in the `.aar`) are pruned in
  `build-gact-runtime.sh`, with their `RECORD` lines. The `codex` binary stays: it is the
  macOS write fence (`runtime/sandbox.py`), and dropping it would leave no fence. A live
  probe through `compose_codex_spawn` with the helpers removed still denied writes outside
  the territory and allowed them inside; the Codex provider itself never spawns `codex`.
  Linux keeps the helpers until a Linux run proves the same.

Expected, with the 71.1 s link: about 3 s saved by (e1), the Office tail and Node prepare
overlapping the app download by (g), and 2.6 s by (d). Together with (a), the Node
entitlements in #1978, that is about 52–55 s.

Measured live on the Mac mini (12 headless installs alternating A = develop and
B = (g)+(d)+(e1), app archive from a mirror held to 10.7 MB/s, LibreOffice from upstream,
isolated `HOME`; runs that overlapped other traffic excluded): median total **63.3 s → 59.4
s**; download 45.2 → 42.3, install 3.0 → 0.0, prepare 15.0 → 17.0 (now also waiting for
Office).

- **(e3) Playwright's Node.js on first browser use.** The `playwright` wheel carries the
  official Node.js binary its driver runs on (`playwright/driver/node`, 122 MB raw, 28 MB
  LZMA in the `.aar`), needed only when the agent first opens a browser.
  `install/stage_playwright_node.py`, run by both `build-gact-runtime.sh` and `.ps1`,
  finds the byte-identical binary in the nodejs.org release archive, fails the build if it
  differs, writes `playwright/driver/clio-node.json` (archive URL and SHA-256 from
  `SHASUMS256.txt`, binary SHA-256 from the hash-locked wheel) and deletes the binary and
  its `RECORD` line. `gact/browser/driver.py` fetches the archive on first browser use
  (browser tools and HTML rendering), checks size, both hashes and `node --version`, and
  moves the result into `<cache>/browser/node-<version>-<hash>` in one rename, so a failed
  or interrupted fetch leaves nothing that looks installed. Like Chromium's first-use
  download, the fetch outlives the action and answers `browser_runtime_preparing` with
  bytes so far ("first use only: 12 of 28 MB"); it tries 3 times and then answers
  `browser_runtime_install_failed`, and the next browser use starts over. Playwright is
  pointed at the binary through its own `PLAYWRIGHT_NODEJS_PATH`, which a person's own
  setting overrides. The same code runs on macOS, Linux and Windows. The macOS binary is
  signed by the Node.js Foundation with hardened runtime and the JIT entitlements, so the
  fetched copy runs as is; #1978's signing script picks entitlements by content, so one
  file fewer to sign needs no change there.

  Windows finding: `build-gact-runtime.ps1` deleted every `*.exe` under site-packages except
  Codex and iowarp-core, which removed `playwright\driver\node.exe` and nothing restored it,
  so Windows bundles had no browser driver at all (from reading the build, not run on
  Windows). Staging runs before that sweep, so Windows now fetches it on first use too.

  Expected: 28 MB less to download (about 1.5 s at the 18 MB/s of the 71.1 s run, 2.6 s
  on the 10.7 MB/s A/B mirror) and 122 MB less to unpack.

## Side findings

- During `--prepare-runtime` with an isolated `HOME`, the store migration reported
  `store_unavailable`: it detected another CLIO's running clio-core daemon through
  machine-wide core-hosts state. Prepare isn't fully isolated by `HOME`. No action taken.
- v0.9.5.1's prepare breaks the installed app's code seal: something launched during
  prepare runs the bundled Python without `-B` and writes about 20 `.pyc` files
  (`encodings`, `xml`, `plistlib`, `office_compat`, …) into `Contents/Resources/gact-runtime`.
  `codesign --verify --deep --strict` then reports "a sealed resource is missing or
  invalid". Both #1966 and this branch install the release as-is, so both show it. Main
  already fixes it (d3ca42a2e, `protect_bundled_runtime`), so it disappears with the next
  release.
- On npm, `HEAD` returns no `Content-Length`, so the plan generator now reads the size from
  a one-byte range request.
- **One Desktop per machine.** tauri-plugin-single-instance binds a socket hard-coded to
  `/tmp/ai_iowarp_clio_desktop_si.sock` (the identifier, not `HOME`, `TMPDIR` or the install
  folder, because of the 104-byte socket path limit). A second Desktop, even a scratch install
  with its own `HOME`, focuses the running one and exits 0 with an empty log. Side-by-side
  installs and parallel launch tests are impossible: these measurements had to stop the user's
  CLIO and take turns with other sessions. A per-install identifier, or a socket under the
  install's own data folder, would fix it.
- `smoke_desktop_package.py --quit` counts clio-core processes started after launch across
  the whole host, so another session's cores show up as `clio_core_left_running`. That
  happened once here: 4 cores from a concurrent test run. It should count only the launched
  tree's descendants.
- Measuring this caused the user's "command line developer tools" dialogs: an early timing
  loop ran a bare `python3` and an `xcrun stapler` loop, both of which resolve to CLT stubs on a
  Mac without Xcode tools. The measurement scripts now use absolute interpreters and
  `date +%s`. The `install.sh` desktop path calls no stub.

## Windows and Linux parity

The same install shape should hold on all three operating systems: an install plan with a
sha256 for each asset, concurrent downloads hashed as they arrive, one runtime archive that
is streamed into place and swapped in atomically, the shared phases with progress lines and
timings, and CI budgets for each phase. This section compares Linux and Windows against the
macOS backend above.

| Shape | macOS | Linux (`install.sh --desktop`, deb/rpm) | Windows (`install.ps1 -Desktop`, NSIS) |
| --- | --- | --- | --- |
| Install plan, sha256 per asset | `install-plan.<target>.bundled.txt` | same plan, already published | now published for `x86_64-pc-windows-msvc` (Office MSI, pnpm, native `@pnpm/exe.win32-x64`); read and checked against `SHA256SUMS` |
| Concurrent downloads | app + plan in parallel | app + plan in parallel | now one `curl.exe` per item in parallel (`Save-ClioDownloads`), serial fallback without `curl.exe` |
| Inline hash | `tee` into `shasum` | app download now hashed inline too (`fetch_hashed`); no second `file_sha` pass | hashed while the file grows; a second pass runs only on a mismatch |
| Prefetch handed to prepare | `CLIO_INSTALL_DOWNLOADS` | `CLIO_INSTALL_DOWNLOADS` | `CLIO_INSTALL_DOWNLOADS` set for the setup, which runs `--prepare-runtime` from its postinstall hook |
| Single archive, atomic swap | `.aar` streamed, folder renamed | `gact-runtime.tar.zst` in the deb/rpm, verified and unpacked by the package's postinst, swapped with a receipt ([Linux runtime archive](#linux-runtime-archive)) | `.tar.zst` pack, extracted by 8 writers, swapped with a `pack.sha256` receipt (already in gact-tui) |
| Phases with progress and timings | resolve/download/verify/install/prepare | same five | same five: the setup's `--prepare-runtime` appends timed lines to `CLIO_INSTALL_LOG`, which `install.ps1` follows live and times as `prepare` (needs the gact-tui `install_log` change) |
| Per-phase CI budgets | `desktop-install-macOS` | `desktop-install-Linux` (now) | `desktop-install-Windows` (now) |

### What changed here

- `scripts/release_install_plan.py` knows the Windows target, and `clio-bundles.yml`
  publishes the plan for every bundled target.
- `document_runtime.PNPM_PACKAGES` pins `@pnpm/exe.win32-x64`, so the Windows plan can name it.
- `install.ps1` resolves the manifest, expected size and plan up front, downloads everything
  concurrently with an inline hash, verifies from that hash, and passes the verified prefetch
  folder to the setup. `-Unattended` runs the setup silently for CI. When the plan has
  `capability waits-for-partial-downloads` (g), it starts the setup as soon as its own exe is
  verified: the plan files keep downloading as `NAME.part`, renamed to `NAME` once their
  hash matches, and the setup's prepare waits for them. Measured on a loaded Windows box
  with the 373 MB LibreOffice MSI from the TDF mirror: download step 97.6 s to 8.1 s, and
  with gact-tui#1062's uncompressed setup the whole install 397 s to 224 s.
- `install.sh` hashes the generic app download (deb, rpm) inline, like the macOS archive.
- `install-matrix.yml` runs `desktop-install` on macOS, Ubuntu and Windows, with the same
  first and second launch smoke and each OS held to its own budget rows.

### Prepare speed, shared by all three operating systems

- **One read of the Windows pack.** gact-tui `runtime_unpack.rs` hashes the archive from the
  bytes the decompressor reads; `runtime_pack.rs` compares that hash before activating the
  staged runtime, instead of a separate full SHA-256 pass first. Unpack progress reports
  files written and MB read. The managed-process stop sweep runs during the unpack.
- **Timed record.** `--prepare-runtime` (all OSes) appends each line, with seconds since it
  started, to `CLIO_INSTALL_LOG` when set; on Windows also to `data\runtime-install.log` in
  the install folder, so a timed install can be read after the setup closes.
- **Store upgrade in parallel.** `document_install` runs `clio-agent migrate` on the main
  thread (clio-core's native handles stay there) while the packages are prepared on a
  worker; leaving waits for both.
- **Node.js during the Python probe.** `prepare_document_runtime` runs the full Python
  import probe on a worker while the Node.js packages are prepared, and repairs the Python
  environment afterwards only if that probe failed.

Measured live on an M-series Mac with a scratch HOME and the 0.9.5.1 bundled runtime plus
this branch's code: `document_install` with the plan prefetched fell from 31.9 s to 15.9 s,
and with nothing prefetched from 38.1 s to 28.8 s (the LibreOffice download is then the
critical path; the plan prefetch hides it).

### Gaps not closed here

- **Windows prepare as its own process.** The NSIS postinstall still runs
  `--prepare-runtime` itself; `install.ps1` times it from the record above.
- **First load of native extensions.** On a fresh install, the Python import probe spends
  about 11 s loading native libraries for the first time (macOS code-signature checks; on
  Windows, Defender scans each new file). It is the remaining critical path on macOS.

## Linux runtime archive

The Linux deb and rpm now ship the bundled runtime the way Windows does: one
`gact-runtime.tar.zst` plus its `gact-runtime.pack.json`, made by
`install/pack-gact-runtime.sh` with the shared `install/compress_runtime.py` (level 19,
128 MB window). The package's maintainer scripts (`install/linux-package`) unpack it into
`/usr/lib/CLIO Desktop/gact-runtime`. That is the directory the desktop already reads on
Linux (gact-tui `bundled_runtime_dir`), so gact-tui needs no change. The runtime stays
root-owned and not writable by group or others. The precompiled `.pyc` files from
`install/precompile_runtime.py` travel inside the archive, and the packer refuses a runtime
without them.

### Measured before

| Item | v0.9.5.1 amd64 deb | Source |
| --- | --- | --- |
| Package | 722,401,870 bytes, gzip `data.tar.gz`, no maintainer scripts | release asset |
| Files | 32,250 files, 4,287 directories, 1.86 GB unpacked; `gact-runtime` alone 32,243 files, 1.80 GB | `data.tar.gz` listing |
| `install.sh --desktop` on CI | 58.1 s: resolve 0.8, download 2.6, verify 0.0, install 21.8, prepare 32.8 | install-matrix 42765370459, `install-events.jsonl` |
| dpkg `Unpacking clio-desktop` | about 11.8 s, plus about 2 s setup and triggers | install-matrix 42765370459, package job |
| `apt remove` | 5.2 s | same run, `timings.jsonl` |

The older 75.9 s figure in #1987 had install at 23.8 s and prepare at 44.0 s. The 44 s was
prepare, not dpkg.

### The archive, measured on the real 0.9.5.1 Linux runtime

These were measured on the M4 Mac, using the runtime extracted from the 0.9.5.1 deb.

| Step | Result |
| --- | --- |
| Pack (`pack-gact-runtime.sh`) | 1,783 MiB tar → 443.3 MiB (464.9 MB) in 150 s, 10 workers, build time only |
| Package payload | 722 MB gzip deb → about 465 MB plus the shell (-36%) |
| zstd decode alone | 1.1 s for 1.9 GB |
| sha256 of the archive | 0.9 s, run alongside the unpack |
| postinst unpack on APFS | 19 s, all from creating the files (raw `tar -x` alone 13 s); not an ext4 prediction |
| Round trip | byte-identical to the source tree (`diff -r`) |

### How it installs, upgrades and removes

- **postinst / `%post`** (POSIX sh; rpm embeds the text). Reads the manifest, skips when the
  receipt matches, then hashes and unpacks in parallel into `gact-runtime.installing`. It
  checks the hash and `runtime.json`, writes the receipt, runs one `sync -f`, then swaps the
  directories through `gact-runtime.previous`, rolling back on failure.
- **prerm / `%preun`** on remove (`remove`, `0`): deletes the unpacked runtime so the
  package manager can remove the directory. Upgrades keep it, and the new postinst swaps it.
- **postrm / `%postun`** on remove, purge or `0`: a safety net that also removes the empty
  directory.
- **Ownership.** Only a runtime carrying the `.clio-pack-sha256` receipt is ever deleted. An
  exploded, package-owned runtime from an older release is left to its package manager.
- **Lite variant.** It ships the same scripts. When a lite package replaces a bundled one,
  its postinst finds no manifest and removes the unpacked runtime.
- **rpm upgrade from an exploded rpm.** rpm erases the old package's files after the new
  `%post`, which deletes what it just unpacked. The package therefore also ships the script
  as `gact-runtime.unpack.sh`, and `install.sh` runs it after `apt-get`/`dnf`. When the
  receipt matches it does nothing; otherwise it repairs the runtime. Without `install.sh`,
  `dnf reinstall clio-desktop` repairs it.
- **Repair.** `dpkg-reconfigure clio-desktop` or `apt install --reinstall` re-runs the
  unpack from the archive. The package keeps the archive (dpkg owns it), at a cost of about
  465 MB of disk space; Windows deletes its archive instead.
- **Downgrade to a pre-archive release** may leave stale files in the runtime. Remove the
  package first.
- `zstd` is a package dependency of the bundled variant (default on Ubuntu and Fedora).

Lifecycle checks ran under `/bin/dash` on a scratch root (`CLIO_PACKAGE_ROOT`), and all
passed:

- fresh install;
- no-op rerun;
- bad hash, and a truncated archive: refused, live runtime kept, no staging left;
- upgrade over an exploded runtime;
- rpm `%post 2`, `%preun 1`, `%postun 1`;
- bundled to lite;
- deb remove and purge;
- rpm erase;
- rpm exploded-upgrade repair;
- a relative root refused.

### Expected Linux time, still to be measured on real Linux

| Step | v0.9.5.1 (CI) | Expected |
| --- | --- | --- |
| download | 2.6 s (722 MB) | about 1.8 s on CI; about 14 s less at the 18 MB/s seen on the Mac |
| install | 21.8 s (dpkg unpack ~11.8 s) | dpkg writes one file; the postinst extract plus one sync replace 32,000 fsyncs; estimate 12-17 s |
| prepare | 32.8 s | unchanged by this; #2000's (g)/(d)/(e3) apply once released |
| uninstall | 5.2 s | about 1-2 s (one `rm -rf` and four package files) |

Needs a real Linux machine (none was reachable from the Mac):

- a bundled build with the pack;
- `install.sh --desktop` timing for deb and rpm;
- upgrade from 0.9.5.1, remove, purge, reinstall and `dpkg-reconfigure` on deb, and the same
  on rpm (including the exploded-upgrade repair);
- the install-matrix Linux leg and `desktop-bundle-smoke`, which now builds the same layout.

The `desktop install (Linux)` budgets stay unchanged until that measurement exists.

A single cross-platform unpacker (the Rust `runtime_unpack.rs`, run on every OS, design A2
in `install-and-startup-implementation.md`) would replace the shell extract. It needs a
gact-tui change and a pin bump, so it is left as follow-up work.
