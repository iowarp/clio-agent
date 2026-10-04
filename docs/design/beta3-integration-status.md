# Beta 3 implementation and qualification

This is an implementation checkpoint, **not release approval**. Publication is
separate from the October 5 Delta qualification with the owner.

## Branches and preserved work

- Agent: `codex/beta3-integration`, based on `develop` plus the existing smooth
  vector branding commit `c34ea657`. Merge commit `bcecf675` preserves PR #1490
  and ports attention declaration into the current typed DSPy request boundary.
- UI: `external/gact-tui`, branch `codex/beta3-integration`; `9976580f` reconciles
  develop, `9c8c00bb` preserves PR #502 and resolves transcript integration.
- Schemas: `codex/beta3-integration`, shared contracts commit
  `e1ac2efbdd7a9e40f0bfee8949a1af6b0611be7b`, based on existing `main`; `develop`
  was established at that same base for PR #25. Agent and UI pin
  that exact revision. Version `0.6.0b2` is not published to PyPI.
- Marketplace: `external/clio-agent-marketplace`, branch
  `codex/beta3-integration`, based on `main`; SPOTTER now uses the same pinned
  attention profiles/reducer and verified local capture files.

## Attention profile and direct-store checkpoint

- Shared schema/reducer: uniform mean and decayed-maximum presets, editable
  direction/normalization/block/display reduction, immutable profile revision
  hashes, duplicate-step elimination, and explicit unretained coverage.
- Agent exposes profile heat separately from uniform mean source mass. Tool
  blocks carry recorded call IDs and exact text revisions. UI matches tool cards
  by call ID, scopes pending attention to the connected endpoint, and recomputes
  a selection when its profile changes.
- SPOTTER has read-only `list_attention_calls` and `inspect_attention` tools over
  its configured Flowcept store. The reader verifies byte count/SHA-256, prompt
  partition and tensor dimensions; rejects partial and ambiguous captures; and
  stays within an explicit local capture root or local mirror. No SSH fetching
  or automatic quarantine is part of these tools.
- Checks: 70 Agent attention/handoff tests, 21 source files checked with Linux
  Mypy, 34 SPOTTER tests and Pyright, 47 focused UI tests, UI typecheck/lint.
- Recorded EarthScope capture replay gave exactly equal top-token scores and
  residual mass in CLIO and SPOTTER for both presets. This is recorded evidence,
  **not fresh inference or a completed OPAL demonstration**. Run
  `tests.test_gact.test_attention.verify_spotter_replay` with SPOTTER's `impl/src`
  on PYTHONPATH and `uv run --no-sync --with safetensors python -m ...`.
- Browser: selected text, opened attention, changed preset/decay/direction,
  opened the info explanation and applied the profile. The displayed mass stayed
  unchanged. Screenshot: `docs/qualification/beta3/attention-profile-editor-light.jpg`.
  The requested browser viewport override did not change the measured viewport;
  this checkpoint does **not** claim a completed narrow-layout profile review.
- Still required: shared revision-bound multi-surface selection entry points,
  deduplicated lookup in both directions, exact evidence navigation/findings,
  portal highlights, and fresh Delta inference plus both OPAL/SPOTTER demos.

## Completed checks at this checkpoint

- Agent attention, typed LM trace, and route guard tests: **66 passed**.
  Both synchronous/asynchronous typed requests and keyword/positional requests
  preserve caller configuration, declaration context, and response identifiers.
- Schemas: **762 passed**; canonical JSON export verification and focused
  Pyright passed. Generated TypeScript fixtures include the new contracts.
- UI attention: **59 passed**; merged attention typecheck passed.
- Storage, infrastructure, model plans and route guard: **48 passed** including
  inventory, path inheritance, and freezing legacy deployment paths. Focused
  Pyright and Ruff checks passed.
- Infrastructure UI, route behavior, state isolation, transcript state:
  **48 passed**, plus the new storage form regression passed (in a 17-test run
  with the route suite). Typecheck, focused lint, and production build passed.
  Vite reports CSS Custom Highlight optimizer warnings and existing large bundles;
  the build succeeds, but attention needs its separate rendered qualification.
- Real isolated-backend browser review: storage form, overrides, target-side
  folder browsing, saving locations, inherited defaults, phone navigation,
  click-open info tooltip, and desktop light/dark palettes.
  Evidence is under `docs/qualification/beta3/`.
- Read-only production storage probe through OpenSSH to homelab:
  `/` free 5,316,571,136 bytes, not writable, fails a 20 GiB requirement;
  `/data` free 2,820,260,794,368 bytes, writable, passes. Reproduce with:
  `uv run python scripts/qualification/beta3_host_storage.py homelab / /data --required-gib 20`.
  This is filesystem inspection, not a completed service deployment test.

## Work in progress

