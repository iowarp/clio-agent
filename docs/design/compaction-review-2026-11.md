# Compaction: an audit of CLIO against the field, and the changes (2026-11-03)

Compaction decides whether a long run stays on its task once the context fills. Until now CLIO's compaction was only fixed mechanically: it could not fire on `claude_code` (finding 22), and after one compaction it stopped for the rest of the turn (`b919ae60`). Nobody read the summaries, judged the prompt, or measured alignment across compactions. This review does that, against the four real compactions on record and the practices of nine other harnesses (`compaction-research-2026-11.md`, from primary sources at pinned commits).

## The four real compactions

All four come from H5 (the export investigation with compaction at 5% of a 1M window), `claude-sonnet-5-5` on `claude_code`: head1 runs 1 to 3 (`sess_ab31af77c1d1`, `sess_b6ec5ac53518`, `sess_ff166b20c00c`) and `h5-probe2` (`sess_6e503f03b9bd`). Traces: `~/.local/state/clio-agent/traces/semantic_traces/<sid>.semantic.jsonl`.

- **Outcome.** Every run finished correctly. After each compaction the agent continued from the summary and re-read nothing it had already read. But these are short: one or two compactions within 6 to 9 steps, no plan in use. They show the summaries are usable, not that a long run stays aligned.
- **Summary quality.** 6.7k to 8.9k characters of structured, evidence-preserving bullets: exact paths, counts, the README's documented events, which files were and were not read. Good material.
- **The goal is missing from it.** Every summary says the equivalent of "The user's actual analysis question is not visible in this transcript". The summarizer is given only the segments being folded; the open turn's request is kept verbatim after the summary (`keep_head`), so it never reaches the summarizer. The agent's next context then holds a summary that says the goal is unknown, directly above the goal.
- **The wrong summarizer.** The summary is produced by `agent._run_chat_agent(prompt, "")`, the legacy chat module: its system prompt is CLIO's old chat persona ("HDF5 optimization, Parquet analysis, statistical profiling…") with `question`, `images`, `files` and `session_context` fields, and the compaction instructions arrive as the user's question.
- **Nothing recent is kept.** The keep policy keeps the open turn's request and the latest working state (session facts, plan mode, plan, todos), and no steps (`compaction.keep.last_steps` 0, `last_turns` 0). The agent loses the exact output it was in the middle of using. Earlier turns' requests are folded into the summary.
- **Summaries of summaries.** A later compaction folds the previous summary as transcript text and writes a new one from it.
- **Context waste around it.** Every foreground shell result reached the model twice (as the tool result and again as a `task_result` notice), each wrapped in the raw MCP task envelope: 427 of 453 shell results in the stored traces. That fills the window about twice as fast and is being fixed separately.

## CLIO against the shared practices

