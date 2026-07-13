# Case 06: Genomics Memory Follow-Up

Status: not passed.

## Prompt Intent

Ask CLIO in a new session to continue from prior genomics findings in the same
workspace without restating all details.

## Semantics To Prove

- Explicit same-workspace memory search/read evidence.
- No leakage from unrelated workspace sessions.
- Retrieved evidence is compact and cited in the follow-up analysis.
- Active genomics blueprint continues from retrieved facts instead of inventing
  prior results.

## Required Expert Decomposition

- `main`: decides whether the prompt contains explicit same-workspace intent.
- `memory`: searches prior same-workspace sessions and returns compact evidence
  with source session IDs.
- `genomics`: continues the scientific analysis from retrieved facts.
- `audit`: verifies no unrelated workspace evidence was used.

This is not a memory-tool unit test. It must be a scientific follow-up where
the retrieved evidence changes the genomics answer.

## Current Core Problem

Focused memory-scope proofs exist, but not as part of a real scientific
follow-up workflow.
