# Session recall coverage audit

Audited October 20, 2026 against clio-agent
`9bf46e6eea0011a1295e6ec4ce44c39a997ead55`. This is an evidence audit of the
original request at that revision. The active Task extension is documented in
`docs/design/task-transcript-coordination.md`; this old-source audit is not
acceptance of that extension or a new memory system.
The Tasks finish line remains the active goal. Further reviews follow the
human's latest preference: Claude Opus only; no new Astra review.

## What exists and what the evidence proves

| Capability | Current implementation | Verified evidence and limit |
| --- | --- | --- |
| Discover a permitted session | `session_search` searches retained transcript excerpts and returns source/session identifiers and an authorized agent target. | Existing contracts cover search provenance, scopes, trusted grants, denial despite model intent and rollback. Supplied-marker discovery in earlier peer scenarios is not paraphrase recall. |
| Read recent session activity | `session_read_summary` returns counts, message IDs and excerpts from the last five messages, each bounded to 360 characters. | Four summary/frame contracts include no invented summary, original source IDs, native/HTTP summary equality and a production-storage application reload. No model-generated whole-session summary is supplied. |
| Inspect context assembly | `session_read_context_frame` returns up to 50 source/inclusion records with provenance. | A contract checks explicit partial coverage and no fabricated source text. `app.state.context_frames` still starts empty; durable context-frame recovery is not qualified. |
| Read a full transcript programmatically | Authenticated product HTTP routes provide paginated session messages and exact message reads. | Their existence is not proof that the native agent tool catalog exposes an equivalent paginated, authorized transcript reader. The three memory tools do not provide that reader. |
| Coordinate sessions/workflows | Shared task handles, mail, grants, observe/wait and subscriptions have separate Tasks evidence. | R338 uses two actual declared workflows. Messaging/handle discovery does not establish semantic retrieval of another workflow's old decisions. |
| Read another compute node's history | No distributed history reader is established by this audit. | The attachment illustrates host Codex/Claude tooling and filesystem inspection. It is not CLIO cross-node acceptance and grants no authority to inspect unrelated histories. |
| Maintained long-term knowledge | The design proposes source-linked facts/decisions, corrections and forgetting over ARC/Core. | The human design explicitly remains a proposal. No accepted live old-decision/paraphrase/correction/forgetting matrix was found. |

The old October 12 design finding is partly stale: native search and the
agent-callable HTTP search now call the same `agent_session_search` function.
It authorizes session candidates through Core relationships before search,
counting and ranking; an intent string grants nothing. Full response parity is
not established: native results add `agent_target`, while the HTTP result uses
its own projection. The historical design is preserved unchanged.

## Actual CI execution

The audit read existing GitHub XML artifacts in memory. It launched no tests,
models, services or reviewers and retained no artifact archives.

- Completed CI [39349269414](https://github.com/iowarp/clio-agent/actions/runs/39349269414),
  source `524734626b0496d5a281a1164c6fd82be7da26d6`: all 28 cases in
  `test_memory_stats` and `test_memory_summary_contract` passed separately on
  Python 3.12, 3.13 and 3.14. The overall run failed elsewhere.
- Current CI [39358960951](https://github.com/iowarp/clio-agent/actions/runs/39358960951),
  source `9bf46e6e`: at this snapshot seven of twelve XML reports were available.
  All 28 memory cases passed on Python 3.14; Python 3.13 had ten reported passes
  and Python 3.12 four. Missing reports are pending, not passed.
- The three audited production modules and these two test modules have no
  changes between those two source commits.

Bounded evidence:
`D:/Libraries/Documents/projects/clio-agent-task-qualification/.pytest-runtime/static/r504-session-recall-evidence-audit.json`.
The receipt identifies every case, report, source revision and audited file hash.

## Originally queued work, now superseded by the active Task extension

The human subsequently made transcript coordination active Tasks scope. The
shared Task implementation now replaces new default memory-tool attachment;
current CI/live status is in the three canonical ledgers. Keep session inspection separate from maintained knowledge;
do not create a second history store or count peer messaging as memory acceptance.

Required live scenarios remain unqualified:

1. Put an important decision early in A and add unrelated work. In independent B,
   retrieve and cite the original decision using different wording, without
   supplying its answer, exact marker or source session identifier.
2. Repeat after isolated-service restart and distinguish restored transcript,
   summary, context frame and maintained knowledge.
3. Correct the decision; a fresh session uses the correction with its sources.
4. Forget/delete under an explicit policy; restart and verify that summaries,
   retrieval and indexes do not resurface the forgotten record.
5. Read exact bounded transcript ranges and referenced typed contents, with
   permission denial, stable cursors, rollback and honest missing-source results.
6. Use genuinely separate authorized node/service contexts if cross-node history
   is promised. Shared-service peer messaging alone cannot qualify it.

Retain the human's existing
[session recall and long-term memory design](../design/session-recall-and-long-term-memory.md).
No new production memory behavior changed in this audit.
