# Per-expert models and multi-model routing

Each expert can run on its own model. An expert's `.md` declares a provider and a
model; clio resolves that to an endpoint, carries the right auth, and sends the
model on the wire. Two serving shapes share one declaration.

## Declaring an expert's model

In the expert's `.md` frontmatter:

```yaml
---
id: analysis
provider: argonne_metis
model: gpt-oss-120b
---
```

- `provider` is a registry **preset id** (e.g. `argonne_sophia`, `argonne_metis`,
  `lm_studio`, `openai`) **or** a wire **kind** (`argonne`, `openai`, `lm_studio`).
  A preset id resolves to its own `api_base` and auth — so two experts on presets
  that share a kind (both `argonne`) still reach distinct endpoints.
- `model` is the model the endpoint serves. Omit it to use the preset's suggested
  model.
- An expert that declares neither inherits the run's base model.

## Shape A — multiple endpoints (one model each)

Pin each expert to a distinct provider preset; each has its own endpoint and auth.
For example a data expert on `argonne_sophia` (gpt-oss-120b) and an analysis expert
on `argonne_metis` — two ALCF gateways, two models, in a single run. This is the
heterogeneous-team shape and clio routes it natively: the preset id is resolved to
its own endpoint even when several presets share a wire kind.

## Shape B — single endpoint, multiple models (a router)

Point several experts at one endpoint that serves multiple models and routes by the
OpenAI `model` field:

- **llama.cpp native router** (build from Dec 2025+): run `llama-server` *without*
  `-m`. It discovers models under `--models-dir` / `--models-preset`, JIT-loads one
  on first request, and keeps up to `--models-max` (default 4) resident with LRU
  eviction. Clients route by sending the model name.
- **LM Studio**: JIT loading does the same behind `http://localhost:1234/v1`.

For clio these are just a provider whose `api_base` is the router; its experts
declare different `model`s. clio sends the model field and the router loads/routes —
no dispatch layer in clio.

Validate a declared model is actually served before the call:
`HandshakeReport.resolve_model(model_id)` matches exactly or by basename (a declared
`openai/gpt-oss-120b` matches a served `gpt-oss-120b`); `None` means the endpoint
does not serve it, and `available_model_ids()` lists what it does — a fail-fast
signal instead of a confusing error at call time.

## The VRAM caveat (Shape B)

A router holds only so many models in GPU memory at once. If experts alternate
between models that don't co-fit, every switch pays a full unload+reload (thrash) or
runs out of memory. Size the resident set to the experts you run concurrently:

- llama.cpp: raise `--models-max` to cover the set (VRAM permitting).
- LM Studio: turn Auto-Evict off and `lms load` each model so they stay pinned.

N co-resident models need roughly N× the per-model VRAM; consumer GPUs realistically
hold one or two.

## Context window

Each model's context window — the auto-compaction denominator — resolves from the
provider handshake, then `litellm`, then the static model-limits table, **per
expert**. A smaller-window model therefore compacts sooner than a larger one in the
same run.
