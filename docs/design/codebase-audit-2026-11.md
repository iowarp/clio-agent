# Codebase audit: what is old, dead or duplicated, and what to do about it (2026-11-03)

Three bugs this campaign shared one shape:
- the compaction summarizer ran through a chat module and persona left behind after their reader was deleted;
- dead built-in prompts stayed editable on the Prompts page with no effect;
- compaction never shrank the context on `claude_code`, because the provider had grown two models of "a conversation" on top of an SDK that has one.

Old code stays and looks alive. New code grows on a wrong model, and the bugs that model causes add more code. The size ratchet (`scripts/check_file_size.py`) exists to make that visible. It was read as a file-splitting rule instead.

`src/clio_agent` is 353,311 lines. This audit ranks what to delete, merge or rebuild by the bug surface each removes.

## 1. The Claude Code provider: two models of one conversation (rebuild)

**What it owns.** It runs the agent's model through the Claude CLI: one CLI process per conversation; send the new messages; stream the reply; interrupt; replace the process when it dies or its system prompt changes. It is 23 `providers/claude_code_*.py` files and 4,538 lines, plus the conversation registry (`lm/engines/conversations.py`, 189 lines).

**What is duplicated.**
- The conversation registry keys conversations by (session, scope, model, cwd, thinking) and gives each a handle: the CLI `session_id`.
- The client pool (`claude_code_sessions.py`, 791 lines, `_StreamClientEntry` alone about 425) keys CLI clients by GACT session.
- The CLI keeps one conversation per client, whatever the `session_id` (probe, 2026-11-03). So the registry modelled several conversations on one client, and a "new" conversation was appended to the old history. That is why compaction never shrank the context.
- `on_client_dropped`, `session_evicted`, `conversation_cleared` and the `/clear` patch all exist to keep the two models in step.

**What is extra.** Each pooled entry runs its own thread and event loop. Every call crosses from the LM loop to it through `AsyncStreamQueue` and `StreamProducer`, and the cancel, abandon, idle and dead paths each handle that crossing.

**Action.** One `ClaudeConversation` per conversation, owning one client:
- "new conversation" is `/clear`, or a new client when the system prompt or thinking setting changes;
- side calls get their own conversation;
- one small pool is keyed by (GACT session, role);
- the registry's held-messages logic moves onto the conversation;
- the duplicate layers are deleted.

See `claude-code-provider-review-2026-11.md`.

**Done (`ee78181c`).** The conversation lives on the pooled entry that holds the client (`providers/claude_code_conversation.py`):
- deleted: the registry, its LRU and capacity key, and the drop-listener and cross-key reset plumbing;
- size: `conversations.py` + `claude_code_engine.py` + `claude_code_sessions.py` went from 190 + 663 + 791 lines to 57 + 139 + 645 + 772, counting the new module;
- live on the real CLI: a delta, an ARC-op reset that clears the CLI (it forgot the earlier word), a side call, and a cancel in 0.6 s.

**Still to do: the per-entry thread and event loop.** Every query is driven on the shared LM loop, but each entry still owns a thread and a loop, with cross-loop queues for streaming, cancel and teardown. Removing them requires every caller to use the LM loop: the sync engine twin drives its own loop today.

## 1b. One history writer for every session (rebuild; owned by the main thread)

**What it owns.** Every session's history writes go through one store-I/O writer (`gact/core_io.py`), behind global locks: the task history (`gact/task_history*.py`), the task index revision (`gact/task_index*.py`) and the artifact registry. Their callers are the admission paths in `gact/external_tasks.py` and `native_*`.

**What the measurements say.** Under parallel sessions it is the bottleneck. In the burst bench with `--sessions`, p90 per-call latency goes from 26 ms at one session to 750 ms at eight, with CPU at only 0.4-0.5 and the writer at 13-20 ms per call. The earlier dismissal ("GIL-bound, the writer is not the limit") rested on one session's burst.

**Action.** Per-session (per-tree) ordering in place of the single writer (A4). The main thread is rebuilding it; this audit does not change these files.

