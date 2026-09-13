# Campaign: Land the composer / workspace-resource pipeline on develop

**Status:** LANDING (fix slices A1–A4 complete on `codex/clio-composer-pipeline`, PR
[#1278](https://github.com/iowarp/clio-agent/pull/1278) → `develop`).
**Executor:** Codex (five repos). **Reviewer / re-lander:** Claude (this repo's review session).
**Keep this doc updated as work lands** (house rule; cf. `system-cleanup-2026-07.md`).

This document is the campaign's source of truth: what Codex delivered, the review verdicts at the
exact reviewed heads, what the re-land ported / trimmed / dropped, the deviations taken with their
reasons, and the follow-ups this landing does not close.

---

## (a) What Codex delivered

The wave spans **five repositories**:

| Repo | Branch / PR | Reviewed head | Landing state |
| --- | --- | --- | --- |
| clio-agent | `codex/clio-composer-pipeline` — [#1278](https://github.com/iowarp/clio-agent/pull/1278) | `7bd46c95` | Rebuilt on develop, re-landed through the same PR |
| gact-tui | `codex/clio-composer-pipeline` — [#382](https://github.com/iowarp/gact-tui/pull/382) | `241aef1f` | Open; 5 fix slices landed on-branch |
| clio-agent-marketplace | `codex/clio-composer-pipeline` — [#58](https://github.com/iowarp/clio-agent-marketplace/pull/58) | `2e0caaf` | **MERGED** to `main` @ `dd0acd9b` |
| clio-web-search | `codex/clio-composer-pipeline` — [#4](https://github.com/iowarp/clio-web-search/pull/4) | `71667d83` | Open; 4 contract fixes landed on-branch |
| clio-kit | — | — | Already contained; no wave change required |

**The "all UI" claim is corrected.** The wave was presented as a UI change. It is not: the majority
of the delivered surface is *backend* — a new immutable workspace-resource service (custody,
MIME detection, conversion, delivery provenance, agent tools, 16 HTTP routes), a durable
message-intent store with its own routes, a provider-catalog route, and v3 protocol projectors.
The clio-agent half alone is ~7.2k added lines across 48 source files. The gact-tui work is the
client for that backend, not the substance of the wave.

**Unmentioned PRs.** Codex also opened, against clio-agent and outside the composer wave's own
delivery claim, [#1290](https://github.com/iowarp/clio-agent/pull/1290)
(`codex/child-interaction-contracts`, unified child interaction routing) and
[#1291](https://github.com/iowarp/clio-agent/pull/1291) (`codex/child-work-projection`, child-work
provenance projection), both under #1273; and marketplace
[#59](https://github.com/iowarp/clio-agent-marketplace/pull/59) (`codex/factorio-flat`). None of
these are part of this landing; they are reviewed on their own.

---

## (b) Review verdicts at the exact reviewed heads

### clio-agent @ `7bd46c95` — NOT MERGEABLE AS-IS → **rebuilt on develop, not merged**

Five-dimension opus review (resources, message intents/SSE, providers/catalog, react/marketplace,
guards/config) per the `codex-review` method. The feature *designs* are sound — resumable custody
uploads, a revision-guarded queue store, live catalog provenance — and the store layer is genuinely
well built (revision-checked queue ops, idempotent acceptance, callback-under-lock promotion,
restart-aware tests that are substantive rather than theatrical). The guards were **not** tampered
with: all nine ratchet edits go down, env references regenerate clean, the route-count guardrail is
exact and honestly documented.

The branch nonetheless could not be merged:

**Structural**

- **163-commit-stale fork.** 15 files re-implement work develop already ships in newer, hardened
  form (the a2ui trio, `routes/a2ui`, `protocol/v3`, `session_cancellation`, capabilities). A
  take-branch merge would revert 12 landed hardening commits and 3 config keys, and break develop's
  own tests on import (`test_conf_operational_tunables.py`). **Resolution: develop wins wholesale on
  all overlap** — hence a rebuild rather than a merge.
- **Extends a deleted transport.** `providers/codex_app_server.py` was removed on develop
  (`ce09130e`, sdk-native provider sessions); the branch extends it and adds
  `codex_app_server_command.py` / `codex_app_server_pool.py` / `codex_cancel.py`.
- **Marketplace gitlink pinned to a feature-branch-only SHA** (`93bb9455`, reachable only from
  `codex/base-agent-direct-response`) — dangles on branch deletion.
- **File-size ratchet RED at head:** `routes/sessions.py` 1597>1530, `agent_blueprints.py` 1090>1060,
  `routes/blueprints.py` 898>896, `app.py` 2522>2521, plus a new `agents/reactv2.py` 843>800.
- **Built-in main agent deleted with no migration** (`catalog.py` `_builtin_agents() -> []`): every
  pre-existing session would raise `no_resolvable_agent` on its next turn and an offline install
  would 503 on every session create — a RULE 2 break.

**Ground-rule violations dropped rather than ported**

> Superseded on 2026-09-05 for ReAct completion only: the accepted contract now
> treats non-empty tool-free prose as a direct response and removes every
> post-loop forced-submit/repair call. See
> `docs/design/react-loop-completion-2026-09.md`. The bullets below record the
> earlier landing decision for historical accuracy.

- `lm/io_logging.py::_frame_codex_answer` — wraps markerless Codex output in `[[ ## answer ## ]]` at
  the LM transport layer: a fabricated field assignment (⚑#1/#2), justified by a ChatAdapter
  behavior claim the DSPy source contradicts. **Deleted.**
- `agents/reactv2.py::_direct_response_outputs` — converts "model emitted no tool call" into
  `termination_reason="direct_response"`, short-circuiting `_bounded_submit_repair` ("the model
  decides"); a mid-plan `next_thought` gets served as the final answer. **Deleted** (the activity
  suppression commit at `7bd46c95` is kept).
- `runtime/lm_stream.py` `_SECTION` de-anchoring — re-opens the documented inline-marker truncation
  bug and deletes `normalize_escaped_section_boundaries`, which develop's `lm/adapters.py` imports
  (a naive merge is an ImportError). **Develop's file wins.**
- `routes/capability_contract.py` — a hand-rolled envelope hardcoding `a2ui_versions: []`,
  `retention: 256`, `stale: False`, orphaning develop's live `capabilities_to_v3`. **Deleted;**
  develop's projection restored.

**Semantic defects, each re-landed with a failing-first test**

1. `loop_inbox.py` — an attachment-only steer (file, no caption) was claimed then dropped: never ran,
   never cancellable (409 `steer_already_claimed`), survived restart.
2. `composer_runtime.py` / `session_cancellation.py` — cancelling a session auto-promoted the next
   queued message, so Esc restarted the agent.
3. `routes/misc.py` cursor guard — heartbeats carried timeline ids excluded from `latest_event_id`,
   so any reconnect after one heartbeat declared a bogus `cursor_epoch_reset` and force-replayed the
   buffer; `/message-state`'s `next_cursor` (head+1) tripped the same guard by construction. One
   cursor convention, written down.
4. `protocol/v3/event.py` — none of the 8 composer events had a projector; `message.accepted` and
   `queued_message.reordered` shipped without `entity_id`; the session projector's `del payload`
   discarded the cancellation-honesty envelope the branch itself computed.
5. Resources lane — OOXML detection dead (the ZIP signature preempted the extension, so every
   `.docx` classified as `application/zip` and the Office branch was unreachable); HTML/SVG previews
   served inline same-origin without the CSP its derivative sibling has; chunked uploads bypassed the
   size cap (unbounded RAM); refresh clobbered a concurrent cancel; `native` delivery recorded for
   PDFs/audio/video that never reach the model; delete left in-flight processing to resurrect the
   tree; converter status-poll failures silently swallowed; a corrupt index bricked `build_app`.
6. `MessageBehavior.confirmation_policy` — accepted, echoed, persisted, enforced by nothing (see
   follow-ups).

**Also fixed in-campaign:** unbounded `message_intents.json` growth plus a full-file sync rewrite on
the event loop (bounds/retention through `conf`); restart recovery that restored transcript rows but
never re-enqueued steers; queued-on-idle messages that never started; per-session resource event
fan-out (0 sessions → no events, N sessions → N duplicates); ~35 hardcoded operational tunables
migrated to `conf.resolve` / `config.defaults.yaml`; dead vocabulary deleted
(`protocol/v3/capabilities.py`, `delivery:"steer"`, `state:"queued"`, a duplicate cancel registry).

The two PR-body liveness claims verify at the reviewed head (the stream stays subscribed; per-session
cursor resolution) modulo the heartbeat defect above.

### gact-tui @ `241aef1f`

Reviewed; **5 fix slices landed on-branch**. PR #382 remains open against `develop` and is gated
separately — this landing does not merge it.

### clio-agent-marketplace @ `2e0caaf` — **MERGED @ `dd0acd9b`**

Merged to `main` after a **test rewrite** and **one handcuff removal** (a behavioral constraint bolted
onto pack prose to suppress an observed failure — ⚑#3). `main` now ships **`base-agent` 0.2.0**: one
`react` root over CLIO's native workspace tools, no expert hierarchy and no hidden routing layer.

### clio-web-search @ `71667d83`

Reviewed; **4 contract fixes** landed on-branch. PR #4 remains open — the document-processor is an
optional dependency of this landing (`resources.document_processor_url` unset leaves resources served
as originals), so clio-agent does not block on it.

### clio-kit

**Already contained** — the wave required no clio-kit change; nothing to review or land.

---

## (c) The re-land inventory

The branch was **rebuilt on current develop** in three `port:` commits plus four fix-slice rounds
(A1–A4), not merged. 29 commits; 48 source files; ~7.2k insertions.

### Ported

| Lane | Port commit | Landed surface |
| --- | --- | --- |
| Workspace resources | `e1604137` | `resource_custody` / `resource_mime` / `resource_processing` / `resource_lifecycle` / `resource_delivery` / `resource_enrichment` / `resource_tools`, `routes/resources.py` |
| Message intents | `63403f6d` | `message_intents` / `message_submission` / `message_contract` / `steer_delivery`, `routes/message_intents.py`, the `loop_inbox` steer lane |
| Provider catalog | `5b7f4262` | `provider_catalog.py`, `routes/provider_catalog.py` |

Plus `composer_runtime.py` (store wiring + route registration), the `protocol/v3/composer.py`
projectors, the unscoped `GET /v1/questions` attention route, and the config-key declarations
(`e2d5cef9`, `9b591d7d`).

### Trimmed

- Every file where the stale fork overlapped develop's hardened version — **develop's copy kept
  wholesale**, the branch's re-implementation discarded (a2ui trio, `routes/a2ui`, `protocol/v3`
  bases, `session_cancellation`, capabilities projection, `lm/adapters` + `runtime/lm_stream`).
- The `0.2` SSE framer was re-landed *inside* its existing size ratchet (`b5097ccc`), and the
  resource lifecycle / bounded reads (`583a39ac`) and steer delivery (`a1b5540c`) were split into
  owner modules rather than appended to a god file.

### Dropped

- **The codex app-server / cancel / pool lane** — `providers/codex_app_server.py`,
  `codex_app_server_command.py`, `codex_app_server_pool.py`, `codex_cancel.py`, `codex_stateful.py`.
  **Why:** develop's sdk-native provider-session refactor (`ce09130e`) *deleted that transport*.
  Re-landing the lane would resurrect a transport the product no longer has and fork the Codex
  provider in two. The capabilities the lane genuinely added are recorded as follow-ups against the
  sdk-native path instead.
- `lm/io_logging.py::_frame_codex_answer`, `agents/reactv2.py::_direct_response_outputs`,
  `routes/capability_contract.py`, and the `catalog._builtin_agents() -> []` deletion (see (b)).

### Marketplace default switched to `base-agent`

`DEFAULT_AGENT_BLUEPRINT_ID` (the in-code default behind `agents.default_blueprint_id`) moves from
`earthscope-gnss-region` to **`base-agent`** — the wave's marketplace-default migration intent, now
that marketplace `main` ships the pack. The knob still SELECTS an installed marketplace artifact; the
pack keeps ownership of its prompts, tools and hierarchy, and nothing is defined in code. **Develop's
builtin `main` is retained beneath it as the RULE 2 offline fallback:** a box that cannot reach the
registry still creates sessions and runs the code-shipped `react` main, with the failed bootstrap
surfaced as a typed DISABLED discovery row carrying its diagnostic — pinned by
`test_missing_default_registry_still_serves_a_session_on_the_builtin_main`.

---

## (d) Deviations recorded

**1. Three new persistent stores — a noted RULE 4 deviation.** The lane adds
`<state_root>/message_intents.json`, `<state_root>/resource_deliveries.json` and the
`<state_root>/resources/` custody tree beside `sessions.json`. RULE 4 says "do not add a fifth
store"; this adds three. They are recorded here rather than hidden: the intent store holds
*un-sent user intent* (queued messages, pending steers, acceptance replay) and the resource stores
hold *immutable uploaded bytes and their delivery provenance* — neither is a re-materialization of
conversation history, which is what #737 exists to collapse. They are in-scope for the
[#737](https://github.com/iowarp/clio-agent/issues/737) event-sourcing consolidation and must be
folded into the normalized log + thin projections when it lands, not carried forward as separate
truths.

**2. `ResourceMessageBlock` pends clio-schemas 0.2.4.** clio-agent pins `clio-schemas==0.2.3`, which
has no block type for an immutable resource reference. The `resource_ref` part is served on the wire
today from clio-agent's own model; the **client ships a tolerant hand schema** until clio-schemas
0.2.4 adds `ResourceMessageBlock`. Contract versioning is patch-level, so this is a 0.2.x bump, not a
minor. Until then the two sides are bound by the hand schema, not by generated types — the wire shape
must not drift.

**3. The research report is a point-in-time record and is NOT edited.**
`docs/design/research/clio-composer-attachments/report-source.md` captures the attachment research as
it stood when written. **Its Scrollspy recommendation is superseded** — the landed composer does not
use a Scrollspy-driven navigation model. The report stays untouched as the historical record; this
document is where the supersession is recorded. Do not amend the report to match the outcome.

---

## (e) Follow-ups this landing does not close

1. **Dropped codex-provider capabilities — candidates for re-land on the sdk-native path.** The
   deleted app-server lane carried three capabilities the sdk-native transport does not have today:
   **native image inputs**, a **turn/interrupt handshake**, and **stderr diagnostics**. Each is a
   real gap, not a regression of this landing (the transport they rode was already deleted on
   develop). They should be re-landed against `providers/codex_litellm` + `codex_stream`, one at a
   time, with the sdk-native session model — never by resurrecting `codex_app_server.py`.
2. **`provider_catalog.refreshed` only fires on an explicit refresh.** There is no stale-catalog push
   signal: a client that never calls refresh never learns its catalog aged out. The refresh event is
   honest about what it is; what is missing is a server-side staleness signal on the same channel.
3. **EarthScope pack de-handcuffing against marketplace `main`.** The marketplace merge removed one
   prose handcuff; the `earthscope-gnss-region` pack still carries others (⚑#3 — behavioral
   constraints bolted onto expert `.md` prose to suppress observed failures). They must be replaced
   by root fixes in code/data-flow, and clio-agent's marketplace-prompt assertion tests
   (`tests/test_gact/test_agent_blueprints.py`, the `earthscope-*` parametrized cases) pin the
   current prose and will need to move with them.
4. **`MessageBehavior` has no server consumer.** `reasoning_effort`, `execution_mode` and
   `confirmation_policy` are captured verbatim on message metadata and replayed on queued promotion,
   but nothing on the server reads them: reasoning effort is carried by the model/thinking lane
   (`ModelRef`), and permission enforcement is driven by `/v1/policies`. The field set is an honest
   record of the user's selection and nothing more — binding `confirmation_policy` to the permission
   gate is a separate slice, not a rename.

---

## Verification at the landing head

Slice A4 ran the whole CI ladder locally on Windows / Python 3.12:

| Gate | Result |
| --- | --- |
| `ruff check src/ tests/ scripts/…` | All checks passed |
| `scripts/check_file_size.py` | OK — no file over its ratchet baseline |
| `scripts/check_no_class_in_function.py` | OK — `agents/builders.py` ratcheted DOWN 5 → 3 |
| `scripts/check_no_summaries.py` / `check_no_settle_vocabulary.py` / `check_tool_instrumentation.py` | OK (baseline 0 each) |
| `scripts/check_silent_fallbacks.py` | OK — total 1, at baseline |
| `scripts/gen_env_reference.py --check` | OK — env reference matches the source tree |
| `mypy src/` | Success, 499 files — **one branch-introduced error fixed here** (`protocol/v3/event.py` could not resolve `COMPOSER_PROJECTORS` across the composer↔event import cycle; the table is now explicitly annotated) |
| route-count guardrail (`test_decomposition_guardrails.py`) | 4 passed |
| full suite — `pytest tests/ -m "not integration"` | **6516 passed, 84 skipped, 76 deselected, 0 failed** in 39:06, `PYTEST_EXIT=0` |
| CLI baseline smoke (`ui/cli.py --help` / `--version`) | exit 0, reports 0.9.1.1 |

### One intermittent develop test, diagnosed not waved

`tests/test_tools/test_mcp_fleet_lifecycle.py::test_boot_listing_is_sequential_one_chain_at_a_time`
failed in ONE of three whole-suite runs (`boot listing held 2 stub chains alive simultaneously`).
It is not attributable to this landing and was not patched:

- the MCP listing path it exercises (`tools/gateway.py`, `tools/execution.py`, `tools/mcp_config.py`)
  and the test itself are **untouched by this branch** — they are develop's;
- it passed the other two whole-suite runs of this same tree, and passes 4/4 solo, 3/3 as its module,
  and inside the whole `tests/test_tools` package (781 passed);
- the assertion's instrument samples the OS process table every 25 ms for any process whose cmdline
  names a stub script, so a namespace's stub that has exited but is not yet reaped still counts as
  "alive". Under whole-suite load on Windows the exit lag at a namespace boundary exceeds the
  sampling interval, and a genuinely sequential listing reads as two concurrent chains.

The invariant is right; the instrument has a teardown-lag blind spot that only opens under load on
Windows. Tightening it belongs to the develop test's owner, on a platform where the fix can be
verified — not to a blind Windows-only patch in this landing.

CI on [#1278](https://github.com/iowarp/clio-agent/pull/1278) is the merge gate.
