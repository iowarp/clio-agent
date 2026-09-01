"""Make property values survive the cmf-server's backslash defect, losslessly.

UPSTREAM BUG (cmf-server / cmflib metadata push)
================================================

A CMF **execution** whose ``properties`` or ``custom_properties`` contain a
value with a literal backslash (``U+005C``) is **silently discarded**, together
with every event attached to it, while ``POST /api/mlmd_push`` still answers
``200 {"status": "success"}``.

Characterised black-box against a live cmf-server (probe namespaces
``clio-bsprobe-*`` / ``clio-bslayer-*``):

* **Trigger is exactly the backslash.** One execution, one custom property, only
  the value varying: ``a\\b``, ``abc\\``, ``D:\\Lib\\a.csv`` and a JSON blob
  containing a Windows path are all DISCARDED (0 executions held). A newline, a
  tab, a double quote, a single quote, a percent, braces, non-ASCII, and a
  400-character ASCII value are all HELD and round-trip byte-identical. Position
  in the value does not matter.
* **Scope is the execution subtree.** A backslash in an execution property or
  custom property loses the execution AND its events; the same backslash in an
  artifact's name, ``properties.url`` or custom properties is harmless.
* **The failure is swallowed.** ``cmf_merger.process_execution`` calls
  ``handle_execution``, which wraps ``merge_created_execution`` in
  ``except Exception as e: logger.error(...)``; the events loop is wrapped the
  same way. So the raising write is logged server-side and the response is still
  ``success`` -- there is no wire signal that anything was dropped.

This is what made a live qualification push 13 executions, ZERO artifacts and
ZERO events with provider health reporting no failure: the executions carrying
``clio_instrument_json`` (327 chars, Windows paths inside) were discarded whole,
and ``clio_environment_json`` (455 chars, backslash-free) was harmless -- length
was never the variable, the backslash was.

Two hypotheses are RULED OUT by the evidence and should not be repeated in an
upstream report: it is not the unescaped Cypher interpolation in
``graph_wrapper._create_execution_syntax`` (a raw ``"`` would break that string
literal just as badly, and quotes survive), and it is not a length or encoding
limit (400-char and non-ASCII values survive). The mangling step itself lives in
the deployed server's execution-write path, which is NEWER than the cmflib 0.1.0
sdist available here, so the exact line is deliberately not attributed.

THE CLIO-SIDE CONTRACT
======================

Nothing may reach an execution property with a literal backslash in it. Two
complementary rules, no heuristics -- neither guesses what a value "looks like":

1. :func:`posix_path` is applied where CLIO's OWN schema declares a value is a
   filesystem path (the artifact ``url`` property, ``clio_path``). A POSIX
   representation is faithful cross-host and stays readable in the CMF UI.
2. :func:`encode_property_value` is applied to EVERY property and custom
   property value. It is a bijection -- ``%`` -> ``%25`` then ``\\`` -> ``%5C``
   -- so an arbitrary payload (a JSON blob of tool arguments, say) survives
   byte-exactly and :func:`decode_property_value` restores it on read. Values
   with no backslash and no percent are returned untouched, so the common case
   is unchanged.

Rule 1 runs before rule 2, so a declared path normally contains no backslash by
the time it is escaped and no ``%5C`` appears in it at all.
"""

from __future__ import annotations

from typing import Any

#: The character the cmf-server chokes on, and its escape.
_BACKSLASH = "\\"
_BACKSLASH_ESCAPE = "%5C"
#: Escaped first so the encoding is reversible.
_PERCENT = "%"
_PERCENT_ESCAPE = "%25"


def posix_path(value: str) -> str:
    """Return a filesystem path in its POSIX (forward-slash) representation.

    Applied ONLY to values CLIO's schema declares to be paths. A Windows path
    written with forward slashes names the same file on Windows, so this is a
    faithful representation rather than a lossy rewrite, and it keeps the value
    readable in the CMF UI instead of percent-escaped.

    Args:
        value: A filesystem path, in any separator style.

    Returns:
        The same path with backslash separators replaced by forward slashes.
    """
    return value.replace(_BACKSLASH, "/")


def encode_property_value(value: Any) -> Any:
    """Encode one property value so the cmf-server cannot discard its entity.

    A bijection on strings: ``%`` becomes ``%25``, then ``\\`` becomes ``%5C``.
    Non-strings (ints from ``clio_version`` / ``clio_size_bytes``) pass through
    untouched so they stay typed as MLMD ints.

    Args:
        value: The property value.

    Returns:
        The encoded value, or the input unchanged when it needs no encoding.
    """
    if not isinstance(value, str):
        return value
    if _PERCENT not in value and _BACKSLASH not in value:
        return value
    return value.replace(_PERCENT, _PERCENT_ESCAPE).replace(_BACKSLASH, _BACKSLASH_ESCAPE)


def decode_property_value(value: Any) -> Any:
    """Reverse :func:`encode_property_value`.

    Args:
        value: A value read back from CMF.

    Returns:
        The original value, byte-exact.
    """
    if not isinstance(value, str):
        return value
    if _PERCENT not in value:
        return value
    # Backslashes first, then percents: the exact inverse of the encode order,
    # so a literal "%5C" in the ORIGINAL value (encoded as "%255C") survives.
    return value.replace(_BACKSLASH_ESCAPE, _BACKSLASH).replace(_PERCENT_ESCAPE, _PERCENT)


def encode_properties(properties: dict[str, Any]) -> dict[str, Any]:
    """Encode every value in one property mapping."""
    return {key: encode_property_value(value) for key, value in properties.items()}


def decode_properties(properties: dict[str, Any]) -> dict[str, Any]:
    """Decode every value in one property mapping."""
    return {key: decode_property_value(value) for key, value in properties.items()}


def has_hostile_value(properties: dict[str, Any]) -> bool:
    """Whether any value would be discarded by the cmf-server as-is.

    Used as a pre-send assertion: after encoding, this must be ``False`` for
    every execution in a push document.
    """
    return any(isinstance(value, str) and _BACKSLASH in value for value in properties.values())


__all__ = [
    "decode_properties",
    "decode_property_value",
    "encode_properties",
    "encode_property_value",
    "has_hostile_value",
    "posix_path",
]
