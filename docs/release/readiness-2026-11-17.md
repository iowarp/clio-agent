# CLIO release readiness, 2026-11-17

Overnight preparation for the next CLIO release. No release action was taken: no tags, no publishing, no merges into `main` or `develop`, no pull requests, no version or pin bumps. Every fix is on a `claude/release-readiness` branch.

Baseline (03:40 UTC monitor): clio-agent develop `ba8c47f`, gact-tui develop `def3ae0`, marketplace develop `f860020`, clio-schemas develop `e138128`, clio-kit main `4b5e6e3c`.

**Status of this file:** sections marked PENDING are filled in as the last runs finish.

## Access limits that shaped the night

- `add_repo` with push access was **denied by the session's permission classifier** for **iowarp/gact-tui** and **iowarp/clio-schemas**. I did not retry or work around it.
  - **gact-tui:** I cloned it read-only over git, which also covers the submodule. My two fixes are local commits only, exported as patches in `docs/release/gact-tui-patches/`. I could not push `codex/release-readiness`, so gact-tui CI never ran on them, and I could not read gact-tui's Actions logs.
  - **clio-schemas:** a read-only clone. Nothing needed fixing.
- Pushes succeeded to iowarp/clio-agent, iowarp/clio-agent-marketplace and iowarp/clio-kit.
- **JaimeCernuda/Burette** was not attached, so it was not examined.
- **The container restarted at about 06:34 UTC while the session was idle.** This killed the first memory-gate attempt after its run 1, and the gate was restarted (see the memory gate section).

## Per repository

### clio-agent (`claude/release-readiness`, head `3d1c6e2`)

**Why develop was red** (runs 43974744014, 43787776688, 43741541724):
1. **Lint:** the per-file noqa-swallow ratchet failed (`client/aio.py`, `gact/attached_clients.py`). That stopped the job before mypy, which had 13 more errors.
2. **Tests:** 18 distinct test failures across all 18 shards. I first saw them in CI run 44017048512, which ran on my lint fix `42877ff`.
3. **Docker (run 43787776633):** the editable build could not find `catalogs/connectors`, which `pyproject.toml` force-includes but the Dockerfiles never copied.

**Fixes, with root causes:**

