# External PR review: maintainer decisions

Nothing has been executed yet. These are recorded decisions only.

**Base: `develop`.** The SDK work (`claude/sdk-plugins-flows`) is now in clio-agent `develop` (verified 2026-10-28: develop has `plugins/`, `sdk/` and `gact/fleet/`, and `expert_packs.py` is gone; develop is 125 commits ahead of main). All PRs were revalidated against the SDK branches on 2026-10-24. Develop has moved since then, so each port is re-checked on develop at execution time. clio-schemas uses its own `develop`.

**Also in flight:** clio-agent `claude/install-and-startup` reworks install, setup and shutdown (229 files: `install/install.sh` and `install.ps1` rewritten, desktop installers removed, bundle workflow changed). The HDF5 packaging (the `hdf5-mcp[data-routes]` extra in the desktop bundle) must follow whatever that branch establishes. Check it before doing #1834.

## Execution plan (on the maintainer's go-ahead)
1. In each repo, start the integration branch `claude/gracious-archimedes-uu7gbm` from the latest `develop`.
2. For each approved external PR, **retarget its base to the integration branch** instead of rebuilding it. Then merge it into the integration branch, keeping the contributor's commits and authorship. If GitHub can merge it cleanly, merge it there. If not, merge `pull/N/head` locally, resolve the conflicts in the merge commit, and push the integration branch. GitHub then marks the PR merged, because its head is in its base.
3. Make the recorded tweaks as our own commits on top, on the integration branch.
4. Close each merged PR with "landed in #X, thanks", where #X is the final integration PR. Superseded PRs (#1736, marketplace#103, marketplace#107, marketplace#96) are closed with the same note pointing at where the work now lives.
5. When everything is in and green, open one PR per repo from the integration branch to `develop`. The maintainer merges it.

**Global rules (maintainer):**
- **Nothing is implemented until the SDK branch (`claude/sdk-plugins-flows`) is merged.** After that there is no "old main". Every approved PR is ported, rebased and re-validated against the SDK-era main. Main-only concerns (blueprints, `expert_packs.py`, `agent_blueprints.py`) don't matter.
- **We don't send review requests to contributors.** Where changes are needed, we make them ourselves and keep the contributor as commit author.
- **Don't fix, report:** prefer typed errors returned to the calling agent over silent repair.

## Group 1: CI and repo hygiene

| PR | Decision | Action when executed |
|---|---|---|
| clio-agent#1792 (skip filesystem-contract on fork PRs) | Approved. **SDK revalidation: unchanged; ports cleanly.** | Merge first, so fork PRs show real CI. The SDK branch also adds `claude/**` to the PR triggers; the guard covers them too. |
| clio-agent#1811 (exact ratchet baselines) | Approved after rebase. **SDK revalidation: one extra baseline edit.** | Conflict on the `ui/cli.py` entry: keep the base's 1014. New: lower the `gact/app.py` entry from 1828 to 1826 (the only padded entry on SDK). The SDK branch has no stale entries. Then close issue #1203 as completed. Land it first; #1822 and #1803 must then hold exact baselines. |
| clio-agent#1842 (hyoklee: #1839, #1840, #1841) | Approved, including the linked issues. **SDK revalidation: all 3 bugs still reproduce on SDK; ports cleanly.** | Retarget from `develop` to the SDK-era main. Close #1839, #1840, #1841 on merge. |
| clio-agent-marketplace#107 (model-pin lint spellings) | **Close as superseded by marketplace#108**, crediting mattjala | Superseded because the blueprint format it lints is gone on the SDK branch, and #108 rewrites `check_model_pins.py` for `Prefers`. |

