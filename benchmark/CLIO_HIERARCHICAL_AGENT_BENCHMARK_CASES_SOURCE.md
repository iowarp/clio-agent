# CLIO benchmark — hierarchical agent cases (design spec)

A set of scientifically grounded benchmark cases for CLIO. This is a
**design document** to hand to the Claude Code instance building CLIO;
it contains no code.

## What this is for

CLIO is a hierarchical agent system. The primitives (from the project's
founding definitions):

- **Expert** — an agentic loop with its own prompt, tools, skills, and
  optional model/provider defaults.
- **Agent** — a hierarchy of Experts acting as one domain intelligence.
- **Pack / Blueprint** — the file-backed definition of an Agent
  (experts, hierarchy, tools, skills, prompts, defaults).
- **Session** — an instantiation of a Blueprint.
- **Sync delegation** — a parent expert delegates to a child and waits
  on its result (a barrier / merge point).
- **Tiers** — tier-1 orchestrator (root), tier-2 specialists, tier-3
  nanoagents (narrow leaves, typically spawned in parallel across
  items).

The benchmark must exercise **hierarchical agent semantics**, not
single-tool file inspection. The governing design rule used for every
case below:

> A case qualifies only if its correct answer **requires** the expert
> tree. The sharp form of the test: a flat, single-expert agent should
> produce a **wrong or incomplete** answer (miss the planted defect,
> pick the wrong decomposition, fail to recover), while the intended
> hierarchy gets it right. That gap is the score.

## The semantics each case is built to stress

- **Fan-out / merge (map-reduce):** a specialist spawns tier-3
  nanoagents across items (samples, runs, columns, stations) and a
  parent merges at a barrier.
- **Decision sub-tree:** a specialist spawns *alternative* approaches in
  parallel, evaluates them, and selects — the planner must try and
  choose, not be told.
- **Recovery re-delegation:** on failure (404, unsupported delivery,
  lossy dtype, bad file) the parent re-routes to an alternate child or
  path.
- **Cross-source reconciliation / verification:** one expert's output is
  checked against an independent source or against the raw data (catches
  what a single pass misses).
- **Shared sub-agent reuse:** one sub-tree (the NDP collector) is reused
  verbatim across multiple cases — tests composition, not copy-paste.
- **Partial-failure tolerance:** one branch fails; the agent still
  returns a correct partial result with the failure reported.
- **Routing under ambiguity:** the natural prompt names no expert, tool,
  or path; the orchestrator must infer the tree.

## Prompt rule

Every user prompt is natural and goal-only. It never names an expert, a
tool, a file format, or a path. The planner has to discover the
hierarchy.

## Data hygiene

All real sources below are **no-auth, unattended-fetchable** (no
human-in-the-loop registration), which is required for reproducible
scoring. Synthesis cases ship a deterministic generator the owner
writes; their "real data" is the *format and the known ground truth*,
which is what those domains lack open labeled data for.

------------------------------------------------------------------------

## Shared component — NDP data-collection sub-agent (reused by cases 4, 7, 10)

Defined once, embedded as a child sub-tree in each NDP case. It is
itself a small hierarchy, which is the point — it tests sub-agent reuse
and internal recovery.

- **Sub-blueprint:** `NDPDataCollector`
- **Root:** `CollectorCoordinator` (tier-2 inside the parent agent)
  - `CatalogSearchNanoagent` (tier-3) — query the NDP catalog.
  - `CandidateSelectNanoagent` (tier-3) — choose among results, record
    rationale.
  - `ResourceResolveNanoagent` (tier-3) — resolve the chosen record to a
    resource URL.
  - `DownloadNanoagent` (tier-3) — fetch a **bounded** subset; emit a
    provenance note (record id, URL, size, checksum).
- **Recovery re-delegation built in:** a 404/redirect → retry an
  alternate resource; a non-file delivery (e.g. a stream) → report
  unsupported and try another candidate; an oversize resource → take a
  bounded subset, never the whole.
- **MCP:** the NDP catalog MCP (anonymous, read-only: search /
  get-details / list-orgs). No credentials. NDP discovery access was
  confirmed open in Phase 1.
- **Skill:** `ckan-discovery` — the search→select→resolve→download
  procedure and the provenance-note format, shared across the three NDP
  cases.

------------------------------------------------------------------------

# The cases