Host-bound persistent storage settings and browsing now have HTTP contracts and
UI controls. Models store their resolved paths in deployment configuration;
older model receipts are frozen before defaults change. The six Infrastructure
sections and retained management state are being connected to full workflows.

### Connected-data checkpoint (October 4)

- Local and SFTP adapters stream explicit copies into Agent namespaced storage;
  Drive uses its file API and Globus submits/reconciles native transfer tasks.
  Provider adapters declare actual modes; browser-uploaded folders and Globus
  transfers do not claim to be writable filesystem mounts.
- Source records bind workspace, owning CLIO, host and OS owner. Durable transfers
  preserve prior inputs on failure. Working-copy review shows bounded text diffs,
  checks upstream/local revisions, applies only selected files, retains immutable
  baselines and records partial success without promising directory atomicity.
- Trusted browser sign-in uses state/PKCE and private credential storage. No token
  is returned in source records. File policy and sandbox projections exclude
  credentials and protect read-only inputs; unsupported child fences fail closed.
  Source policy changes refuse busy turns and recycle resident idle tool fleets.
- Composer and Files share the source picker. Folder upload preserves nested
  paths, uses resumable resource custody, then publishes a verified baseline.
  Approved references retain source/revision identity in existing resource custody.
- Focused storage suites: **33 passed**; subsequent source-policy, HTTP and route
  guards: **8 passed**. Focused Pyright and Ruff passed. UI source/upload/composer:
  **14 passed**; typecheck, focused Oxlint and production build passed. The build
  retains the existing CSS Custom Highlight and bundle-size warnings.
- Browser checked: connect/materialize/refresh local working copy, inspect a real
  diff, apply one selected file, verify its upstream bytes, browse and attach a
  source reference without creating a persisted session; explicitly upload a
  desktop folder and browse its preserved hierarchy; mobile tap explanation and
  light/dark source views. Screenshots are under `docs/qualification/beta3/`.
- **Live homelab SFTP passed** through the production adapter against an owned
  synthetic fixture in `/data/clio-beta3-qualification/sftp-input`: nested transfer,
  selected writeback, unchanged unselected file, restart receipt and restored
  original fixture. Receipt: `docs/qualification/beta3/homelab-sftp.json`.

Not yet qualified: live Google/Globus authorization and transfers (distributor
application registrations/test-user access still needed), full source setup skill,
every provider error/reconnect browser flow, and live sandbox exclusions on each OS.
This checkpoint is not completion of #1617 or of the beta-3 acceptance gate.

### Model acquisition checkpoint (October 4)

- Models & storage now searches the public Hugging Face registry and accepts exact
  repository/revision inputs. Acquisition is separate from runtime startup.
- Linux target workers resolve immutable commits, check capacity, download to an
  explicit host folder, verify file hashes, and retain durable receipts. Cancellation
  checks boot/process identity, preserves partial bytes, and serializes with retries.
  Corrupt or changed files cannot be reported as a reusable verified model.
- The CLIO ownership ledger retains original roots when defaults change. Retrying
  uses the recorded root/revision. Changing an SSH route cannot assign existing model
  ownership to a different host. Operation directories are also host-namespaced for
  shared HPC filesystems. Unsupported hosts expose a disabled action and explanation.
- **18 backend tests passed**, covering acquisition, host paths and route guards;
  **19 UI tests passed**, covering model controls and existing Infrastructure behavior.
  UI typecheck and focused lint passed. Model-native qualification is Linux-only.
- **Live homelab passed**: actual tiny-gpt2 download, registry hash verification,
  cancellation before retry, same-job retry and cache reuse without file changes.
  Evidence: `docs/qualification/beta3/homelab-model-lifecycle.json`.
- Browser checked real registry search, explicit host/path selection, download,
  completed receipt, Activity navigation and retained host selection. Narrow layout
  and tap-accessible info were reviewed at 390 × 844. Screenshots are retained beside
  the lifecycle receipt. The browser used the real command transport through the
  bounded OpenSSH qualification bridge, not a packaged Desktop SSH acceptance run.

Not yet complete: native vLLM, connection of downloaded models to guided runtime
setup, packaged installation, GPU inference and the remaining deployment gates.

### Ares allocation and SSH-hop checkpoint (October 4)

- Slurm job `24356` allocated `ares-comp-27` with two CPUs and 8 GiB for a bounded
  one-hour request. Actual use was under five minutes. The job was explicitly
  cancelled after checks; `scontrol` confirmed `CANCELLED` and `squeue` was empty.
  Slurm accounting is disabled; no accounting result is claimed.
- Production host inspection and model acquisition ran through `ProxyJump ares`
  with the existing Ares identity and a verified node host key. The node reported
  usable Docker and Podman, uv, no Apptainer and no GPU. No inference was attempted.
