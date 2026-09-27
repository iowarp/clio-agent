# Agents that onboard unseen datasets

## Why not a format-specific MCP
If an agent needs code changes whenever the data changes, it isn't adaptive. It's a complex script with a chat front end. ORNL's OPAL agent shows the trap:
- A custom bundle loader carried guard-rails for a weak model writing one-shot pandas.
- It was coupled to one export version.
- It still missed real data problems, which generic profiling found in one session.

**Scientific exports usually describe themselves** (manifest, column catalogs, README, docs). Teach the agent to read them at runtime instead of restating them in code.

**Pattern:**
- **MCPs = generic verbs:** shell/uv, DuckDB/Parquet query, plotting, geometry, imaging.
- **Knowledge = skills:** checks and methods.
- **Deterministic helpers = scripts bundled in skills.**
- **The per-dataset adapter is an *output* of the agent:** a saved loader plus an experiment card.

## First contact with a dataset (L0 onboarding skill)
1. **Read the self-description first:** README, manifest, docs, column catalogs. Keep what's *stated* (docs), *checked* (verified in data) and *inferred* (your judgement) apart in everything you report.
2. **Take an inventory:** files, sizes, formats; what one row means in each table; keys; whether declared counts match actual counts.
3. **Audit** (bundled scripts, deterministic):
   - **missing-value encodings:** max-float / ±1e300, -9999, impossible zeros, empty strings in text-typed columns;
   - **column health:** all-empty columns, constant columns, **near-duplicate column names** (labels differing only by rounding, e.g. a second, empty, text-typed copy of each numeric band column);
   - **stringly-typed data:** list-like and date-like strings;
   - **flag–data agreement:** a flag on about 99% of rows is noise, and flags can miss most of the real problems;
   - **key format consistency across tables:** the same ID encoded differently, or a column named like a key that shares no values with its supposed partner;
   - **physical plausibility:** cross-check the same quantity between instruments or methods (that's how a wrong unit scale shows up);
   - **time series:** plateaus can be instrument limits (check crops/bounding boxes against frame edges), irregular sampling, several readings per period;
   - **design:** balance, how factors are encoded (dose vs category), and what's **absent** (treatment meaning, units). Absent semantics become questions for the owners, never guesses.
4. **Joins:** coverage differs by source, so use outer joins, compare row counts before and after, and watch for column collisions.
5. **Write it down:**
   - **Experiment card:** `<data_root>/.clio/experiment-card.md`, keyed by the manifest hash + format version. It holds facts, traps found, open questions, and *proposed* lessons phrased as checks.
   - **Loader:** `<data_root>/.clio/loader.py`, a PEP 723 `uv` script with pinned dependencies, idempotent, with output hashes recorded in the card.
   - **Validated views:** `<data_root>/.clio/views/`.
   - **If the data root isn't writable** (a shared facility mount), key the card by hash under the workspace instead.
6. **Next session:** find and reuse the card; don't re-profile. If the manifest hash changed, regenerate.

**Context isolation:** profiling floods the context. Running onboarding in a child agent (`spawn_subagent_with_skill`) and getting back only the card and loader path is a real win. **The parent re-runs the loader and compares hashes before trusting the card.**

## Lessons-learned promotion
- **Cards hold facts.** Skills hold checks.
- **Propose** a new check when a card finding looks like it would recur. **Promote** it only with evidence: seen in two or more instances, or stated in the format docs. Don't edit skills silently. (Today promotions are logged; eventually clio does the approving.)
- **Phrase checks generically.** No counts, IDs or magnitudes; the skill-literal lint enforces this.

## Evals for a data agent
- **Trap-catch rate on first contact** with an unseen instance (score against a known trap list).
- **Card reuse:** the second session doesn't re-profile.
- **Loader determinism:** the same output hashes on re-run.
- **Refusals / asks:** unknown treatment semantics and suspicious unit scales are asked about or flagged, not assumed.
- **Out-of-contract refusal:** e.g. a synthetic next format version with a renamed column.
- **Held-out sources:**
  - real second and third instances;
  - **mutated copies** of the first (recoded factors, an unbalanced subset, a dropped source, renamed columns, a sentinel moved to another table) made by a variant script that copies tables, manifest and docs but never heavy assets, and never writes into the source.
- **The cheapest test that could prove the design wrong:** onboard instance A, then instance B, with **only the L0 skills loaded**, and measure what the L2 skills actually need to add. Write the L2 skills *after* this.

## Environment gotchas
- **Shell limits:** `shell_bash` defaults to 4,000-character commands and 16 KB output (128 KB max), and **drops** what's past the cap (see issue #1487). For analysis work, raise the `CLIO_SHELL_MAX_COMMAND_CHARS` / `CLIO_SHELL_DEFAULT_OUTPUT_BYTES` / `CLIO_SHELL_MAX_OUTPUT_BYTES` environment variables, and write scripts to files instead of long heredocs.
- **Allowed roots:** the data root must be under `CLIO_ALLOWED_ROOTS` (or be the workspace) to be read and written.
- **clio-kit `parquet` / `pandas` tools:**
  - `parquet` works on single files, with ~16 KB responses;
  - `pandas` is stateless, takes file paths, and returns at most 100 rows;
  - there's no DuckDB in either.
  - For multi-table exports, a skill script running DuckDB or pyarrow through `uv` is usually the better first choice.
- **Kernels:** a persistent Python kernel helps with big tables, but if you add one, make it built into clio (per session, memory-capped, killable), not an MCP outside the sandbox. Shared cached dataframes are a known footgun: generated code mutates them for every later query.