Each case lists: domain & complexity; the natural prompt; why it matters
and the semantic it stresses; the Blueprint; the expert tree (with tiers
and branching); the sync-delegation / merge points; tools per expert
(marked **\[existing\]** CLIO data tool, **\[lib\]** common CLI/Python
library wrapped as a tool, or **\[new\]** a CLIO tool to add) and
skills/MCPs; the real data source; expected outputs; minimum benchmark
evidence (the objective pass); failure/recovery to test; and a topology
ablation to compare.

------------------------------------------------------------------------

## Case 1 — Genomics cohort QC

**Domain:** genomics / bioinformatics. **Complexity:** complex.

**Prompt:** "I've got a batch of genome samples from a study cohort.
Some of them look off — check the whole batch for quality problems, flag
anything suspicious, and tell me which samples I should drop before
downstream analysis."

**Why / semantic stressed:** Cohort QC is the gatekeeping step before
any population analysis. It forces **fan-out/merge** (per-sample metrics
computed independently, then pooled into a cohort distribution) plus
**cross-source reconciliation** (inferred attributes checked against the
manifest) plus **partial-failure tolerance** (a corrupt file must not
abort the batch). A flat agent that scans files one-pass will miss the
sample-ID swap, because the swap is only visible when *cohort
statistics* and the *manifest* are cross-checked — which is exactly the
structure being tested.

**Blueprint:** `GenomicsCohortQC`

**Expert tree:**

- tier-1 `CohortQCOrchestrator`
  - tier-2 `PerSampleMetricsExpert`
    - tier-3 `SampleQCNanoagent` (spawned once per sample/shard):
      heterozygosity, het/hom ratio, call rate, X-heterozygosity /
      Y-coverage for inferred sex.
  - tier-2 `CohortOutlierExpert`: pools per-sample metrics, builds
    cohort distributions, flags excess-heterozygosity (contamination),
    low call rate, and inferred-sex outliers.
  - tier-2 `ManifestReconciliationExpert`: cross-checks inferred sex /
    relatedness and the flagged set against the sample manifest; detects
    sex mismatch and sample-ID swap.

**Sync delegation / merge:** `PerSampleMetricsExpert` waits on all
`SampleQCNanoagent`s (barrier). `CohortOutlierExpert` sync-depends on
the pooled metrics. `ManifestReconciliationExpert` sync-depends on both
the per-sample inferences and the cohort flags. Orchestrator merges into
a drop/keep advisory.

**Tools / skills:** `vcf_per_sample_stats` **\[new\]**, `vcf_summarize`
**\[new\]** (cyvcf2/pysam under the hood); `csv_read_table`
**\[existing\]** for the manifest; `plot_histogram` / `plot_bar_chart`
**\[existing\]**. Skill: `cohort-qc-thresholds` (shared metric
definitions and outlier cutoffs).

**Real data:** 1000 Genomes Phase 3, release `20130502` (GRCh37), public
EBI FTP, no auth. Region-subset a few-Mb window of chr20 plus an X
window across ~50 samples (via tabix/bcftools so only the region's bytes
download), plus the sex-annotated pedigree manifest `20130606_g1k.ped`.
Plant four defects into the fetched cohort: a sex mismatch (manifest
flip), a contamination signature (excess het), a low-call-rate sample,
and a sample-ID swap (a male/female pair).

**Expected outputs:** a per-sample QC table, cohort distribution plots,
a flagged list with reasons, and a drop/keep advisory with the manifest
discrepancies noted.

**Minimum evidence (pass):** the flagged set equals exactly the four
planted defective samples, each with the correct reason; clean samples
not flagged; the manifest mismatch and the ID swap are both reported.

**Failure/recovery:** one truncated/corrupt VCF → that nanoagent errors,
the orchestrator reports it and completes QC on the remainder; a missing
manifest column → degrade and report the gap.

**Ablation:** monolithic single-expert QC vs the map-reduce tree; and
the tree with/without `ManifestReconciliationExpert`. The swap should be
caught only when reconciliation is present — quantifies what the
cross-check layer buys.

------------------------------------------------------------------------

## Case 2 — DFT convergence audit

**Domain:** materials science / computational chemistry. **Complexity:**
complex.

**Prompt:** "Here's a folder of electronic-structure calculations I ran.
Tell me which ones actually converged, which ones are junk, and what I
should rerun."

**Why / semantic stressed:** **Fan-out/merge** over a heterogeneous
folder (per-run parsing in parallel, then unified classification) and
**routing under ambiguity** (outputs vary; some are unparseable). A flat
agent tends to trust a manifest label or a single keyword; the tree must
classify from the *parsed iteration series* and refuse to call an
incomplete run "converged."