- An owned directory under `/mnt/common/jcernudagarcia/clio-beta3-qualification`
  used the writable shared filesystem with approximately 13 TB free. Real model
  cancellation, retry, immutable-revision hash verification and cache reuse passed.
- Disconnect/reconnect retained the same model operation and recovered its ready
  state without a duplicate download. Evidence: `ares-model-lifecycle.json`,
  `ares-host-and-model.json`, and `ares-reconnect.json`. The latter records the
  observed misleading disconnected-host explanation; a focused regression now
  checks that disconnection is reported before platform compatibility.
- Retained model bytes are deliberate evidence/cache. No service or allocation
  remains running from this test. This qualifies the model lifecycle through the
  hop, not native inference, provenance deployment or the packaged Desktop bridge.

### Native runtime supervisor checkpoint (October 4)

- Native vLLM and the pinned attention profile use an owned Linux supervisor,
  separate install/start operations, boot-and-start process identity, private
  credentials, durable receipts, and explicit removal versus data deletion.
  Probe activation precedes engine construction; the attention producer does not
  own persistence. Installation alone never marks attention as verified.
- The real pinned Flowcept HTTP service exercised this supervisor on homelab:
  install cancellation/retry, install without starting, HTTP serving, stop,
  restart, removal, and retained logs/cache/evidence passed. No provenance ingest
  or inference is claimed by this check. Receipt: `homelab-native-supervisor.json`.
- The downloaded-model link carries its exact host/path/revision into runtime
  setup. Browser review confirmed this with the homelab model. Container vLLM
  binds downloaded model inputs read-only. The full Services redesign remains open.
- Focused backend regression run: 80 passed. UI native lifecycle tests: 3 passed;
  existing service/model tests: 42 passed. UI typecheck, full UI lint, and backend
  file-size checks passed. CI-found composer and cleanup-ledger regressions were
  repaired and covered locally. Packaged/GPU qualification remains outstanding.

### Independent monitoring checkpoint (October 4)

- Managed Flowcept and HPE CMF now use versioned definitions with separate,
  ownership-labelled dependencies, private host credentials, explicit data paths,
  logs and durable deployment receipts. Flowcept has exactly one collector.
  CMF runs independently through its direct server API without Flowcept or vLLM.
- Both services passed real homelab install, start, fresh provenance write/readback,
  stop, restart, fresh re-verification and removal with retained evidence. CMF
  verification checked both input and output artifact edges, not just HTTP success.
  Evidence: `homelab-flowcept-managed.json`, `homelab-cmf-managed.json`.
- CMF used the existing immutable server image identified in its receipt. The
  default source-build path and rootless Podman are implemented but not yet live
  qualified. Service data stayed under `/data/clio-beta3-qualification/monitoring`.
  Existing host services were preserved; qualification containers were removed.
- Verification is bound to configuration and process generation and expires after
  restart. Image IDs and the Python lock digest remain inspectable after removal.
  Image pulls/builds check the engine image-store filesystem separately from the
  selected service data filesystem. Low root capacity cannot be hidden by `/data`.
- Focused service/provider regression: 67 passed; additional native/monitoring
  regression: 20 passed. Linux-target typecheck passed. Full CI is still open:
  missing attention fixture and minimap expectations were repaired; Python 3.12
  CI currently exits unsuccessfully after its test summary and needs investigation.

### Services interaction checkpoint (October 4)

- Services now starts with installed/retained resources, grouped into Inference,
  Monitoring and provenance, and Supporting services. Deploy new and Connect
  existing have distinct entry points. Management uses Status, Configuration,
  Logs and Storage tabs with host identity and accessible explanation icons.
- Selection, configuration drafts and tabs survive in-app navigation; backend
  operations restore after reload. Registered SSH hosts can be selected in a
  browser without creating a new Desktop transport. A URL retains the selected
  host through full reload. Unapplied edits do not affect status or stop requests.
- The real browser flow installed and started Flowcept on homelab, verified a
  fresh record, changed its private API port, observed verification invalidation,
  restarted, reconnected, reverified, stopped and removed the runtime. Mongo data
  and evidence remain; its owned containers are gone. Final API state is retained
  in `flowcept-ui-final-state.json`; mobile light/dark screenshots are alongside it.
  This uses the explicit OpenSSH qualification bridge, not a packaged Desktop
  transport. Current container-log refresh additionally has regression coverage.
- Focused service UI tests: 66 passed across six files after adapting lifecycle
  assertions to the management tabs. UI typecheck and lint passed. Backend:
  69 path/model/docs/cleanup/attention tests and 39 monitoring/native/attention
  tests passed in overlapping focused runs; Linux-target typecheck passed.
