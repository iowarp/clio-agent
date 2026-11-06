"""``clio-agent store-check`` and ``clio-agent reset-store``: an old store, found and moved aside.

``store-check`` says whether this machine's CLIO data was written by CLIO 0.9.5.1 or
older (:mod:`clio_agent.arc.store_format`), reading the store itself while no CLIO uses
it. ``reset-store`` moves the store aside, unchanged, so CLIO starts with an empty one
(:mod:`clio_agent.gact.store_aside`); it never deletes anything. The installers
(``install/install.sh``, ``install/install.ps1``) run ``store-check`` before an upgrade's
store migrations, ask, and run ``reset-store --yes``; ``clio reset-store`` runs it by hand.

Exit status of ``store-check``: 0 nothing to do (this version's format, no conversation
yet, no store, or no clio-core), 3 CLIO is running (``store_in_use``: nothing was read),
4 the store could not be opened (``store_unavailable``), 5 written by CLIO 0.9.5.1 or
older (``arc_store_format_old``), 6 cannot tell (``arc_store_format_unknown``).

Exit status of ``reset-store``: 0 moved (or nothing to move), 1 not moved (the person
said no, or no terminal to ask and no ``--yes``), 3 CLIO is running, 4 the store could
not be opened to describe it, 7 refused (``arc_store_custom_config``,
``arc_store_move_failed``: the message says what to do).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from typing import Any

from clio_agent.arc.store_format import LAST_OLD_FORMAT_RELEASE, StoreFormat, inspect_store
from clio_agent.gact.store_aside import (
    MovedAside,
    StoreAsideRefusedError,
    StoreLocation,
    locate,
    move_aside,
    planned_moves,
)

EXIT_OK, EXIT_NOT_MOVED, EXIT_IN_USE, EXIT_UNAVAILABLE = 0, 1, 3, 4
EXIT_OLD, EXIT_UNKNOWN, EXIT_REFUSED = 5, 6, 7

Emit = Callable[[str], None]


def _version() -> str:
    from clio_agent import __version__  # noqa: PLC0415 - avoid import cycle

    return __version__


def _in_use(emit: Emit, verb: str) -> tuple[int, dict[str, Any]] | None:
    """The refusal when a CLIO (or anything else on clio-core) is running, else ``None``."""
    from clio_agent.gact.store_migrate_command import _other_store_clients  # noqa: PLC0415

    others = _other_store_clients()
    if not others:
        return None
    emit(
        f"CLIO is running (process {', '.join(map(str, others))}), so its data was not "
        f"{verb}. Stop CLIO (`clio stop`, or quit CLIO Desktop) and run this again."
    )
    return EXIT_IN_USE, {"status": "refused", "reason": "store_in_use", "pids": others}


def _no_store(location: StoreLocation | None, emit: Emit) -> tuple[int, dict[str, Any]] | None:
    """Nothing to check or move: no clio-core on this platform, or no store created yet."""
    from clio_agent.arc import history_mode  # noqa: PLC0415

    mode = history_mode.resolve()
    if mode.is_history:
        emit("This CLIO runs without clio-core (History mode): it keeps no store to check.")
        return EXIT_OK, {"status": "skipped", "reason": "history_mode"}
    assert location is not None
    if not location.exists:
        emit(f"No CLIO data in {location.store_dir} yet: nothing to do.")
        return EXIT_OK, {"status": "skipped", "reason": "no_store"}
    return None


def _inspect(location: StoreLocation, emit: Emit) -> StoreFormat | tuple[int, dict[str, Any]]:
    """Attach the store alone, read its format, release it."""
    from clio_agent.arc.init_degradation import ArcStoreUnavailableError  # noqa: PLC0415
    from clio_agent.gact.store_format_boot import attached_store  # noqa: PLC0415

    try:
        with attached_store() as store:
            return inspect_store(store)
    except ArcStoreUnavailableError as exc:  # typed: clio-core could not open the store
        emit(f"Could not open the CLIO data in {location.store_dir} to check it: {exc}")
        return EXIT_UNAVAILABLE, {"status": "failed", "reason": "store_unavailable"}


def _describe_moves(location: StoreLocation, emit: Emit) -> dict[str, Any]:
    """Say where the data would go (or why it is not moved here); the planned moves."""
    try:
        moves = planned_moves(location, _version())
    except StoreAsideRefusedError as exc:
        emit(exc.message)
        return {"move": None, "move_refused": exc.error_type}
    emit("Moving it aside keeps it unchanged at:")
    emit(f"  {moves.store_to}")
    if moves.sessions_to is not None:
        emit(f"  {moves.sessions_to} (the conversation list it was imported from)")
    emit("Nothing is deleted. CLIO then starts with an empty store.")
    return {"move": moves.as_dict()}


def _report(verdict: StoreFormat, location: StoreLocation, emit: Emit) -> dict[str, Any]:
    """Print what the check found, in plain words; the JSON payload."""
    payload: dict[str, Any] = {"store_dir": str(location.store_dir), **verdict.as_dict()}
    if verdict.verdict == "old":
        emit(
            f"Your CLIO data in {location.store_dir} was written by CLIO "
            f"{LAST_OLD_FORMAT_RELEASE} or older. CLIO {_version()} cannot open it, so your "
            "earlier conversations, memory and artifacts will not show."
        )
        payload |= _describe_moves(location, emit)
    elif verdict.verdict == "unknown":
        emit(
            f"Could not tell which CLIO version wrote the data in {location.store_dir} "
            f"({verdict.error}). Data from CLIO {LAST_OLD_FORMAT_RELEASE} or older cannot be "
            f"opened by CLIO {_version()}."
        )
        payload |= _describe_moves(location, emit)
    elif verdict.verdict == "empty":
        emit(f"Your CLIO data in {location.store_dir} holds no conversations yet: nothing to do.")
    else:
        emit(f"Your CLIO data in {location.store_dir} is in this version's format: nothing to do.")
    return payload


def check(emit: Emit = print) -> tuple[int, dict[str, Any]]:
    """``store-check``: which CLIO wrote this machine's store (exit status above)."""
    location = locate()
    done = _no_store(location, emit) or _in_use(emit, "checked")
    if done is not None:
        return done
    verdict = _inspect(location, emit)
    if not isinstance(verdict, StoreFormat):
        return verdict
    payload = {"status": "checked", **_report(verdict, location, emit)}
    code = {"old": EXIT_OLD, "unknown": EXIT_UNKNOWN}.get(verdict.verdict, EXIT_OK)
    return code, payload