**Blueprint:** `DFTConvergenceAudit`

**Expert tree:**

- tier-1 `DFTOrchestrator`
  - tier-2 `OutputParsingExpert`
    - tier-3 `RunParseNanoagent` (once per run): extract SCF energy
      series, total forces, iteration counts; flag unrecognized formats
      as unparseable.
  - tier-2 `ConvergenceClassifierExpert`: apply SCF / force /
    energy-drift tests; classify each run as converged / max-iterations
    / oscillating-SCF / force-drift / indeterminate.
  - tier-2 `RerunStrategyExpert`: per failed run, propose concrete rerun
    parameters keyed to the failure mode.

**Sync delegation / merge:** parsing nanoagents merge at
`OutputParsingExpert`; classifier sync-depends on the merged series;
rerun strategy depends on the classification.

**Tools / skills:** `dft_parse_output` **\[new\]** (Quantum ESPRESSO
pw.x text); `plot_scatter` **\[existing\]**; `read_file`
**\[existing\]**. Skill: `scf-convergence-criteria`.

**Real data:** synthesis — a deterministic generator emitting six pw.x
logs in the real QE format (two converged, plus max-iterations,
oscillating-SCF, force-drift, and a truncated run). The QE output format
can be anchored against public QE example reference outputs (QEF/q-e on
GitHub, no auth) so the synthesized logs are format-faithful. Open
labeled "converged vs not" DFT corpora don't exist, which is why this is
synthesis.

**Expected outputs:** a per-run classification table, energy-trace
plots, and a rerun plan.

**Minimum evidence (pass):** all six runs classified to match the
generator's manifest; the truncated run is "indeterminate," never
"converged"; rerun params match the failure mode.

**Failure/recovery:** an unparseable format → flag and proceed; a run
missing its final-energy block → indeterminate.

**Ablation:** single-expert "read each file and label" vs the
parse→classify→advise tree. The single pass should mislabel the
oscillating and truncated runs.

------------------------------------------------------------------------

## Case 3 — Regional climate anomaly detection

**Domain:** climate / weather. **Complexity:** medium-complex.

**Prompt:** "Look at the recent temperature record across this region
and tell me where the anomalies are — anything that deviates
meaningfully from the seasonal baseline — and summarize what stands
out."

**Why / semantic stressed:** Made **regional on purpose** so it forces
**fan-out/merge**: per-station anomaly detection in parallel, then a
regional synthesis that distinguishes a true widespread event from a
single-station glitch. A flat agent looking at one series can't tell a
regional heat dome from a sensor spike; the tree can.

**Blueprint:** `RegionalClimateAnomaly`

**Expert tree:**

- tier-1 `ClimateOrchestrator`
  - tier-2 `StationIngestExpert`: resolve the region to a set of
    stations; load the variable/time window; handle gaps.
  - tier-2 `AnomalyExpert`
    - tier-3 `StationAnomalyNanoagent` (per station): build a
      day-of-year climatology, deseasonalize, z-score, flag windows.
  - tier-2 `RegionalSynthesisExpert`: merge per-station anomalies;
    classify widespread vs isolated; rank, plot, narrate.

**Sync delegation / merge:** anomaly nanoagents merge at
`RegionalSynthesisExpert`, which needs *all* stations before calling an
event regional.

**Tools / skills:** `netcdf_read` / `netcdf_summarize` **\[new\]** (or
`csv_read_table` **\[existing\]** for the daily-summary form);
`plot_scatter` / `plot_summary` **\[existing\]**. Skill:
`deseasonalize-climatology`. **Not sourced from NDP** (Phase 1 found NDP
climate coverage too thin).

**Real data:** NOAA GHCN-Daily, public, no auth. A regional set of
Pacific Northwest stations including Portland (`USW00024229`) over
recent years; the late- June 2021 heat dome is the known regional
anomaly (TMAX up to ~46.7 °C across multiple stations). A synthesis
fallback reproduces the same multi-station window when the live host is
unavailable.

**Expected outputs:** per-station anomaly windows, a regional anomaly
summary, deseasonalized plots with the event highlighted.

**Minimum evidence (pass):** the heat-dome window is detected as a
**regional** event across stations (not just one), within tolerance; the
baseline period is not flagged; a single planted single-station spike is
correctly classified as isolated, not regional.

**Failure/recovery:** missing time steps → handle/flag; a requested
variable that doesn't exist → list available and proceed with the best
match.

**Ablation:** single-station analysis vs the regional fan-out. The
single-station version cannot separate the planted isolated spike from
the real regional event.