- Packaged macOS startup workflow 37190116552 passed on macOS 14, 15 and 26.
  Full Agent CI remains open: generated configuration docs and broad-exception
  regressions are repaired; Python 3.12 runtime cleanup now reports its underlying
  failure for diagnosis. It is not yet a passed integration gate.

### Marketplace ownership checkpoint (October 4)

- Installs stage and validate replacements before swapping; failed copy/swap
  preserves the working copy. Individual updates retain pins, refuse dirty pinned
  sources and refuse overwriting local edits. Invalid legacy default repairs keep
  the previous files in a receipt-linked backup.
- Marketplace/scope-qualified identities coexist even when author IDs match.
  Legacy ambiguous references return a conflict. File reads, activation, updates,
  uninstall and frontend selection retain ownership. Workspace tombstones persist;
  forgetting the default marketplace no longer recreates its source registration.
- Mutations invalidate A2UI/workflow discovery and emit a connected-CLIO revision
  event. UI queries and blueprint file caches are scoped to the endpoint; source
  errors and skipped choices are retained, and failed installation envelopes no
  longer produce success toasts. Individual update work runs off the ASGI loop.
- Backend marketplace regression: 175 passed. Core outcome/identity tests: 6
  passed; stream/active-blueprint tests: 22 passed. UI typecheck/lint passed.
  Managed-service tests: 26 passed after making names follow branding vocabulary.
- CI typing failures were corrected (Linux-target mypy: 961 files passed). The
  Python 3.12 cleanup cause was read-only parent directories in storage fixtures;
  the test-owned cleanup now restores directory permissions before removal.
  Fresh CI remains required. The product read-only fence is unchanged.
- Browser review verified the actual default-blueprint actions and marketplace
  inventory after restarting the isolated backend. The screenshot
  `marketplace-default-actions-dark.jpg` is a checkpoint, not complete UI acceptance.
- Draft/publish, complete source Reload and safe session turn-boundary application,
  transparent activation of not-yet-materialized entries, and final marketplace
  composition/qualification remain open. Do not treat this as closing #1627.

### Blueprint authoring and turn-boundary checkpoint (October 4)

- Save draft writes an isolated, workspace/source-qualified tree in Agent state.
  Invalid drafts do not modify source or runtime. Byte hashes protect concurrent
  editors; clean files pick up source edits while dirty files preserve conflicts.
- Publish validates the draft, detects upstream changes, and replaces selected
  authoring files individually. Its resumable receipt does not imply a filesystem
  transaction across the entire directory. Explicit Git options commit only the
  selected blueprint paths and optionally push to the configured upstream;
  unrelated staged work is retained. Runtime application remains a separate Reload.
- Reload gates whole turns and dependent child turns, queues new turns, releases
  waiting-session fleet holds, and recycles idle MCP fleets before installation.
  Request cancellation does not release the guard while a filesystem worker is
  still committing. Durable operation receipts and revision events record outcomes.
  Session activation metadata receives the applied checksum. Runtime startup/probe
  qualification for changed MCP implementations still needs live acceptance.
- The Blueprint tab now separates Save draft, Publish and Reload, retains unsaved
  buffers and selected files across navigation, exposes source/host/revision via
  compact labels and info icons, and refreshes its catalog metadata after events.
  The narrower layout reserves more room for editing; installation metadata is
  excluded from the editable file tree.
- Backend regression: **177 passed**; Linux-target mypy: **966 files passed**.
  UI editor/conflict/resource and canvas tests: **31 passed**; core session
  contracts: **13 passed**. Final UI typecheck and lint passed.
- Browser review on the isolated backend proved navigation retention and the
  three separate file states: draft-only, source-published/runtime-unchanged,
  and reloaded with a new session checksum. Desktop light/dark and narrow dark screenshots
  are in `docs/qualification/beta3/blueprint-authoring-*.jpg`.
- The preceding pushed Agent head `b9288963` passed all CI shards, coverage,
  filesystem/Flowcept integration, Pages, Docker, schema and macOS startup jobs.
  UI `de614588` passed CI/schema but its workspace browser gate found two open
  transcript regressions: an 11 px minimap hit-area gap and one 56 ms stream
  task. These remain failures; thresholds have not been relaxed.

### Blueprint selection and transcript checkpoint (October 4)

- The catalog includes available marketplace choices without installing them.
  Selecting a new conversation's blueprint materializes it on the connected CLIO
  before creating the session. Missing/ambiguous choices leave no empty session.
  Workspace choices stay scoped to their owner; path activation uses a namespaced
  snapshot. Selection reuses the installed revision until explicit Reload.
- Discovery now retains changed source snapshots and records their checksum
  difference. It preserves marketplace names, pins, errors and available entries;
  a read cannot overwrite those user choices or resurrect uninstalled snapshots.
