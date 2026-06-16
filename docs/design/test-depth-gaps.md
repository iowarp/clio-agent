# Test-depth gap analysis + fix plan (distributable-expert-runtime)

8-agent adversarial gap analysis (2026-06-16): 37 suspected bugs (19 high), 7 real-ALCF
deep-test designs. Tracking the fix work here. Confirmed bugs marked ✅REPRO.

## Tier 1 — confirmed / high-severity correctness bugs (flaky-for-real)

### cee_transport (CONFIRMED via probe — 3 bugs in one run)
- [ ] ✅REPRO **Handler exception hangs the parent** — `serve_one` lets the child exception
  propagate, never publishes a result → `invoke` waits the full timeout. FIX: catch → publish
  `status="failed"`.
- [ ] ✅REPRO **One failing child kills the whole worker loop** — `run_worker` dies on the
  unhandled exception. FIX: isolate per-rid failures.
- [ ] ✅REPRO **Orphaned `.req` blob leaked on timeout**. FIX: `discard(rid)` on timeout + raise clear error.
- [ ] **Corrupted JSON blob** not caught → `serve_one` silently returns None, request re-served forever. FIX: drain as failed.
- [ ] **Concurrent workers double-execute same rid** — no claim. FIX: best-effort claim + document at-least-once (exactly-once needs a clio-core CAS/lease, cluster #659).

### _dynamic_agent_lm_config
- [ ] **Unknown/typo preset id silently falls back to LM Studio** (e.g. `argonne_sohpia` → localhost:1234). FIX: validate; raise/warn on an unknown provider that's neither a preset id nor a known kind.
- [ ] **Context window not propagated** for a per-expert model that differs from base (narrow condition at app.py:4088). Verify the `_resolve_expert_context_window` ladder actually covers it; widen if not.

### background_tasks
- [ ] **Unbounded memory growth** — records never evicted. FIX: add `clear()`/eviction.
- [ ] **wait(until=…, timeout=None)** can hang forever. FIX: guard/document.

## Tier 2 — resolve_model edge cases (offline, cheap; a.2 GPU live deferred)
- [ ] whitespace in model_id silently non-matches; trailing slash → empty basename; multiple slashes; basename collision returns first silently.

## Tier 3 — REAL-ALCF / REAL-CTE deep tests (the main ask — exploit ALCF)
- [ ] **Full-app `CLIO_EXPERT_INVOKER=loopback` on real ALCF** — does the settle loop survive the lossy loopback prediction (no trajectory/tools)? (biggest unknown)
- [ ] **Multi-expert blueprint, two distinct ALCF models** — each expert hits its declared provider on the REAL settle loop (assert endpoint via trace).
- [ ] **Concurrent N children over real CTE** (stress, unique markers, no cross-talk, no orphans).
- [ ] **Parent continues after child failure** over real CTE (timeout → graceful, non-empty answer).
- [ ] **Tool-backed child trajectory crossing the boundary**.

## Tier 4 — cee_server
- [ ] `::` separator injection (scope/name collision). FIX: encode/validate.
- [ ] **CTE backend + BM25 untested** (tests are LocalFS-only). Add integration test on real CTE.
- [ ] concurrent publishes to same (scope,name).