------------------------------------------------------------------------

## Case 4 — Wildfire fuels & fire-behavior risk (NDP)

**Domain:** remote sensing / earth-surface. **Complexity:** complex.

**Prompt:** "I'm assessing fire risk for an area. Find relevant fuels
and recent fire data for it, pull what's usable, and give me a risk
picture with the supporting numbers."

**Why / semantic stressed:** **Shared sub-agent reuse** (the NDP
collector) + **recovery re-delegation** (a dead resource → alternate) +
**parallel domain branches that merge** (a fuels-raster branch and a
fire-perimeter-vector branch run independently and combine into one risk
picture). A flat agent fetches one layer and stops; the risk picture
requires both branches.

**Blueprint:** `WildfireRisk`

**Expert tree:**

- tier-1 `WildfireOrchestrator`
  - tier-2 `NDPDataCollector` (shared sub-agent; see above).
  - tier-2 `FuelsRasterExpert`: read fuels GeoTIFFs, compute fuel-load
    metrics.
  - tier-2 `FirePerimeterExpert`: read recent-fire vector data (runs in
    parallel with the fuels branch).
  - tier-2 `RiskSynthesisExpert`: merge fuels + perimeters into a risk
    picture with a map and supporting numbers.

**Sync delegation / merge:** fuels and perimeter branches both
sync-depend on the collector's downloads; `RiskSynthesisExpert` merges
both branches at a barrier.

**Tools / skills:** NDP MCP (via the collector); `geotiff_read`
**\[new\]** (rasterio), `vector_read` **\[new\]** (geopandas);
`plot_summary` / `plot_bar_chart` **\[existing\]**. Skill:
`ckan-discovery` (shared), `fuel-load-metrics`.

**Real data:** NDP catalog (anonymous CKAN) → WIFIRE / LANDFIRE fuels
GeoTIFFs and fire-perimeter vectors. Bounded subset (target rasters are
tens of KB).

**Expected outputs:** fuel-load metrics, a risk map/plot, and a
provenance note.

**Minimum evidence (pass):** the collector resolves the expected fuels
dataset with provenance recorded; a fuel-load metric matches ground
truth within tolerance; the risk picture combines both data types.

**Failure/recovery:** a resource 404 / redirect failure → alternate
resource or a declared gap; multiple search candidates → disambiguation
with rationale.

**Ablation:** collector + single fuels branch vs collector +
fuels∥perimeter merge. Only the merged version produces a defensible
risk picture.

------------------------------------------------------------------------

## Case 5 — Proteomics LFQ differential abundance

**Domain:** proteomics / mass spectrometry. **Complexity:** complex.

**Prompt:** "I have label-free quantitative proteomics results comparing
two conditions. Find the proteins that change significantly between
groups and summarize the findings."

**Why / semantic stressed:** The strongest **decision sub-tree** case.
Choosing a normalization is consequential, and the *right* choice is
empirically discoverable: the planner should spawn alternative
normalizations in parallel, evaluate them against the data's structure,
and select. The known spike-in ground truth makes the choice objectively
scorable — the raw fold-changes are inflated and only good normalization
recovers the true value. A flat agent picks one normalization blindly
and reports an inflated answer.

**Blueprint:** `ProteomicsLFQ`

**Expert tree:**

- tier-1 `ProteomicsOrchestrator`
  - tier-2 `MatrixQCExpert`: load the intensity matrix, remove
    contaminant/decoy rows, profile missingness, declare the
    imputation/exclusion policy.
  - tier-2 `NormalizationSelectionExpert`
    - tier-3 `NormalizationTrialNanoagent` × 3 (median / quantile / VSN,
      in parallel): each normalizes and reports a quality criterion
      (e.g. reduced inter-sample CV on housekeeping proteins).
    - selects the best method (decision point).
  - tier-2 `DifferentialExpert`: using the chosen normalization,
    two-group test with FDR control.
  - tier-2 `ReportExpert`: significant-protein list, volcano plot,
    missingness summary.

**Sync delegation / merge:** the three normalization nanoagents run in
parallel and merge at the selection point; `DifferentialExpert`
sync-depends on the selected matrix.

**Tools / skills:** `csv_read_table` **\[existing\]** (tab-delimited
MaxQuant tables); `lfq_normalize` **\[new\]** (median/quantile/VSN);
`parquet_compute_statistics` **\[existing\]**; `plot_scatter` /
`plot_histogram` **\[existing\]**. Skill: `maxquant-parsing`
(space-delimited sample columns, `CON__`/Reverse contaminant handling).

