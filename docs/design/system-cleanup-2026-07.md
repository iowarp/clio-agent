# System Cleanup Program — 2026-07 Audit

**Status:** Phases 0–1 complete (2026-07-05); Phases 2–4 open · **Origin:** 2026-07-01 nine-reviewer
architecture & code-quality audit of
`clio-agent` + `gact-tui` · **Tracking:** [clio-agent umbrella #775](https://github.com/iowarp/clio-agent/issues/775),
[gact-tui umbrella #237](https://github.com/iowarp/gact-tui/issues/237) · all issues labeled `audit-2026-07`.

This is the master plan. The gact-tui pointer doc is `docs/system-cleanup-2026-07.md` in that repo.

---

## 0. Execution status (living)

Kept current as work lands; the running per-PR log is the umbrella #775.

- **Phase 0 — surgical fixes: ✅ COMPLETE.** All P0s #755–#766 closed. Most were already fixed in
  earlier PRs and only needed closing (confirm-still-present caught this); #761 needed real work.
- **Phase 1 — deletion release: ✅ COMPLETE (2026-07-05).** #768 landed in full — the banned
  deterministic guards and dead layers are grep-verified gone from `develop`. It bundled #766 and
  the rest of #765 (both closed in the Phase 0 sweep). #768's three "explicit decisions" became
  their own issues, all now closed: **#799** (one front door — the CLI is a gact client, `ui/api.py`
  removed, entry points unified), **#800** (one doctor — `/v1/health` on the single
  `collect_runtime_status` probe engine), **#801** (optimizer = honest not-implemented stub).
  Closing PRs: #795–#798, #828, and #829 (#799 + #800 shipped as one unit). The writer-less ARC
  structs (`DatasetProfile`/`ProceduralMemory`) were descoped by the owner (kept as useful).
