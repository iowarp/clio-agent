# Artifacts Campaign — Landed Record (2026-07)

Campaign A of the artifacts/sandboxing pair (parent issue
[#966](https://github.com/iowarp/clio-agent/issues/966)): every meaningful session
output — tool-generated files, agent-designated reports, staged datasets, UI
payloads — becomes a first-class, durable, hash-pinned, **versioned** artifact
record with **provenance by construction** (`b = transform(a)`), lineage queryable
both directions, exportable as a bundle that hands the user the scripts that
produced each artifact. The path-string mechanisms this replaced are **deleted,
not preserved**.

Authority docs (in-tree): `docs/design/artifact-provenance-design.md` (v2 —
mechanisms, evidence classes, custody, adversarial-review resolutions) and
`docs/design/artifacts-research-2026-07.md` (prior-art survey + local pathway
inventory). Campaign B (sandboxing / write-confinement) is a separate later
campaign; every degradation here carries a permanent typed label so B's absence is
visible, never silent.

## Landed slices (real merge history)

| Slice | Issue | Delivered | Key commits |
| --- | --- | --- | --- |
| **S1** | #967 | Record model + registry projection + minting floor (observer / fs_write / grounding-hook seams; events trace-only). Storage is a projection over `_emit_semantic_event` — no new store. | `591cd04c`, `504e090a` (review: live W&B dedup, atomic mint, union fold, containment, loop safety), merge `0d5627a0` |
| **S2** | #968 | Wire identity: `artifact.*` on SSE, `resource_link` parts at turn finalize, the `/v1/…/artifacts` read routes, SPEC co-edit №1. | `57d742db`, `399e0e29` (review: exact metadata lock, real byte-parity, honest cursors, streamed bytes, turn-id threading) |
| **S3** | #969 | `create_artifact` tool (the model decides) + the inert `structured_outputs.artifacts` field DELETED; marketplace rider №1. | `4bcaaec2`, `09dd086b` (review: gated content writes, workspace-grounded paths, honest created flags) |
| **S4** | #970 | Version chains, content dedup, aliases — the ONE version-decision point; SPEC co-edit №2. | `b0782f09`, `0859e582` (review: reconciler wired, relink idempotent, honest lease, alias grammar) |
| **S5** | #971 | TransformRecords: tiered environment, used edges, lineage + transform routes; SPEC co-edit №3 + relay-convergence issue. | `7d118854`, `cbe515e9` (review), `a7589c67` (designation-by-result + parent aggregation), `4dfea2e3` (boot-fold placement), `f54c74e5` (trace-only SSE exclusion) |
| **S6** | #972 | CAS: small-artifact ingestion at mint, budget ratchet, reachability GC (roots = pinned aliases + retained sessions + **export manifests**, the S7 stub). | `0b11a1ca`, `2b0fa3d3` (review: verified evictions, visible tmp, TOCTOU re-check, zero-fs finalize) |
| **S7** | #973 | **This slice.** Consumer re-sourcing (evidence grounding + harness/benchmark graders), RO-Crate export + deterministic `reproduce.py` renderer, prose migration, close-out. | (in this PR train) |

## S7 deliverables

1. **Answer grounding re-sourced (deletion item 4).** The `evidence.py`
   heuristics (`_ground_fabricated_local_artifact_paths`,
   `_verified_local_artifact_paths_by_ext`, `_is_remote_artifact_ref`) are DELETED.
   Grounding (`gact/artifacts/grounding.py::ground_answer_artifacts`) now
   validates/rewrites a final answer's fabricated deliverable-path citations
   against the session's **registered artifacts** (registry-sourced, `sha256`,
   `include_children` reach), scoped to the pack schema's declared deliverable
   *extension* vocabulary (a cheap type list, never a path scan). Precision gain:
   the registry's evidence class separates a produced deliverable (`hashed-at-use`)
   from a staged remote input (`authority-asserted` / `external-referenced`), so an
   incidental staged input is never a substitution candidate. A grounding-parity
   suite proves registry-sourced ≥ the old heuristics on the six recorded EarthScope
   corpora + the widget de-domaining case.

2. **Harness / benchmark graders re-sourced (deletion item 5).** `clio_sut._artifacts`
   (the tool-output path scraper), `run_demo_benchmark._artifact_paths` /
   `_visualization_artifact_paths`, and the stress-benchmark `_artifact_paths` are
   DELETED. Each now queries `GET /v1/sessions/{sid}/artifacts?include_children=true`
   (`_registry_artifacts` / `_registry_artifact_paths`) so every benchmark run
   live-tests the artifact contract.

3. **RO-Crate export + `reproduce.py` (item 3, owner extension).**
   `gact/artifacts/export.py` builds an RO-Crate bundle (metadata JSON-LD +
   `data/` bytes + reproduce scripts) for one artifact's lineage or a whole
   session; `GET /v1/artifacts/{id}/export` and `/v1/sessions/{sid}/export/bundle`
   serve it as a zip. TransformRecords serialize as schema.org `CreateAction`s;
   File entities carry PROV `wasGeneratedBy` / `wasRevisionOf`; gap versions map to
   an unknown Agent. `gact/artifacts/reproduce.py` compiles the lineage into a
   deterministic re-run via a per-tool translation registry, each stage ending in an
   executable `assert sha256(output) == <pin>` with an honest per-stage verdict.
   Exports register their shipped hashes as CAS GC roots (closing S6's loop). SPEC
   co-edit №4.

4. **Prose migration (deletion item 6).** `docs/DSPY_BLUEPRINT_EXPERT_RUNTIME.md`,
   `docs/SEMANTIC_EXECUTION_TRACES.md`, and the earthscope/wildfire expert prose are
   rewritten from "hand-compose / cite `/artifacts/…` path strings" to the
   designation channels; marketplace PR №2.

5. **Close-out.** This doc, the CHANGELOG GACT-surface entry, and the baseline-0
   CI guard `scripts/check_no_artifact_scraper_vocabulary.py`.

## Deviations honestly recorded

- **Grounding keeps a schema-derived extension vocabulary.** A purely
  registry-derived vocabulary cannot flag a fabricated deliverable type on a
  data-blocked run whose registry is empty (the honest neutralize case). Grounding
  therefore keeps `WorkflowStateSchema.artifact_extensions` as the *type* scope
  while the registry is the sole source of *which artifacts exist*. This is a
  deliberate hybrid, not the pack frontmatter being load-bearing (a pack declaring
  no extensions grounds nothing; the field is a cheap type list, verified by the
  de-domaining grep-guard which now also scans `artifacts/grounding.py`).
- **Benchmark `_registry_artifact_paths` returns all registered on-disk paths**
  (no evidence-class filter); the grader unit tests inject controlled
  `registry_artifacts` lists (a staged metadata catalog reached via `data_files`,
  deliverables via `artifacts`) to exercise the routing logic. The old scraper's
  ndp-staging-path metadata heuristic does not map to an evidence class, so the
  deleted path-string parsing unit tests were removed with the function (their
  coverage moves to the registry minting + S7 suites).
- **The gate sagas (S2–S6, carried from the slice PRs):** the trace-only
  provenance-event SSE exclusion (#971 C5), the boot-fold placement + typed
  boot-stall so turns never pay the fold (#971), the designation-by-result channel
  + parent aggregation added when the S5 gate exposed empty parent records, the
  clio-core#793 CTE interaction, the SSE override leak, and the S6 verified-eviction
  / TOCTOU re-check review findings are recorded in their slice commits above.
- **Scope held for later campaigns:** Campaign B (write confinement, `isolated`
  custody), gact-tui client rendering (artifacts panel / Recreate tab — wire
  contract only this campaign), mcpui/a2ui rendering (`ui_payload` = record +
  delivery only), federation transport (relay-compatible shapes; S5 filed the
  convergence issue), the AGENTIC replay runner (re-handing a task to a live agent —
  a session-runtime feature), and the future `plan`-kind producer.

## Typed follow-ups

- Relay convergence: `ArtifactUse` is frozen `extra='forbid'` with no metadata, so
  clio's mechanism/evidence/environment extras ride `ArtifactRef.metadata`
  (`clio.provenance.v1`) until relay's schema converges (filed in S5).
- The AGENTIC replay runner + the UI Recreate tab's three states (copy the script /
  download the bundle / agentic replay) are a post-campaign session-runtime slice.
- Campaign B (sandboxing) adds the `isolated` custody class and the traced
  write-confinement mechanism the permanent typed labels here anticipate.