### Open question from marketplace#107
- Should the clio-agent loader accept three model-pin spellings (`model`, `default_model`, `defaults.model`) or just one? If it is cut to one, the loader and this lint should both change together.
- Haven't model pins changed recently? The change to model selection (the model fleet) is NOT on main/develop. It lives on the SDK branch: `claude/sdk-plugins-flows`, i.e. clio-agent#1825 (draft) and marketplace#108 (plugins and agent classes replace Agent Blueprints). So judge #107 against that branch. The `expert_packs.py` / `agent_blueprints.py` loader paths it targets on main (`gact/expert_packs.py:317`, `:510-517`; `gact/agent_blueprints.py:288`, `:833`) may be replaced there. If so, #107 only lints a format that is on its way out, and the one-spelling-vs-three question should be settled in the SDK branch's plugin/agent-class schema instead.

### Answers (researched on `claude/sdk-plugins-flows`)
- **What a pin means now:** a soft preference, `Prefers(model=…, provider=…, effort=…)` (`sdk/model.py`). Selection order is: explicit `model=` on the call, then the target's `Prefers` (subagent → agent → plugins), then the caller's model (`gact/fleet/selection.py`). A preference never blocks a session. If the model isn't connected or fails `Requires`, the child falls through and the reason is recorded in `model_choice`. So a pin no longer means "this expert runs on X"; it means "try X first". The lint still has a purpose: a pack that ships a preference behaves differently depending on which accounts a user has connected.
- **One spelling or three:** the three legacy spellings disappear with the SDK branch (`gact/expert_packs.py` and `gact/agent_blueprints.py` are deleted there). The new surface is already one concept per format: `Prefers(model=)` on Agent/Subagent/Plugin classes, and `prefers` in a plugin.json `ai.iowarp.clio` extension or declaration. Claude Code subagent frontmatter `model:` is mapped into `prefers` too (`plugins/claude_code.py:306`). That is an external format, so we can't drop it. The extension's `model` object is `Requires` (hard needs), not a pin.
- **What this means for #107:** marketplace#108 already rewrites `scripts/check_model_pins.py` to check `Prefers(model/provider)` and plugin.json `prefers`. #107 polishes a script for a format that is being deleted. It's only worth merging if another blueprint-format release ships before the SDK branch lands. Not yet checked: whether #108's lint covers Claude Code-format subagent `model:` frontmatter.
- **Status:** still on hold. Choose between: close #107 as superseded by #108, or merge it as a stopgap for remaining blueprint releases.

## Group 2: HDF5

Direction (maintainer): clio-agent should have no built-ins. Domain knowledge and skills belong in marketplace plugins (the SDK branch `claude/sdk-plugins-flows`).

Verified on the SDK branches:
- clio-agent `claude/sdk-plugins-flows` has **0** `gact/builtin_skills` (main has 12). The built-ins are already moved out.
- marketplace `claude/sdk-plugins-flows` already has `plugins/hdf5/skills/hdf5/`: the router plus all 22 references, identical to clio-agent#1736. The only difference is that the marketplace copy adds `title: HDF5` to SKILL.md. It was added in be8451d ("ship the HDF5 team's hdf5 skill in the hdf5 plugin", Oct 21), and `plugins/hdf5/plugin.json` is v1.1.0.
- data-semantics on that branch already uses it: its `data` and `hdf5` subagents declare `plugins=["hdf5", …]`, and the hdf5 subagent prompt says to load the `hdf5` skill.

