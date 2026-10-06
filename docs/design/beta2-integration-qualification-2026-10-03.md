# Beta 2 integration qualification

This records the integrated follow-up to the [A2UI handoff](a2ui-release-handoff-2026-10-03.md).
The release combines the release-user-issues fixes with the A2UI campaign, using
Agent 0.9.5b2, UI 0.11.3-beta.4 and published clio-schemas 0.5.3.
CI conclusions and merged dependency identities remain authoritative on the release PRs.

## Live model and browser acceptance

The real Agent, installed Base Agent, Codex Direct provider and production UI
served an isolated local workspace with a synthetic 500-row stations CSV.
Each row had a unique `station_id`, latitude, longitude, amplitude and depth.
Both models received the same request for linked scatter, map and table views,
using normal catalog discovery and one registered dataset instead of inline rows.

| Final run | Tool calls | Catalog lookup roundtrips | All skill lookup roundtrips | Surface attempts |
| --- | ---: | ---: | ---: | ---: |
| Codex Luna, `sess_4d087809ff7f` | 7 | 2 | 3 | 1 |
| Codex Astra, `sess_f32b4e4c6367` | 7 | 2 | 2 | 1 |

Luna loaded its presentation procedure separately; Astra loaded it alongside the
catalog index. Both then read the required component schemas as a batch. Neither
final run needed a rejected-tool correction, copied the dataset, or chunked it.
Earlier trials exposed inconsistent batch instructions and a request containing
both `file` and `files`; the integration combines these inputs and deduplicates them.
Earlier trial failures remain in the local evidence rather than being counted as passes.

Browser review of both final surfaces confirmed 500 chart rows, 500 map locations
and a paginated 500-row table. Selecting `station_000` on the map selected its
table row and highlighted the same chart point. All views retained the same
artifact reference and row identity. The CLIO owl and custom UI icons rendered.

## Focused qualification

- Backend: 70 integration regressions, 40 ARC/native-client tests, 35 transcript
  and environment tests, 18 release-policy tests, 134 producer/catalog tests,
  and 61 skill-runtime/event tests passed. Ruff and full source Mypy passed.
- UI: the full web unit suite, 251 core tests and 59 desktop tests passed.
  Thirteen A2UI data browser cases passed, alongside chart, desktop-map CSP and
  temporary-composer checks. The 1,000-message/100-delta-per-second regression
  passed three consecutive runs after snapshot validation began yielding between
  batches; its existing 50 ms responsiveness threshold was retained.
- A stalled-daemon test now confirms the process is stopped before probing it,
  closing a signal-delivery race. Suspend and kill modes passed three repetitions
  each without relaxing their typed-error or timeout assertions.
- DSPy proxy loading waits for an in-progress DSPy import to finish. A controlled
  import-lock regression and 21 repeated import-order/first-query tests passed in
  an isolated environment using the published dependency, rather than the
  workstation's locally modified DSPy installation.
- All 48 UI browser cases and both deployment-dialog cases passed in CI. The
  packaged corpus smoke was updated for the current accessible surface label and
  deferred rendering; it locally verified every one of the 43 published examples.
- The final UI head `45785c37` passed the complete workspace, Go and schema jobs,
  including Windows/Linux Desktop debug builds and the real native WebView proof.
- UI follow-up `1f1cfa14` changes only Docker context/CI policy: preserve the PNG
  imported by the widget gallery instead of excluding every PNG. All three Docker
  image builds and Go CI passed. The UI beta tag was refreshed to this corrected
  source; the application code remains identical to `45785c37`.
- The 0.9.5b2 source distribution and wheel built successfully. A clean persistent
  tool install reported `clio-agent 0.9.5b2`; installed metadata pinned schemas
  0.5.3, and native/relocated filesystem roots resolved correctly. The pre-publish
  wheel smoke now uses the same explicit FastMCP beta roots as the official
  installers and published-package smoke. All 19 release-policy tests passed.
- A Linux parent-death test publishes its PID file atomically, preventing the
  reader from observing an empty file while the child starts. Its kernel reaping
  assertions remain unchanged; the Linux run is verified in CI.
- The busy-lane transcript fixture holds its simulated writer lock until the
  reader returns instead of releasing it after a fixed sleep. Both loop modes
  passed five repetitions each with the existing latency and audit assertions.
- Website: 27 unit tests, three video tests, Astro checks, a 60-page production
  build and internal-link checks passed. Home and interactive widget pages were
  visually reviewed. Marketplace's 191 tests passed with schemas 0.5.3.

