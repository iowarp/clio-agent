# A2UI campaign: merge and release handoff

Prepared on 2026-10-03. Scope: the A2UI, selection/reference/image semantics,
widget gallery and demo work from this campaign. Unrelated local branches are
excluded. This is a publication handoff, not a release qualification report.

## Branches to integrate

| Repository | Branch | Merge target | Scope |
| --- | --- | --- | --- |
| [clio-schemas](https://github.com/iowarp/clio-schemas/tree/codex/a2ui-widgets) | `codex/a2ui-widgets` | `main` | Interactive widget and geo contracts, map trajectories, numeric legends, exploratory filter fields, automatic linked selection guidance, connected component trees, catalog bounds and chart fixtures. |
| [gact-tui](https://github.com/iowarp/gact-tui/tree/a2ui_fixes) | `a2ui_fixes` | `develop` | Renderer revision recovery, shared affordances, linked selection, filtered references, trajectory selection, mesh selection and region capture, sent reference cards, registered artifact images, image sizing, bounded tool output, autoscroll, startup status and component permalinks. |
| [clio-agent-marketplace](https://github.com/iowarp/clio-agent-marketplace/tree/codex/a2ui-visual-artifacts) | `codex/a2ui-visual-artifacts` | `main` | Base Agent guidance to inspect the source behind selections and display generated PNG/JPEG/SVG artifacts through A2UI with downloads, distinct figure surfaces and preserved source views. |
| [clio-agent](https://github.com/iowarp/clio-agent/tree/a2ui_fixes) | `a2ui_fixes` | `develop` | Backend A2UI production/action/data-reference contracts, table export/query and raster queries, damaged transcript/ledger handling, presentation guidance, standard prompt guidance, rebuilt widget docs, thermal linked example and continuously recorded demos. |

The two `a2ui_fixes` branches already consolidate earlier surface-affordance,
revision-recovery, chart-guard and approval-routing slices. Integrate the
combined branches; those older slices are not separate merge targets.

## Published dependency heads

- UI: `19d624671af72d4874e7e288399f241fc14bed6e`.
- Schemas: `34b08f9` (no additional dirty changes at handoff).
- Marketplace: `ee2c9fe1a1e92424c4bfebfe43b8debc2c5e4c35`.
- Agent: use the published HEAD of `a2ui_fixes`; this document is included in
  that branch. The agent's GACT UI submodule and Pages build pin the UI head
  above. Its marketplace submodule pins the marketplace head above.

At the refreshed remote snapshot, before adding the final agent commits:

| Branch | Target-only commits | Branch-only commits |
| --- | ---: | ---: |
| Agent `a2ui_fixes` | 91 | 51 |
| UI `a2ui_fixes` | 4 | 59 |
| Schemas `codex/a2ui-widgets` | 3 | 12 |
| Marketplace `codex/a2ui-visual-artifacts` | 9 | 1 |

These counts are `git rev-list --left-right --count target...HEAD`, include
merge commits and are a snapshot. Refresh before integrating.

## Integration and release sequence

1. Refresh all four repositories. Rebase or otherwise reconcile schemas with
   `main`, UI with `develop`, marketplace with `main`, and agent with `develop`.
   Preserve the combined campaign work and newer upstream fixes.
2. Reconcile schema/package version metadata and the generated catalog mirrors.
   `clio-schemas` v0.5.2 is already published; this branch also declares 0.5.2,
   so its new schema work needs the next release version. The agent branch still
   pins `clio-schemas==0.5.1` and declares agent 0.9.4.24. These are not the
   intended next beta versions. Do not release those stale versions.
3. Qualify the integrated build with current schema exports, backend, UI and
   installed Base Agent together. Run CI and the full release gates. Focused
   checks and website playback do not establish full release readiness.
4. Merge/release schemas first, update the agent dependency and UI catalog
   mirrors, merge marketplace guidance and update the agent's marketplace pin,
   then merge the UI and agent to `develop`. Update the GACT UI gitlink and Pages
   pin after any rebase changes commit identities.
5. Follow the normal develop-to-main beta release process and verify published
   assets and required workflow conclusions. No merge, tag or release was
   performed during this push request.

## Evidence already obtained

- Latest prompt/skill checks: 10 passing; marketplace checks: seven passing;
  media checks: 24 passing. TypeScript and focused lint checks passed.
- Earlier focused checks covered bounded output, transcript autoscroll,
  registered image resolution, sent references, startup status and artifact
  metadata arrival. See the corresponding committed regression tests.
- Latest website rebuild: 60 pages; internal links valid.
- Browser review confirmed the captured image preview directly below the
  thermal views, with an 8-pixel gap, before explanatory sections. Changing
  example tabs clears the prior preview.
- Selection demo: 23.1 seconds. Region/image demo: 63.5 seconds, actual source
  capture, attachment inspection, request, draft image, real refinement request
  and final image opening. Embedded and fullscreen playback reviewed.
- The final region figure was generated in a real Codex/Luna session. New image
  surfaces preserve its source map. It is not a scripted replacement response.

No additional tests were run solely for this commit/push request. The evidence
above was obtained during implementation; integration must be checked again
after rebasing and resolving conflicts.

## Remaining work and constraints

- The requested third linked Abaqus example still needs real baseline/optimized
  mesh or element-density cycle exports. The local experiment10 directory and
  `JaimeCernuda/abaqus-scripting` contain final STL meshes/reports, not those
  exports. CHPC SSH reached the host but rejected authentication. A source-file
  location has been requested. Do not invent the screenshot's stress/volume
  numbers or substitute a synthetic field. Keep the existing thermal example.
- Runtime crash recovery/damaged ARC state received targeted handling; repeated
  demo process crashes do not by themselves establish that the underlying
  runtime fault is resolved. Include that path in release qualification.
- Raw recordings and transient build output are not Git source. Original
  recordings remain under
  `D:/Libraries/Videos/clio_recordings/2026-10-03-image-widget-reshoots/`
  (27 native takes, 393 hash-verified files), with prior shoots in sibling
  archives. This is local preservation, not an off-machine backup.
- Published edited clips, captions, poster frames and the final PNG are in
  `site/public/media/`. Capture/edit scripts, exact edit manifests, reshoot
  procedure and lessons are in `site/video/`.

## Local checkout locations

- Agent: `D:/Libraries/Documents/projects/clio-agent`.
- UI: `D:/Libraries/Documents/projects/clio_develop_workspace/temp/gact-tui-a2ui-fixes`.
- Schemas: `D:/Libraries/Documents/projects/clio_develop_workspace/temp/clio-schemas-a2ui-052`.
- Marketplace: `D:/Libraries/Documents/projects/clio-agent/external/clio-agent-marketplace`.

Agent-created UI logs remain untracked locally. Raw recordings, recorder profile,
screenshots, rendered builds and generated widget bundles remain excluded from
the commits. The source and edited website deliverables are committed.
