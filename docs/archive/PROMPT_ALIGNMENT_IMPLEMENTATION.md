# Prompt Alignment Implementation

Tracking issue: https://github.com/iowarp/clio-agent/issues/334

This document records the completed backend prompt-alignment pass. The public
reference matrix remains in [PROMPT_ALIGNMENT_REFERENCE_MATRIX.md](PROMPT_ALIGNMENT_REFERENCE_MATRIX.md);
this file describes what CLIO now enforces in code.

## Built-In Prompt Profiles

Built-in prompts enter CLIO through `PromptRegistry` as `PromptDefinition`
objects. Each built-in family now exposes these behavioral profiles:

| Profile | Runtime Intent |
| --- | --- |
| `default` | Baseline CLIO grounding: declared capabilities only, tool telemetry as evidence, exact scientific identifiers, and visible provenance. |
| `heavy` | Larger-model scientific workflows: deliberate delegation checks, richer evidence separation, explicit fallback/retry/permission/context warnings. |
| `light` | Lower-latency operation: concise routing, quick declared-tool or expert selection, structured follow-up when input is missing. |
| `small_model` | Smaller models: narrow action spaces, explicit schemas, bounded failure, and strict JSON where planner output requires it. |
| `fine_tuned` | CLIO-trained models: minimal role reminders while retaining non-negotiable capability, evidence, and provenance guardrails. |
| `debug` | Benchmark and development runs: provenance-oriented details without changing user-facing truth. |

## Prompt Families

The alignment pass covers:

- `clio.main.planner`
- `clio.main.answer`
- `clio.chat`
- `clio.expert.data`
- `clio.expert.analysis`
- `clio.expert.visualization`

Each family carries metadata linking it to the public reference matrix and a
set of family requirements. The runtime API exposes that metadata through
`GET /v1/prompts` and `GET /v1/prompts/{id}`.

## Non-Negotiable Behaviors

All built-in profiles reinforce these backend truths:

- Tool claims require telemetry or explicit tool/expert observations.
- Planner actions may use only declared tools and experts.
- Hierarchical delegation must match the child expert's declared scope and
  tool surface.
- Context files, memory search, context frames, prompt provenance, retry
  attempts, permission decisions, and model/provider fallback may be mentioned
  only when runtime metadata proves they happened.
- Unsupported commands, voice, missing tools, provider failures, and permission
  denials must be surfaced as capability/error state, not converted into a
  confident answer.

## Verification

Regression coverage:

- `tests/test_core/test_prompt_registry.py::test_builtin_prompt_profiles_encode_alignment_requirements`
- `tests/test_core/test_prompt_registry.py::test_prompt_alignment_profile_resolution_keeps_builtin_provenance`
- `tests/test_gact/test_prompts_api.py::test_builtin_prompts_are_listed_and_resolvable`
- `tests/test_docs/test_prompt_alignment_reference_matrix.py`

These tests prove that aligned profiles are available through the registry and
GACT prompt API, that profile provenance is carried with resolved prompts, and
that the reference matrix still covers all required prompt families.