**Real data (verified open):** CPTAC Study 6 UPS1-in-yeast spike-in,
MaxQuant `proteinGroups.txt` + `peptides.txt`, from the `statOmics/PDA`
repository, branch `data`, path `quantification/cptacAvsB_lab3/` (raw
GitHub, no auth — confirmed fetchable, ~4.6 MB + ~7.1 MB; the
`Intensity 6A_*`/`6B_*` columns are present). Ground truth: the 48 human
UPS1 proteins are the differential set (theoretical log2 fold-change ≈
1.566 for 0.74 vs 0.25 fmol/µL); yeast background is invariant. ~44 of
the 48 are detectable in this subset; raw fold-changes run ~2.2 and
should move toward the theoretical value under good normalization.

**Expected outputs:** the chosen normalization with its justification, a
significant-protein list with fold-change and adjusted p-values, a
volcano plot, a missingness report.

**Minimum evidence (pass):** ≥ half the 48 UPS proteins recovered with
positive direction (6B \> 6A); the selected normalization brings the UPS
median log2FC into ≈ 1.566 ± 0.3; yeast invariant; contaminants removed;
FDR controlled.

**Failure/recovery:** a failed-run sample with pervasive missing values
→ flagged and excluded per the declared policy, not dropped silently.

**Ablation:** this case *is* an ablation — score how well each
normalization sub- strategy recovers the spike-in truth, and whether the
agent's selection matches the best one. Also: single-normalization flat
agent vs the selection sub-tree.

------------------------------------------------------------------------

## Case 6 — HPC I/O performance regression

**Domain:** HPC logs / performance traces. **Complexity:** complex.

**Prompt:** "Two versions of my simulation are giving different run
times. Figure out what changed in the I/O or performance profile and
where the regression is."

**Why / semantic stressed:** **Parallel ingest of two heterogeneous
inputs + merge/ diff** (a barrier where the two profiles must be aligned
before diffing) + **attribution**. A flat agent reading one trace can't
localize a regression; the diff is inherently a two-branch merge.

**Blueprint:** `HPCRegression`

**Expert tree:**

- tier-1 `HPCOrchestrator`
  - tier-2 `TraceIngestExpert`
    - tier-3 `VersionParseNanoagent` × 2 (one per version, possibly
      different trace formats): parse into per-phase metrics; normalize.
  - tier-2 `RegressionDiffExpert`: align and diff the two profiles;
    locate the regressed phase and the metric that moved most; confirm
    other phases stable.
  - tier-2 `RootCauseExpert`: attribute to an I/O pattern change.

**Sync delegation / merge:** the two parse nanoagents merge at
`RegressionDiffExpert` (both required before diffing).

**Tools / skills:** `darshan_parse` **\[new\]** (Darshan text traces);
`plot_bar_chart` / `plot_scatter` **\[existing\]**; `read_file`
**\[existing\]**. Skill: `io-trace-normalize`.

**Real data:** synthesis — paired Darshan-format text traces (baseline
vs regressed) with one injected regression: collective→independent
writes, transfer size 4096→65536, a small-write storm, ≈ +147 % MPIIO
write time, ≈ +18 % run time; plus a differing-format fixture and a
truncated fixture. Darshan is HPC tooling, not an NDP dataset — **not
sourced from NDP**.

**Expected outputs:** an aligned per-phase comparison, the regressed
phase + metric, and a root-cause attribution.

**Minimum evidence (pass):** the regressed phase and metric match the
injected change within tolerance; non-regressed phases reported stable.

**Failure/recovery:** the two versions use differing formats → normalize
or report incomparable fields; a truncated trace → partial analysis with
a caveat.

**Ablation:** single-trace analysis vs the two-branch diff. The
single-trace path cannot localize the regression.

------------------------------------------------------------------------

## Case 7 — Seismic event discovery & hazard summary (NDP)

**Domain:** seismic / geophysics. **Complexity:** complex.

**Prompt:** "For this region and time window, find the relevant seismic
station data, pull it, and give me a summary of notable events and what
they imply for local hazard."

**Why / semantic stressed:** **Shared collector reuse** + **per-station
fan-out/ merge** + **partial-data tolerance** + **unsupported-delivery
recovery**. A flat agent fetches one station and summarizes; a regional
hazard read needs many stations merged, with bad/streaming resources
handled.

**Blueprint:** `SeismicHazard`

**Expert tree:**

