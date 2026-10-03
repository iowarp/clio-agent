# Beta 2 integration qualification

This records the integrated follow-up to the [A2UI handoff](a2ui-release-handoff-2026-10-03.md).
The release combines the release-user-issues fixes with the A2UI campaign, using
Agent 0.9.5b2, UI 0.11.3-beta.2 and published clio-schemas 0.5.3.
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
- The 0.9.5b2 source distribution and wheel built successfully. A clean persistent
  tool install reported `clio-agent 0.9.5b2`; installed metadata pinned schemas
  0.5.3, and native/relocated filesystem roots resolved correctly. The pre-publish
  wheel smoke now uses the same explicit FastMCP beta roots as the official
  installers and published-package smoke. All 19 release-policy tests passed.
- A Linux parent-death test publishes its PID file atomically, preventing the
  reader from observing an empty file while the child starts. Its kernel reaping
  assertions remain unchanged; the Linux run is verified in CI.
- Website: 27 unit tests, three video tests, Astro checks, a 60-page production
  build and internal-link checks passed. Home and interactive widget pages were
  visually reviewed. Marketplace's 191 tests passed with schemas 0.5.3.

Raw session messages, run logs and CI diagnostics are preserved locally under
`out/beta2-live/`; they are ignored build evidence, not shipped application data.
Full CI is evaluated separately on each final PR head; focused passes do not
override a failing full-suite check. Existing platform-gated skips are not passes.

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
