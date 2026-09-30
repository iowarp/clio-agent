# Handoff: the attention view (SPOTTER-AI showcase), to completion, testing and release

Written 2026-09-27. The owner wants a Monday demo: select text in an agent answer, click **Understand attention**, and see which parts of the conversation the model attended to while generating it. Read this whole document before touching code.

## 1. What the feature is (owner semantics, locked)

- The user selects text in an agent answer and clicks **Understand attention**. An **Attention mode** banner appears at the top of the transcript, with an X to dismiss it and return to normal mode.
- The view answers "what in the conversation led to this piece of the answer?" It shows:
  - **heat** over the transcript text: user messages, earlier answers, tool-call reasoning, tool inputs and tool results;
  - a **scrollbar strip** marking where the high-attention regions are in a long conversation;
  - a **sources breakdown**: how much came from tool results, agent reasoning, the user's messages, the system prompt, tool definitions, and so on. When tool results dominate, show a calm warning chip, because that is a possible poisoning signal.
- **Keys are the prompt only, on purpose.** The connector scores each generated token against the prompt, which is the whole conversation sent on that call (all earlier turns, tool calls and results, even when prefix-cached). It deliberately excludes the answer's own earlier tokens: they dominate attention for grammatical reasons ("the" pointing at its noun) and hide where the answer came from.
  - Present this as a design choice, never as a limitation.
  - Whether the same call's `<think>` text should be tracked is on the owner's group-meeting agenda. Do not change it.
- **The residual is not "unknown".** Each row's mean attention is a softmax over the prompt tokens, so it sums to 1. `topk_residual` is the attention that went to prompt positions below the connector's top-10% cut, spread thin. Label it "Rest of the conversation (spread thin)". Never use "not attributed" or "unknown".
- **CLIO does not acquire attention data.** CLIO reads the connector's SafeTensors file only where it can open it:
  - in place, when CLIO runs on the GPU node (the demo setup: CLIO on the node, the UI connects from the desktop);
  - from a local copy, `provenance.attention.files_dir`.

  How remote vLLM plus Flowcept should serve attention data (the owner leans toward Flowcept serving it) is open with the connector and Flowcept teams. Do NOT add SSH, sync or fetch paths. One was built and removed on purpose (commit `62ec8015`).
