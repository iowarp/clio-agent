"""P2.7 hook trust — content fingerprints + a colocated trusted-fingerprint store.

A hook is operator-declared code (a subprocess command + script, an HTTP endpoint, a
prompt). "Trust" here answers exactly one question: has the CONTENT of a
previously-seen hook changed since it was last trusted? The threat model is a
repo-shipped hook silently rewritten by a ``git pull`` — new behaviour running under
an old approval. So each loaded hook gets a content-hash FINGERPRINT keyed by its
stable ``id``; on load :func:`evaluate_trust` compares it to the persisted trusted
fingerprint:

* fingerprint UNCHANGED  -> ``trusted``   (the hook runs);
* fingerprint CHANGED    -> ``untrusted`` (the hook does NOT run silently — the
  dispatcher drops it from ``matching`` and a typed reason is recorded so the change
  is queryable; the operator must re-approve, i.e. re-persist the new fingerprint);
* fingerprint UNSEEN     -> trust-on-first-use: ``trusted`` + persist. A brand-new
  hook the operator just authored is not a "change"; the invariant this module
  protects is "a hook that CHANGED does not run silently", not "no hook ever runs".

Persistence is a small JSON map (``{id: fingerprint}``) COLOCATED with the hook
config — it IS hook config, not agent state, so it introduces no fifth store
(RULE 4 / #737). Only the discovery/load path (:func:`build_hook_dispatcher`)
evaluates trust; a directly-constructed :class:`HookEntry` (the many unit tests)
defaults to ``trust="trusted"`` and is never fingerprinted, so trust is a property of
LOADED hooks only.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path

from clio_agent.gact.hooks.config import HookEntry
from clio_agent.gact.hooks.wire import record_hook_reason

logger = logging.getLogger(__name__)

TRUST_TRUSTED = "trusted"
TRUST_UNTRUSTED = "untrusted"


def _config_material(entry: HookEntry) -> dict[str, object]:
    """Return the SECURITY-relevant declarative config of a hook, in a canonical form.

    ``enabled`` is deliberately EXCLUDED: toggling a hook on/off is a run-state change,
    not a content change, and must not re-trigger a trust re-prompt. Everything that
    determines WHAT the hook does or WHEN it fires (its command/args/url/prompt, its
    match predicate, its timeout, fail-closed posture, and the events it runs on) is
    included, so any edit to behaviour flips the fingerprint.
    """

    match = entry.match
    return {
        "id": entry.id,
        "on": sorted(entry.on),
        "scope": entry.scope,
        "match": {
            "tool": match.tool.pattern if match.tool is not None else None,
            "annotations": dict(sorted(match.annotations.items())),
            "args_pattern": match.args_pattern.pattern if match.args_pattern is not None else None,
        },
        "run": {
            "type": entry.run.type,
            "command": entry.run.command,
            "args": list(entry.run.args),
            "url": entry.run.url,
            "prompt": entry.run.prompt,
        },
        "timeout_ms": entry.timeout_ms,
        "fail_closed": entry.fail_closed,
        "loop_limit": entry.loop_limit,
    }


def _resolved_material(entry: HookEntry) -> list[bytes]:
    """Return the bytes of the RESOLVED command/script backing a hook.

    A ``command`` hook's real behaviour lives in the script it execs, not just the
    argv string — so hash the content of the command binary and any argument that is
    an existing file on disk (the ``git pull`` that rewrites ``pre_tool.py`` changes
    THIS, even when the config entry is untouched). A missing/unreadable file
    contributes a stable sentinel so an absent script still fingerprints
    deterministically (and a later appearance flips it).
    """

    material: list[bytes] = []
    run = entry.run
    if run.type != "command":
        return material
    candidates = [run.command, *run.args]
    for candidate in candidates:
        text = str(candidate or "")
        if not text:
            continue
        try:
            path = Path(text)
        except (TypeError, ValueError):
            continue
        try:
            if path.is_file():
                material.append(path.read_bytes())
            else:
                material.append(b"\x00missing:" + text.encode("utf-8", "replace"))
        except OSError:
            material.append(b"\x00unreadable:" + text.encode("utf-8", "replace"))
    return material


def compute_fingerprint(entry: HookEntry) -> str:
    """Compute the content-hash fingerprint of one loaded hook (config + resolved script)."""

    digest = hashlib.sha256()
    digest.update(json.dumps(_config_material(entry), sort_keys=True).encode("utf-8"))
    digest.update(b"\x1e")
    for blob in _resolved_material(entry):
        digest.update(blob)
        digest.update(b"\x1e")
    return digest.hexdigest()


def load_trust_store(path: Path) -> dict[str, str]:
    """Load the colocated ``{id: fingerprint}`` trust map (empty if absent/malformed)."""

    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("[clio-hooks] failed to read hook trust store %s: %r", path, exc)
        return {}
    if not isinstance(data, dict):
        logger.warning("[clio-hooks] hook trust store %s is not an object; ignoring", path)
        return {}
    return {str(k): str(v) for k, v in data.items() if isinstance(v, str)}


def save_trust_store(path: Path, data: dict[str, str]) -> None:
    """Persist the trust map atomically-ish next to the hook config (best-effort)."""

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    except OSError as exc:
        # No silent fallback: a failed persist means the NEXT load cannot detect a
        # change for the newly-seen hooks, so it is logged (never swallowed).
        logger.warning("[clio-hooks] failed to persist hook trust store %s: %r", path, exc)


def evaluate_trust(entries: Iterable[HookEntry], *, store_path: Path) -> list[HookEntry]:
    """Tag each loaded hook ``trusted``/``untrusted`` by comparing to the persisted store.

    Trust-on-first-use: an id never seen before is trusted and its fingerprint is
    persisted. A previously-seen id whose fingerprint now differs is ``untrusted`` — a
    typed :func:`record_hook_reason` (``hook_untrusted_content_changed``) makes the
    change queryable, and the dispatcher will NOT run it (so a content change never
    runs silently). Returns a new list of entries carrying ``fingerprint``/``trust``.
    """

    store = load_trust_store(store_path)
    result: list[HookEntry] = []
    dirty = False
    for entry in entries:
        fingerprint = compute_fingerprint(entry)
        known = store.get(entry.id)
        if known is None:
            trust = TRUST_TRUSTED
            store[entry.id] = fingerprint
            dirty = True
        elif known == fingerprint:
            trust = TRUST_TRUSTED
        else:
            trust = TRUST_UNTRUSTED
            record_hook_reason(
                "hook_untrusted_content_changed",
                hook_id=entry.id,
                event="",
                scope=entry.scope,
            )
        result.append(replace(entry, fingerprint=fingerprint, trust=trust))
    if dirty:
        save_trust_store(store_path, store)
    return result


def trust_store_path_for(config_path: Path) -> Path:
    """Return the trust-store path colocated with an explicit single-file hook config."""

    return config_path.with_name(config_path.name + ".trust.json")
