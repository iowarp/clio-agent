# CLIO 12-Case Benchmark Evidence Contract

This contract defines what must be committed before a case in
`iowarp/clio-agent#628` can be checked off.

The benchmark is a live, full-agent semantic run. A prompt is sent to CLIO
against the intended session and Agent Blueprint, streamed operations are
monitored while the agent works, and the resulting JSONL/session traces are
audited afterward. Unit tests and static checks are guardrails only; they do
not make a benchmark case pass.

## Folder Layout

Each case owns one folder:

```text
benchmark/caseXX-short-name/
```

The folder must contain:

- `README.md` - human case contract and current status.
- `prompt.md` - exact prompt used for the live run.
- `session.json` - session, workspace, provider, blueprint, and marketplace
  metadata for the run.
- `events.jsonl` - streamed session events captured while the agent worked.
- `semantic.jsonl` - durable semantic execution events for the run.
- `trace.jsonl` - benchmark runner row or audited session trace.
- `outputs/` - artifacts produced by the run, or an `outputs/README.md`
  explaining why no artifact is expected.
- `result.md` - short human summary: what happened, why it passed or failed,
  and links to the trace/artifacts in the same folder.

Historical evidence files can be referenced from `result.md`, but the case is
not complete until its own folder contains the run evidence above.

## Live Monitoring Requirement

The run must be watched through the streaming/event surface, not inspected only
after completion. The event record should show the user-facing progression:

- turn accepted and routed,
- orchestrator or active expert call,
- sync delegation starts and completions,
- spawned subagent/nanoagent starts and completions when used,
- tool/MCP calls with arguments, results, errors, and durations,
- memory and artifact access,
- hooks and semantic logging events when relevant,
- recovery or surfaced-error events,
- final parent return.

## Pass Evidence

A case can be marked passed only when its `result.md` cites objective evidence
from the folder proving:

- the natural prompt did not name the internal route or tools,
- the active Agent Blueprint and expert hierarchy match the case,
- the route was not a deterministic shortcut or keyword-only path,
- the required tools/MCPs/memory/artifacts were actually exercised,
- sync delegation returned to the immediate parent with compact evidence,
- final prose agrees with the trace, not invented facts,
- expected artifacts exist on disk when claimed,
- expected surfaced errors or recoveries are represented as structured events.

## Failure Handling

If a case fails, keep the folder. `result.md` should say where it failed and
link the focused issue opened for the defect. After the fix lands, rerun the
case and update the folder with the passing evidence rather than deleting the
failed history.

## Naming

The current case folders are:

- `case01-genomics-cohort-qc`
- `case02-genomics-memory-followup`
- `case03-proteomics-lfq-qc`
- `case04-proteomics-format-validation`
- `case05-hpc-io-regression`
- `case06-format-bridge-integrity`
- `case07-terrain-lidar-suitability`
- `case08-ndp-seismic-waveform-png`
- `case09-catalog-recovery`
- `case10-custom-mcp-workflow`
- `case11-hooks-logging-streaming`
- `case12-marketplace-workspace-swap`

If the research benchmark design changes a case name, update
`iowarp/clio-agent#628`, this file, and the folder name together.