- Future design (not now): selection actions on any transcript content (#1525), document or artifact text, image regions, and A2UI or MCP-app nodes. The selection registry (`web/src/lib/selection-actions.ts`) is the extension point.

## 2. The data (connector output)

- The connector is vllm-attn-connector, branch `fix/on-safetensors-e857fd0` @ `95ab2ac` on github.com/spotter-ai-genesis/vllm-attn-connector. It runs with the Flowcept fork `e638b4e2`.
- **Per vLLM request:** one SafeTensors file `<out_dir>/<workflow_id>/<request_id>_g<group>.safetensors`, plus one Flowcept task (`activity_id: decode_attention`) that points at it through `attention_stats.uri`, with `bytes`, `sha256` and health counters.
- **File tensors:**
  - `prompt_token_ids [T]`
  - `attn_sum [T]` and `attn_peak [T]`: whole response, every position
  - `segments [n,3]`, rows `(lo, hi, keep)`
  - per decode step: `topk_pos`, `topk_head`, `val_all_max`, `val_all_avg` (each `[G,k]`) and `topk_residual [G,1]`
  - Selection keeps the top 10% of positions per 128-token segment, ranked by the max over 64 layers × 32 heads. Both the max and the mean are stored. Position 0 (the sink) is never selected.
- **Join key:** the vLLM `request_id` is CLIO's `lm.call` `response_id` plus `-<hex>`, so match by prefix.
- **Verified facts (job 3237185 bundle, 29 calls):**
  - Re-rendering CLIO's recorded messages with granite's own chat template and tokenizer equals `prompt_token_ids` exactly on 29/29 calls.
  - `G` = output tokens + 1 (the stop token) on 29/29 calls, and row `t` is the step that PRODUCED output token `t`.
  - No generated token ids are stored, so the server refuses (with a typed reason) whenever the count does not match or a health counter is nonzero.
  - `val_all_max` saturates at 1.0 on many positions, so rank by the mean. Chat-template markers act as extra sinks: position 2 hits the max on nearly every step. They are grouped under "template" / "Formatting" and never highlighted.
- **The bundle** (read `SCHEMA.md` there first) is at `D:/crv/p/attn-dataset/`: `README.md`, `SCHEMA.md`, `example_query.py`, `full/` (29 files, 2.4 GB, mongoexport plus mongodump) and `lite/`.
- Earlier design notes: `D:/crv/p/attention-data-model.md`, `attention-reference-impl.md` and `attention-storage-contract.md`. The storage contract is superseded: the connector shipped SafeTensors, not per-step Mongo tasks.

## 3. State of the code

### clio-agent: PR #1490 (draft), branch `feat/attention-view`

The local worktree is `D:/Libraries/Documents/projects/clio_develop_workspace/temp/attn` (local branch `feat/attention-view-st`, pushed to `origin/feat/attention-view`). It is merged with develop as of 2026-09-27 (after the v0.9.4.20 back-merge). CI was green on the last full run.

Owner package `src/clio_agent/gact/attention/`:

| module | role |
|---|---|
| `declare.py`, `ranges.py`, `chat_render.py`, `tokenizer_source.py` | write path: on `hosted_vllm/` calls with `provenance.attention` on and Flowcept configured, render the prompt with the model's template and tokenizer, then declare one token range per transcript section as `kv_transfer_params.ranges`; the labelled ranges are recorded on the `lm.call` |
| `contract.py` | parses the `decode_attention` descriptor task |
| `files.py`, `byte_source.py` | locate the file (in place, else under `files_dir`) and verify size + sha256 once per file |
| `safetensors_file.py` | reads whole `[T]` tensors and `[G,k]` row ranges by byte offset (no dependency on the `safetensors` package) |
| `store.py` | the descriptor through Flowcept `task_query`, plus the file reads |
| `lm_calls.py` | `lm.call` records from the native JSONL journal first, then Flowcept |
| `selection.py` | selected span → producing call → output tokens → decode rows |
| `rendered.py` | rendered selection text → markdown source span (the UI sends only rendered text) |
| `aggregate.py`, `transcript_map.py`, `textmap.py` | per-token and per-section mass; transcript parts located in the rendered prompt by exact match (verbatim or JSON-escaped); heat runs are char offsets into each part's own text |
| `service.py`, `routes.py`, `reasons.py` | the payload (`clio.attention.v1`) and the routes; every failure is a typed `{available:false, reason, message}` |

- **Routes:**
  - `GET /v1/sessions/{sid}/attention/availability` returns `{enabled, reason?, message?, messages: {id: bool}}`.
    - It answers `enabled: false` when `provenance.attention` is off or Flowcept isn't configured.
    - Otherwise an answer is marked available when its turn made a vLLM call with a `decode_attention` record.
    - One Flowcept query per session, and no attention file is read. Commit `86a97184`; about 75 ms live.
  - `POST /v1/sessions/{sid}/messages/{mid}/attention` with body `{text}` or `{part_id, field, start, end}`.
- **The UI guard (gact-tui `9b9ec17e`):** the client fetches availability when a session opens and whenever the transcript grows. The Understand attention action is offered only on answers marked available, so users without attention capture never see it.
- **Config keys** (in `config.defaults.yaml`, `docs/ENVIRONMENT.md` and `.env.example`, regenerated by `scripts/gen_env_reference.py`): `provenance.attention`, `provenance.attention.tokenizer`, `provenance.attention.files_dir`.
- **Tests:** `tests/test_gact/test_attention/` (51 pass). The fixture is one real call from job 3237185 plus its real file, with rows cut to the top 64 entries; `tests/fixtures/attention/build_fixture.py` documents the derivation. `*.safetensors` is marked binary in `.gitattributes`. The route-count guard is at 283.
- **Sample payload for UI work:** `clio_develop_workspace/temp/attn-probe/sample-attention-payload.json`, plus `sample-transcript.json`.

### gact-tui: draft PR #502, branch `feat/attention-mode`

- **Status:** built and verified against the local stack (section 4). Screenshots are in `clio_develop_workspace/temp/attn-shots/` (01 to 17: normal, toolbar, loading, shown light/dark, card badges, answer heat, unavailable, dismiss; 20 to 24: the rail). Typecheck, lint and build pass; the web suite passes (1831 tests).
- **The rail (owner request, commit `14ed3dc6`):** attention mode colors the existing LEFT transcript rail (`transcript-minimap.tsx`); the separate right-edge strip is deleted.
  - Heated messages are red ticks, longer and stronger with their share; the rest fade to grey.
  - The rail magnifies like a dock (`web/src/lib/minimap-magnify.ts`, tested): ticks near the pointer grow and spread apart, and the row under the pointer stays put. This matches the owner's reference recording (`D:/Libraries/Videos/Recording 2026-09-27 174254.mp4`).
  - The hover preview shows "N% of attention traced here".
  - Magnification applies in normal mode too.
  - Hover shows the message's share **of the attention that reached the conversation's messages** (all messages sum to 100%), not of all attention: raw shares read as ~0.1% because most attention is spread thin or on the system prompt, tool definitions and formatting. Messages under 3% of it stay grey (`HEATED_MIN_FRACTION`). Commit `1f2eb1a6`.
- **Best demo selections** (the rail tells a story across turns):
  - `msg_asst_dbe05dc4a44e` (report turn), select "identified 72 stations": 66% on its own turn's steps, 24% on the turn-2 answer where the 72 stations were found.
  - `msg_asst_d2da36acf534` (follow-up question), select "MTA1": turn 2 31%, report turn 16%, the three user questions 11% each.
- **Known gaps to close before merge:**
  1. Heat does not render inside the "Technical details" dialog (portaled outside the transcript); the card badge is the signal there.
  2. The client pairs a tool call's thought/input blocks with its result block by document order (`web/src/lib/attention-tool-index.ts`), because attention blocks carry part ids but not the tool `call_id`. Fix at the root: add `call_id` to each block in `service.py` (the transcript parts have it), then key the client by it and delete the ordinal zip.
- The worktree is `D:/Libraries/Documents/projects/clio_develop_workspace/temp/gact-attn` (off `origin/develop`).
- **New files:**
  - `packages/core/src/v3/attention-{domain,repository,schemas}.ts`
  - `web/src/components/clio/attention-{minimap,mode-banner,tool-badge}.tsx`
  - `web/src/components/clio/use-message-attention-index.ts`
  - `web/src/hooks/use-attention-{mode,highlights}.ts` plus tests
  - edits across the conversation rendering components (data attributes for message, part and field)
- **UI spec (what #502 implements):**
  1. Register a selection action **Understand attention** for kind `agent-answer-text`, ordered after More details, following `use-add-to-chat-selection-action.ts` and `use-more-details.ts`.
  2. `use-attention-mode` has per-session states idle, loading, shown(payload) and unavailable(message). It clears on X or on session change.
  3. The banner shows "Attention mode", the quoted selection and an X. Under it:
     - one stacked bar of the `sources` shares, normalized over the retained shares, with a separate muted "Rest of the conversation (spread thin) N%" segment for `residual`;
     - a legend with human labels: System prompt, Your messages, Tool definitions, Tool calls, Tool results, Agent reasoning, Earlier answers, Formatting;
     - a warning chip when `flags` contains `tool_result_dominant`.
  4. **Heat:** use the CSS Custom Highlight API (`CSS.highlights` + `Range`) over the rendered DOM. Map source offsets to rendered offsets with the same projection as `rendered.py`: drop `* _ \` # > | ~ [ ]`, list markers and link targets, and collapse whitespace. Use about 4 intensity buckets relative to the payload max, merge adjacent runs in the same bucket (some blocks have 1000+ runs), and mark the selected text in a distinct color.
  5. Collapsed tool cards with heat get a small share badge; expanding the card shows the highlights.
  6. The left transcript rail carries the heat (see "The rail" above); clicking a tick jumps to that message.
  7. Rules: reui / `components/ui` primitives only, light and dark themes, no explanatory paragraphs, no em dashes in UI strings, no flags or fallbacks.

## 4. Local verification stack (works now; reproducible)

This is everything needed to demo on this Windows box over the real job 3237185 data.

1. **Docker containers:**
   - `attn-mongo` (mongo:7, `127.0.0.1:27018`) with the bundle's mongodump restored. Use `MSYS_NO_PATHCONV=1` in Git Bash for `docker cp`/`exec` paths, then `mongorestore --gzip --archive=/tmp/a.gz`. The result is `flowcept`: 645 tasks, 4 workflows.
   - `attn-redis` (redis:7, `127.0.0.1:6380`).
2. **Flowcept settings:** `clio_develop_workspace/temp/attn-stack/flowcept.yaml` (mq/kv_db on 6380, mongodb on 27018, `project.db_flush_mode: online`; Flowcept refuses offline mode with DBs enabled).
3. **CLIO on :17910 from the attn worktree:** `bash clio_develop_workspace/temp/attn-stack/serve.sh`. It sets:
   - `CLIO_USER_DIR` and `CLIO_SESSIONS_PATH` inside the stack directory (without `CLIO_SESSIONS_PATH`, sessions land in `<cwd>/.clio/agent`);
   - `CLIO_PROVENANCE_PROVIDERS=jsonl,flowcept`, `CLIO_PROVENANCE_JSONL_PATH`, `FLOWCEPT_SETTINGS_PATH`;
   - `CLIO_PROVENANCE_ATTENTION=1`, `CLIO_PROVENANCE_ATTENTION_FILES_DIR=D:/crv/p/attn-dataset/full/files`;
   - `CLIO_PROVENANCE_ATTENTION_TOKENIZER` = the local HF snapshot of `ibm-granite/granite-4.2-30b` tokenizer files;
   - CORS for `localhost:5174`.

   The worktree venv needs `uv sync --extra dev --extra flowcept --prerelease allow`. Revert any `uv.lock` churn afterwards.
4. **The session:** workspace `ws_d31f15060ff4`, session `sess_732c53f97cb3` (ids in `attn-stack/ids.txt`). It was rebuilt from the run's own `lm.call` records by `clio_develop_workspace/temp/attn-probe/import_bundle_session.py <bundle> <messages dir> <journal file> <sid>` (run it with the server stopped, then restart the server).
   - Good selections: `msg_asst_42a2e00349b3` "The closest station is MTA1"; `msg_asst_dbe05dc4a44e` "identified 72 stations".
   - curl check: `POST .../attention` with `{"text":"The closest station is MTA1"}` returns `available:true` in about 0.5 s (about 270 KB).
5. **Web client from the UI branch on :5174** pointed at :17910 (use `localhost`, not 127.0.0.1; vite binds `::1`). The owner wants to see the demo live on the desktop or the web client over this data. When the UI works, send them the URL. For the desktop, build it from the branch and attach it to :17910.
6. **Teardown when done:** stop the :17910 process (only that PID), `docker rm -f attn-mongo attn-redis`, and delete worktrees after their PRs merge.

## 5. Remaining work, in order

1. **Close the two UI gaps** listed under gact-tui #502 (`call_id` on blocks, server and client in the same change; decide whether heat is needed in the Technical details dialog). Re-screenshot the affected states against the stack. The web client runs with `CLIO_DEV_REMOTE_ENDPOINT=http://127.0.0.1:17910 npx vite --port 5174 --strictPort` from `gact-attn/web`.
2. **Adversarial review** of both PRs (a clean-context reviewer). Specific risks:
   - the rendered-text projection on markdown-heavy answers;
   - selections repeated in several parts (the server prefers answer text, latest part first);
   - large payloads;
   - highlight performance on 1000+ runs;
   - the unavailable path showing the server `message` verbatim.
3. **Live run on Delta (demo path).** CLIO runs on the GPU node next to vLLM (granite-4.2-30b, attention connector), Redis, Mongo and the Flowcept DocumentInserter; the owner's desktop connects through the SSH tunnel or remote deploy.
   - CLIO config on the node:
     - `provenance.agentic.providers: [jsonl, flowcept]`
     - `provenance.agentic.flowcept.settings_path` → the node's Flowcept settings
     - `provenance.attention: true`
     - the vLLM provider `api_base` → `http://127.0.0.1:8000/v1`
   - The tokenizer resolves from vLLM `/v1/models` `root`, or set `provenance.attention.tokenizer` to a local tokenizer directory (nodes may be air-gapped: `/projects/bekn/hf` has the HF cache).
   - Run a turn, select text, and confirm the view.
   - This is also the first live check of the **write path**: declared ranges should make the connector report `segment_mode: "variable"` and the payload report `sections_source: "declared"`. If the connector rejects or ignores the ranges, record exactly what it logged. The owner's spotter note still describes the older kvnorm job; the job must run the attention connector.
   - Leave nothing running on the cluster.
4. **Merge:** clio-agent #1490 → develop (mark it ready; CI green on a fresh base). Then the gact-tui PR → develop. Then bump the `external/gact-tui` submodule in clio-agent to the gact-tui release.
5. **Release** with the repo skill `.claude/skills/release-clio/SKILL.md`: patch versions, clio-agent 0.9.4.21 with gact-tui 0.11.2.22, cut from `main` (merge develop → main, bump on main, tag, back-merge). Covers:
   - the version-bump sites (pyproject, `__init__`, `uv.lock`, install README, `tests/test_scripts/test_release_install_policy.py`, README and docs/INSTALL)
   - the CHANGELOG roll
   - the memory gate (`scripts/live_verification/run_with_private_cte.py scripts/mcp_mem_attribution.py ... --runs 3 --assert-budget`); close the desktop app first, because the script counts every `clio_run` on the machine
   - real release notes with no attribution
   - PyPI, bundles and `release-check` green
   - a marketplace release only if a pack changed
6. **Update the plan** (`C:\Users\jaime\.claude\plans\hashed-toasting-sunbeam.md`, section "ATTENTION SHOWCASE").

## 6. Rules that apply (owner-locked)

- **No Claude or AI attribution** anywhere: no Co-Authored-By trailers, no "Generated with" footers.
- **Git:** gitflow (feature → develop → main); releases only from `main`; conventional commits; delete merged branches and worktrees.
- **Cut, don't keep:** a replacement deletes the old path in the same PR.
- **No silent fallback:** every degraded path has a typed reason.
- **No accretion:** new logic goes in owner modules; `scripts/check_file_size.py` ratchets.
- **Scratch location:** only under `D:\Libraries\Documents\projects\clio_develop_workspace`.
- **Credentials:** never print tokens or keys. OpenRouter tests use free models only.
- **Clusters:** no remote side effects; clean up everything you start.
- **Commits:** full test suite on CI; locally, targeted tests + lint.
- **Owner-only calls:** UI shaping beyond this spec is the owner's call.
