# Addendum A to the delegation, fork and science log plan: sources, data access and isolated workspaces

Folded into delegation-fork-science-plan.md on 2026-10-27; this file records the decisions.

This addendum changes [delegation-fork-science-plan.md](delegation-fork-science-plan.md) while it is being built. It does four things:

1. **Sources.** Every Observation points at the recorded tool calls that produced it, and the log fills in the command, parameters and artifact versions from those records. The model cites; the record supplies the facts.
2. **Data access.** The agent gets the measured values programmatically, as data it can analyze in its own code with whatever statistics the experiment needs. The log computes no statistics.
3. **Isolated workspaces on request.** A workflow run started with isolation gets its own folder, and each node writes only to its own subfolder.
4. **No built-in notion of "better".** Whether A beats B is the agent's judgement, recorded as a claim with its sources. The log has no objective, promotion or master.

Everything else in the plan stands. The plan's rules, verification and done criteria apply to this addendum unchanged.

## How to apply it

1. Fold this addendum into the plan in the same change that implements it. Update the sections named under **Changes to the plan**, and record the decisions below in the science ledger as the next `FD` numbers.
2. If Phase 3 is done, implement this as Phase 3b before the UI and qualification phases continue. If it is not, implement it as part of Phase 3.
3. Any of F11, F13 and F15 that has already run is run again after this lands, because its sessions must show sources. Earlier runs stay in their `.cloud-run/` folders as superseded evidence and are not published.

## Rules

1. **The model points, the record supplies.** The agent never types a command, parameter or hash into an Observation. It cites recorded tool calls, and the log copies what they did from the records that already exist.
2. **The platform provides facts and data, not judgements.** The log records what ran and hands the values to the agent. It does not compute statistics, rank results, decide that one result beats another, or check the agent's numbers against a rule. Frontier models choose the right analysis for an experiment and weigh trade-offs between metrics better than a fixed rule would, and a fixed rule breaks the next experiment that does not fit it.
3. **Every number in a claim can be reproduced from the record.** A quantitative claim cites the analysis call that produced it. That call's record already holds the script's hash, its input files' hashes and the environment it ran in, so anyone can run the same analysis on the same data.

## What already exists

Sources are built from records CLIO already keeps. Nothing here adds a store or parses command text.