- Real browser selection exposed and corrected dropped blueprint metadata in the
  frontend repository request. Keyboard selection then materialized the chosen
  blueprint, populated its exact identity/checksum and opened its installed files.
  Evidence: `blueprint-selection-materialized-light.jpg`. This is a focused flow,
  not complete marketplace browser acceptance.
- Backend selection/default/blueprint regression: 165 passed after extracting
  creation into its own route module; ownership/discovery regression: 11 passed.
  The earlier overlapping selection/capability run passed 209 tests. Linux-target
  typechecks, frontend lint/typecheck, 16 UI and 16 core contract tests passed.
- UI commit `59617e65` makes minimap magnified hit regions contiguous and preserves
  unchanged message references while streaming. Focused tests passed; rendered
  adjacent targets meet within 0.011 px. Its full browser CI remains pending.

### Attention identity and CI repair checkpoint (October 4)

- Text selections carry the exact message, part and source revision. Stale
  revisions, repeated passages and multiple matching model outputs refuse
  attribution instead of choosing an occurrence by position or recency.
- Prompt projection requires a unique compatible passage and transcript owner.
  Unmapped content has an accessible info explanation; the source breakdown no
  longer calls a small tool-result share "most" of total attention.
- Selecting an installed blueprint reads its snapshot under the revision gate
  without recycling the temporary composer's warm tool fleet. New installs
  retain the mutation boundary. CI fixtures now use real installed blueprints
  and mutate the installed copy when simulating a stale installation.
- Backend regression: 79 passed; Linux-target mypy: 22 files passed. UI selection,
  revision and hook tests: 19 passed. Browser replay resolved a revision-bound
  selection and its unmapped-content explanation; evidence is
  `attention-revision-unmapped-dark.jpg`. This uses a retained capture, not fresh
  inference. CI still needs to qualify the pushed repair.

### Bidirectional attention lookup checkpoint (October 4)

- Shared content references now identify the exact transcript field. Agent lookup
  accepts bounded, revision-checked selection sets in both directions: generated
  text to prompt sources, and source passages to later recorded generations.
  Overlapping selections count captured positions/steps once. Every result retains
  capture SHA-256 and profile identity; absent media coordinates stay unavailable.
- The inspector retains selections and profile settings across navigation in the
  current browser tab, scoped to endpoint, credential identity and session. It
  re-reads captures on demand, supports paged later-call lookup, and links back to
  exact transcript parts with a revision check. Unicode offsets and repeated words
  no longer fall back to highlighting the first matching substring.
- The schema commit is `fdba1f98b0c15130608e524406d762f2ca7db295` (0.6.0b2).
  Agent and SPOTTER pin that exact revision. No attention reader fetches via SSH.
- Backend attention/message regression: **122 passed**; mypy: **22 files passed**.
  Frontend selection/projection/navigation tests: **41 passed**, followed by the
  expanded inspector suite: **3 passed**. Core contracts: **254 passed**. Frontend
  typecheck and lint passed. The preceding Agent commit `b20da37e` passed full CI
  and the macOS packaged startup job; this new checkpoint needs its own CI.
- Browser evidence in `attention-*-lookup-*.png` and
  `attention-lookup-profile-narrow-dark.png` covers both directions, exact-field
  navigation, navigation retention, profile recomputation, tap-open explanations,
  light/dark and 390 px layout. These are recorded-capture checks, not new inference.
- Remaining attention work includes tool/media/A2UI selection producers,
  multi-selection transcript heat, profile-aware SPOTTER finding links, and both
  Delta demonstrations. This checkpoint does not close #1628.

### Provenance connection and activation checkpoint (October 4)

- Services can attach Flowcept or CMF independently, verify fresh write/readback,
  and select the connection for the next CLIO start. Flowcept's process-global SDK
  is verified in a child process; activation never leaves an old SDK silently
  writing to another service. CMF uses its existing direct-server provider.
- Settings-file hashes and all capture settings participate in connection
  identity. Edits invalidate readiness; failed rechecks revoke it. A completed
  probe cannot recreate a forgotten or edited connection. Legacy generic
  connection controls cannot bypass these checks.
- Configuration changes preserve the other backend and existing user settings;
  workspace overrides refuse activation instead of reporting an ineffective save.
  Attention configuration supports `provenance.attention.enabled` beside
  `files_dir`, retaining compatibility with the older boolean opt-in.
- Disconnect changes the next-start consumer configuration. Forget removes only
  an inactive connection record. Neither action stops an external service or
  deletes its data. Packaged Desktop and the remote installer include the
  Flowcept extra; the bundle install set resolves against the lock for Windows
  x64, macOS arm64, Linux x64 and Linux arm64.
