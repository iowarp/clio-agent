# CLIO Hierarchical Benchmark Review

Source artifact: https://claude.ai/public/artifacts/e0549525-289b-445e-b6ff-a9eecb4baa4b

Date reviewed: 2026-06-03

## Executive Take

This is a useful benchmark design document, but it should be treated as a proposal to refine, not as a finished CLIO benchmark plan. The strongest parts are the use of planted ground truth, topology ablations, shared reusable NDP collection, failure recovery, and cross-domain cases that would force more than a single file-inspection tool call.

The main correction is semantic: tier labels must not be interpreted as graph depth. A durable expert that sits deeper in a delegation chain can still be a tier-2 expert. Tier-3 should mean a narrow, bounded worker or nanoagent spawned for a specific fan-out or policy decision, not "the third expert in a chain." This matters because otherwise the benchmark teaches the wrong CLIO mental model.

## What I Like

The cases are scientifically grounded enough to be useful for demos and regression evidence. Genomics QC, proteomics LFQ, HPC I/O regression, seismic NDP, format conversion integrity, and terrain/lidar are especially good because they have objective pass criteria: planted defects, known spike-ins, injected regressions, known event catalogs, dtype preservation checks, or tolerance-based terrain metrics.

The ablation idea is important. For 1.0 readiness we should not only show that CLIO produced a plausible answer; we should show that the intended hierarchy produced better evidence than a flattened baseline. That gives us a defensible story for why hierarchical orchestration is not just architecture theater.

The shared NDP collector is the right kind of stress test. Reusing the same discovery/recovery/provenance component across wildfire, seismic, and terrain would test pack composition, marketplace reuse, bounded downloads, unsupported delivery handling, and parent-owned recovery semantics.

## What I Would Change

The document sometimes blurs Expert, Agent, and sub-agent. For CLIO, an Agent is the whole hierarchy instantiated from a pack into a session. The nodes inside it are Experts. A reusable NDP component can be modeled as a reusable expert subtree or pack module, but it should not be casually described as a separate Agent unless we intentionally instantiate a separate session/agent boundary.

The tier framing needs to be fixed before presentation. The benchmark should say: tier-1 is the root/orchestrator of a session; tier-2 is a durable domain expert in that agent hierarchy; tier-3 is an ephemeral or narrow nanoagent used for bounded fan-out, trials, policy checks, or per-item work. Depth in the graph is not the tier.

The rule that a flat expert "should produce a wrong answer" is too strong as a universal requirement. A strong model may sometimes get the right answer flat. The better benchmark criterion is that the hierarchical run must produce stronger evidence, provenance, recovery behavior, and ablation deltas than the flat baseline. Correctness still matters, but the score should include structure and trace quality.

The NDP collector is useful, but download recovery should remain agentic at the parent level. A failed NDP fetch should return structured context to the owning Data expert, not get trapped in deterministic "local path exists" checks. The parent should be able to retry alternate resources, use generic web/download tools, switch source, or report a partial result with evidence.

## Case Priorities

For the first real benchmark wave, I would prioritize six cases:

1. Genomics cohort QC.
2. Proteomics LFQ differential abundance.
3. HPC I/O performance regression.
4. Seismic event discovery with NDP.
5. Scientific format bridge with integrity guard.
6. Terrain site suitability with NDP.

Those give us biology, proteomics, HPC, NDP/geoscience, data engineering, and geospatial coverage. They also test different hierarchy shapes: fan-out/merge, decision subtrees, recovery, independent verification, and reusable NDP collection.

The climate, wildfire, DFT, tabular drift, and manuscript-writing cases are valuable, but some need more tool work or fixture design before they are ready to be objective benchmark gates.

## Tooling Gaps Implied

This proposal implies several tools that are not just optional polish:

- VCF/cohort QC metrics, likely backed by cyvcf2/pysam or bcftools.
- LFQ normalization and differential abundance utilities.
- Darshan or HPC trace parsing.
- NetCDF/Zarr and climate station handling.
- GeoTIFF/vector/raster and LAS/LAZ point-cloud handling.
- DFT output parsing for Quantum ESPRESSO or VASP-style logs.
- Stronger conversion/integrity tooling for HDF5 to Parquet and dtype policy.
- Verified WTF-P MCP schema before the manuscript case can be scored.

This should guide marketplace pack expansion. Each benchmark case should come with a pack, experts, tools/skills, fixture or fetch recipe, and a scoring expectation.

## Presentation Framing

This should be presented as the next-stage benchmark plan, not as something already implemented. The message is:

CLIO is moving from "can route among experts" to "can prove hierarchy matters across scientific workflows." The benchmark cases require decomposition, sync delegation, fan-out/merge, recovery, provenance, and independent verification. Each case has a flat baseline and a hierarchical run so we can measure the value of the architecture.

The corrected terminology for the slide should be:

- Expert: a specialized loop with prompt, model/provider defaults, tools, skills, and memory scope.
- Agent: a hierarchy of experts instantiated for a domain goal.
- Pack/Blueprint: the file-backed definition of that agent.
- Session: a runtime instantiation of a pack.
- Tier-3/Nanoagent: a bounded worker spawned for fan-out, trials, or policy checks; not simply "any expert at depth 3."

## Recommendation

Use this artifact as the seed for the benchmark roadmap, but revise it before turning it into issues or release claims. The revised version should normalize terminology, pick the first six cases, map each case to existing versus missing CLIO tools, and define the evidence log checks we expect after running the TUI/session end to end.