Raw session messages, run logs and CI diagnostics are preserved locally under
`out/beta2-live/`; they are ignored build evidence, not shipped application data.
Full CI is evaluated separately on each final PR head; focused passes do not
override a failing full-suite check. Existing platform-gated skips are not passes.

## Release follow-up qualification

- Native workflow `37166419334` built the repaired Mac DMG and passed signature
  verification before and after real packaged backend/Desktop startup on macOS
  14, 15, and 26. The original bundle mutated its sealed Python resources during
  first startup; bytecode writes are now disabled and the repair fingerprint is
  prepared before signing. Agent CI `37166419290`, Docker, schema and Pages passed
  at `26902b90`. The vector-branding follow-up `a0e5037a` also passed full Agent
  CI `37168002479`, Mac startup workflow `37168002474`, Docker, schema and Pages.
  The final release DMG must pass the same native gate.
- Combined UI pin `96d50c7f` passed native workflow `37169633416`: the final app
  built and preserved its signature through startup on macOS 14, 15 and 26.
- UI follow-up adds the saved **Enable beta updates** preference, instability
  warning, beta-number comparisons, published-release selection and a native
  bridge retaining signature verification and no-downgrade behavior. Local checks
  passed: 64 focused unit/integration tests, two native endpoint-policy tests,
  lint, typecheck and production build. Settings persistence and the form/action
  lifecycle passed three browser repetitions each.
- UI head `61bdde2a` passed complete workspace CI `37168616909`, including browser,
  accessibility/visual checks, all 43 corpus examples, Windows/Linux native builds
  and the real native WebView proof. PRs #533 and #534 rebase-promoted it through
  develop to main `d5edd72b`; the trees match exactly. The Agent and Pages pins use
  that main commit, tagged UI `v0.11.3-beta.3` without replacing public UI beta 2.
- The packaged CSP now permits the specific GitHub API origin used by beta
  metadata checks. A browser regression failed with the old policy, then passed
  with the packaged policy while showing beta 2 available to a beta-1 Agent.
  Channel changes and background checks cannot invalidate an active installation.
- The form regression reproduced loss of typed input on an unchanged legacy
  surface without a part ID. Skipping an unchanged revision fixes the failure
  while retaining rebuilds for actual legacy updates and fresh lifecycles.
- Header, footer and favicon use the supplied true vector owl. Mobile docs have
  separate Docs index and On this page controls. Six browser tests passed,
  covering 360/390/412px viewports, reaching the final sidebar entry, navigation,
  gallery pages without a heading menu, and checking that home/docs logos contain
  paths rather than embedded bitmaps. The gallery regression reproduced the
  missing index; the control now lives independently in the shared header.
  Rendered light/dark mobile screenshots were inspected; 27 site unit tests and Astro checks
  passed (zero errors/warnings; five existing hints).
- Download cards already fetched the latest stable release, but their heading
  advertised the package's beta version. The heading and notes now identify the
  stable downloads; beta downloads have a separate link and instability warning.
  A browser regression checks the displayed channel against the installer URL.
- Inspection of the release web archive caught a production-only SVG filename
  bug: the UI plugin emitted `.svg_xml`, which decoded with naturalWidth=0 for
  both the logo and wordmark. UI beta 4 emits `.svg`; a real branded production
  build served through HTTP decoded both at naturalWidth=1024 and rendered them.
  The regression failed before the fix; all 18 branding tests, lint, typecheck
  and production build passed afterward. The final UI pin is `e0d30349` (PRs
  #535/#536); runtime UI and Rust code are unchanged from the qualified beta 3.
- Desktop beta 1 requires one manual Desktop upgrade to obtain channel-aware
  updates. Its hardcoded stable feed cannot be pointed at beta without exposing
  stable users. Drafts remain excluded; beta 2 becomes available after publication.

## Remaining acceptance boundaries

The third Abaqus demonstration still requires its real source exports. Original
CHPC deployment and interactive Desktop-exit acceptance have not been reproduced
on that user's host. The namespace and SSH/lifecycle regression checks cover
their code paths but do not replace host-specific acceptance.

The blank composer remains temporary while warming workspace services; navigation
alone creates no session. A failed message retry reuses the newly created session
while that draft remains open. Session creation does not yet carry an idempotency
key, so a lost creation response or leaving a failed first-message draft can still
leave a real session. This limitation is distinct from the navigation-only bug.
