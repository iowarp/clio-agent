# Redacted equivalence corpus (committed)

These `redacted_*.json` files are the **committed, redacted** fixture corpus for the
S0 equivalence harness (design `docs/design/unified-arc-highway.md` §4.1.C). Each is a
real session ledger from the local `.clio/agent/messages` store, passed through the
`tests/equivalence/corpus.py::redact_ledger` pass:

* **content redacted** — every non-structural string leaf (text, prose, tool args,
  paths, metadata values, tool names, diffs) is replaced with a length-preserving
  `x`/whitespace placeholder. The redaction is *redact-unless-structural*: a value
  survives only if its key is in `corpus._STRUCTURAL_KEYS`, so a leak requires
  ADDING a key, never forgetting one.
* **structure preserved** — ids, kinds, roles, types, discriminators, enums, agent/
  model refs, and timestamps are kept verbatim, so the ledger's SHAPE is intact.

The redaction is proven **shape-preserving under every normalizer**
(`test_redaction.py`): `normalized_shape(normalize(original)) ==
normalized_shape(normalize(redacted))`. So the harness treats a redacted ledger and
its original identically in shape — the redacted corpus is a faithful stand-in for
the gate while carrying no real content or secrets.

## Regenerating

Do NOT hand-edit these files. To refresh from the live local store, re-run the
generator against `.clio/agent/messages` (it re-asserts shape-preservation and
scans for leaks before writing).

## Full local sweep (not committed)

The full ~60-ledger local corpus is **never committed** (35 MB, possibly
secret-bearing). Point `CLIO_EQUIV_CORPUS` at a directory of raw `sess_*.json`
ledgers and the sweep runs over all of them at full fidelity:

```
CLIO_EQUIV_CORPUS=/path/to/.clio/agent/messages uv run pytest tests/test_equivalence/test_persistence_corpus.py
```