- Live homelab checks: fresh Flowcept transport/query readback; fresh CMF
  input/output lineage; independent CMF activation after restart; both providers
  active after a second restart; disconnect/restart retained both external
  services (each still returned HTTP 200); failed recheck after service shutdown
  revoked readiness. Owned qualification services were stopped and uninstalled;
  data/evidence remained under `/data/clio-beta3-qualification/monitoring`.
  The user's existing services were not managed by this exercise.
- Browser evidence covers connection forms, pending/active/disconnected states,
  receipts, private-settings error, host-specific folder browsing, tap/keyboard
  explanations, dark/light and 390 px layout. See `provenance-*.png` and sanitized
  `provenance-connection-receipts.json`. This used a real isolated Windows CLIO
  API and an explicit OpenSSH qualification tunnel, not packaged Desktop transport
  or fresh inference. Reproduce the setup with the two
  `scripts/qualification/*provenance*.py` helpers; forward ports 19938/19939/19940/
  19980 to homelab 18038/16389/37027/18380 respectively.
- Browser boot exposed local blueprint copying of SPOTTER's `.venv`. Installation,
  drafts and checksums now exclude machine-local runtimes/caches, checksum reads
  are bounded, and capability projection runs off the API event loop.
- Automated regression: **166 passed**, including provenance ownership, paths,
  settings migration, blueprint portable files, route count, release policy and
  executable installer fixtures. Focused UI: **47 passed**, followed by the
  updated connection suite (**5 passed**). UI typecheck/full repository lint,
  Ruff and focused mypy (**5 files**) passed. Windows release-tag fixture now
  uses Git Bash with POSIX paths instead of accidentally invoking WSL.
- `e955c92b` passed the macOS packaged startup, schema, Docker and Pages jobs;
  its CI failed only the intentional route-count expectation. That expectation
  now includes lookup plus the six provenance connection routes (325 pairs).
  This checkpoint still needs CI against its newly pushed commit.

### Trusted connected-data handoff checkpoint (October 4)

- The main agent now has the read-only `connected_data_status` native tool and
  the built-in `connect-data` skill. Its result offers **Connect data**, opening
  the composer/Files picker after checking the owning CLIO and workspace.
  Authentication, source creation and permission changes remain trusted UI actions.
  Tool output contains only approved source identity, status and materialized paths;
  neither authentication objects nor source configuration enter the observation.
- Picker state and requests are scoped to both endpoint and credential identity.
  Switching identities closes the setup action and clears the visible selection;
  late attachment callbacks cannot populate a different connection's composer.
- A real isolated API exercised the status tool, rendered its recorded result,
  registered an owned local folder read-only, and materialized its 35-byte CSV.
  Reopening after navigation preserved the source selection. Browser evidence
  includes light/dark, 390 px layout, and the tap-open privacy explanation under
  `connected-data-*.png`. The seed explicitly labels the call as qualification,
  without inference. Reproduce with `serve_beta3_provenance.py --seed-storage`
  and a new private `--state-dir`.
- Storage/skill tests: **75 passed**. Tool-observer/presentation/provenance tests:
  **52 passed**. Focused UI: **11 passed**, core presentation: **7 passed**;
  UI typecheck/lint and focused mypy/Ruff
  passed. The provenance child now fails directly; its parent still withholds
  private stderr and publishes only validated receipt fields or a fixed error.
  This fixes the new broad-exception CI guard failure without changing its baseline.
- `8a4d4e85` passed macOS packaged startup and bundle lock resolution. Its CI
  reported the provenance exception guard above; the next commit needs fresh CI.
  Google/Globus live OAuth still awaits CLIO-owned application registration.

### Marketplace configuration checkpoint (October 4)

- Registered marketplaces retain their source identity when their folder, branch,
  tag or pin changes. Workspace registrations of the same repository are distinct.
  Save configuration uses optimistic concurrency and preserves installed files;
  Reload refuses a source edited or removed during preparation. Repository
  discovery honors the pin, and installation uses its inspected commit even if
  the upstream branch moves. Explicit pin changes require a configuration save.
- Working checkouts are persisted on the connected CLIO and used by the existing
  draft/publication path. Install, Reload and removal reject unresolved workspaces;
  workspace registrations cannot install into another workspace. Legacy default
  discovery retains its unique existing installation instead of duplicating it.
- Marketplace rows now disclose source details and blueprint inventory on demand.
  Configuration includes scope, host-specific folder browsing, branch/tag, pin,
  working checkout and tap/focus explanations. Settings queries and editor state
  include credential identity. Workspace actions use qualified blueprint identities;
  selected workspace/tab persist across navigation. The phone Settings menu exposes
  every section without placing the entire navigation above the active form.
- Browser review used the real isolated API: add/browse local source, Reload a
  valid root-agent blueprint, configure a working checkout, change source,
  confirm installed version 1.1 remained after Save, then explicitly Reload and
  confirm version 1.2. Light/dark, 390 px, keyboard/menu and info-icon states are
  retained under `marketplace-*.png` and `settings-navigation-narrow-dark.png`.
  This does not qualify a live MCP fleet replacement or inference.