- tier-1 `SeismicOrchestrator`
  - tier-2 `NDPDataCollector` (shared sub-agent).
  - tier-2 `EventDetectionExpert`
    - tier-3 `StationEventNanoagent` (per station): detect events,
      compute magnitudes/peaks.
  - tier-2 `HazardSummaryExpert`: merge events, map them, write a
    local-hazard narrative.

**Sync delegation / merge:** event nanoagents merge at
`HazardSummaryExpert`.

**Tools / skills:** NDP MCP (via collector); `waveform_read` **\[new\]**
(obspy optional); `plot_scatter` / `plot_summary` **\[existing\]**.
Skill: `ckan-discovery` (shared), `event-detection`.

**Real data:** NDP catalog → EarthScope per-station data (series +
station GeoJSON), anonymous. Bounded subset.

**Expected outputs:** a ranked event list, an event map, a hazard
narrative, a provenance note.

**Minimum evidence (pass):** the collector resolves the expected station
dataset; detected events match a reference catalog within
distance/magnitude tolerance; provenance recorded.

**Failure/recovery:** a station missing coordinate metadata → excluded
from the map but kept in the event count; an NDP record delivered as a
stream rather than a file → recognized as unsupported delivery and
reported, with an alternate tried.

**Ablation:** single-station vs multi-station fan-out; collector
with/without the unsupported-delivery recovery branch.

------------------------------------------------------------------------

## Case 8 — Tabular drift audit

**Domain:** tabular / data-quality (MLOps). **Complexity:** complex.

**Prompt:** "Here are two snapshots of the same dataset taken months
apart. Tell me what drifted — schema, distributions, data quality — and
whether anything would break a model trained on the old one."

**Why / semantic stressed:** A clean **three-way parallel fan-out +
merge**: schema, distribution, and quality analyses are independent and
run in parallel, then a model-impact expert merges them into one
judgment; the distribution branch itself fans out per column. A flat
agent does one of the three and misses the interaction (e.g. that a
rename looks like a drop+add unless schema and value-overlap are
considered together).

**Blueprint:** `TabularDrift`

**Expert tree:**

- tier-1 `DriftOrchestrator`
  - tier-2 `SchemaDiffExpert`: adds/drops, and rename-by-value-overlap
    (not two unrelated changes). ║ parallel
  - tier-2 `DistributionDriftExpert` ║ parallel
    - tier-3 `ColumnDriftNanoagent` (per shared column): KS / PSI drift
      score.
  - tier-2 `QualityDeltaExpert`: null-rate changes, mixed-type columns,
    cardinality. ║ parallel
  - tier-2 `ModelImpactExpert`: merges all three → break-or-not call.

**Sync delegation / merge:** the three tier-2 branches run in parallel
and merge at `ModelImpactExpert` (barrier).

**Tools / skills:** `parquet_analyze_schema` /
`parquet_compute_statistics` / `csv_read_table` **\[existing\]**;
`drift_stats` **\[new\]** (KS/PSI); `plot_histogram` / `plot_bar_chart`
**\[existing\]**. Skill: `drift-tests`.

**Real data:** synthesis — two snapshots (v1/v2) of one tabular dataset
in CSV and Parquet, with injected drift: an income distribution shift, a
notes null-rate rise (5 %→35 %), a column rename
(`email_addr`→`contact_email` with identical values), a type change
(`score` float→string), a mixed-type column (`device_ver`), one added
and one removed column.

**Expected outputs:** a prioritized drift report (schema +
distribution + quality) with a model-impact verdict.

**Minimum evidence (pass):** drifted columns equal the injected set; the
rename is identified as a rename (not drop+add); the mixed-type column
is profiled without crashing; the model-impact call names the breaking
changes.

**Failure/recovery:** rename vs drop+add disambiguation; a mixed-type
column handled without error.

**Ablation:** sequential single-expert audit vs the three-way parallel
merge; and with/without `SchemaDiffExpert`'s value-overlap check (which
is what catches the rename). Quantifies the value of running the three
analyses jointly.

------------------------------------------------------------------------

## Case 9 — Scientific format bridge with integrity guard

**Domain:** scientific data engineering / visualization. **Complexity:**
medium-complex.

**Prompt:** "I need this scientific dataset in a different format so my
downstream tools can read it — convert it, make sure nothing was lost,
and show me a quick visual to confirm it looks right."

**Why / semantic stressed:** **Dynamic re-delegation** (per-column
lossy/unsafe handling) + a **policy-denial recovery path** + a
reviewable propose-then-apply write. A flat converter silently truncates
the unrepresentable dtypes and reports success; the tree must detect
each problem column, route it to a policy decision, and refuse an
out-of-bounds write.

