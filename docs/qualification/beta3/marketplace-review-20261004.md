# Marketplace review — 2026-10-04

The marketplace screen now uses the existing buttons, tabs, dialogs, status badges,
and host folder picker with a searchable catalog, visible settings/remove actions,
and advanced registration fields collapsed by default. Errors have short recovery
advice with their complete diagnostics retained in expandable details.

## Correctness changes

- Staged installation previously ignored tools mounted on the running host. It now
  uses the same runtime tool catalog as interactive blueprint validation, both for
  direct installation and Reload. Genuine missing tools still reject the revision;
  the rollback test checks that neither installed pack changes.
- A failed add can persist a marketplace registration. The UI now refreshes the
  inventory on failure so the saved row is visible and can be repaired. Failed
  reloads also refresh their persisted status. A fresh Add clears the prior error.
- Catalog descriptions survive schema decoding, search includes descriptions,
  and installing one blueprint no longer labels the marketplace as reloading.
- A new marketplace inherits the selected workspace. Switching from a repository
  to a folder clears repository selectors. Existing configuration retains its pin
  and authoring checkout, and Save still does not perform Reload.

## Verification

- Backend: 23 passed across `test_blueprint_install_revision.py`,
  `test_blueprint_source_configuration.py`, `test_blueprint_reload.py`, and
  `test_blueprint_operations.py`. No skipped tests. These include real filesystem
  revisions, HTTP routes, local Git history, and an isolated MCP startup/list check.
- Frontend: 20 passed across settings, source dialog, source row, operation status,
  and blueprint details tests. No skipped tests.
- Workspace and core TypeScript checks passed. Scoped oxlint, core ESLint, Python
  Ruff, and mypy (five changed production modules) passed. Component reuse, icon
  vocabulary, frontend size and Python file-size guards passed; scoped diff checks
  passed. The global brand-literal guard still reports six existing violations in
  connected-source/account files outside this marketplace change. No marketplace
  brand-literal violations remain.
- Browser acceptance used the real API in a new isolated state directory on port
  18947 with no inference agent or account credentials: add a local marketplace,
  use the host folder picker, save its name, browse contents, search, reload a
  changed pack from 1.0.0 to 1.0.1, uninstall/reinstall that pack, and inspect the
  installed details. The installed file/version and Reload receipt confirmed the
  new revision. The removal confirmation was inspected and cancelled; cleanup
  removed only the temporary fixture through the API.
- Original browser connection restored to port 18825. Temporary registrations on
  both servers were removed. The isolated acceptance process was stopped.

## Earlier preview limitation (superseded by the live recheck below)

Port 18825 is an older Python process. A live add exposed mixed loaded/source
modules (`cannot import name 'validation_tool_names'`); it needs a restart to load
the backend changes consistently. An earlier automatic approval review rejected
restarting that preview with only `blocked by policy`, so that action was not
repeated. The UI updates are live; fresh-server acceptance covers the backend fix.

The existing built-in marketplace Reload receipt reports missing Cluster Operator
relay tools. This no-agent preview actually lacks those tools. Passing the live
catalog fixes false missing-tool failures when tools are mounted; it does not make
unconfigured relay tools available. No real model run, private Git authentication,
or user marketplace upgrade was claimed as tested.

## Rendered evidence

### Follow-up organization and details

- Installed blueprints are grouped by marketplace, with marketplace/status filters,
  name/recent-install sorting, result counts, clear filters, and visible Details buttons.
- The workspace selector says **Shared across workspaces** or **Workspace: name**;
  shared view filters out workspace-only installations. A workspace view includes its
  shared dependencies. This is separate from filtering by marketplace.
- Details use Overview, Instructions and Installation tabs, with service names,
  requirements, full source location and revision, and rendered blueprint instructions.
- Follow-up tests: 9 passed across installed-list, settings, and details tests.
  Browser checks on the current 14-blueprint inventory verified search, empty status
  filtering, reset, opening Details, rendered instructions and installation metadata.
- [Grouped marketplace list](marketplace-grouped-20261004.png)
- [Blueprint details](marketplace-details-20261004.png)

### Earlier acceptance

- [Fresh-server browser acceptance](marketplace-browser-acceptance-20261004.png)
- [Simplified Add dialog](marketplace-add-dialog-20261004.png)
- [Original preview, including its retained failure](marketplace-current-preview-20261004.png)


## Live preview recheck and polish — evening follow-up

The current preview on port 18825 was restarted using its owned qualification
launcher, and the real UI on 4394 was exercised against it. No live model was run.

- Reduced the heading and toolbar footprint; labelled workspace scope separately
  from search. The marketplace filter appears when there is more than one group.
  Status and sort fit side by side at narrow widths.
- Fixed the settings navigation overflow at 337 px. Installed rows keep descriptions
  across the full narrow width and put actions beside their version/scope metadata.
- Details are reachable from Installed and marketplace browsing. Full descriptions
  scroll with the Overview, leaving the tabs accessible for long descriptions.
- Catalog rows distinguish the available version from the actual installed version.
  A failed reload no longer suggests that the new catalog version is installed.
- Staged validation names the failing blueprint. Toasts use short recovery advice
  for install/update/degraded outcomes too; the full error remains expandable on
  the page. This includes individual install errors, which were still raw toasts.

### Live acceptance

Created a disposable local marketplace with two blueprints in `ws_default`, using
Add marketplace in the browser. Reloaded both from 1.0.0 to 1.0.1, opened their full
instructions directly from the marketplace, removed Zulu through the UI and
reinstalled it through the catalog. Search, descending name sort, marketplace
filter, empty status filter and Clear filters were exercised. Shared view showed
14 installations; workspace view showed 16 with the two workspace-only fixtures.

Changed both source versions to 1.0.2 and introduced an unavailable tool into Zulu.
Reload failed with a named, concise error. Both installed directory checksums and
versions remained exactly at 1.0.1. The catalog correctly showed Available 1.0.2
beside Installed 1.0.1. Correcting the fixture and retrying applied 1.0.2 to both and
cleared the failure. Removed the temporary registration and both installed test
blueprints through the API. The fixture remains under Temp for inspection.

The built-in marketplace still cannot reload as a whole in this no-agent preview:
Cluster Operator requires host relay/JARVIS/Spack tools that are absent. The earlier
runtime-tool-catalog propagation fix is covered by installation and reload tests;
this remaining missing dependency is real and validation remains enforced.

### Checks

- 23 backend tests passed, no skips: install revision, source configuration, reload,
  and operation history. This includes named failure and atomic rollback checks.
- 26 marketplace UI tests passed, no skips, plus 6 source/revision regression tests.
- Workspace TypeScript, scoped oxlint, Ruff check/format, mypy and diff checks passed.
  Type checking also exposed two earlier source-work typing defects: an optional
  download flag and unsupported Testing Library `exact` options. Both were corrected;
  their revision/mapping regression tests passed.
- Rendered at 1100 px and 337 px; the viewport override was reset. No horizontal
  document overflow at the narrow width. No real private Git or model execution
  was exercised in this follow-up.

Evidence:
- [Installed view](marketplace-installed-polished-20261004.png)
- [Details](marketplace-details-polished-20261004.png)
- [Narrow details](marketplace-details-narrow-20261004.png)
- [Failed reload with retained versions](marketplace-rollback-20261004.png)
- [Live acceptance receipts](marketplace-live-acceptance-20261004.json)