| Practice (research, "Practices most of them share") | CLIO today |
| --- | --- |
| Summary is a handoff to a new agent, no tools | Handoff wording yes; run through the chat persona module |
| Fixed sections (goal, state, decisions, files, errors, pending, next step) | Evidence rules, no sections |
| Next step anchored to the latest request, quoted | No; the summarizer cannot see the request |
| User messages kept verbatim (Codex: all, newest first, 20k tokens) or listed verbatim (Claude Code §6) | Open turn's request only; earlier turns' requests folded |
| Recent tail kept verbatim (15k to 20k tokens, or half the budget) | None |
| Instructions rebuilt from source; summarizer told what returns | System prompt and working state rebuilt; the prompt says plan and todos return |
| Repeated compaction updates the previous checkpoint (OpenCode, Factory's best score) | Re-summarizes the old summary as text |
| Artifact trail computed by code (Cline; Factory's weakest dimension for every method) | Partly: the exact-evidence index and context-file inventory |
| Proactive threshold, reactive path on overflow, thrash bound | Threshold and floor rule; overflow fails the turn typed |

## Changes (built on `claude/compaction-handoff`)

Each is a mechanism or a fact given to the model, not a behavioural rule in a prompt (`.claude/CLAUDE.md` principles 1 to 3).

1. **The summarizer continues the agent's own request** (`gact/compaction_summarizer.py`). The automatic trigger sends the loop's next request (same model, system, tools, config and messages) with the compaction prompt as one more user message. This fixes two findings at once:
   - It sees what the agent sees: the request, every typed message, the plan, the todos and an earlier checkpoint at the head. "The question is not visible" cannot happen.
   - It hits the cache. On the H5 compactions the summarizer read 0 and 789 cached tokens against 33k to 158k on the agent steps around it. Now a provider that keeps the conversation (`claude_code`, Codex) is sent only the new messages (`lm/engines/conversations.py`), and a prefix cache (OpenAI, vLLM, LM Studio, and the Anthropic Messages API, whose agent steps had no cache marks at all and now mark the history) serves the rest.
   - Tools and config stay unchanged, because changing either invalidates a cached prefix. The prompt says the reply is text and runs no tools.
   - The manual trigger between turns has no conversation to continue, so it reads the scope's context once and sends it with the prompt on the session's model.
2. **The legacy chat module is deleted:** `ClioAgent.chat_agent`, `_run_chat_agent` with its old persona and unused image and file fields, and the dead `signatures/` package. Nothing else used them.
3. **Sections.** The prompt asks for: Objective (quoted); Requirements and constraints (quoted, with later changes); Progress and state; Findings and evidence (the evidence rules as before); Decisions and ruled out; Files and artifacts; Open threads; Next step, quoting the latest request. It says what returns verbatim and to update an earlier checkpoint rather than summarize it again (anchored update, change 6 of the first draft, falls out of change 1). The prompt is part of CLIO core (`gact/compaction_prompt.md`), not a built-in prompt pack or a marketplace agent: an agent has its own system prompt, tools and model, and any of those breaks the shared cache. The removed `compaction.prompt_file` setting, still set, fails the compaction typed.
4. **Every typed message stays verbatim.** The keep rule keeps the person's own messages (requests and steers), newest first, within `compaction.keep.user_chars` (default 80000 characters, about 20k tokens, Codex's budget).
5. **The newest step stays verbatim** (`compaction.keep.last_steps` default 1): the output the agent was using.
6. **Re-injection stays as built** (plan while draft or approved, todos, session facts); the prompt says so.
7. **Measurement:** `lm.call` records `cache_write_tokens` as well as `cache_read_tokens`, so each compaction's cache use is visible per call.

8. **A new `claude_code` conversation starts from an empty CLI** (`providers/claude_code_clear.py`, found by the first live run below). One `ClaudeSDKClient` holds one conversation whatever the query's `session_id`: a probe query under `conv-b` recalled a word given under `conv-a`, reading its tokens from the cache. Every full send the registry planned (after a compaction, a prefix mismatch, an eviction) was appended to the history the CLI held, so compaction never shrank the model's context unless a different system prompt forced a reconnect. That is what the old summarizer's chat persona did, by accident. Now `/clear` (no model call; the system prompt stays cached) runs before a full send on a client that holds a conversation. One-shot calls run on the session's side client.

## Live results (H5, `claude-sonnet-5-5` on `claude_code`, 2026-11-03)

| Run | Summarizer: cached / total input | Context before → after each fold |
| --- | --- | --- |
| head1 (old summarizer) | 0 and 789 cached | 33k → 33k (reset only by the persona's reconnect) |
| cmp1 (new summarizer, no `/clear`) | 51,286 / 69,269; 159,942 / 177,393 | 69k → 114k; 177k → 221k (the old history stayed in the CLI) |
| cmp2 (with `/clear`) | 51,223 / 67,977; 395,627 / 403,522; 109,006 / 116,387 | 68k → 72k; 404k → 54k; 116k → 52k |

Both cmp runs answered correctly. cmp2 compacted three times in one run.

- **The checkpoints are good** (cmp1, read in full):
  - the objective is quoted;
  - progress is split into done and not done;
  - the findings are ranked with their sources (for example run_0112's uncorrected +2.48 zero offset, from the exact instrument.log line and the dark-mean A_net);
  - the second checkpoint updates the first.
- **Redundancy:** "Requirements and constraints" restates the system instructions (workspace root, `python -I`), which return verbatim anyway. H6 tests constraints from the person.
- **What a fold cannot shrink:**
  - The first cmp2 fold kept a 35k newest step, so the context did not shrink.
  - One `shell_bash` result of 247 KB then wrote 323k tokens in one step. It reached the model twice, in the raw MCP task envelope: the duplicate task_result bug (next fix).

Still to measure: Codex on Windows (`windows-verification-log-2026-11.md` §10), and alignment over several compactions with constraints from the person (H6 below).

## Measuring alignment

The H tasks so far end in under ten steps. A lighthouse for alignment needs a run that compacts several times and must still honour something said only at the start:

- **H6:** H4's export with a request that carries explicit constraints (units, an excluded campaign, the shape of the answer), compaction low enough to fire three or more times, and a second user message mid-session that changes one constraint. Graded mechanically: each constraint honoured, the change honoured, the early findings still present, no repeated work after a compaction.
- Before and after the changes above, three runs each, with the summaries read, not only the grades.