**Blueprint:** `FormatBridge`

**Expert tree:**

- tier-1 `FormatOrchestrator`
  - tier-2 `SourceInspectExpert`: schema, shapes, dtypes; flag edge-case
    dtypes.
  - tier-2 `ConversionExpert`: propose then apply the conversion.
    - tier-3 `LossyPolicyNanoagent` (spawned per problem column): decide
      flag / skip / ask for complex, float16, uint64-overflow, datetime
      cases.
  - tier-2 `IntegrityExpert`: round-trip verify counts, dtypes, NaNs,
    checksums.
  - tier-2 `VizExpert`: a confirmation plot.

**Sync delegation / merge:** `ConversionExpert` spawns a policy
nanoagent per offending column and waits; `IntegrityExpert` sync-depends
on the applied write.

**Tools / skills:** `hdf5_list_datasets` / `hdf5_analyze_dataset` /
`hdf5_analyze_file` / `hdf5_check_compression` **\[existing\]**;
`parquet_analyze_schema` / `parquet_compute_statistics`
**\[existing\]**; `propose_edit` / `apply_edit_write` **\[existing\]**
(reviewable write, honoring allowed-roots); `format_convert`
**\[new\]**; `plot_summary` **\[existing\]**. Skill:
`dtype-mapping-rules`.

**Real data:** synthesis — a source HDF5 with edge-case dtypes: a
gzip-compressed float column with an embedded NaN (index 13), a UTF-8
string column, a uint64 column whose first value overflows int64, a
float16 column, a complex128 column, and a datetime64 column.

**Expected outputs:** the converted file, an integrity report
(before/after), a list of lossy/unsafe columns with the decision taken,
and a confirmation plot.

**Minimum evidence (pass):** row count preserved; the NaN preserved at
index 13; complex128 and float16 flagged lossy (no faithful target
type); uint64 flagged unsafe (overflow); a conversion targeted outside
the allowed roots is denied and writes nothing.

**Failure/recovery:** an unrepresentable dtype → flagged lossy and
routed to the policy nanoagent rather than truncated; an out-of-root
write → a clean policy denial.

**Ablation:** single-shot converter vs
inspect→convert(+policy)→integrity tree. The single-shot path silently
loses the complex/float16 columns and "passes" its own check — the
failure the tree is designed to expose.

------------------------------------------------------------------------

## Case 10 — Terrain site suitability from lidar (NDP)

**Domain:** geospatial / GIS. **Complexity:** complex.

**Prompt:** "I'm evaluating sites in this area. Find high-resolution
terrain data, pull it, and tell me which locations meet my slope and
elevation constraints."

**Why / semantic stressed:** **Shared collector** + **conditional
re-delegation on delivery form** (a ready DEM vs a raw point cloud
needing gridding routes to different sub-paths) + constraint masking. A
flat agent assumes a ready raster and fails when the data arrives as a
point cloud.

**Blueprint:** `TerrainSuitability`

**Expert tree:**

- tier-1 `TerrainOrchestrator`
  - tier-2 `NDPDataCollector` (shared sub-agent).
  - tier-2 `TerrainDerivationExpert`: if a DEM is delivered, derive
    slope/aspect/ elevation directly; if a point cloud is delivered,
    re-delegate to
    - tier-3 `GriddingNanoagent` (build a DEM), then derive.
  - tier-2 `SuitabilityExpert`: mask cells by the slope and elevation
    constraints; rank candidate sites.

**Sync delegation / merge:** derivation sync-depends on the collector
and, when needed, on the gridding nanoagent; suitability sync-depends on
the derived terrain.

**Tools / skills:** NDP MCP (via collector); `dem_terrain` **\[new\]**
(rasterio), `pointcloud_read` **\[new\]** (laspy); `plot_summary` /
`plot_bar_chart` **\[existing\]**. Skill: `ckan-discovery` (shared),
`terrain-derivation`.

**Real data:** NDP catalog → OpenTopography lidar/DEM holdings (e.g. a
california-arra-lidar or Kaibab-plateau subset), anonymous, bounded
subset (no multi-GB tile when an MB subset demonstrates the metric).

**Expected outputs:** a slope/elevation surface, a suitable-cell mask, a
ranked site list, and a provenance note.

**Minimum evidence (pass):** a slope value matches ground truth within
tolerance; the suitable-cell count matches expectation; provenance
recorded.

