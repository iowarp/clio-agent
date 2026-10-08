# Download routing audit

The pass covers user-initiated file downloads and exports across the CLIO workspace UI and its desktop shell. Changes are in draft UI PR [#556](https://github.com/iowarp/gact-tui/pull/556), pinned by [clio-agent #1661](https://github.com/iowarp/clio-agent/pull/1661). Installed beta 5.2 does not include this patch.

## Routing

`surface-export.ts` owns URL, Blob, byte and text downloads. It creates one download anchor, preserves the filename and no-referrer policy, opens native Downloads on desktop, and delays revocation of Blob URLs until the browser can consume them. The Windows desktop shell additionally observes WebView2's actual DownloadStarting event in every webview and opens that webview's default Downloads dialog. This catches downloads initiated by embedded editors or direct browser navigation as well as CLIO controls. File-opening choices stay with the user through native history's controls. The event observer leaves destination, cancellation and progress with WebView2; it does not synthesize history entries.

| Entry point | Formats / content | Route after audit |
| --- | --- | --- |
| Session menu and navigation | Transcript HTML; artifact/workspace ZIP | Prepared download URL → shared handler |
| Original-file actions | Workspace files, attached resources and derivatives, artifact revisions | Repository bytes → shared handler |
| Document workspace and image preview | Original document/image | Repository bytes → shared handler |
| Artifact card | Original artifact | Repository bytes → shared handler |
| Versions → Export with evidence | RO-Crate ZIP | **Fixed:** repository bytes → shared handler; previously made an independent anchor and revoked its URL immediately |
| Reusable conversation-download component | Markdown | **Fixed:** formatted text → shared handler; previously made an independent anchor. Currently available as a reusable component, rather than used by the main session export menu |
| Charts | PNG, SVG, JPEG, inline CSV and server table formats | Shared Blob/text/table handlers |
| Tables | CSV, JSON, Parquet, current view or complete data | Shared text/byte handlers |
| Maps | PNG, GeoJSON, CSV, JSON, server table formats | Shared Blob/text/table handlers |
| Raster views | PNG and sampled CSV | Shared Blob handler |
| Mesh views | PNG and serialized 3D formats | Shared Blob/byte handlers |
| Mermaid and workflows | SVG, PNG, Mermaid source | Shared Blob/text handlers |
| Code views | Source or patch | Shared text handler |
| Embedded editors and browser-originated file responses | Browser-controlled downloads | **Added:** native Windows DownloadStarting observer |

Raw download-anchor construction in production UI is now confined to the shared handler. Remaining object URLs support previews, attachments and browser opening. External links open the user's browser, whose downloads belong to that browser's history. Offline exported review pages also run in the host browser.

Connected-source transfers save copies on the workspace host and retain their existing operation progress in Sources/Files. Model acquisition and app updates retain their host/update progress. These service-managed operations do not create browser downloads; their code was checked separately from file export paths.

## Validation

- 70 focused Vitest tests passed across shared helpers, evidence export, Markdown export, session HTML/ZIP export, file/document views, chart/map serialization, Mermaid downloads and table exports. Five added regressions cover both hosts, ZIP byte-view boundaries, Markdown content and failed evidence preparation. Fetch/provider responses are test doubles; the download components and shared handler run unchanged.
- `pnpm exec playwright test e2e/evidence-download.spec.ts e2e/workspace-presentation.spec.ts --grep 'evidence|unsupported workspace file'`: two passed. The actual browser downloaded one valid ZIP and preserved its filename/bytes; the shared unsupported-file action also saved its original bytes. The fixture service and routed ZIP response are test-owned. Screenshots were inspected and retained locally under `.local/beta-ui-evidence/download-routing/`.
- Full workspace lint, web/core type checks through the production web/offline build, and scoped Rust formatting checks passed. A repository-wide Rust formatting check also reports an existing assertion layout in `installer_runtime_stop.rs`; this pass leaves that unrelated source unchanged.
- Windows: `cargo run --manifest-path desktop/src-tauri/Cargo.toml --locked --example downloads_acceptance` builds the production desktop library and a real WebView2 acceptance executable. Direct CSV and embedded-frame JSON downloads both saved intact and each automatically opened native Downloads. The test closes history between downloads, checks WebView2's actual dialog state and requires exactly two native download events. The frame action uses trusted DevTools mouse input, preserving browser download protection.
- The native example includes its own Common Controls v6 manifest, as the normal app already does. Its output retains example-only default-icon and WebView2 teardown diagnostics. This proves the native download hook and generic embedded-frame path; a full live OnlyOffice/Collabora server session was not exercised.

New-head CI must be evaluated after push. This patch remains unreleased.
