# The Claude Code provider: why it is 4,500 lines, and a smaller shape (2026-11-03)

**The trigger.** The H5 runs showed that compaction never shrank the model's context on `claude_code` (`compaction-review-2026-11.md`, change 8). A probe found the cause: one `ClaudeSDKClient` holds one conversation, and a query's `session_id` tags the message but opens no other history. The conversation registry was built on the opposite belief. It planned "new conversations" by handing out new session ids on a client that kept the old history. The `/clear` fix makes that work, but the bug lived for months because the provider is too large to see what it models.

## What is there

There are 23 `providers/claude_code_*.py` files, 4,538 lines in total. The largest are:
- `claude_code_sessions.py`, 791 lines: two classes, of which `_StreamClientEntry` alone is about 425 lines;
- `claude_code_engine.py`, 663 lines;
- `claude_code_stream_bounds.py`, 445 lines.

The conversation registry sits apart, in `lm/engines/conversations.py`.

The job underneath is small:
- keep one CLI client per conversation;
- send the new messages;
- stream the reply;
- interrupt on cancel;
- replace the client when it dies or its system prompt changes.

What makes it large:

1. **A thread and an event loop per pooled entry** (`_ensure_loop`). Every call crosses from the LM loop (`run_on_lm_loop`) to the entry's own loop through `AsyncStreamQueue` and `StreamProducer`, and the cancel, abandon and idle paths each handle that crossing. The SDK client is asyncio and only needs one loop. The LM loop is already a single persistent loop.
2. **Two models of "a conversation".** The registry (`conversations.py`) keeps a handle (session id), the system prompt and the messages sent, per key. The pool (`claude_code_sessions.py`) keeps a client per GACT session. They are joined by a session id that the CLI ignores. The drop announcements (`on_client_dropped`), the `session_evicted` reset and today's `conversation_cleared` reset all exist to keep the two in step.
3. **The lifecycle spread over five modules.** Pre-connect (`precede_connect`), connect slots (`#1577`), the idle reaper, dead-entry marking (`F6b`), lifecycle release (`#1305`) and stream bounds each own part of when a client exists.

## The proposed shape

- **One `ClaudeConversation` object per conversation.** It owns one `ClaudeSDKClient` on the LM loop, with no extra thread or loop. It holds what the registry holds today (the system prompt, the messages sent and the provider's reply), so "continue" and "start over" are decided where the history actually is:
  - starting over is `/clear` on the same process;
  - a changed system prompt or thinking setting replaces the client;
  - a dead client is replaced, typed.
- **A small pool** keyed by (GACT session, role), where the role is the agent loop or side calls. It has one capacity and idle policy, and session release closes both.
- **The stream bounds, expiry, cancel and audit** become functions over a conversation, not over a pooled entry crossing loops.
- **Expected size:** about 800 lines in place of `sessions` + `engine` + `stream_bounds` + `lifecycle` + `idle_reaper` + `cancel` + `conversations` (about 2,500 lines). Login, plan limits, multimodal and options stay as they are.

## Risks to check first

- **Interrupt semantics on the shared LM loop.** The SDK's `interrupt()` must not block other sessions' streams (B14).
- **The connect bound** (`#1577`, at most N concurrent connects) must hold without the per-entry loop.
- **What the per-entry loop isolates today.** Read the history of `#COPPER12`, `#993` and `#901` for the reason it was introduced, before removing it.
- **The tests that pin cross-loop behaviour** (`test_claude_code_b1_tuning.py`, `test_claude_code_connect_wait.py`, `test_claude_code_session_lifecycle.py`) must be read for what they protect, not ported line by line.

Status (2026-11-03): the one-model-of-a-conversation part is built (`ee78181c`, `providers/claude_code_conversation.py`); the per-entry thread and loop removal is not.