**Failure/recovery:** a point-cloud delivery (no ready DEM) → recognized
and gridded rather than skipped; an oversize tile → bounded subset.

**Ablation:** DEM-only path vs the path with the point-cloud→gridding
branch. Only the latter survives the point-cloud delivery.

------------------------------------------------------------------------

## Case 11 — Manuscript results-section authoring (WTF-P)

**Domain:** scientific writing / provenance. **Complexity:** complex.
**Status:** design-ready, with two prerequisites flagged below.

**Prompt:** "I just finished a simulation run. Draft the results section
of my paper from it — pull the key numbers, make the figures, and write
it up so the claims actually match the data."

**Why / semantic stressed:** A **cross-checking / verification loop**: a
writing expert drafts prose, and a separate verifier reconciles every
numeric claim in the draft against the actual simulation values,
flagging any fabrication. Plus **routing-under-ambiguity** — the agent
must recognize the writing tool is a *writing* tool, not an I/O tracer.
A flat agent that lets one expert both compute and write has no
independent check on hallucinated numbers; the verifier branch is the
whole point.

**Blueprint:** `ManuscriptResults`

**Expert tree:**

- tier-1 `ManuscriptOrchestrator`
  - tier-2 `SimDataExpert`: extract the key numbers (means, maxima,
    trends) from the run; produce the authoritative value table.
  - tier-2 `FigureExpert`: build figures from those numbers (parallel
    with drafting inputs).
  - tier-2 `WritingExpert`: draft the results section (WTF-P).
  - tier-2 `ClaimVerifierExpert`: reconcile every numeric claim and
    figure reference in the draft against `SimDataExpert`'s table; flag
    any unsupported claim and loop back if needed.

**Sync delegation / merge:** writing sync-depends on the data table and
figures; verification sync-depends on both the draft and the
authoritative table (a reconciliation barrier; a failed check
re-delegates to `WritingExpert`).

**Tools / skills:** `hdf5_*` **\[existing\]** for the run; `plot_*`
**\[existing\]** for figures; the WTF-P writing MCP
**\[new/external\]**; a `bib` citation tool. Skill: `claim-provenance`
(the rule that every number traces to a `SimDataExpert` value).

**Real data:** synthesis — a small simulation-output HDF5 with known
summary statistics, an expected-numbers manifest (every claimable
statistic), and a minimal `.bib`. Source project:
`github.com/akougkas/wtf-p`.

**Expected outputs:** a results-section draft, the figures, and a
verification report mapping each claim to a data value.

**Minimum evidence (pass):** every numeric claim in the draft traces to
a `SimDataExpert` value (no fabricated statistics); figures correspond
to the cited numbers; a deliberately planted wrong claim is caught by
the verifier.

**Failure/recovery:** the agent must not mistake the writing tool for an
I/O tracer; an unsupported claim → flagged and corrected, not passed
through.

**Two prerequisites before this case can be scored** (honest gaps, not
buried):

1.  The synthesis generator (sim HDF5 + expected-numbers manifest +
    `.bib`) must be written — no Phase 3 dossier produced it.
2.  The WTF-P MCP tool schema must be confirmed firsthand — Phase 1
    flagged it as unverified. The `WritingExpert` tool bindings depend
    on it.

**Ablation:** one expert that both computes and writes vs the writer +
independent verifier. The combined-role version should let the planted
false claim through.

------------------------------------------------------------------------

# How to use this for scoring

Each case ships an objective ground truth (planted defects, spike-in
truth, known event, injected regression/drift, expected numbers). Score
a Session by comparing its observable artifacts to that ground truth. To
prove **hierarchy matters**, run each case in at least two topologies —
the intended tree and a flattened baseline — and report the gap; the
ablation noted per case identifies the specific layer whose removal
should break the answer. A case earns its place in the suite only if
that gap is real.

# Coverage summary

Genomics (1), materials/DFT (2), climate (3), remote sensing/wildfire
(4), proteomics (5), HPC traces (6), seismic (7), tabular/data-quality
(8), scientific viz/format (9), geospatial/lidar (10), scientific
writing (11). NDP-dependent: 4, 7, 10 (three, via the shared collector).
Real open data: 1, 3, 5, and the NDP discovery for 4/7/10. Synthesis
(format real, ground truth known): 2, 6, 8, 9, and the fixtures for 11.
The shared `NDPDataCollector` sub-agent and the per-case ablations are
the two pieces that most directly exercise CLIO's hierarchical semantics
beyond a single chain.
