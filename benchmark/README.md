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

A benchmark case is a semantic workflow contract, not a single prompt plus a
plausible answer. The case definition must name the required intermediate
transformations so the trace can be audited at each boundary. For data-search
cases, the expected shape is:

```text
natural language intent
-> resolved domain/spatial/temporal intent
-> catalog discovery
-> dataset/resource selection
-> data acquisition
-> data validation
-> scientific analysis
-> visualization or durable artifact
-> final evidence-backed synthesis
```

Each arrow is a benchmark boundary. A run does not pass if CLIO skips a
boundary, hides it inside a domain-specific shortcut, or replaces it with
unsupported prose. The trace must show the expert, tool, input object, output
object, provenance, and artifact evidence needed to decide whether that
boundary was crossed correctly.

Case definitions may compare hierarchy shapes:

- depth semantics: one long expert chain that tests state preservation;
- width semantics: parallel evidence branches that test fanout and merge;
- domain semantics: grouped `data`, `analysis`, `visualization`, and
  `synthesis` branches that test reusable capability boundaries.

Domain grouping must not erase semantic boundaries. For example, a `data`
expert may own geospatial resolution, but if the user prompt contains spatial
intent, the trace still needs an explicit geospatial output before any
domain-specific catalog query consumes the location.

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
