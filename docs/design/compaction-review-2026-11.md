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

## Changes

Each is a mechanism or a fact given to the model, not a behavioural rule in a prompt (`.claude/CLAUDE.md` principles 1 to 3).

1. **A dedicated summarizer call.** A plain request with its own system text (a handoff writer, no tools), the session's pinned model, outside the stateful provider scope as today; not the chat module.
2. **The summarizer sees every user message.** Every message the person typed in the session, verbatim and marked, including the open turn's request that stays in context after the summary; it is told which of them stay verbatim so it does not restate them, and that their limits apply only if quoted.
3. **Sections.** The prompt asks for: Objective (the request in the person's words); Requirements and constraints (quoted); Progress and state; Findings and evidence (the existing evidence rules); Decisions and ruled-out paths; Files and artifacts; Open threads; Next step, anchored to the latest request with a quote. Missing sections are recorded in the compaction event, not enforced.
4. **Every typed user message stays verbatim.** The keep policy keeps all of the person's messages (newest first within a budget, `compaction.keep.user_tokens`), not only the open turn's request.
5. **A recent tail stays verbatim.** The newest steps of the open turn up to `compaction.keep.tail_tokens` (default a share of the threshold), so the floor rule (`b919ae60`) still holds.
6. **Anchored update.** A later compaction gives the summarizer the previous checkpoint as "the checkpoint so far" and the newer history, and asks for the updated checkpoint, instead of folding the old summary as transcript.
7. **Re-injection stays as built** (plan while draft or approved, todos, session facts, the goal when one is set); the summarizer is told so.

## Measuring alignment

The H tasks so far end in under ten steps. A lighthouse for alignment needs a run that compacts several times and must still honour something said only at the start:

- **H6:** H4's export with a request that carries explicit constraints (units, an excluded campaign, the shape of the answer), compaction low enough to fire three or more times, and a second user message mid-session that changes one constraint. Graded mechanically: each constraint honoured, the change honoured, the early findings still present, no repeated work after a compaction.
- Before and after the changes above, three runs each, with the summaries read, not only the grades.
