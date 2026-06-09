"""Model-id normalization shared by every context source.

Provider-reported model ids are messy: a vLLM backend may serve
``meta-llama/Llama-3-70B-Instruct`` while models.dev keys it ``meta/llama-3-70b``
and a curated marketplace entry uses ``llama-3-70b``. To match across these we
generate, for any raw id, an ordered list of progressively-stripped *candidate*
forms and try each against a source's index.

The candidates are, in order of specificity (most specific first):

1. the full id, lowercased (``meta-llama/llama-3-70b-instruct``)
2. everything after the last ``/`` (``llama-3-70b-instruct``)
3. a vendor-stripped basename: like (2) but with a leading ``<vendor>-`` chunk
   removed when the basename starts with a known/echoed vendor token
   (``llama-3-70b-instruct`` -> unchanged here; ``qwen-qwen3-27b`` -> ``qwen3-27b``)

Every form is lowercased and surrounding whitespace stripped. The list is
de-duplicated while preserving order so callers can simply take the first hit.
"""

from __future__ import annotations

#: Vendor tokens that backends sometimes echo as a basename prefix
#: (e.g. ``Qwen/Qwen3-27B`` -> basename ``Qwen3-27B`` whose leading ``qwen`` is
#: redundant). Used only for the optional vendor-stripped basename candidate.
_ECHOED_VENDOR_PREFIXES: tuple[str, ...] = (
    "qwen",
    "llama",
    "gemma",
    "mistral",
    "phi",
    "deepseek",
    "nemotron",
    "gpt",
)


def normalize_id(model_id: str) -> str:
    """Return the canonical, lowercased, whitespace-trimmed form of ``model_id``."""
    return (model_id or "").strip().lower()


def iter_id_candidates(model_id: str) -> list[str]:
    """Return ordered, de-duplicated candidate keys for ``model_id``.

    Most specific first: full id, then the post-slash basename, then a
    vendor-stripped basename. Empty/blank inputs yield an empty list.

    Args:
        model_id: A raw provider-reported model identifier.

    Returns:
        Ordered list of normalized candidate strings to probe against a source.
    """
    base = normalize_id(model_id)
    if not base:
        return []

    candidates: list[str] = [base]

    # Everything after the last '/': "meta-llama/llama-3" -> "llama-3".
    if "/" in base:
        basename = base.rsplit("/", 1)[1].strip()
        if basename:
            candidates.append(basename)
    else:
        basename = base

    # Vendor-stripped basename: drop a leading echoed-vendor token when present,
    # e.g. "qwen-qwen3-27b" -> "qwen3-27b". Only strip when a separator follows
    # so we never mangle a name that merely *starts* with the vendor letters.
    for vendor in _ECHOED_VENDOR_PREFIXES:
        for sep in ("-", "_", "."):
            prefix = f"{vendor}{sep}"
            if basename.startswith(prefix) and len(basename) > len(prefix):
                candidates.append(basename[len(prefix) :])
                break

    # De-duplicate, preserving order.
    seen: set[str] = set()
    ordered: list[str] = []
    for cand in candidates:
        if cand and cand not in seen:
            seen.add(cand)
            ordered.append(cand)
    return ordered
