"""Era-gated removals ratchet: ping / logging/setLevel / roots list_changed
never regrow into clio_agent's own source (#1285, C1-S5 item 5).

The 2026-07-28 core REMOVES three client-facing surfaces (obligations doc):

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

This is a REPO-WIDE grep, not a single-file check (unlike ``test_mcp_tasks.
py::test_removed_task_methods_are_never_called``, which pins one file's own
removed-method CONSTANT): clio is a pure CLIENT for all three, so "zero hit"
means clio's source never calls the removed/deprecated surface, not that a
string never appears (a doc/comment mentioning "ping" for other reasons is
fine; matching on the SDK's own class/method names is the precise signal).
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