- Backend marketplace/route regressions: **177 passed**. Focused UI: **12 passed**,
  responsive Settings navigation: **1 passed**, core repository contract: **7 passed**.
  Full UI lint/typecheck, Ruff, size/exception guards and mypy (three new modules)
  passed. The connected-source callback guard now updates its ref in a layout effect,
  preserving the account-switch protection while satisfying React's lint rule.
- `f4fbaec7` CI passed lint, packaging lock resolution, filesystem contract,
  Flowcept integration and coverage. Its test failures were two stale generated
  environment/config reference files; regeneration and all **26 reference tests**
  now pass. A fresh pushed-head CI run is still required.

### Marketplace staged Reload checkpoint (October 4)

- Reload stages and validates every selected pack before starting fresh MCP
  initialize/list probes. It checks each active workspace's effective configuration,
  preserves explicit descriptor enablement, and refuses missing tools or changes to
  credential-bearing endpoints. New descriptors are never enabled implicitly.
- A journaled installation revision restores all previous packs on swap/audit
  failure and recovers interrupted swaps before discovery. Source/configuration,
  installed-file and descriptor races fail closed. The existing whole-turn gate
  prevents mixed revisions in an active turn; prior fleets are recycled and rebuilt
  on demand, rather than kept alive across a failed preparation.
- Durable operation receipts survive navigation and restart. Duplicate Reload for
  the same target joins its active operation. An interrupted operation is explicitly
  unknown until reconciliation, never reported as successful cleanup or activation.
  Receipts expose checksums and sanitized MCP outcomes without source credentials.
- Blueprint cards and the editor show marketplace ownership. Editor buffers, file
  queries and operation receipts include authenticated connection identity. Unsaved
  edits survive navigation and concurrent saved-file changes require review. The
  editor distinguishes its authoring checkout from the registered Reload source.
- Browser review: an invalid MCP launcher retained installed version 1.2; a real
  HTTP MCP initialize/list allowed version 1.3 and its receipt persisted after
  navigation/reload. Light/dark 390 px receipts and the Blueprint editor are retained
  under `marketplace-reload-*.png`, `marketplace-failed-reload-retains-1.2.png` and
  `marketplace-blueprint-editor-ownership-light.png`. No inference or tool execution
  was used. A stdio probe on this Windows connected-storage fixture was refused
  because its active child-process fence could not enforce source exclusions; that
  path remains unqualified in this environment. No sandbox protection was bypassed.
- Automated: **239 backend regressions passed**, then **16** staging/configuration
  tests after the final static-before-runtime change and **4** descriptor/workspace
  tests. UI operation/settings/streaming tests: **26 passed**; editor account/conflict
  tests: **3 passed**; core repository: **8 passed**. Typecheck, UI lint, Ruff and
  file-size/silent-fallback guards passed. Fresh CI is required for this checkpoint.
- The preceding pushed Agent `82e1cc43` and UI `b0ca41af` passed CI. Agent packaged
  macOS startup passed on 14, 15 and 26. This is packaging evidence, not Delta GPU
  or end-to-end inference qualification.

### Transcript attention and evidence checkpoint (October 4)

- The message content picker returns authoritative part/call identities and exact
  revisions for text, reasoning, arguments, results, artifacts and media. Whole
  media references explicitly report missing coordinate maps. The picker is
  paginated and authenticated-connection scoped; it does not expose image bytes.
- Combined generated selections and reverse source lookup can apply one capture's
  heat to the transcript. Reverse heat uses all mapped retained output rows, not
  the truncated highest-token preview. Tool-result detail dialogs preserve raw
  Unicode coordinates and refuse changed arguments/results. Lifted tool reasoning
  retains its stored identity. No numerical reducer was duplicated in the UI.
- Evidence links carry selections, resolved profile/revision, model call and capture
  digest. Reopening a link re-reads that exact capture, opens the owning activity
  and tool details, and refuses a missing/different capture. Strict-mode remounts,
  session changes, clearing selections and pending-request cancellation are covered.
- Browser replay: whole-answer selection, source inspection, exact tool-result
  navigation and heat after reload, reverse lookup, custom decay, selection retention
  through Settings, 390-pixel light/dark views and tap-open explanations. Screenshots:
  `attention-tool-result-restored-light.png`, `attention-profile-narrow-light.png`,
  `attention-reverse-lookup-narrow-light.png`, `attention-explanation-narrow-dark.png`,
  `attention-reverse-heat-narrow-dark.png`. The replay supplies explicit test call IDs
  for its recorded pairs; production does not infer identities from order. This is
  recorded-capture qualification, not fresh inference or a completed OPAL demo.