| PR | Decision | Action when executed |
|---|---|---|
| clio-agent#1834 + gact-tui#673 (h5grove API + h5web viewer) | **Approved: ship HDF5 viewer support as built in #1834 + #673.** The UI plugin system idea is dropped for now (maybe later). | Land together, #1834 first. Required fixes from the review: (1) package h5grove/hdf5-mcp as an extra included in the desktop bundle, and lock it, so the route actually mounts; (2) replace the silent `except ImportError: return False` with a typed reason, and advertise the capability; (3) fix the route-count test; (4) drop the 1 GiB whole-file cap for this route and bound slice size instead; (5) check HDF5 external links / VDS so they can't read outside the workspace. In #673: only use the h5web viewer when the server advertises the capability. Depends on clio-kit#390. **SDK revalidation:** the problem still exists and both still apply. On SDK, `hdf5_mcp` is not in clio-agent's environment: the marketplace hdf5 plugin runs clio-kit's hdf5 server in its own environment. So the route silently never mounts, and the packaged extra becomes **mandatory**. New items: (6) pin `hdf5-mcp[data-routes]` to the version clio-kit 2.11.0 ships; (7) advertise HDF5 viewing as a host capability, not tied to an agent having the hdf5 plugin; (8) **#673 doesn't compile on SDK**: replace the `next-themes` import in `hdf5-viewer-frame.tsx` with `useThemeKey()` from `web/src/theme/runtime`; (9) resolve the `pnpm-lock.yaml` and `vite.offline.config.ts` conflicts, keeping `clio-theme-boot`. Minor: the viewer only gets light/dark, so the Notebook theme won't carry through. After these fixes: #1834 14 tests pass; #673 lint, typecheck and build pass, hdf5-viewer 25/25, viewer tests 37/37. |
| clio-agent#1736 (built-in `hdf5` skill) | **Not in clio-agent.** The content is wanted, in the marketplace hdf5 plugin. | Already present, verbatim, on the marketplace SDK branch. Close #1736 as superseded, crediting mattjala and pointing at `plugins/hdf5/skills/hdf5`. Future HDF5 skill updates go to that plugin. |
| marketplace#103 (data-semantics → core `hdf5` skill) | **Opposite direction: do not point at a core skill.** | On the SDK branch, data-semantics already uses the hdf5 plugin's skill. Close #103 as superseded by the SDK branch (#108). |

### Follow-up: watch h5web releases for React 19 support
- #673 runs h5web in a separate iframe page (`packages/hdf5-viewer`) with its own React 18, a postMessage bridge, and on desktop a separate `gact-viewer:` origin and CSP. The main reason for all that is that h5web requires React 18 and the app is on React 19.
- When h5web (`@h5web/app`, the React viewer, not h5grove, which is the Python server) releases React 19 support, revisit this: render h5web directly in the app. That would drop the iframe, the bridge, the separate viewer origin, the extra build step (~11 s), and much of the ~1.5 MB of extra bundle.
- Owner and tracking: open an issue to watch h5web releases. (Not created yet.)

### What "the 1 GiB cap defeats slicing" means
#1834 resolves files through the same helper as `/files/read` (`readable_workspace_file`), which refuses any file over `DEFAULT_MAX_FILE_SIZE_BYTES` (1 GiB) with a 413. That cap exists because `/files/read` sends the whole file. h5grove only reads the small piece asked for (tree, attributes, one slice), so file size does not matter there. HDF5 files are often tens or hundreds of GB, so the viewer would refuse exactly the files it is for. Fix: keep the path, containment and symlink checks, drop the whole-file size cap for this route, and bound the size of each slice response instead.

## Group 3: Workspaces

| PR | Decision | Action when executed |
|---|---|---|
| clio-agent#1804 (refuse control characters in workspace roots) | Approved, with small changes. **SDK revalidation: unchanged; ports cleanly; 47 tests pass.** | Use one status code with the neighbouring mkdir error (422 vs 400). Strip whitespace on both POST and PATCH (today only PATCH strips). Optional: also refuse `Cf` characters such as bidi overrides. |
| clio-agent#1803 (read workspace mcp.yaml from the workspace root) | Approved. **SDK revalidation: the bug persists in a new place; needs an SDK-specific rewrite.** | On SDK, `_build_tool_gateway` calls `load_agent_mcp_servers(plugin_servers=...)` without `cwd` (the handshake in `routes/mcp_specs.py` does pass it). Add `cwd=cwd` there and `cwd: str \| Path \| None` in `mcp_config.py`. Move the test into `tests/test_gact/test_workspace_tool_executor.py` (the PR's `test_agent_blueprint_lazy_mount.py` is deleted on SDK). Drop the PR's comment block so `agent.py` stays at its 905 baseline. Note: `runtime/mcp_launcher.py:65` also loads without `cwd`. Release note: a launch-folder `mcp.yaml` no longer applies to workspace gateways. |
| clio-agent#1822 (per-session artifact badge) | Approved; **keep the badge** (useful for evals and tests). **SDK revalidation: the bug still exists (the new tests fail on SDK base).** | Rebase. Set the `routes/artifacts.py` baseline to **897**, and drop the PR's stale `routes/agents.py: 931` entry (that file doesn't exist on SDK). |
| clio-agent#1771 + gact-tui#628 (one workspace per folder) | **Approved with changes.** The folder is the workspace's identity; a duplicate folder gets a 409. | See the decision below. |

