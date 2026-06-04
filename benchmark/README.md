# CLIO Benchmark

This directory is the benchmark contract for CLIO's public scientific agent
evaluation. It intentionally contains no historical run reports, no old JSONL
evidence, and no partial demo claims.

The benchmark starts from 12 cases. A case is not passing because a unit test
passed, a provider call returned text, or a small tool invocation produced an
artifact. A case passes only when the case folder contains live provider
evidence, semantic traces, tool provenance, artifacts where required, and an
audited result note that matches the prompt intent.

## Current Status

Status: reset.

None of the 12 cases are marked as passed in this directory. Previous reports
were removed because they mixed infrastructure proofs, old scaffold prompts,
SAC-specific shortcuts, and partial demo evidence with the benchmark target.

## Files

- `CASE_MATRIX.md` defines the 12 cases and the semantic gaps they must close.
- `CASE_EVIDENCE_CONTRACT.md` defines the evidence required inside each case
  folder.
- `caseXX-*/README.md` defines each case's prompt intent, expected hierarchy,
  and pass criteria.

## Benchmark Standard

Every case must exercise a real CLIO session through the normal orchestrator,
not a direct tool call or a hand-routed internal path. The prompt should be
natural and should not name internal experts, tool names, or the answer schema.

The final benchmark must prove:

- generic planning rather than benchmark-specific shortcuts;
- marketplace Agent Blueprints rather than native hardcoded domain experts;
- meaningful expert hierarchy, fanout, handoff, merge, and parent synthesis;
- real data discovery, staging, validation, and artifact provenance;
- explicit failure/recovery behavior without invented downstream results;
- trace inspection that confirms the semantics, not just pass/fail counters.

Generated reports belong in the individual case folder for the run they support
or under `tmp/` while still being audited. Do not add broad historical report
dumps back to this directory.
