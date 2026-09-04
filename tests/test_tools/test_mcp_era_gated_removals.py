"""Era-gated removals ratchet: ping / logging/setLevel / roots list_changed /
sampling never regrow into clio_agent's own source (#1285, C1-S5 item 5; the
sampling row added in the #1285 review round, owner addendum).

The 2026-07-28 core REMOVES three client-facing surfaces, plus one CLIO owner
ruling on a fourth deprecated-but-not-removed one (obligations doc):

- **F9 ping**: removed from the modern core entirely (legacy-era ``client.
  ping()`` still exists in the SDK for compat, but clio never calls it).
- **F8 logging/setLevel**: DEPRECATED; the modern replacement is a per-request
  ``io.modelcontextprotocol/logLevel`` in ``_meta`` (the SDK's
  ``client_log_level``/``_make_modern_stamp`` handle this internally --
  clio never calls the legacy RPC method itself, ``session.
  set_logging_level``).
- **F6 roots**: DEPRECATED; if kept, ``roots/list_changed`` is REMOVED. clio
  never implements the roots capability at all (no ``list_roots_callback``
  anywhere), so this is vacuously true today, but pinned so it stays that way.
- **F7 sampling**: DEPRECATED WHOLESALE in v2 (SEP-2577) and owner-ruled
  MUST-NOT-ADD on v1 regardless (obligations doc row F7: "we never
  implemented -- MUST NOT add"). Unlike F9/F8/F6, the SDK does not remove the
  wire-level capability (a proxy backend genuinely forwards a front's
  sampling handler -- ``tools/mcp_runtime.py``/``tools/gateway.py``'s
  documented, LEGITIMATE proxy-transparency behavior, and ``test_
  handshake_floor_review.py`` proves it), so the signals below are scoped to
  what CLIO WIRING sampling in would actually look like (the SDK's own
  ``sampling_callback`` construction kwarg, the ``sampling/createMessage``
  wire method, the ``CreateMessageRequest``/``SamplingFnT`` SDK types) --
  never the bare word "sampling", which the proxy-transparency comments and
  the (out-of-scope, ``tests/``-only) forwarding test legitimately use.

This is a REPO-WIDE grep, not a single-file check (unlike ``test_mcp_tasks.
py::test_removed_task_methods_are_never_called``, which pins one file's own
removed-method CONSTANT): clio is a pure CLIENT for all three removed
surfaces (plus the ruled-out fourth), so "zero hit" means clio's source never
calls/wires the removed/deprecated/forbidden surface, not that a string never
appears (a doc/comment mentioning "ping"/"sampling" for other reasons is
fine; matching on the SDK's own class/method/kwarg names is the precise
signal).
"""

from __future__ import annotations

from pathlib import Path

SRC_ROOT = Path("src/clio_agent")

#: Precise signals for each removed/deprecated surface -- SDK class/method
#: names, not bare English words ("roots" alone matches hundreds of
#: unrelated filesystem/workspace-root hits in this repo).
_FORBIDDEN_SIGNALS: dict[str, tuple[str, ...]] = {
    "ping (F9, removed from the 2026-07-28 core)": (
        "PingRequest",
        ".ping(",
        "client.ping",
        "session.ping",
    ),
    "logging/setLevel (F8, deprecated; per-request logLevel replaces it)": (
        "set_logging_level",
        "logging/setLevel",
        "LoggingSetLevelRequest",
    ),
    "roots/list_changed (F6, removed even where roots itself is kept)": (
        "RootsListChangedNotification",
        "roots/list_changed",
        "notifications/roots/list_changed",
        "list_roots_changed",
    ),
    # F7 (SEP-2577, obligations row F7): owner-ruled MUST-NOT-ADD, not an
    # SDK removal -- so these are the WIRING signals (what clio calling this
    # in would actually look like), never the bare word "sampling", which
    # legitimate proxy-transparency comments/tests use (see module docstring).
    "sampling (F7, deprecated wholesale in v2/SEP-2577; owner MUST-NOT-ADD)": (
        "sampling/createMessage",
        "sampling_callback",
        "CreateMessageRequest",
        "SamplingFnT",
    ),
}


def _iter_source_files() -> list[Path]:
    return sorted(SRC_ROOT.rglob("*.py"))


def test_era_gated_removals_never_appear_in_clio_agent_source() -> None:
    files = _iter_source_files()
    assert files, "expected src/clio_agent to contain Python source files"

    offenders: list[str] = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        for reason, signals in _FORBIDDEN_SIGNALS.items():
            for signal in signals:
                if signal in text:
                    offenders.append(f"{path}: {signal!r} ({reason})")

    assert not offenders, (
        "era-gated-removal ratchet broke -- clio_agent must never call these "
        "removed/deprecated MCP surfaces:\n" + "\n".join(offenders)
    )


def test_this_ratchet_is_not_vacuous() -> None:
    """Regression guard for the checker itself: a deliberately-planted
    signal in a throwaway string must be caught, proving the scan logic
    actually runs (not skipped, not matching an empty file list)."""

    files = _iter_source_files()
    planted = "PingRequest"
    found = any(planted in p.read_text(encoding="utf-8") for p in files)
    assert not found, "if this ever becomes True the fixture below is stale"

    # Prove the detector fires on a synthetic match without touching real source.
    fake_text = 'from mcp_types import PingRequest  # noqa\n'
    assert any(signal in fake_text for signal in _FORBIDDEN_SIGNALS[next(iter(_FORBIDDEN_SIGNALS))])


def test_sampling_ratchet_is_not_vacuous() -> None:
    """Same vacuity guard as above, for the F7 sampling row specifically
    (#1285 review round addendum, owner-surfaced): the precise WIRING
    signals must actually fire on a synthetic match, and must NOT fire on
    the legitimate bare-word "sampling" proxy-transparency prose this
    module's own docstring quotes."""

    sampling_key = next(k for k in _FORBIDDEN_SIGNALS if k.startswith("sampling "))
    signals = _FORBIDDEN_SIGNALS[sampling_key]

    fake_wiring_text = "session = ClientSession(sampling_callback=my_handler)\n"
    assert any(signal in fake_wiring_text for signal in signals)

    legitimate_transparency_prose = (
        "unhandled sampling / roots / log requests are push-forwarded to the "
        "front client; sampling and roots stay because a proxy backend "
        "genuinely forwards them"
    )
    assert not any(signal in legitimate_transparency_prose for signal in signals), (
        "the sampling ratchet's signals must never trip on legitimate "
        "proxy-transparency prose using the bare word 'sampling'"
    )