### #1771: what happens today vs what is proposed
- The PR implements **gact-tui#219**, opened by JaimeCernuda and labelled `wave:v1.0-must`, milestone v0.11. The issue asks for a 409 or for returning the existing workspace.
- **Today (main):** any number of workspaces can point at one folder. Each has its own id, name and sessions. They share the folder's files, the write-grant registry (keyed by folder), and `<folder>/.clio` storage, unless `storage_root` is set.
- **Proposed:** the folder is the workspace's identity. A second create on the same folder, or re-pointing a workspace at a folder another workspace already uses, gets a 409 that names the existing workspace. Duplicates already on disk still load, with a warning.
- **Correction to the earlier review:** fbfdbc4 does not contradict this. It made grant revocation safe for the shared-folder case that main allows. Old duplicates survive #1771, so that bookkeeping is still needed. The 2 failing tests set up the shared case with `create()`. They would need to build it the way legacy data looks, bypassing `create()`, rather than being deleted.
- **Real problems to fix:**
  - Renaming an existing duplicate fails, because PATCH always re-sends the folder. Only check the folder when it actually changes.
  - `ws_default` sits on the server's launch folder, so creating a workspace for that folder gets a 409. That is a common case.
- **Decisions (maintainer):**
  - (a) Refuse a duplicate folder with **409 `workspace_root_conflict`**, as the PR does. Don't return the existing workspace.
  - (b) **`ws_default` is not special.** It is "home": one big workspace for loose sessions. The same rule applies to it, so creating a named workspace on the default's folder gets a 409 that names the default. Sessions in the default workspace **share it**: no auto-workspace or subfolder per session. Two sessions in default are expected to see the same files and storage and be able to work with each other.
- **Required changes before merge:**
  - Fix the rename bug: in `WorkspaceStore.update`, run the conflict check only when the folder actually changes (`same_root(new, ws.root_path)` skips it).
  - Rebase onto main.
  - Make the 2 fbfdbc4 tests (`test_workspace_folder_changes.py::test_repointing_does_not_revoke_another_workspace_grant`, `::test_revocation_keeps_another_workspace_owner_at_the_same_primary`) build the shared folder the way legacy duplicates exist, bypassing `create()`. Keep the tests: legacy duplicates still load, so fbfdbc4's grant bookkeeping still matters.
  - gact-tui#628 (spec): it already says "a PATCH that changes `root_path`". Add explicitly that resending the current root or a rename never returns 409, and that the default workspace follows the same rule.
- **SDK revalidation:** unchanged. Workspace code on SDK is identical to main, and the rename bug is still there. #628 cherry-picks cleanly onto the gact-tui SDK SPEC. #1771 has one test conflict, in `test_mcp_servers.py`: keep the SDK `metadata.agent_ref` flow, plus the PR's 409-reuse handling. On SDK, the only failures are the 2 known fbfdbc4 tests.

## Group 4: Transcript, streaming and error contracts