def _no_daemon_left() -> bool:
    """Whether no clio-core daemon serves the store (a leftover one no client uses is
    stopped first, as a new client would replace it)."""
    from clio_agent.arc import runtime_stop, storage  # noqa: PLC0415

    try:
        parts = storage._daemon_pidfile().read_text(encoding="utf-8").split()
    except OSError:
        return True  # no pidfile: no daemon was started for this machine
    pid = int(parts[0]) if parts and parts[0].isdigit() else None
    created = float(parts[1]) if len(parts) > 1 and parts[1].replace(".", "", 1).isdigit() else None
    if not runtime_stop._daemon_process_alive(pid, created):
        return True
    return runtime_stop.cleanup_runtime_after_client_crash()


def _confirm(ask: Callable[[str], str] | None, emit: Emit) -> bool:
    if ask is None:
        emit("Run `clio-agent reset-store --yes` to move it (nothing is deleted).")
        return False
    return ask("Move it aside now? [y/N] ").strip().lower() in ("y", "yes")


def reset(
    *, yes: bool, emit: Emit = print, ask: Callable[[str], str] | None = None
) -> tuple[int, dict[str, Any]]:
    """``reset-store``: move this machine's store aside (exit status above).

    ``ask`` reads the person's answer; ``None`` when there is no terminal to ask on.
    """
    location = locate()
    done = _no_store(location, emit)
    if done is not None:
        return done
    if not location.movable:
        return _refused(location, emit)
    done = _in_use(emit, "moved")
    if done is not None:
        return done
    if not yes:
        verdict = _inspect(location, emit)
        if not isinstance(verdict, StoreFormat):
            return verdict
        if verdict.verdict in ("current", "empty"):
            emit(
                f"Your CLIO data in {location.store_dir} is readable by this version; "
                "moving it aside hides its conversations from CLIO."
            )
            _describe_moves(location, emit)
        else:
            _report(verdict, location, emit)
        if not _confirm(ask, emit):
            emit("Nothing was moved.")
            return EXIT_NOT_MOVED, {"status": "not_moved", "verdict": verdict.verdict}
    if not _no_daemon_left():
        return _in_use(emit, "moved") or (
            EXIT_IN_USE,
            {"status": "refused", "reason": "store_in_use"},
        )
    try:
        moves = move_aside(location, _version())
    except StoreAsideRefusedError as exc:
        emit(exc.message)
        return EXIT_REFUSED, {"status": "refused", "reason": exc.error_type, **exc.details}
    _report_moved(moves, emit)
    return EXIT_OK, {"status": "moved", **moves.as_dict()}