## 2. Four materializations of the same history (#737; merge, later and large)

**What is duplicated.** One session's history lives in:
- the ARC live plane and its semantic event log (`arc/`);
- the gact session and message store (`gact/session_store.py`, `messages/sess_*.json`);
- the workflow state (`run.extra.workflow_state`);
- the transcript projections (`gact/transcript.py`, 939 lines).

Each has its own writer and reader, and most of the reconcile, migration and "restore" code exists to keep them agreeing.

**Action.** The agreed direction is #737: one normalized log, with the rest as projections. It is too large for this pass, and it is the second-largest source of bugs after item 1.

## 3. Code whose readers are gone (delete)

Confirmed with an import graph of `src/` plus `scripts/`, `install/` and `pyproject.toml` (string references included):

| Module | Lines | Why it is dead |
| --- | --- | --- |
| `gact/loop_handoff.py` | 183 | No production importer; only its own test. |
| `gact/agents/external_mcp_calls.py` | 165 | No production importer; tests only. |
| `arc/prompt_recorder.py` | 105 | Used only by test fixtures (`tests/test_arc/conftest.py`). `.claude/CLAUDE.md` still describes it as the live context plane's recorder. |
| `gact/turn_spawn_result.py` | 55 | No importer anywhere. |
| `gact/agents/task_summary.py` | 48 | No importer; `context_references.py` only uses the string `"task_summary"`. |

Not dead, though nothing imports them:
- `runtime/office_bootstrap.py`, run by `install/prepare-office.ps1`;
- `plugins/pytest_plugin.py`, documented for plugin authors;
- `gact/code_mode/kernel_main.py` and the `searxng_*` shims, run by path.

Already deleted this campaign:
- the chat module and persona (`609a2c0a`);
- the `signatures/` package;
- the chat, planner, answer and expert prompts with their profile machinery (`3fd9b417`).

## 4. Files pinned at the cap (rearchitect where a model is wrong; do not split)

59 files are between 750 and 800 lines, and 13 are above 800 on ratchet baselines:
- `gact/app.py` 1,820;
- `runtime/status.py` 1,101;
- `arc/memory.py` 1,065;
- `ui/cli.py` 1,014;
- `tools/execution.py` 985;
- `gact/transcript.py` 939;
- `gact/routes/providers.py` 915;
- `gact/tool_observer.py` 904;
- `gact/routes/mcp.py` 903;
- `gact/routes/artifacts.py` 897;
- `gact/routes/sessions.py` 820;
- `gact/artifacts/registry.py` 812;
- `arc/working_set_fold.py` 802.

A cluster at 790–800 is code grown to the cap, not code designed to a size. The groups worth reading first, because they repeat a concept:
- **Grants and permissions:** `gact/runtime/grants.py` 799, `grant_resolver.py` 800, `permission_gate.py` 799, `permission_policies`.
- **Infrastructure services:** `gact/infrastructure/node_service.py` 800, `node_models.py` 796, `model_runtimes.py` 792, `runtime.py` 790.
- **Conversations and tasks:** `gact/conversations.py` 791, `conversation_assignments.py` 791, `task_history.py` 791, `external_tasks.py` 784. These overlap item 2.
- **The app and its routes:** `gact/app.py` 1,820 assembles everything. Several routes files hold logic rather than routing.

Action, per group: write down what it owns. Where two modules model the same thing, merge them. Delete what has no reader. Lower or remove the ratchet entries.

## 5. Legacy surfaces still shipped

`ui/cli.py` (1,014) and the CLI REST entry points are "legacy" (`AGENTS.md`), but the CLI is still the smoke test. The TUI is frozen. Action: keep `doctor` and the smoke path; delete what only the frozen TUI or the deleted planner used. This needs its own import check.

## Order

1. The Claude Code provider rebuild (item 1); the history writer (item 1b, main thread).
2. The dead modules (item 3).
3. The cap clusters (item 4), by group.
4. #737 (item 2).

The reminders and injection code are being rebuilt separately and are out of scope here.