| PR | Decision | Action when executed |
|---|---|---|
| clio-agent#1772 + gact-tui#629 (announce a streamed part on its first real content) | Approved | Small change: leave unannounced open parts out of the mid-stream reload projection (`routes/messages.py::_live_assistant_message`, via `live_parts_alias`), or narrow the claim in the docs. Merge #1772 first or together with #629. **SDK revalidation:** unchanged. The code is byte-identical on SDK, both cherry-pick cleanly, and 283 tests pass. #629's SPEC text ("appears neither live nor on reload") is only true once the reload gap is fixed. |
| clio-schemas#41 (resource message block) | **Approved: merge as is, plus our own additions in the same integration pass.** | Bump to 0.6.0b16. In the same pass, we add the two shapes the SDK emitter already sends but the closed union rejects: `notice.metadata {hook, revises}` and the `context_reference` block. After release, gact-tui bumps its pin, deletes the hand-written `resourceMessageBlockSchema` and `TODO(schema 0.2.4)`, and regenerates. Upcoming (maintainer): clio-schemas will also be extended to support diffusion models, so keep the union easy to extend. (The 29 `test_roundtrip` golden-ts failures also happen on develop alone.) |
| clio-agent#1805 (typed 503 for an unreadable transcript) | **Keep the goal. We do the rework ourselves; nothing is sent to mattjala.** | Rework: (1) a rebase onto main's current read path (`read_framed_blob` retry; atoms-only `materialize_ledger`); (2) two causes: transient (storage unreachable → `recoverable: true`) and corrupt (frame/decode failure after the retry → not recoverable); (3) a typed cause code in `details`, with no internal tag or blob names in the message; (4) a companion gact-tui PR: SPEC §14.2 row plus UI handling (for corrupt, offer to remove the session, per the issue). Also document that the handler is app-wide. **SDK revalidation:** the problem is confirmed on SDK: GET /messages returns a 500 `internal_error`, `recoverable: false`, and leaks blob names in `details.original_message`. The rework must target the `read_framed_blob` read callback: a `BlobFrameError` after its single retry means corrupt data, and a GetBlob `RuntimeError` may be transient. It must also target the atoms-only `materialize_ledger` (wrap `materialize_from_atoms`), with the `LedgerReadError` handler placed next to `RecordingUnconfirmed`. UI/SPEC: gact-tui SDK handles neither `messages_unavailable` nor `core_recording_unconfirmed`, so cover both. |

## Group 5: Agent runtime

| PR | Decision | Action when executed |
|---|---|---|
| clio-agent#1812 (refuse an expert whose inputs match none offered) | **Approved: port onto the SDK-era code after the SDK merge.** It fits "don't fix, report", as long as the error reaches the calling agent. | After the SDK merge, port `bind_task_inputs` / `ExpertInputsUnmappableError` to `clio_react.py`. Add `"reason": "expert_inputs_unmappable"` to `details`, so `child_task_error_reason` returns it to the parent instead of `agent_error`, and add a test that the parent sees it. Keep mattjala as author. | Rebase (the conflict is only the import block in `clio_react.py`). Add `"reason": "expert_inputs_unmappable"` to the error's `details` (convention: `errors.py:244`, `:366`), so `child_task_error_reason` (`turn_spawn_failures.py:20-25`) carries it to the parent instead of `agent_error`. Keep mattjala as the commit author. Optional follow-up: an install-time check that covers non-ReAct expert types. |

### #1812 root cause: main vs the SDK branch
- **Main:** an expert pack's frontmatter can declare its own input names (`signature: {inputs: …}`, read at `gact/expert_packs.py:250` and `:603`). The runtime always sends the task as `question`. `clio_react.py` keeps only the kwargs whose names match the declared inputs, and drops the rest silently. So two contracts are declared separately and silently intersected; that is the design flaw.
- **SDK branch (`claude/sdk-plugins-flows`):** `expert_packs.py` is gone, and nothing in `plugins/` or `sdk/` populates `AgentDef.signature`. Every agent and subagent gets the fixed contract `system_prompt + question (+ images/files)`, so plugin authors can no longer trigger the bug. Dead code is left behind: `AgentDef.signature` (`gact/types.py:633`), the `raw_inputs` parsing in `gact/agents/builders.py:330-393`, and the silent filter at `clio_react.py:319`.
- **Note:** with the fixed contract, plugin authors can't currently trigger this. #1812 is a guard that reports, and fires if any path (dead code, a future input-declaring feature) breaks the contract. Optional separate cleanup: remove the dead `signature.inputs` path.

