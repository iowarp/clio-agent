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

### 5. Verify locally BEFORE tagging (cheap; avoids failed CI cycles)
```sh
uv version --short                     # == X.Y.Z
uv run python -c "import clio_agent, clio_agent.config, clio_agent.gact.app, clio_agent.ui.api; print('ok')"
uv build && ls -lh dist/               # BOTH wheel AND sdist must be < 100 MB (PyPI limit)
# confirm no direct/git deps leaked into wheel metadata:
python3 -c "import zipfile,glob; z=zipfile.ZipFile(glob.glob('dist/*.whl')[0]); m=[n for n in z.namelist() if n.endswith('METADATA')][0]; print('git+ in METADATA:', any('git+' in l for l in z.read(m).decode().splitlines()))"
```

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

## Hard-won gotchas (all hit on the 0.5.3 release — check these)

- **PyPI rejects direct/git dependencies.** A `... @ git+https://...` in ANY extra → upload `400 Can't have direct dependency`. Keep such deps OUT of `[project.optional-dependencies]` (document manual install instead). `[tool.hatch.metadata] allow-direct-references` lets it BUILD but PyPI still rejects the UPLOAD.
- **PyPI 100 MB file limit.** The default hatchling sdist includes ALL VCS-tracked files → `docs/ref/`, `benchmark/`, vendored content balloon it past 100 MB (`400 File too large`). Add `[tool.hatch.build.targets.sdist] include = ["src/clio_agent","pyproject.toml","README.md","uv.lock"]`. **Verify with a local `uv build` — CI checks out submodules so its tree differs from a quick local glance.**
- **`git add a b` is atomic.** If one pathspec doesn't match (e.g. already `git rm`'d), the WHOLE `git add` fails and stages NOTHING → your edit silently doesn't get committed. Add paths separately or re-check `git show HEAD:<file>`.
- **Re-tagging:** `git tag -d <tag>` takes NO `-q`. To move a pushed tag: `git tag -d v; git tag -a v HEAD -m ...; git push --force origin v`. The failed-publish version is NOT on PyPI, so re-tag+republish the same version is fine (PyPI rejects re-upload only of a *successfully* uploaded version).
- **`git fetch` before integrating.** `develop`/`main` advance via others' PRs; a stale local branch → non-fast-forward push rejection. Reconcile (`git merge origin/<branch>`) — content is usually identical, it's just merge-commit topology.
- **Submodule gitlink must be committed.** `git submodule status` showing a leading `+` means the checked-out commit isn't recorded in the parent — commit the gitlink before tagging or the release ships the old submodule.
- **ghcr `403 Forbidden` on first push** = org hasn't allowed Actions to create the org-level package. Fixes: org Settings→Packages allow creation, OR bootstrap each package once with a `write:packages` PAT (`docker login ghcr.io` + push), then link the repo (package Settings → Manage Actions access → add repo, Write). Repo default workflow permission + workflow `packages: write` are necessary but NOT sufficient for first creation.
- **Desktop sub-version** (`external/gact-tui/apps/desktop/package.json`/`tauri.conf.json`) is versioned independently by the gact-tui team — don't edit inside the submodule; just pin the gact-tui release tag.

## Validation (optional, before release)
Run the EarthScope grind on the demo model(s) to confirm no regression — see [[grind-clio-case]] and the ALCF hot-model routing note (check `/jobs` for the live cluster; route gpt-oss → `argonne_sophia:openai/gpt-oss-120b`).
