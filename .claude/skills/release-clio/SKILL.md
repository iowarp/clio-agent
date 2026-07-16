---
name: release-clio
description: Cut a clio-agent release (vX.Y.Z) end-to-end — merge feature→develop→main, bump version, pin submodules, tag, push, and verify all CI (PyPI, bundles, ghcr) goes green. Use when asked to "do a release", "cut vX.Y.Z", "publish", or "ship".
---

# Releasing clio-agent

clio-agent ships across **PyPI** (the `clio-agent` package), **GitHub Release assets**
(installer scripts + `clio-tui-{os}-{arch}` binaries + desktop bundles + web zip), and
**ghcr.io** container images (`clio-{api,web,tui}`). A release is driven by **pushing a
`v*` git tag** to `iowarp/clio-agent` — three workflows fire on the tag:

- `release.yml` → builds sdist+wheel, publishes to **PyPI** (OIDC trusted publishing).
- `clio-bundles.yml` → builds + uploads **GH release assets** (installers, `clio-tui-*`,
  desktop `.msi/.dmg/.deb/.AppImage/.rpm`, `clio-web-*.zip`).
- `docker.yml` → builds + pushes **ghcr.io/iowarp/clio-{api,web,tui}** images.

Two git submodules ship pinned: `external/gact-tui` (the TUI/web/desktop frontend) and
`external/clio-agent-marketplace` (blueprints). Both must point at a released tag.

## Preconditions
- Working tree clean. Revert runtime churn first: `git checkout -- src/clio_agent/providers/handshake/sources/data/model_limits.json` (timestamp-only regen).
- You are an org admin (tags, ghcr). `gh auth status` ok.
- Decide the version: `vX.Y.Z`. Patch bump for fixes; the user usually says "0.5.x+1".

## Steps

### 1. Marketplace submodule release (do FIRST — clio-agent will pin its tag)
```sh
cd external/clio-agent-marketplace
git checkout main && git merge --ff-only <feature-branch>   # or PR-merge
git tag -a vX.Y.Z -m "release: vX.Y.Z — <summary>"
git push origin main && git push origin vX.Y.Z
cd ../..
```

### 2. Pin submodules in clio-agent
```sh
git -C external/gact-tui fetch origin --tags && git -C external/gact-tui checkout vA.B.C   # the aligned gact-tui release
git -C external/clio-agent-marketplace checkout vX.Y.Z
```

### 3. Integrate branches (gitflow: feature → develop → main)
```sh
git fetch origin                       # ALWAYS — others push develop/main via PRs
git checkout develop && git merge --no-ff origin/develop   # reconcile remote first
git merge <feature-branch>             # bring the release work onto develop
git checkout main && git merge --no-ff origin/main         # reconcile remote first
git merge --no-ff develop -m "Merge develop: release vX.Y.Z — <summary>"
```

### 4. Bump version (these must all agree; release.yml hard-checks tag == `uv version`)
- `pyproject.toml` → `version = "X.Y.Z"`
- `src/clio_agent/__init__.py` → `__version__ = "X.Y.Z"`
- `uv.lock` → run `uv lock` (updates the clio-agent entry), commit it.
- `install/README.md` → bump the `CLIO_VERSION=` example.
- Commit the submodule gitlink bumps + version together: `chore(release): vX.Y.Z`.

### 4b. Roll the CHANGELOG (GACT-contract surface only)
`CHANGELOG.md` tracks changes to clio-agent's **GACT-contract surface**
(the TUI/HTTP surface) — not every internal change. Before tagging:
- Rename the `## Unreleased` heading to `## [X.Y.Z] — YYYY-MM-DD` (today's
  date), keeping the Added/Changed/Fixed/Removed sections it accumulated.
- Add a fresh empty `## Unreleased` block above it for the next cycle.
- If this release had **no** contract-surface change, say so explicitly
  under the new version heading rather than leaving a stale Unreleased block.
- Commit alongside the version bump (`chore(release): vX.Y.Z`).

### 5. Verify locally BEFORE tagging (cheap; avoids failed CI cycles)
```sh
uv version --short                     # == X.Y.Z
uv run python -c "import clio_agent, clio_agent.config, clio_agent.gact.app, clio_agent.ui.cli; print('ok')"
uv build && ls -lh dist/               # BOTH wheel AND sdist must be < 100 MB (PyPI limit)
# confirm no direct/git deps leaked into wheel metadata:
python3 -c "import zipfile,glob; z=zipfile.ZipFile(glob.glob('dist/*.whl')[0]); m=[n for n in z.namelist() if n.endswith('METADATA')][0]; print('git+ in METADATA:', any('git+' in l for l in z.read(m).decode().splitlines()))"
```

