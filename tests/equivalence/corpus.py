"""Corpus governance for the equivalence sweep (design §4.1.C).

Real session ledgers persist FULL content and secrets have historically reached the
store, so committing raw captures = credentials in git + LFS quota + staleness. This
module implements the two-part policy the brief mandates:

* **(a) A REDACTED, committed corpus.** :func:`redact_ledger` runs a
  length-preserving redaction over a handful of real ledgers — text CONTENT becomes
  placeholder bytes while every structural signal (ids, kinds, roles, types,
  timestamps, numeric fields, part shape) is preserved. The redaction is proven
  SHAPE-preserving (:func:`normalized_shape`) so a normalizer treats a redacted
  ledger and its original identically in shape — the redacted corpus is a faithful
  stand-in for the gate. The redacted files live in ``tests/equivalence/corpus/`` and
  ARE committed.
* **(b) A local full-corpus pointer.** :func:`local_corpus` reads the full ~60-ledger
  local corpus from the ``CLIO_EQUIV_CORPUS`` directory (NOT committed) so the sweep
  runs at full fidelity on a developer machine without any raw bytes entering git.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

#: The committed redacted corpus directory (this package's ``redacted_corpus/``).
#: Named distinctly from this ``corpus`` MODULE so the two never shadow on import.
COMMITTED_CORPUS_DIR = Path(__file__).parent / "redacted_corpus"

#: Env var naming a directory of raw ``sess_*.json`` ledgers for the full local sweep.
LOCAL_CORPUS_ENV = "CLIO_EQUIV_CORPUS"

# --------------------------------------------------------------------------- #
# Redaction — length-preserving, structure-preserving
# --------------------------------------------------------------------------- #

#: Dict keys whose STRING values are STRUCTURAL — ids, discriminators, enums, agent
#: refs, model refs, timestamps — and are preserved VERBATIM. EVERY other string leaf
#: (text, paths, tool args, metadata values, tool names, free prose) is redacted.
#: This "redact-unless-structural" default guarantees the committed corpus cannot
#: leak content/secrets: a leak requires ADDING a key here, never forgetting one.
_STRUCTURAL_KEYS: frozenset[str] = frozenset(
    {
        # identity
        "id",
        "part_id",
        "call_id",
        "message_id",
        "session_id",
        "turn_id",
        "parent_session_id",
        "source_message_id",
        "attempt_id",
        "question_id",
        "workspace_id",
        "run_span_id",
        "expert_span_id",
        "trace_id",
        "turn",
        "parent_id",
        "source_id",
        "user_message_id",
        "assistant_message_id",
        # discriminators / enums
        "type",
        "kind",
        "role",
        "status",
        "stage",
        "threshold_state",
        "source",
        "source_kind",
        "mode",
        "edit_mode",
        "routing_mode",
        "execution_path",
        "stream_source",
        "media_type",
        "auth_method",
        "error",  # ErrorInfo taxonomy TAG (not the prose ``message``)
        "stop_reason",
        "scope",
        # agent / model refs (identifiers, not content)
        "agent_id",
        "parent_agent",
        "child_agent",
        "selected_agent",
        "specialization",
        "provider_id",
        "model_id",
        "variant",
        "provider",
        "backend",
        # timestamps (when, not what)
        "created_at",
        "updated_at",
        "added_at",
        "last_modified",
        "expires_at",
        "accepted_at",
    }
)


def _redact_scalar(value: str) -> str:
    """Length-preserving placeholder: every non-whitespace char → 'x', whitespace
    kept (so multi-line structure and token-ish length survive for the normalizers).

    Length preservation matters: the persistence normalizer's ``to_wire`` uses
    ``exclude_defaults``, so a value must stay non-empty (and non-default) to keep
    the SAME wire keys — redacting to ``""`` would DROP a field and change the shape.
    """

    return "".join(" " if ch.isspace() else "x" for ch in value)


def _redact(value: Any, *, key: str | None) -> Any:
    """Recursively redact every string leaf whose key is not structural.

    Dict KEYS are always preserved (they are the structure); only VALUES are
    considered. A structural-key string is kept verbatim; any other non-empty string
    is redacted length-preserving. Numbers/bools/null are always kept.
    """

    if isinstance(value, dict):
        return {k: _redact(v, key=k) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v, key=key) for v in value]
    if isinstance(value, str) and value and key not in _STRUCTURAL_KEYS:
        return _redact_scalar(value)
    return value


def redact_message(payload: dict[str, Any]) -> dict[str, Any]:
    """Redact one persisted message payload (a row of a ledger file)."""

    return {k: _redact(v, key=k) for k, v in payload.items()}


def redact_ledger(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Redact a whole session ledger (list of message payloads)."""

    return [redact_message(row) for row in rows]


# --------------------------------------------------------------------------- #
# Shape extraction — proves redaction is SHAPE-preserving under a normalizer
# --------------------------------------------------------------------------- #


def normalized_shape(value: Any) -> Any:
    """The structural skeleton of a (normalized) value: dict keys + list lengths +
    leaf TYPES, with all scalar VALUES erased.

    ``normalized_shape(normalize(original)) == normalized_shape(normalize(redacted))``
    is the redaction-fidelity invariant: redaction changed content VALUES only, never
    keys/types/list-lengths — so any normalizer (which is a structural projection)
    sees an identical shape.
    """

    if isinstance(value, dict):
        return {k: normalized_shape(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [normalized_shape(v) for v in value]
    return type(value).__name__


# --------------------------------------------------------------------------- #
# Loaders
# --------------------------------------------------------------------------- #


def _load_ledger_file(path: Path) -> list[dict[str, Any]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"ledger {path} is not a JSON list")
    return [row for row in raw if isinstance(row, dict)]


def committed_corpus() -> list[tuple[str, list[dict[str, Any]]]]:
    """The committed REDACTED corpus: ``(session_id, rows)`` for each ``*.json``."""

    if not COMMITTED_CORPUS_DIR.is_dir():
        return []
    return [
        (fp.stem, _load_ledger_file(fp)) for fp in sorted(COMMITTED_CORPUS_DIR.glob("*.json"))
    ]


def local_corpus() -> Optional[list[tuple[str, list[dict[str, Any]]]]]:
    """The full local corpus from ``$CLIO_EQUIV_CORPUS``, or ``None`` when unset.

    Returns ``None`` (not ``[]``) when the env var is unset so a caller can tell
    "no local corpus configured" from "configured but empty".
    """

    root = os.environ.get(LOCAL_CORPUS_ENV)
    if not root:
        return None
    root_path = Path(root)
    if not root_path.is_dir():
        raise FileNotFoundError(f"{LOCAL_CORPUS_ENV}={root!r} is not a directory")
    return [(fp.stem, _load_ledger_file(fp)) for fp in sorted(root_path.glob("*.json"))]


def sweep_corpus() -> list[tuple[str, list[dict[str, Any]]]]:
    """The corpus the sweep runs over: the full local corpus when ``$CLIO_EQUIV_CORPUS``
    is set, else the committed redacted corpus. So CI sweeps the redacted stand-in and
    a developer machine sweeps all ~60 real ledgers with one env var."""

    local = local_corpus()
    if local is not None:
        return local
    return committed_corpus()