| Commit | Fix |
|---|---|
| `42877ff` | `aio.py` forwards the exception it catches, so I declared it with the guard's `forwards:` marker. `_create_time` swallowed every `Exception`; it now catches only psutil's own errors. `Session.agent` had `default_factory=AgentRef`, which could never construct a default because `AgentRef.id` is required; it now defaults to `{"id": "main"}`, the same as the stored record. The other 11 mypy errors were annotation-only. New tests fail on develop and pass here. |
| `823ff68` | Both Dockerfiles copy `catalogs/connectors` into the builder and runtime stages. |
| `d5d44c6`, `9da7957` | The run-limit check added in `b6a66fc` walks `parent_session_id`. Two test fakes (`live_plane_context` and the scratch-folder test) built sessions without the `id` and `parent_session_id` fields every stored session has. The fakes were incomplete, not the code. |
| `fa4d484` | `arc.cte.metadata_disk_reserve` (FN-14) had no operator note. I added it and regenerated `docs/ENVIRONMENT.md`, `.env.example` and `config.defaults.yaml`, which also picked up `CLIO_LOG` and `CLIO_STOP_WITH_LAST_CLIENT`. |
| `814ca6f` | The three turn-transcript goldens differed only in `agent_resolution.digest`. The core plugin prompts changed after the goldens were recorded, and the event sequence is unchanged, so I re-recorded them. |
| `b44e10b` | **A real bug.** Since `82135eeb`, a restart skipped every descendant in `task_state_uncertain`, including one whose cancellation the restart itself had just failed to confirm. The restart could therefore launch a successor over work that may still be running. Now only calls that were already uncertain when the restart started are exempt. The contract test (failing on develop, and noted in #2365) and `82135eeb`'s own test both pass, as do 230 restart-related tests. |
| `3d1c6e2` | The release notes draft, docs only. |

**CI on the branch:**

| Run | Commit | Result |
|---|---|---|
| CI [44017048512](https://github.com/iowarp/clio-agent/actions/runs/44017048512) | `42877ff` | Lint green on 3.12, 3.13 and 3.14. All 18 check shards failed on the test failures fixed above. |
| CI [44022751750](https://github.com/iowarp/clio-agent/actions/runs/44022751750) | `b44e10b` | Attempt 1: every job passed except check (3.13, shard 0), with 1 failure in `test_mcp_reconnect.py::test_reconnect_waits_for_a_server_still_starting` (504 after a 1 s no-progress window). Attempt 2 re-ran only the failed jobs and the run concluded **success**. |
| Docker [44017195005](https://github.com/iowarp/clio-agent/actions/runs/44017195005) | `823ff68` | **success**. Build only; images are pushed only on `v*` tags. |
| CI [44032362022](https://github.com/iowarp/clio-agent/actions/runs/44032362022) | `3d1c6e2` | **success on the first attempt**: 31/31 jobs, including lint on 3.12, 3.13 and 3.14, all 18 check shards, coverage-floor jobs on 3.12, 3.13 and 3.14, flake-hunt, filesystem-contract and native-service lifecycle. The later commits (`d6a1fde` and after) touch `docs/release/` only. |

**The reconnect flake:**
- I did not find its root cause.
- Locally it passed 5 of 5 alone, and 3 of 3 with coverage and 4 workers alongside its neighbours and `tests/test_tools` (999 passed each time).
- Its window is 1 s, and it relies on a 3 s busy-loop server. My best explanation is that on a loaded runner the server tree has no measurable root before the window ends, and the wait counts "no server process" as no progress. I have not confirmed this. It is worth an issue.

**Local suites:**
- **Lint and guards on my fixes:**
  - Ran on the branch at `823ff68` and again on the trial trees built on `b44e10b`.
  - ruff check, ruff format --check, mypy (1,773 files) and `compile_model_overlay --check` pass.
  - So do these guards: file size, class-in-function, no-summaries, no-settle, no-elicitationId, no-middle-dot, tool-instrumentation, silent-fallback and noqa-swallow.
  - `plugin check` gave 69/69 and `bash -n homelab-env.sh` passed.
- **Develop baseline:** shards 0 to 2 of 6 ran locally at `ba8c47f`, each 5 failed, the same failures as CI. I stopped it after that because CI covered the rest.
- **Six shards and the coverage floor on my branch:** PENDING. They run after the memory gate, because the test suite's clio-core daemons must not overlap with the gate's samples.

### gact-tui (local branch `claude/release-readiness`, NOT pushed)

**Local results** in `/home/user/gact-tui-wt`, develop `def3ae0` plus my fixes:
- `pnpm install --frozen-lockfile`: OK.
- Brand descriptor regenerate and diff: clean.
- `pnpm lint`: OK.
- `pnpm typecheck`: **failed on develop with exit 134**, a heap-limit abort with no type error. It passes with fix 2 below.
- Package tests (`pnpm -r --filter '!@clio/workspace' test`): OK.
- Web unit tests (`vitest run --maxWorkers=2`): 802/802 files and 5,617 tests passed.
  - An earlier full run under heavy machine load had 159 failures. 137 of them were timeouts, and 158 of the 159 passed when re-run alone. The one real failure is fix 1 below.
- `pnpm build`: OK.
- `pnpm --filter @clio/workspace i18n:extract`: OK, and the working tree was clean afterwards.
- **Not run:** the Playwright e2e and component suites, and the Tauri desktop jobs (the full CI tier).

**Fixes** (patches in `docs/release/gact-tui-patches/`):
1. `1cdf69a`: `tests/contract/fixture-routes.test.ts` failed because the core client gained `GET /v1/system/problem-report` and no browser test reaches that page. I added the route to `fixture-unserved-routes.json`, as the test asks and as `3fe667eb9` did before.
2. `ef980f8`: `tsc -b` over `web/` peaks at 2.38 GB (5,993 files), above Node's default limit of about 2 GB on a 15 to 16 GB machine. The `typecheck` script now runs `node --max-old-space-size=4096 .../tsc -b`. With that it passes clean.

**CI:** I can't confirm this is the `workspace` (apps.yml) failure on develop, because I could not read gact-tui's CI logs. CI was never run on these fixes.

### clio-agent-marketplace (`claude/release-readiness`, head `7d731d6`)

- **CI on develop has not run since Nov 10** (`62f2fc8`, success), although develop advanced to `f860020`. The workflow only runs on pushes to main or develop and on PRs, and has no `workflow_dispatch`, so it cannot run on my branch.
- **Two local plugin-test failures on develop, fixed:**
  - `87395fd`: the abaqus `TopologyViewport` lacked the `nodeOpacity` that clio-schemas added to `clio.mesh-viewport.v1`. I copied it verbatim.
  - `7d731d6`: the kicad `fork-skills.json` listed a never-committed `__pycache__/*.pyc`. I removed that entry.
- **Local CI suite at `7d731d6`, against my clio-agent branch:**
  - policy unittest: 25 tests passed;
  - `check_model_pins` and `check_skill_literals`: OK;
  - `plugin check .`: 69/69, and `plugin check testing`: 8/8;
  - plugin tests: 401 passed;
  - spotter-impl ruff, pyright and pytest: 43 passed.

### clio-schemas (no changes; read-only)

- **Local CI suite at develop `e138128`, all pass:**
  - ruff, format and pyright: 0 errors;
  - `export --verify`: 44 schemas canonical;
  - version pins agree: `0.8.0b1`;
  - `check_version_bump --base v0.7.0`: OK;
  - pytest: 935 passed;
  - ts-gen `npm run check`: OK.
- **Not checked:** clio-schemas CI, because I have no access to it.

### clio-kit (`claude/release-readiness`, head `0cdd7203`, based on main)

**CI on the branch, both success at `0cdd7203`, and I checked them myself:**
- Quality Control [44021371448](https://github.com/iowarp/clio-kit/actions/runs/44021371448): 230 jobs; the one skipped job is PR-only.
- Marketplace acceptance [44021909464](https://github.com/iowarp/clio-kit/actions/runs/44021909464): both protocol jobs.

On main, 40 Quality Control jobs and both acceptance jobs had been failing.

**Fixes:**

| Commit | Fix |
|---|---|
| `5fa311c7` | The README server count is 27 after the globe was removed. |
| `1abc03b7` | dnspython 2.9.2 in 21 locks, for CVE-2026-128089. |
| `6efba436` | ndp test. |
| `ccd3a494` | setup-mcp kept every lane on Python 3.12 and dropped pytest-cov. It now exports `UV_PYTHON`, and the server's `.python-version` is the default. |
| `b65ed1f0` | server.json extraction for upstream wrappers. |
| `8c800ba0` | paraview hints, tags, prompt and server.json. |
| `3b8a648e` | slicer prompt. |
| `bdb4bef4` | acceptance starts upstream wrappers with their probe environment, and non-Python servers negotiate the protocol era with mode "auto". |
| `0cdd7203` | earthdata test on Python 3.10. |

**Root verification at `0cdd7203`:**
- `uv sync`: OK;
- `marketplace sync`: no diff;
- pytest: 709 passed;
- ruff, format, mypy and file size: OK.

**Decisions only you can make:**
- **Patch bumps:** the relocked servers that already shipped at their current versions need patch bumps before the next clio-kit release. Nothing was bumped.
- **Python 3.10:** it has no patched dnspython (2.9.x needs Python 3.11 or later).
- **paraview tool hints:** the destructive and idempotent hint values were set by the fixing agent. Please review them.
- **pdf protocol era:** the pdf server is accepted at protocol era 2025-11-25, under "auto".
- **Stale ParaView docs and skill:** these still name the removed tools.

## Still red, and why

- **gact-tui:** CI is unverified. My fixes are local only because the repository could not be attached. Playwright and Tauri were not run.
- **marketplace:** CI cannot run on the branch. Develop's next push or PR will be its first CI run since Nov 10.
- **clio-agent:** CI is green on the code head. The reconnect test above failed once in 3 full CI runs and is not root-caused.

## Memory budget gate

PENDING. Command: `scripts/mcp_mem_attribution.py --agent clio/clio-data --private-core-root <owned dir> --provider claude_code --sessions 3 --settle-s 1830 --runs 3 --assert-budget`, at `b44e10b`.

**Attempt 1 (interrupted):**
- Run 1 completed with peak 1.47 GB and final 0.82 GB, against a budget of 3.2 / 0.92 GB.
- The container restart killed it during run 2. It needs 3 complete runs, so it does not count.

**Caveats that apply to any result:**
- **Synthetic dataset.** The repository does not ship `sensor_readings.csv`. I generated a deterministic one with 6,026 rows and planted faults.
- **Lighter load than the budget was recorded with.** In run 1 the clio-data sessions used only built-in tools (`fs_read_file`, `shell_bash`, `fs_glob`); no hdf5, adios or parquet MCP server started. The answers were correct and grounded in the data. I did not determine whether that was the agent's choice for a CSV or the MCP tools being unavailable under lazy install.
- **Leftover processes and directories.** `codex` sandbox processes, about 0.08 GB, were still alive at FINAL after the 1,830 s settle. The script leaves `/tmp/clio-mem-gate-xdg-*` behind after each run; I deleted them.

## Release preparation (report only)

### CHANGELOG `## Unreleased` (develop)

It covers only the Python SDK and unattended-run work. Most contract changes since v0.9.7 are missing:
- connectors and their routes and events, plugin offers, MCP Apps routes and events, file views;
- Stop scope and restart (#2354);
- S3 links and byte ranges, Report a problem, `fs_git_commit` and `.git` write refusal (#2305), outside-sandbox shell (#2364);
- the `invalid_action` permission error, lazy install (`--prepare-tool-servers`), the default workspace folder and shutdown status semantics.

Other problems:
- The clio-kit pin bullets are misfiled under Removed.
- Develop has no `[0.9.7.1]` section; it has to come from main.
- v0.9.7.1 itself pinned clio-kit 2.12.3. The 2.12.5 and 2.12.7 pins landed after the tag, and main lists them under Unreleased.

The full inventory, with commits and PR numbers, is in the next section.

### Contract-change inventory

The detailed inventory was produced in this session from `git log f4b05a1..origin/develop`. Its key points are in the CHANGELOG subsection above and in the release notes draft. PR numbers I verified: #2305, #2354, #2358, #2364 and #2365.

### Dependencies, for their own release from `main`

| Repository | Version on develop | `develop`→`main` (trial merge, local, not pushed) | Notes |
|---|---|---|---|
| gact-tui | 0.13.0 (latest tag v0.13.0); Desktop 0.7.3; CHANGELOG has an Unreleased section | **Clean, no content change.** main's 5 merge commits (#1246, #1178, #1144, #1133, #1099) each equal a develop ancestor. | Needs a new release: the version is unchanged since v0.13.0. Apply the two patches first. |
| marketplace | latest tag v0.8.0; CHANGELOG has `[Unreleased]` | **Clean, no content change.** main `99bf35a` (#135) has the tree of develop's ancestor `9d06bf4`. | Release from develop plus `87395fd` and `7d731d6`. |
| clio-schemas | `0.8.0b1`; CHANGELOG has `[0.8.0b1] - unreleased` | **Clean, no content change** (#53, #51). | PyPI's latest is 0.7.0. clio-agent requires `clio-schemas==0.8.0b1`, so 0.8.0b1, or a renamed 0.8.0 plus a clio-agent pin change, must be published. |
| clio-kit | 2.12.7 on main, but main carries unreleased servers (paraview-mcp 3.1.0, geo 2.4.0, slicer, the globe removal) | `develop` is stale: main is 146 commits ahead. Releases come from main. | clio-agent develop builds clio-kit from git `88874fbc` (in main). A new clio-kit release (2.13.0 or similar) is needed before pinning; add the patch-bump decisions above. |

### clio-agent main→develop trial merge (local, not pushed)

- **Shape:** merge-base `f4b05a1`. Develop is 722 commits ahead and main 31.
- **main's own commits:** `git cherry` finds 12 of main's 14 non-merge commits already in develop. The two that are not are the v0.9.7.1 release commit and the superseded 2.12.5 pin.
- **8 conflicted files:**

| File | Resolution |
|---|---|
| `CHANGELOG.md` | Keep both: Unreleased first, then `[0.9.7.1]`. |
| `install/README.md` | Take develop, and check the clio-kit text. |
| `install/install.ps1` | Take **develop's side only**. Develop moved the clio-kit spec block while main edited it in place, so taking both duplicates the block. |
| `pyproject.toml` | Decided by the pin step. |
| `src/clio_agent/plugins/service.py` (`DEFAULT_MARKETPLACE_COMMIT`) | The marketplace release commit. |
| `runtime/document_stack/cli.py` | Take develop's `end_on_termination`. |
| `ui/cli.py` | Union of the imports. Check that `serve` is still used. |
| `uv.lock` | Regenerate with `uv lock` after pinning; don't merge it by hand. |

### Submodule gitlinks on develop, against what must be pinned

| Submodule | develop records | Distance from dependency's develop | Pin at release |
|---|---|---|---|
| `external/gact-tui` | `8adc029` | 6 commits behind gact-tui develop `def3ae0` | the new gact-tui tag's commit |
| `external/clio-agent-marketplace` | `499589a`, the same as `DEFAULT_MARKETPLACE_COMMIT` | 1 merge (#148) behind marketplace develop `f860020` | the new marketplace tag's commit, in both places |

gact-tui's 6 missing commits include the Python-SDK companions: Desktop counting itself as an attached client, and the flow railroad view.

`[tool.uv.sources]` builds clio-schemas from git `33b0024` and clio-kit from git `88874fbc`. The release removes both and pins the PyPI versions with their hashes in `uv.lock`.

### Files the version bump touches

Develop says `0.9.7`, but main and v0.9.7.1 say `0.9.7.1`, so the next version must sort after 0.9.7.1.

**Listed in the release skill:**
- `pyproject.toml:3`;
- `src/clio_agent/__init__.py:43`;
- `uv.lock` (the clio-agent entry, via `uv lock`);
- `install/README.md:103`, `:116` and `:180` to `:181` (`CLIO_VERSION=`);
- `tests/test_scripts/test_release_install_policy.py:25` (`EXPECTED_VERSION`) and `:28` (`DOCUMENTED_VERSION`);
- `README.md:72` and `:76`;
- `docs/INSTALL.md:9` and `:148`.

**Also carry the version:**
- `site/src/content/docs/docs/server-setup.mdx:71` and `:167`;
- `site/src/content/docs/docs/install.mdx:240`;
- `site/public/install.md:96`;
- `site/src/content/docs/docs/versions.mdx:24`.

**Expected new files:** a curated `docs/releases/vX.Y.Z.md`, like `docs/releases/v0.9.7.md`, and the CHANGELOG roll.

### Local release qualification (skill step 4, before main integration)

Run at `823ff68`, and again at head `d6a1fde`.
- `uv version --short` reports `0.9.7` (not bumped, as instructed).
- The import check is OK.
- `uv build` gives a 6.4 MB wheel and a 5.6 MB sdist (both well under 100 MB). Neither has `git+` in its metadata, and the wheel ships 43 `_catalogs/connectors` files.
- The wheel requires `clio-kit==2.12.7` and `clio-schemas==0.8.0b1`.
- **Release-script tests at the head: 141 passed.** That covers `test_release_install_policy.py`, `test_check_release_completeness.py`, `test_github_release.py`, `test_release_install_plan.py`, `test_release_notes_from_changelog.py`, `test_upload_release_assets.py` and `test_mcp_mem_budget.py`.

**Release notes draft:** `docs/release/next-release-notes-draft.md`. It follows the skill's style rules and the version is a placeholder.

## Unmerged feature branches (trial merges onto `b44e10b`, local only)

### `claude/meta-clio-guide-builder` (clio-agent `88bfdc0`; gact-tui `e482384`)

- **clio-agent: 2 conflicts.**
  - `gact/plugin_authoring.py`: develop's unattended-run auto-allow against the branch's new `authored_trust._ask` gate. I resolved it as unattended first, otherwise the branch's gate.
  - The route count in `test_decomposition_guardrails.py`: 486 + 4 + 3 = 493.
  - The goldens also conflict because both sides re-recorded them; regenerating them on the merged tree is the fix.
- **Gates after resolution:**
  - ruff, format, file size, class-in-function, noqa, env reference and `plugin check` (69/69) pass.
  - **mypy fails with 3 errors in the branch's own new code:** `gact/builder/presentation.py:70` (redefined `blocks`), `:229` (union-attr), and `gact/local_publish.py:111` (union-attr). CI lint would fail.
- **Tests:** the 25 test files the branch touches, plus plugins, flows, guardrails and goldens: 705 passed, 1 skipped.
- **Gitlink:** the branch moves `external/gact-tui` to `e482384`, the head of the unmerged gact-tui branch.
- **gact-tui `claude/meta-clio-guide-builder`:** merges cleanly into gact-tui develop (30 files). Its suites were not run.
- **Verdict:** not ready as-is. Fix the 3 mypy errors, merge gact-tui's branch first, and re-pin.

### `claude/chemistry-in-clio` (clio-agent; clio-kit; marketplace; Burette)

- **Branch head moved.** The 03:40 monitor recorded `336ab62`. I tested `7cd7ea6`, fetched at about 04:20, which contains `336ab62`. By about 06:05 the branch head was `fc70eb3`, which I did not test. It was still being pushed overnight.
- **clio-agent:** a clean merge, 41 files.
  - It re-pins the marketplace (gitlink and `DEFAULT_MARKETPLACE_COMMIT`) to `0d2149d` and clio-kit (`[tool.uv.sources]`) to git `006d6b68`. Both are commits on the chemistry branches, merged nowhere.
  - **Gates all pass:** mypy clean on 1,773 files, plugin check 81/81, and all the guards.
  - **Tests:** `tests/test_connectors`, `tests/test_plugins`, the question-notice, guardrail and golden tests, and `test_gact/test_*task*`: 634 passed. `test_default_marketplace_pin` gave 4 passed once the trial merge was committed locally (it reads the gitlink from `HEAD`).
- **marketplace chemistry `0d2149d`:** merges cleanly onto my marketplace branch. Its suites were not run.
- **clio-kit chemistry `bdd0e527`:** merges onto my clio-kit branch with 1 conflict, the README server count (27 against 33). The count test decides the number. Its suites were not run.
- **Burette:** not accessible.
- **Verdict:** mechanically mergeable. It needs its dependency branches merged and released first, and a re-test at the final head.

## Morning checklist (in order)

1. **gact-tui**
   1. Apply `docs/release/gact-tui-patches/0001` and `0002` to a branch from develop. If the branch name starts with `codex/` or you open a PR, CI runs.
   2. Get apps.yml green, including the full tier, which I did not run.
   3. Merge into develop, then develop→main (clean in the trial merge).
   4. Release with a new version. The version, CHANGELOG and curated notes are your call.
2. **clio-agent-marketplace**
   1. Merge `claude/release-readiness` (`7d731d6`) into develop. That push is the first CI run since Nov 10; confirm it is green.
   2. develop→main (clean).
   3. Tag and release.
3. **clio-schemas:** decide 0.8.0b1 or 0.8.0. Then develop→main (clean), tag, and confirm the PyPI wheel and sdist exist.
4. **clio-kit**
   1. Decide the patch bumps for the relocked servers (and the review items above).
   2. Merge `claude/release-readiness` (`0cdd7203`) into main.
   3. Release a version that contains `88874fbc` or later.
   4. Optionally resync develop, which is 146 commits behind main.
5. **Feature branches:** decide whether `claude/meta-clio-guide-builder` and/or `claude/chemistry-in-clio` go in. Merge them, and their dependency branches, *before* steps 1 to 4 release, then re-run the suites.
6. **clio-agent**
   1. Merge `claude/release-readiness` (head `3d1c6e2`) into develop.
   2. On a release branch from main, following the skill: pin the gact-tui and marketplace tags (gitlinks and `DEFAULT_MARKETPLACE_COMMIT`), and the clio-schemas and clio-kit PyPI versions. Remove `[tool.uv.sources]` and run `uv lock`.
   3. Bump the version in every file listed above.
   4. Roll the CHANGELOG: fill in the missing contract items, bring in main's `[0.9.7.1]` section, and fix the misfiled pin bullets.
   5. Add `docs/releases/vX.Y.Z.md` from the draft.
   6. Merge develop→main, resolving the 8 conflicts as listed. On `install.ps1`, take develop's side.
7. **Before you tag:**
   - re-run the local qualification: `uv build`, sizes, `git+`, imports, policy and completeness tests;
   - the memory gate, which must print `GATE: PASS` with 3 complete runs;
   - the conversation continuity grid from the skill, which I did not run;
   - fresh CI on exact main.
8. **Tag, publish, author the notes on the draft release, and verify:** skill steps 6 to 7.