- **Tool calls.** Every tool call is a row in its session's transcript, keyed by the tool observer's `call_id`, with its tool, arguments, result and times. Only background and task-capable calls are also task records (`TaskKind`); an ordinary call such as a measurement is a transcript row only. Lineage reads the transcript row for every call, and the task record as well when there is one.
- **Artifacts.** The artifact registry (`gact/artifacts/`) keeps one `TransformRecord` per producing tool call, keyed by the same `call_id`. It carries the instrument (tool and arguments, or the command and the script's hash), the time span, the tiered execution environment, and `used` and `generated` edges to artifact versions, each with its SHA-256 and evidence. Versions dedup by content hash and record revisions, and the registry is persisted in clio-core. A file becomes an artifact by designation, never by scanning: through a tool's declared output paths, `fs_write_file`, or the designation tool. A script's output file that nobody designates is not in the record, so the method skill tells the agent to designate the files its analysis writes.
- **Not this.** `gact/file_changes.py` is the in-memory Undo ledger for `fs_write_file` and `fs_edit_file`. It is not provenance and lineage does not read it.

## Design: sources

**Citable calls.** Every tool result the model sees carries a short call reference (`call:<session>/<call_id>`) built from the tool observer's `call_id`. If a result envelope already exposes that id, use it; otherwise add the reference to the envelope, once, for every tool.

**Observation fields.**

| Field | Written by | Meaning |
| --- | --- | --- |
| `from_calls` | The model (required, at least one) | The recorded calls that produced the result: a measurement, a game, a script run |
| `lineage` | The log | For each cited call: tool, arguments, start and end time, result digest, the authoring session's agent and model, from the transcript row and, when present, the task record |
| `artifacts` | The log | The artifact versions the cited calls used or generated, with their SHA-256, from the calls' `TransformRecord`s |
| `derived_from` | The model (optional) | The Observation or artifact version this candidate came from |
| `measurements` | The model | Structured values: metric, value, unit, and the condition keys they were taken under (settings, seed, strategy, node, repeat index). A value may instead name a field of a cited call's structured result (`{"from": "call:…", "field": "…"}`), and the log copies the value from the record, so values a tool returned are never retyped |

**Validation.** A cited call must exist and the author must be allowed to cite it, or the entry is rejected with `lineage_call_unknown` or `lineage_call_outside_branch`. The author may cite calls in its own branch's task tree, and a fork may also cite its forker's calls up to the fork point, which its copied transcript contains. An Observation with no cited call is rejected with `lineage_missing`. A `derived_from` must resolve, and a measurement that names a result field must name one the cited call returned (`measurement_field_unknown`). The log does not judge whether the values are right.

**Where it shows.**

- In the log panel, an Observation's sources open to each cited call's turn in the transcript, with its arguments and result.
- The study tree draws `derived_from` edges, so a candidate's ancestry shows beside the hypothesis tree.
- Exports carry sources, and every link resolves offline.

## Design: data access

**Values as data.** `query_log` returns an Observation's `measurements` as structured rows, not prose, with its lineage. A result too large for the context takes the path every tool result already takes (`tools/mcp_result_projection.py`): it is written to the session's tool-output folder and the agent is told where, so `query_log` needs no size rule of its own. A new tool, `export_measurements(filter, format="parquet"|"csv")`, writes the matching measurements to a data file next to a manifest. The manifest lists the filter, the Observation ids, the condition keys and the file's SHA-256. Both files are designated as artifacts by the tool's result, so they are versioned and hashed like any other output. In an isolated run the files go to the caller's node folder; otherwise they go to `<workspace>/.clio/science-log/<session_id>/`, the same per-session layout the shell uses for oversized output, and are deleted with the session. The agent then analyzes the file with its own code: a mean for one experiment, a paired t-test for another, a bootstrap or a fitted model for a third.

**Claims cite their analysis.** An Evidence or Conclusion claim that rests on numbers cites the analysis call (the shell or Python run) that produced them. That call's `TransformRecord` already records the script's hash, the exported file it used with its hash, and the environment tier. When the analysis is random (a bootstrap, a permutation test), the Evidence records its seed. "H2 beats H1 on shared seeds, mean difference 0.4, paired 95% interval 0.2 to 0.6" is the agent's claim, backed by a script anyone can rerun.

**Seeing other branches.** Every branch in a study reads the whole study's entries, siblings and cousins included, while they run, and writes only its own branch. With many branches exploring in parallel, each can see what the others have measured and claimed before it claims anything. This replaces the plan's visibility rule (ancestors only, and a fork reading its forker's entries up to the fork). The cost is independence: branches that read each other's claims can converge early, so the method skill tells a branch to form its own hypothesis and design before reading the others, and the Evidence says when it relied on another branch's result. When two branches claim incompatible results, the merge records a Conflict citing both and resolves it, or leaves it open.

**Later, through code.** When programmatic tool calling lands (a separate 1.0 item), analysis code will call `query_log` directly. Until then, the exported file is how values reach code. Nothing is built or reserved for that path now.

## Design: isolated workspaces

**On request.** Isolation is off by default, so existing workflows and skills that write to known workspace paths keep working. A workflow started with `isolate: true` gets `<workspace>/runs/<run-id>/`, and every node gets its own subfolder: `<run-id>/<step-id>/`, forks as `<step-id>~1/` and so on, and children nested under their parent's folder. The `run-a-study` skill tells the agent to start studies isolated, since that is where parallel branches collide.

**How a node knows its folder.** A node's shell runs with its folder as the working directory, and its launch context names the folder, so a relative path such as `results.csv` lands in it.

**Enforcement.** The write territory today is keyed by the workspace root (`runtime/sandbox_roots.py`), shared by every session in the workspace, for both the OS fence (`sandbox.py`) and the tool-boundary check (`tools/file_policy.py`). Isolation adds a per-session narrowing to that one territory source, so both twins narrow together: a node's session may write only its own folder, and a write outside it is refused with the fence's typed reason. A node reads the whole workspace.

Plugin tools are covered at CLIO's tool boundary, which runs per call and knows the calling session. A tool's declared output path (`output_path` and the other names in `gact/artifacts/designation.py`) is already grounded against the workspace root before the call; in an isolated run it is grounded against the node's folder instead, so a relative output lands in the node's folder, and an absolute one outside it is refused with the same typed reason. What the boundary cannot see is a file a plugin's server writes on its own, with no output path in the call's arguments. Those servers (the `fleet` profile) run once per workspace and serve every session, so the OS fence cannot confine them to one node either: two nodes using such a tool may write the same file. Neither study's plugin writes files. The limit is recorded as a decision, and plugin authors are told to take output paths as arguments. Separately, an isolated run whose fence backend cannot narrow (no Landlock, srt or Windows fence available) returns a typed reason at start instead of running unconfined in silence.

**Forks.** A fork in an isolated run starts with a copy of its forker's folder at the moment of the fork, as it starts with a copy of the transcript. Large files are cloned copy-on-write where the filesystem supports it and copied otherwise, never hard-linked, because a hard link is the same file and an edit through one name changes the other. Cloning uses `FICLONE` on Linux (btrfs, XFS), `clonefile` on macOS (APFS) and block cloning on Windows (ReFS, Dev Drive); NTFS, ext4 and other filesystems copy. The method used is recorded on the fork. A fork outside an isolated run copies no folder.

**What the person sees.** The run's folder is an ordinary folder in the workspace, browsable in the file view, and each node's folder is linked from its node in the railroad and the study tree.

## Changes to the plan

| Plan section | Change |
| --- | --- |
| Contract | This addendum governs sources, data access and isolated workspaces |
| Out of scope | Keep "Statistical tooling inside the science log". Add "Objective, promotion or master semantics in the log", "Git worktrees per node", "A remote layout for nodes that run on other machines" and "A performance-tuning study such as HDF5 chunk size; a candidate for a later study" |
| Design: the science log, Entries | Observation's fields become those in the sources table above |
| Design: the science log, Branches and Validation | Every branch reads the whole study and writes only its own branch; add the source rejections |
| Design: the science log, Tools | Add `export_measurements`; `query_log` returns measurements as rows with their lineage, oversized results spilling to file like any tool result; tool results carry call references |
| Design: the science log, Running a study | The method skill tells the agent to start studies isolated; to cite the calls behind every Observation; to designate the files its analysis writes; to form its own hypothesis and design before reading other branches, then check what they have measured and claimed before claiming; to analyze exported data in code with the method the experiment needs, recording the seed of a random analysis; and to cite that analysis in every quantitative claim |
| Design: fork | In an isolated run, a fork starts with a copy of its forker's folder |
| Design: export of flows and subagents | A study's export shows sources, the exported data files and analysis scripts each claim cites, and each node's folder |
| Defaults | Replace "Sibling visibility: none while running" with "Every branch reads the whole study; each writes only its own branch". Add "Isolated workspaces: off by default; studies start isolated" |
| Verification, Audits | `audit_study.py` also reports, as facts and without judging the numbers: every Observation's cited calls and whether each lies in its branch's task tree or its forker's history; and, for every quantitative claim, the analysis call it cites with that call's script hash, input hashes, environment tier and recorded seed |
| Phase 4: UI | The log panel shows sources; the study tree draws `derived_from` edges; nodes link to their folders |
| Phase 7, F11 | F11 also passes only when every Observation cites calls and every quantitative claim cites an analysis over an exported file |
| Wordle and hidden-law criteria | Add the criteria below |

## Defaults

| Question | Default |
| --- | --- |
| Export format | Parquet, with CSV on request |
| Export location | The node folder in an isolated run; otherwise `<workspace>/.clio/science-log/<session_id>/` |
| Hashing | The artifact registry's SHA-256 of the file's content, recorded when the call that wrote it completes |
| Isolation | Off unless the workflow is started with `isolate: true`; the method skill starts studies isolated |
| Large files on fork | Copy-on-write clone where the filesystem allows, otherwise a copy; never a hard link |

## Phase 3b: sources, data access and isolated workspaces

1. Add the call reference to tool result envelopes where a result lacks one.
2. Add the Observation fields, filling lineage from transcript rows and task records and artifacts from `TransformRecord`s, measurements copied from named result fields, and the validation errors.
3. Return measurements as rows from `query_log`, and add `export_measurements` with its manifest and artifact designation.
4. Add `isolate` to the workflow flow: create the run and node folders, set each node's working directory and launch context, narrow the shared territory source per session for both the fence and the tool boundary, ground and check declared output paths against the node's folder, refuse a run whose fence cannot narrow with a typed reason, and clone or copy the forker's folder on fork. Unit-test the macOS and Windows clone paths with the platform call faked; they are qualified live on Linux only.
5. Update the method skill and fragment.
6. Extend `audit_study.py` with the reports above.

## Scenarios

Add to the science ledger:

| ID | Scenario | Prompt | Passes when |
| --- | --- | --- | --- |
| F22 | Write an Observation citing a real measurement call, with one value named by result field | Mechanism | Its sources list the call's tool, arguments, times, result digest and artifact versions, all matching the transcript and the artifact registry, and the named value equals the call's result |
| F23 | Write an Observation with no cited call, one citing a call from another branch, and, in a fork, one citing the forker's call from before the fork | Mechanism | `lineage_missing` and `lineage_call_outside_branch` are returned and nothing is written for the first two; the third is accepted |
| F24 | `export_measurements` on a known set of Observations, in and outside an isolated run | Mechanism | The file's rows match the log, the manifest lists the Observation ids and condition keys, the hash matches the file and its artifact version, and each file is where the defaults put it |
| F25 | An isolated workflow: two parallel steps write a file with the same name; one tries to write outside its folder; a step forks; a merge reads its inputs | Mechanism | Both files exist in their own folders; the outside write is refused with the fence's reason; the fork's folder starts as a copy, cloned or copied as recorded; the merge reads both inputs and writes only its own folder |

Add to the Wordle criteria (F13): strategy comparisons are paired over shared seeds in the agent's own analysis, cited in the merged Conclusion.

Add to the hidden-law criteria (F15): the decoy conclusion cites an analysis showing no effect within its uncertainty, and the regime claim cites Observations on both sides of the threshold.

## Done

The plan's done list gains:

1. F22 to F25 pass with the evidence the Verification section requires, and F11, F13 and F15 have passed again after this addendum landed.
2. Every published study's export shows sources for every Observation, the data and analysis behind every quantitative claim, and each node's folder.