- Checks: all **73 backend attention tests**, **4 route/architecture checks**, focused
  mypy on three modules, **64 initial UI regressions**, then **20 evidence/editor
  regressions** and **46 strict-mount/turn regressions** passed. UI typecheck, full
  lint and file-size guards passed. New pushed-head CI remains required.
- CI exposed two earlier marketplace regressions: the editor conflict test used its
  old unscoped cache key, and descriptor Reload constructed a bare MCP client.
  The test now addresses the authenticated key; Reload uses the shared client
  factory. **5 descriptor/factory tests** and a real HTTP initialize/list against
  the isolated qualification server passed. Neither change weakens a guard.
- Agent `ee64e79c` passed schema, Pages, Docker and packaged macOS startup on 14/15/26;
  Agent CI failed only the factory guard above. UI `f40e33a2` passed core CI/schema;
  workspace CI failed the editor conflict test above. Both require fresh CI.

### Reviewer findings checkpoint (October 4)

- The shared `AttentionEvidenceInspection` contract binds selections to the capture,
  model call and resolved aggregation profile. Agent, UI and SPOTTER pin Schemas
  `e1ac2efb`; Marketplace guidance/pin is `d6d7e8e`.
- Native `raise_alert_card` accepts the reviewer's exact response, capture digest,
  selected decode steps, profile/revision and uncertainty. The owning CLIO maps
  each token to one authoritative parent-transcript field and checks that replay
  reproduces the exact step set. Changed captures, ambiguous mappings, stop tokens
  and another session's response cannot produce an enabled inspection action.
- Findings remain visible when attribution is unavailable. Disabled/unknown actions
  have keyboard/tap explanations. The v3 projection preserves only the validated
  receipt. Inspect evidence restores its profile and heat and opens the existing
  reviewer in the canvas. Discuss opens that same panel without replacing the
  parent conversation. Pending lookups cannot navigate after credentials change.
- Browser replay covered exact 14-token inspection, decayed-max restoration,
  keyboard Discuss, light/dark and 390px reviewer presentation. Retained images:
  `spotter-finding-evidence-dark.png`, `spotter-finding-evidence-light.png`,
  `spotter-reviewer-narrow-light.png`. This remains a recorded fixture; it is not
  a live SPOTTER verdict or Delta inference acceptance.
- Checks: **124 backend attention/card/v3 projection tests**, then **9 binder
  tests** after the final capture recheck; **51 focused UI tests**, shared schema
  round-trip/attention **65 tests** plus **1 receipt bounds test**; focused mypy,
  Ruff, UI lint/typecheck and ownership/file-size guards passed.
- Agent `c6884b54` CI, schema, Pages and Docker passed. Its macOS app built, but
  DMG creation failed in `bundle_dmg.sh` (run `37208222403`); startup qualification
  did not run and this head is not packaging-qualified. New heads need fresh CI.

## Required work still outstanding

1. Finish connected-storage live OAuth qualification. The agent trusted-setup handoff
   is implemented and browser-reviewed. CLIO-owned
   Google/Globus client registrations and redirect URLs have been requested;
   never include secrets in this document or the transcript.
2. Qualify native vLLM with a real GPU; finish versioned service definitions,
   default-build and additional-driver qualification, fresh attention verification,
   and remaining service connection flows. Provenance connect/use/restart is now
   qualified independently and together on the isolated CLIO.
3. Finish marketplace cross-host live fleet qualification. Staging, MCP readiness,
   rollback, interrupted-operation receipts, source configuration/pins and Blueprint
   authoring integration are implemented. HTTP MCP Reload is browser-qualified;
   actual active remote-session fleet replacement and packaged stdio remain open.
4. Finish image-region/structured A2UI selection producers and reviewer follow-up
   conversation controls. SPOTTER finding links are now implemented and replayed.
   Shared profiles, local SPOTTER capture inspection, whole-block/media references,
   bidirectional lookup, multi-selection transcript heat, tool details and exact
   capture/profile navigation are implemented. Fresh inference and both demos
   remain unqualified. No fabricated image patches.
5. Complete browser review of every changed control, empty/error states,
   keyboard, light/dark, narrow layouts and long transcripts. Complete packaged
   install/update-channel checks and exact dependency qualification.
6. Complete integrated host/connection qualification. Homelab monitoring deployment,
   provenance, restart and removal passed on `/data`; Ares allocation, SSH-hop model
   lifecycle and reconnect passed. Packaged transport and GPU gates remain open.
7. Prepare ordered Delta setup, then together run fresh instrumented inference,
   token mapping, both OPAL/SPOTTER demos and evidence retention through shutdown.

Do not label unavailable, skipped, simulated or inspection-only checks as passed
live deployment or inference acceptance. Do not publish beta 3 from this checkpoint.
