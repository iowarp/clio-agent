# ARC live context plane — release-confidence stress verdict

Outcome of the exhaustive stress battery (workflow `arc-live-plane-stress` + the fd-2
fix + the live-ALCF run). The synthesis agent was cut off by a session limit; this is
the verdict written from the completed results.

## Confidence: HIGH — GO for the live-plane core

The live context plane (SegmentStore, the four ops, render/render_keys, as-of-T,
ARC/Trace separation + replay, auto-compaction, and the `_RetainingReAct` read/write
seam) is exercised hard, on both backends and on real ALCF inference, with no
outstanding correctness defects.

## Coverage (all green)

| Module | Result |
|---|---|
| `test_stress_backend_parity.py` | **32/32** — LocalFS vs CTE **byte-identical** across render_keys / render_text / tokens_by_kind / scan_scopes, incl. binary-hostile (non-UTF-8) + 200KB content, multi-scope/session, the real `_RetainingReAct` loop, and persistence. The backend swap is provably invisible. |
| `test_stress_render_fuzz.py` | 198 seeded cases **+ brute force of ALL 5,460 kind-sequences len 1–6, zero invariant violations** — no key-overwrite loss (the consecutive-observation bug class is dead), gapless indices, byte-equality with stock dspy, as-of-T monotonic visibility. |
| `test_stress_replay_audit.py` | 18 — the durable Trace fully reconstructs ARC (incl. CTE-backed); `trace_ref` matches; as-of-T historical replay correct. |
| `test_stress_concurrency.py` | green — concurrent appends/deletes/renders across threads; no lost writes, monotonic logical_time. |
| `test_stress_cte_scale.py` | green — hundreds of segments, large payloads, scan, clear, repeated init over the one-time runtime. |
| `test_stress_live_alcf.py` | **24/24 on real ALCF** `gpt-oss-120b` — needle-in-haystack at start/mid/end, **partial delete** (one of N gone, others recalled), **as-of-T time-travel**, **needle survives compaction**, ≥4 random unguessable codes. ARC IS the context, stressed. |

Full `tests/test_arc/` suite: **494 passed** (offline) + **24 live**.

## Bugs found (the point of the battery) — 1, fixed

- **[HIGH] CTE init silently aborted the host process.** `CTEStore._ensure_runtime`'s
  `dup2(devnull, 2)` stderr-silencing could abort the interpreter (exit 1, zero output)
  under pytest fd-capture + ambient CTE shared-memory state — a CI-invisible crash that
  also hit the pre-existing CTE test. **Fixed** (commit `50793ee`): removed the fd-2
  redirection; `CTP_LOG_LEVEL=error` quiets the C++ logging. Verified 5/5 clean exits.

## Open / follow-ups (not release-blockers)

- CTE shared-memory teardown hygiene: a hard crash can leave `/dev/shm` / `/tmp/chimaera_*`
  residue that the *next* init auto-reaps. A `clio doctor` reaper would harden multi-run
  CI; low severity (the next init recovers). Worth a follow-up.
- Multi-worker (`uvicorn --workers >1`) CTE init is not validated — single-worker is the
  documented mode (the in-process runtime is one-per-process).
- Durable CTE tier (#666) and the post-1.0 planes (#664/#665) remain as tracked.

## Go/no-go

**GO** for the ARC live context plane (single-node, v1) — correctness is proven across
backends, exhaustive fuzzing, replay, concurrency, scale, and real inference, and the one
robustness bug surfaced is fixed. Remaining 1.0 scope is Thread D (clio-core CEE MCP /
semantic discovery), in progress.