**Rule: we do not send review requests to contributors. Where changes are needed, we make them ourselves.**

## Group 6: gact-tui web app

| PR | Decision | Action when executed (on the SDK-era base; we make any changes ourselves) |
|---|---|---|
| gact-tui#646 (clear legacy token store; docs/security.md) | Approved. **SDK revalidation: unchanged; cherry-picks cleanly, tests pass.** | Nit: soften "does not keep the old tokens on disk" (removeItem is not a secure erase), and advise rotating tokens used with old releases. |
| gact-tui#645 (one accessible loading primitive) | Approved. **SDK revalidation: rework onto the existing primitive.** | The SDK branch already has `LoadingFeedback` (`loading-feedback.tsx`, 96d1146b): visible label, role=status + aria-live + aria-busy, and reduced-motion safe. It is used by chart, data-table and map. Make `ResourceLoading` a thin wrapper over `LoadingFeedback variant="skeleton"`, or replace it with `LoadingFeedback` everywhere. Still missed sites: `workspace-resource-browser.tsx` `LoadingResources`, `workbench-resource-browser.tsx:399` local `ResourceLoading`, the artifact-provenance "Loading version history…". `ui/skeleton.tsx` `animate-pulse` has no motion-reduce guard. The new SDK loading sites (settings-plugins, installed-plugin-list, agent-plugin-menu, plugin-review-dialog, flow-run-view/card/parts) already use visible text. |
| gact-tui#644 (move heavy code out of first load) | Approved. **SDK revalidation: manual re-port, smaller gain.** | It conflicts in 6 files on the SDK branch. Keep the SDK side, and drop the blueprint-editor (Ace) and workflow-canvas parts: both are gone on SDK. Trap: the lazy `ClioWorkflowCanvasView` hunk merges cleanly but points at a deleted file, so remove it by hand. Still useful: lazy CSV/JSON viewers, lineage graph, `ConversationRelationships` (takes React Flow out of the app chunk), WhatsNew, gallery repo override. Measured on SDK: 1.498 → 1.395 MB gzip (−6.9%). The duplicate `ResourceLoading` import with #645 is confirmed. TanStack Table is still in the first load (`selection-actions`, 676 kB). Follow-up: drop the dead `ace-builds`/`react-ace` deps and the orphaned `workflow-graph.tsx`. One artifact-provenance lineage test flaked once under load; consider a longer `findBy` timeout. |

Order: #646, then #645, then #644, then #673.

## Group 7: Daisy

| PR | Decision | Action when executed |
|---|---|---|
| clio-agent#1790 (remote deployment tutorial) | Approved, with small changes. **SDK revalidation: unchanged.** All 30 UI labels and 8 stages match the gact-tui SDK branch, and it cherry-picks cleanly. | The page title is already "Remote deployment with CLIO", so "draft" is only in the PR title. Add the compute-node allocation prerequisite. Reword step 12 (the check also finds and reuses an existing CLIO, and fails on a foreign occupant). Name the text label, not just the "green status dot". |
| clio-agent-marketplace#96 (factorio-flat 0.5.0) | **Close as superseded: already absorbed by the SDK branch.** | `wait_job.sh` → `plugins/slurm/skills/hpc-batch-jobs/scripts/`; `exo_summary.py`/`xml_params.py`/`diag_summary.py` → `plugins/morphorm/skills/morphorm/scripts/` (byte-identical; tests kept and passing; 42f390b). The prompt section became the L0 skill `plugins/scientific-method/skills/run-non-interactive-task/SKILL.md` (0e9bc8a), with the handcuffs dropped. "Work directly" is in `plugins/factorio/ai.iowarp.clio/prompt/mission.md` (8fcb2a7). The CHANGELOG credits DaisyQuach (b23ec87). To do: add a `Co-authored-by: DaisyQuach` trailer when the SDK branch lands, because 42f390b names her only in the message body. |