def _refused(location: StoreLocation, emit: Emit) -> tuple[int, dict[str, Any]]:
    try:
        planned_moves(location, _version())
    except StoreAsideRefusedError as exc:
        emit(exc.message)
        return EXIT_REFUSED, {"status": "refused", "reason": exc.error_type, **exc.details}
    raise AssertionError("a store that is not movable is refused")  # pragma: no cover


def _report_moved(moves: MovedAside, emit: Emit) -> None:
    emit("Moved your CLIO data aside, unchanged:")
    emit(f"  {moves.store_from}")
    emit(f"    -> {moves.store_to}")
    if moves.sessions_from is not None:
        emit(f"  {moves.sessions_from}")
        emit(f"    -> {moves.sessions_to}")
    emit(
        "Nothing was deleted. CLIO starts with an empty store. To open the old data again "
        f"with CLIO {LAST_OLD_FORMAT_RELEASE}, move it back."
    )


def _terminal_answer(prompt: str) -> str:
    print(prompt, end="", flush=True)
    return sys.stdin.readline()


def main(verb: str, arguments: Sequence[str] = ()) -> int:
    """Entry point of ``clio-agent store-check`` and ``clio-agent reset-store``."""
    parser = argparse.ArgumentParser(
        prog=f"clio-agent {verb}",
        description=(
            "Say whether this machine's CLIO data was written by CLIO 0.9.5.1 or older."
            if verb == "store-check"
            else "Move this machine's CLIO data aside, unchanged, so CLIO starts empty."
        ),
    )
    parser.add_argument("--json", action="store_true", help="Print one JSON result object.")
    if verb == "reset-store":
        parser.add_argument("--yes", action="store_true", help="Move it without asking.")
    args = parser.parse_args(list(arguments))
    from clio_agent.config import load_project_env_file  # noqa: PLC0415

    load_project_env_file()
    stream = sys.stderr if args.json else sys.stdout  # --json: the words on stderr
    emit: Emit = lambda line: print(line, file=stream, flush=True)  # noqa: E731
    if verb == "store-check":
        code, payload = check(emit)
    else:
        ask = _terminal_answer if sys.stdin.isatty() else None
        code, payload = reset(yes=args.yes, emit=emit, ask=ask)
    if args.json:
        print(json.dumps(payload), flush=True)
    return code


if __name__ == "__main__":
    from clio_agent.gact.store_migrate_command import exit_released

    verb = sys.argv[1] if len(sys.argv) > 1 else ""
    if verb not in ("check", "reset"):
        print("usage: python -m clio_agent.gact.store_reset_command check|reset [...]")
        raise SystemExit(2)
    exit_released(main("store-check" if verb == "check" else "reset-store", sys.argv[2:]))
