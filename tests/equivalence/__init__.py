"""S0 — the standing equivalence harness for the unified-ARC highway (#737/#893).

This package is the executable form of ``docs/design/unified-arc-highway.md`` §4.1
(the F3 "silent divergence" gate). It gates every later store-collapse slice: a
projection may not become a consumer until its output is proven **equivalent** to
today's stored/served form under the per-surface normalization spec (§4.1.A), on
real sessions (§4.1.B).

Module layout choice (documented per the S0 brief):

* This is a **test-only** package (``tests/equivalence/``), NOT ``src`` production
  code. The projections it gates live in ``src`` and change slice-by-slice; the
  *normalizers/differ/masking* here are a comparison **instrument** for the gate,
  not a runtime dependency of the server. Nothing in ``src`` imports this package,
  so it carries no production weight and is free to be strict. If a future slice
  needs a normalizer at runtime (it should not — the server emits the clean stream,
  it never diffs itself), that normalizer graduates into ``src`` at that point.
* The files here are deliberately NOT named ``test_*.py`` so pytest does not collect
  them as tests. The pytest proofs that EXERCISE this harness live in
  ``tests/test_equivalence/``.

The four frozen surfaces (§1, §4.1.A) and their normalizers (:mod:`.normalizers`):

============  ===================================================================
Surface       Normalizer / what "equivalent" means
============  ===================================================================
SSE (1.2)     coalesced ``final_text``-authoritative stream; mask id/created_at/
              duration_ms/tokens/cost_usd; exclude delta/heartbeat/permission.*/
              delegate.* timing; assert event-type PRESENCE (drop-detection).
persistence   re-parse ``Message(**payload).to_wire()`` — NEVER raw-byte diffs of
(1.3)         the stored files (the file is ``model_dump``+sorted-json, not
              ``to_wire``).
context (1.4) byte-identical ``render_working_set`` (internal, no wire masking).
trace (1.6)   replay-to-T == live-at-T on the log clock; mask occurred_at/event_id.
============  ===================================================================
"""

from __future__ import annotations