**Memory budget gate (release-gating, #930/#935).** Bounded memory is a release
requirement: the 3-session claude-haiku acceptance load must hold the recorded
budget in `scripts/mcp_mem_budget.json` (CI cannot run it — live LM + real CTE
required — so it runs here):
```sh
uv run python scripts/mcp_mem_attribution.py \
    --pack external/clio-agent-marketplace/data-semantics \
    --workspace <dir with sensor_readings.csv> \
    --sessions 3 --settle-s 180 --assert-budget    # must print GATE: PASS
```
`--pack` is a SOURCE directory (the script copies it into a fresh stamped gate
XDG itself); the settle must stay ≥ 180s so the fleet reaper's TTL elapses
before FINAL (the script enforces this under `--assert-budget`). `GATE: FAIL`
(over budget, sessions not idle, or degraded substrate) blocks the tag. The
budget only ratchets DOWN — peak is recorded at the honest COLD maximum
(spawn-diet plans expire after 24h, so release runs boot undieted); the unit
test `tests/test_scripts/test_mcp_mem_budget.py` pins the recorded values at
or under the #930 campaign targets (1.8 GB peak / 1.3 GB post-idle) — any
raise past that line fails plain CI.

### 6. Tag + push (triggers all CI)
```sh
git tag -a vX.Y.Z HEAD -m "release: vX.Y.Z — <summary>"
git push origin main && git push origin develop && git push origin vX.Y.Z
```

### 7. Verify CI green + artifacts
```sh
gh run watch $(gh run list --workflow=release.yml --limit 1 --json databaseId -q '.[0].databaseId') --exit-status
curl -s https://pypi.org/pypi/clio-agent/json | python3 -c "import sys,json;d=json.load(sys.stdin);print('X.Y.Z' in d['releases'])"
gh release view vX.Y.Z --json assets -q '.assets[].name' | grep clio-tui   # installer needs clio-tui-{os}-{arch}
gh run list --workflow=docker.yml --limit 1                                 # ghcr images
```
The `clio-bundles.yml` **`release-check`** job must be green — it asserts every
expected bundle uploaded (bundled msi/nsis/dmg/deb/rpm, lite set, tui set, web
zip; #841 F-15). A red `release-check` names the missing artifact — a silently
incomplete release (like v0.5.17's dropped `aarch64` bundled `.dmg`) fails here
instead of shipping. To re-check by hand:
```sh
gh release view vX.Y.Z --json assets -q '.assets[].name' | python3 scripts/check_release_completeness.py
```

## Hard-won gotchas (all hit on the 0.5.3 release — check these)

- **PyPI rejects direct/git dependencies.** A `... @ git+https://...` in ANY extra → upload `400 Can't have direct dependency`. Keep such deps OUT of `[project.optional-dependencies]` (document manual install instead). `[tool.hatch.metadata] allow-direct-references` lets it BUILD but PyPI still rejects the UPLOAD.
- **PyPI 100 MB file limit.** The default hatchling sdist includes ALL VCS-tracked files → `docs/ref/`, `benchmark/`, vendored content balloon it past 100 MB (`400 File too large`). Add `[tool.hatch.build.targets.sdist] include = ["src/clio_agent","pyproject.toml","README.md","uv.lock"]`. **Verify with a local `uv build` — CI checks out submodules so its tree differs from a quick local glance.**
- **`git add a b` is atomic.** If one pathspec doesn't match (e.g. already `git rm`'d), the WHOLE `git add` fails and stages NOTHING → your edit silently doesn't get committed. Add paths separately or re-check `git show HEAD:<file>`.
- **Re-tagging:** `git tag -d <tag>` takes NO `-q`. To move a pushed tag: `git tag -d v; git tag -a v HEAD -m ...; git push --force origin v`. The failed-publish version is NOT on PyPI, so re-tag+republish the same version is fine (PyPI rejects re-upload only of a *successfully* uploaded version).
- **`git fetch` before integrating.** `develop`/`main` advance via others' PRs; a stale local branch → non-fast-forward push rejection. Reconcile (`git merge origin/<branch>`) — content is usually identical, it's just merge-commit topology.
- **Submodule gitlink must be committed.** `git submodule status` showing a leading `+` means the checked-out commit isn't recorded in the parent — commit the gitlink before tagging or the release ships the old submodule.
- **ghcr `403 Forbidden` on push** — check WHO OWNS the package first: `gh api "orgs/iowarp/packages?package_type=container"` (needs `read:packages`; use `MSYS_NO_PATHCONV=1` and no leading slash on Git Bash). The v0.6.1–v0.7.4 saga: the packages EXISTED but were linked to **gact-tui** (created by its pre-move docker pipeline), so clio-agent's GITHUB_TOKEN had no role — org creation settings were irrelevant and every tag push 403'd on a blob HEAD. Fix: link this repo with Write (package Settings → Manage Actions access), or delete the stale packages (restorable 30 days) and let the next tag push recreate them fresh (auto-linked, and public under current org defaults — verify with an anonymous `https://ghcr.io/v2/iowarp/clio-tui/tags/list` pull). For true first creation the old advice stands: org Settings→Packages allow creation, or a one-time `write:packages` PAT bootstrap.
- **Desktop sub-version** (`external/gact-tui/apps/desktop/package.json`/`tauri.conf.json`) is versioned independently by the gact-tui team — don't edit inside the submodule; just pin the gact-tui release tag.
- **Cross-platform CI gotchas (the 0.5.5–0.5.8 install-pathway saga — verify bundles actually upload, don't assume):**
  - `build_clio_tui.sh`: absolutize `$OUT` BEFORE the `cd` into gact-tui, and treat Windows drive-letters (`C:/…`) + backslashes as absolute — a relative `-o` lands the binary in the wrong dir → no `clio-tui-*` assets.
  - `tauri build` rejects **multiple `--config`** — deep-merge the bundled + branding configs (`jq -s '.[0] * .[1]'`) and pass one. Two `--config` flags fail every bundled-desktop job.
  - Use **`shasum -a 256`**, NOT `sha256sum`, in any step that runs on macOS runners (no `sha256sum` on macOS) — it silently fails the mac `.dmg` AFTER it built.
  - POSIX-only APIs crash Windows at import: `faulthandler.register(signal.SIGUSR1)` raises `AttributeError` on Windows (no SIGUSR1) — guard with `hasattr(signal, "SIGUSR1")`. This blocked the entire Windows server.
  - The **Windows launcher `clio.ps1`** must stay at parity with the bash `install/clio` (e.g. `web`/`--web`, the 90s startup wait) — they drift.
  - **Intel-mac (`macos-13`) runners are being deprecated** by GitHub → those desktop jobs queue forever; Apple-Silicon (`macos-14`) covers modern Macs.

## Install pathways — verify ALL are real every release (none aspirational)
CLIO ships **1 engine + 3 frontends** across **4 mechanisms / 6 experiences** (see
`docs/INSTALL.md`). A release must keep every one working — verify after the tag:
- **a) script → CLI/TUI**: `install.sh`/`install.ps1` install clio-agent (PyPI) + `clio-tui-{os}-{arch}` + the `clio` launcher. Confirm `clio-tui-*` assets are on the GH release (clio-bundles.yml).
- **b) docker TUI (no-install)**: `docker run -it ghcr.io/iowarp/clio-tui:<ver>`. Confirm docker.yml pushed it.
- **c) `clio --web`**: launcher `web`/`--web` → gact serves the SPA same-origin via `CLIO_WEB_DIR`; `install.sh` unpacks `clio-web-<ver>.zip` into `$PREFIX/clio-agent/web`. Confirm the web zip asset exists; offline-test the mount (build_app with `CLIO_WEB_DIR` set: GET `/` = index.html, SPA fallback works, `/v1/*` not shadowed).
- **d) docker compose (scaled web)**: `docker compose up clio-web` / `--profile api`. Confirm `ghcr.io/iowarp/clio-{web,api}` pushed.
- **e) desktop**: `.msi/.dmg/.deb/.AppImage/.rpm` on the release (clio-bundles.yml desktop job).
- **f) git clone**: `uv sync` / `uv run`.

Anything not green → fix before announcing, or mark it explicitly in `docs/INSTALL.md` as
not-yet-available (never leave a pathway claimed-but-broken). The multi-spawn port/state
clash across `clio` / `clio --web` / desktop is tracked in #698.

## Validation (optional, before release)
Run the EarthScope grind on the demo model(s) to confirm no regression — see [[grind-clio-case]] and the ALCF hot-model routing note (check `/jobs` for the live cluster; route gpt-oss → `argonne_sophia:openai/gpt-oss-120b`).