- **Phase 2 — in progress.** clio #767: PRs 1–3 **and PR-5** landed (PR #833, 2026-07-05) — the
  `TurnTranscript` ledger owns part identity, finalize is a pure reader, and `message.part.*` is
  the **sole** transcript wire vocabulary (the zero-consumer `turn.*` twins are gone). **PR-4
  (single dedup owner) is parked** under **#832**: the owner ruled the server must emit a clean
  stream (the UI never owns dedup/validity), which rejects the tag+client-collapse design; final
  resolution at the post-Phase-4 website review. **The decomposition landed (PR #836,
  2026-07-07):** `_run_turn_in_background` is decomposed — `turn.py` 3,451 → 806 lines, explicit
  `TurnState` + seam modules (`turn_stream`/`turn_delegation`/`turn_forward`/`turn_watchdog`/
  `turn_usage`/`turn_nanoagents`/`turn_finalize`), pure refactor (goldens byte-identical), and
  the #714 app↔turn lazy-import cycle is dissolved (zero top-level app imports in turn.py).
  **The EarthScope vocabulary extraction landed (PR #838, 2026-07-07; closes #646/#648):**
  core is a generic typed-state engine — the domain vocabulary is a pack-declared
  `WorkflowStateSchema` (AGENT.md frontmatter; marketplace #36, pin `ed895eb`); correctness pinned
  by re-running the existing golden tests through the schema path byte-identically; de-domaining
  proven by a synthetic widget-factory schema + a permanent grep-guard; degradations loud
  (recorded `workflow_state_schema_absent` / blueprint disabled at load). **#767 is 6/7 —
  the clio Phase 2 keystone is complete except the #732 item parked under #832.** **gact-tui #232 is functionally complete**
  (6/9 checklist items; the rest owner-gated): SPEC reconciled + conformance asserts (tui #247),
  wire layers synced (tui #248), the retired `turn.*` vocabulary purged client-side + a
  machine-checked SPEC §7.7 wire vocabulary where declared⇄spec drift fails CI in TS and Go
  (tui #251), one WHATWG SSE grammar + `Last-Event-ID` resume on all three transports (tui #252),
  floating issue ledgers converted to tracked issues (tui #253). Owner-gated remainders: the
  dedup-owner step (#832), wire-type ownership/codegen (decision issue tui #254).
- **Phase 3 — ✅ COMPLETE (2026-07-08).** #769 config convergence (7 slices); **#771 ARC truth &
  durability** landed as PR #859 — chunked `_events` log (kills the O(N²) write amplification),
  `_lock` scoped off store RPCs, one conversation-history channel, writer-less-struct deletion,
  one shutdown story, doc truth. Two real lock-scope races were caught **before merge** (a
  `SegmentStore.release()` "dict changed size" crash by CI flake-hunt's `--count`; a silent
  conversation lost-update by the independent adversarial review, reproduced deterministically)
  and fixed at root with failing-first tests. #772/#773 silent-fallback + test-depth ratchets
  (#844) + weekly mutmut mutation CI (#845). gact-tui #233/#236 landed as self-landing PRs.
- **Phase 4 — ✅ COMPLETE (code) (2026-07-08); owner-gated remainders only.**
  - **clio:** #841 release/distribution hygiene (#846–#849: brand-config mechanism restored, download
    page prefers the bundled installer, packaging CI asserts, CLIO icon family); #830 entry-point
    convergence (#850–#852: containers + launchers on `clio-agent serve` :8100, `clio-agent-gact`
    a deprecation alias); #774 repo/meta-doc hygiene (#853–#858: anti-accretion guards enforcing,
    pyproject scrub, placeholder purge, AGENTS.md truth, submodule contract, CHANGELOG).
  - **gact-tui:** #234 CLI structure (cli→internal/cli, one command context, single command table,
    tapes relocated, markers de-obfuscated, ui-render split **stage 1** — honestly 3 leaf files, the
    rest deferred) + #235 repo hygiene (136 MB screenshot purge, root sweep, Makefile truth,
    CHANGELOG revival, media/LFS policy, history-rewrite runbook). PRs #264–#284; a Phase-4 docker
    baseline regression (H3's `go.work` loop-test member vs `.dockerignore`) was caught on develop
    CI and fixed in #285.
  - **Docs curation + READMEs (both repos, 2026-07-08).** A content-aware pass (each doc read and
    classified, not mechanically folded): **clio** culled 19 benchmark/stress run-logs + stale
    `docs/tui/` duplicates and archived 7 superseded design docs, added `docs/README.md` as the
    index, and repointed the root README at it (#861–#863); **gact-tui** deleted the unwired
    `visual_loop/` harness + untracked `apps/web/screenshots/`, relocated `loop-test/` →
    `examples/`, culled 14 agentic-noise docs, archived the founding `apps/` design series + folded
    `notes/`→`docs/reference/` and `ref/`→`docs/ref/` under a new `docs/README.md`, and rewrote the
    root README as a concise human-centric guide (fixed the wrong `JaimeCernuda` clone URL, unified
    the v0.2 contract references) (#286–#290). Slice-4's archive PR was rebased to avoid resurrecting
    the culled files; the README rewrite's claims were independently verified against the code
    (two overclaims — a non-existent "Gruvbox" theme and a wrong adapter LOC/file-structure line —
    were corrected before merge). Follow-up (content, not a move): a truthfulness pass on clio's
    `CLIO_AGENT_ARCHITECTURE.md` (stale LangChain/CrewAI-supported + tiered-storage claims).
  - **Owner-gated remainders (not executed):** the git history rewrite (owner-only runbook, prepared
    not run); GitHub **LFS quota** confirmation before large baselines land; the U1 size-ratchet
    **flip-to-enforcing** (drop `|| true`, dated 2026-09-01); **ui-split stage 2+** (follow-up tui
    #282); the **gact-tui submodule pin** bump in clio-agent (release-time action per the #857
    contract — develop pin `3c90468` now trails gact-tui develop `9a15565`, which carries the
    #285–#290 hygiene/docs work); P14 owner-local `tmp/` cleanups; and the parked **PR-4 dedup-owner**
    step (#832).

---

## 1. Ground rules

1. **Grounding release first.** A release is cut from the current state before any of this executes,
   so every change diffs against a known-good shipped baseline.
2. **Branches map to issues.** Every change lands on a branch named for its issue; no work stranded
   in local-only commits. One behavior change per PR.
3. **Spec follows reality.** The GACT contract (`gact-tui/contract/SPEC.md`) drifted because it was
   never updated while the implementation evolved. Convergence direction: **re-reconcile the spec to
   today's implementation** (reality leads, spec documents, conformance enforces) — do NOT regress
   code to the stale documented contract. Exception: where the implementation is self-contradictory
   (the `message.created` nesting fork, inconsistent error tags, capability flags that lie), pick the
   coherent current behavior and codify it. See gact-tui [#232](https://github.com/iowarp/gact-tui/issues/232).
4. **Every degraded path is loud.** Silent `try/except → fallback` is a defect class, not a style
   choice. House style to copy: the typed `stream_fallback` reason catalog (`gact/streaming.py`).
   See [#772](https://github.com/iowarp/clio-agent/issues/772).
5. **Every bugfix lands with a failing-first regression test.** Depth over breadth: wrong inputs,
   error paths, concurrency. See [#773](https://github.com/iowarp/clio-agent/issues/773).
6. **Delete, don't gate.** Dead layers and banned heuristics are removed, not env-flagged off.
7. **No accretion.** Patch-driven development is how `turn.py`/`agent.py`/`app.py` grew to 3–4k
   lines; cleanup work must not feed them. A fix that adds more than a trivial amount of code goes
   in an owner module (new file if needed), not appended to a god file. Now that the #767
   split has landed, CI carries **enforcing** guards (`scripts/check_file_size.py`,
   `scripts/check_no_class_in_function.py`) that hold each god-file at a per-file ratchet
   baseline (may only ratchet down) and fail any new offender, so files can't silently regrow (#774).

## 2. Exit criteria (measurable)

- [ ] Live transcript == reloaded transcript, by construction (single-writer ledger; no client-side dedup) — #767, gact-tui #232
- [x] Zero prose-keyword routing/completion/veto heuristics in core (⚑ principle 1 clean) — #768 ✅
- [ ] `grep -c "except Exception" src/` classified; zero unlogged degraded paths; ruff BLE001/S110/E722 enforced — #772
- [ ] 100% of read env vars documented; `CLIO_LM_*` through `conf.resolve`; `.env.example` exists — #769
- [ ] `git status` clean after a normal run (no model_limits churn, no root artifacts) — #763, #774
- [ ] gact-tui pack < 100 MB; no run artifacts tracked — gact-tui #235
- [ ] Conformance suite fails on every drift class found by this audit — gact-tui #232
- [ ] TUI renders streaming + thinking traces at parity with web — gact-tui #233
- [ ] Both CLAUDE.md files describe the real system — #774, gact-tui #235

## 3. Systemic diagnoses

Full evidence lives in the issue bodies; one line each here.

1. **Nothing was ever deleted.** Two turn engines (`agent.py` loop + `gact/turn.py`); `agent.py`'s
   Tier-2 arm is scaffolding around a hard-failing stub; three console entrypoints; dead registry/
   optimizer/ARC-cache halves; a doctor probing a deployment shape that no longer exists.
2. **The same job implemented N times.** 9 final-answer paths · 4 routing-decision types ·
   7 persistence locations · 3 DSPy field-marker parsers · 2 event vocabularies double-published
   per token (one with zero consumers) · 2 agent-resolution pipelines that already disagree ·
   2 history injections per turn · 2 LSM instances on the same files.
3. **One protocol, four hand-copied type systems.** Python/Go/TS + per-adapter vocabularies, no
   codegen; every checked enum drifted in the hand-copied layers; server AND clients both own text
   dedup — live≠reload by construction.
4. **Banned deterministic heuristics still run.** Keyword guards veto/replace model answers in
   `agent.py`; EarthScope demo vocabulary baked into "generic" core (merge/delegation/evidence) and
   into the TUI's placeholder strings; personal SSH registry URL + demo agent as product defaults.
5. **Config & repo entropy.** 89 env vars (37 via `conf`, 50 undocumented, 13 phantom-documented);
   runtime rebind mutates `os.environ` and blocks the migration; meta-docs (CLAUDE.md/PLAN.md)
   actively miscalibrate agent sessions; 669 MB media pack in gact-tui; junk at both roots.
6. **TUI streaming parity gap** (owner-reported, confirmed): the web/gact streaming + thinking-trace
   rework was never ported to the TUI — dual projection, unconsumed normalized channel, half-matched
   semantic allow-list.
7. **Silent failover semantics** (owner-reported, confirmed): unlogged `except → fallback` paths
   hide failures throughout both repos.
8. **Test depth** (owner-reported, confirmed): 2,076 tests collect clean but are happy-path biased —
   every audit P0 lives in an error/concurrency/wrong-input path no test exercises.

## 4. Issue map

### clio-agent — P0 defects

| # | Title |
|---|---|
| [#755](https://github.com/iowarp/clio-agent/issues/755) | Slash-command dispatch runs the LM on the event loop (whole-server freeze) |
| [#756](https://github.com/iowarp/clio-agent/issues/756) | Finalize (~840 lines) outside any try — wedged sessions |
| [#757](https://github.com/iowarp/clio-agent/issues/757) | `live_streamed_field_text` never cleared — leak + cross-turn suppression |
| [#758](https://github.com/iowarp/clio-agent/issues/758) | EventBus.publish not thread-safe |
| [#759](https://github.com/iowarp/clio-agent/issues/759) | Sticky permission decisions never persisted |
| [#760](https://github.com/iowarp/clio-agent/issues/760) | Capabilities advertise routes that don't exist |
| [#761](https://github.com/iowarp/clio-agent/issues/761) | Heartbeats pollute SSE replay; watchdog inert with ≥2 sessions |
| [#762](https://github.com/iowarp/clio-agent/issues/762) | Default deployment can erase the only copy of the event log |
| [#763](https://github.com/iowarp/clio-agent/issues/763) | Repo-shipped model_limits.json mutated at runtime |
| [#764](https://github.com/iowarp/clio-agent/issues/764) | Default registry is a personal SSH URL |
| [#765](https://github.com/iowarp/clio-agent/issues/765) | Windows correctness bundle (local_paths regex, /tmp, CTE stop) |
| [#766](https://github.com/iowarp/clio-agent/issues/766) | Scheduler bypasses staging; swallows errors |

### clio-agent — epics

| # | Workstream |
|---|---|
| [#767](https://github.com/iowarp/clio-agent/issues/767) | Single-writer TurnTranscript (ends the streaming regression class; abs. #693/#731/#732/#733/#736/#737) |
| [#768](https://github.com/iowarp/clio-agent/issues/768) | Deletion release (~15–20% of src, zero behavior change) |
| [#769](https://github.com/iowarp/clio-agent/issues/769) | Config convergence |
| [#770](https://github.com/iowarp/clio-agent/issues/770) | gact server concurrency & lifecycle hardening |
| [#771](https://github.com/iowarp/clio-agent/issues/771) | ARC truth & durability |
| [#772](https://github.com/iowarp/clio-agent/issues/772) | Silent-fallback sweep |
| [#773](https://github.com/iowarp/clio-agent/issues/773) | Test depth |
| [#774](https://github.com/iowarp/clio-agent/issues/774) | Repo & meta-doc hygiene |
| [#830](https://github.com/iowarp/clio-agent/issues/830) | Converge server entry points: one `clio-agent` exe + one container port/healthcheck (Phase 4; #829 follow-up) |
| [#841](https://github.com/iowarp/clio-agent/issues/841) | Release/distribution hygiene: download page serves the bundled installer, current CLIO branding on all installer/app surfaces, CI-asserted (Phase 4; user-reported) |

### gact-tui — P0 defects

| # | Title |
|---|---|
| [#224](https://github.com/iowarp/gact-tui/issues/224) | TUI compact calls /summarize → always 404 (pairs with clio #760) |
| [#225](https://github.com/iowarp/gact-tui/issues/225) | Web ignores every session.updated |
| [#226](https://github.com/iowarp/gact-tui/issues/226) | Stale session-snapshot race shows the wrong transcript |
| [#227](https://github.com/iowarp/gact-tui/issues/227) | TUI SSE read-error is a dead end (no reconnect) |
| [#228](https://github.com/iowarp/gact-tui/issues/228) | Desktop supervisor leaks the old process tree on restart |
| [#229](https://github.com/iowarp/gact-tui/issues/229) | message.created nesting fork (flat vs nested) |
| [#230](https://github.com/iowarp/gact-tui/issues/230) | CLI subcommands ignore config.json backend_url |
| [#231](https://github.com/iowarp/gact-tui/issues/231) | TUI execution ledger unbounded; survives /clear |

### gact-tui — epics

| # | Workstream |
|---|---|
| [#232](https://github.com/iowarp/gact-tui/issues/232) | Protocol convergence (spec follows reality; codegen; conformance asserts drift) |
| [#233](https://github.com/iowarp/gact-tui/issues/233) | TUI streaming & thinking-trace parity with web |
| [#234](https://github.com/iowarp/gact-tui/issues/234) | tui structure (split 623-file ui package; CLI command context; single-source docs) |
| [#235](https://github.com/iowarp/gact-tui/issues/235) | Repo hygiene & media policy (669 MB → <100 MB) |
| [#236](https://github.com/iowarp/gact-tui/issues/236) | Apps correctness cluster (web/desktop) |

## 5. Phasing

**Phase 0 — surgical fixes (days). ✅ DONE.** The P0 one-liners and small patches, each individually
testable: #755 #756 #757 #758 #759 #760+#224 #761 #763 #764 #765 (partial) · gact-tui #225 #226
#227 #231 #230 (via the helper if #234 starts, else minimal). Also: strip pytest `addopts`,
delete root junk (both in #774/#235 but safe now).

**Phase 1 — deletion release (about a week). ✅ DONE (2026-07-05; PRs #795–#798, #828, #829).**
#768 in full, plus #766 and the rest of #765 (the latter two closed in the Phase 0 sweep). The
three explicit decisions shipped as #799 (one front door) / #800 (one doctor) / #801 (optimizer
stub). Shrinks the surface before structural work so Phase 2 refactors less code.

**Phase 2 — the two keystones (the real work).**
- clio #767: single-writer TurnTranscript + turn.py decomposition + EarthScope-vocabulary
  extraction. Ends the live-vs-finalize reconciliation permanently.
- gact-tui #232: spec re-reconciliation → client sync → one dedup owner (server) → conformance
  assertions → codegen exploration. #229 lands here. clio #767 and tui #232 are co-dependent
  (the text/thought duplication and suppression heuristics die on the server side so the client
  filters can die too); sequence server-first.

**Phase 3 — convergence.** #769 (config), #770 (server hardening leftovers), #771 (ARC),
#772 + #773 ratchets running continuously from Phase 0 onward. gact-tui #233 (TUI parity —
after #232 settles the channel) and #236.

**Phase 4 — repo restructure.** #774 and #235 (docs taxonomy, CLAUDE.md rewrites, media
filter-repo + LFS, test-tree split, tui package split #234), plus #841 (release/distribution
hygiene — bundled-vs-lite download correctness + current branding + a packaging CI assertion;
user-reported) and #830 (converge the server entry
point → one `clio-agent` executable + one container port/healthcheck) — mechanical, large,
low-risk last.

## 6. Audit provenance

Nine parallel reviewers, each deep-reading one subsystem with file:line evidence, 2026-07-01:
core orchestration · gact turn/streaming pipeline · gact platform surface · config/providers/tools ·
ARC/optimizer · clio repo hygiene · gact-tui structure · GACT protocol drift · gact-tui apps.
~75 confirmed/suspected defects total; the P0 set above is the confirmed high-impact subset, and
each epic body carries the remainder for its area with citations. Genuinely good foundations to
protect during cleanup: the contract+conformance culture, `GactDeps` DI seam, `gact/context.py`,
the provider handshake subsystem, `providers/registry.py`, `conf.py`, `arc/segments.py`, atomic
persistence everywhere, SSE replay fundamentals, and the test volume.
