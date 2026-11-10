"""A store written by CLIO 0.9.5.1 or older: checked once at boot, reported plainly (owner).

This CLIO cannot open the segment lists an older CLIO stored
(:mod:`clio_agent.arc.store_format`). The installers check before the upgrade's first
start and offer to move such a store aside; a manual upgrade (``uv pip install -U``, a
copied runtime, a desktop swapped in) does not, so the server checks too:

* :func:`checked_boot_store` runs once, right after clio-core attaches and before
  anything reads or writes the store (one bounded name query and a few small reads).
  On an old store it refuses the attach, typed ``arc_store_format_old``: nothing new
  is written into the old data, the conversation list answers with the same plain
  message, and the refusal is kept, so a retried attach answers at once.
* :func:`health_rows` is the ``arc_store_format`` row of ``GET /v1/health``;
  :func:`doctor_rows` is the same row for ``clio-agent doctor``, which runs without a
  server and so reads the store itself (only while no CLIO uses it).

Every message says what happened, that nothing is deleted, and the one step that
fixes it (``clio reset-store``, :mod:`clio_agent.gact.store_reset_command`), with the
exact folders for doing it by hand.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator
from typing import Any

from clio_agent.arc.store_format import (
    ARC_STORE_FORMAT_OLD,
    LAST_OLD_FORMAT_RELEASE,
    ArcStoreFormatOldError,
    StoreFormat,
    inspect_store,
)
from clio_agent.gact.store_aside import StoreAsideRefusedError, StoreLocation, locate, planned_moves
from clio_agent.runtime.status import IntegrationState, IntegrationStatus

__all__ = [
    "ROW",
    "attached_store",
    "checked_boot_store",
    "conversation_message",
    "doctor_rows",
    "health_rows",
    "old_store_message",
    "row_for",
]

logger = logging.getLogger(__name__)

#: The health and doctor row's name.
ROW = "arc_store_format"
_SOURCE = "clio-core store (segment-list records)"
_COMMAND = "clio reset-store"


def _version() -> str:
    from clio_agent import __version__  # noqa: PLC0415 - avoid import cycle

    return __version__


def _how_to_move(location: StoreLocation) -> str:
    """The step that fixes an old store, with the exact folders for doing it by hand."""
    try:
        moves = planned_moves(location, _version())
    except StoreAsideRefusedError as exc:  # a custom clio-core config: by hand only
        return exc.message
    by_hand = f"rename {moves.store_from} to {moves.store_to}"
    if moves.sessions_from is not None:
        by_hand += f", and {moves.sessions_from} to {moves.sessions_to}"
    return (
        f"Quit CLIO (`clio stop`, or quit CLIO Desktop), then run `{_COMMAND}` (or "
        f"`clio-agent reset-store`). It moves the old data, unchanged, to {moves.store_to} "
        "and CLIO starts with an empty store; nothing is deleted. To do it by hand while "
        f"CLIO is stopped, {by_hand}."
    )


def old_store_message(location: StoreLocation) -> tuple[str, str]:
    """``(what happened, what to do)`` for a store written by an older CLIO."""
    summary = (
        f"Your CLIO data in {location.store_dir} was written by CLIO "
        f"{LAST_OLD_FORMAT_RELEASE} or older. CLIO {_version()} cannot open it, so your "
        "earlier conversations, memory and artifacts do not show."
    )
    return summary, _how_to_move(location)


def conversation_message() -> str:
    """The 503 message of a conversation stored in the old format."""
    return (
        f"This conversation was saved by CLIO {LAST_OLD_FORMAT_RELEASE} or older, and this "
        f"version cannot open it. Quit CLIO and run `{_COMMAND}` to move the old data aside "
        "(it is kept, not deleted); CLIO then starts with an empty store."
    )


def checked_boot_store(app: Any, data_dir: str) -> Any:
    """Attach the clio-core store and check its format once; refuse an old store, typed.

    Raises:
        ArcStoreFormatOldError: The store was written by CLIO 0.9.5.1 or older (also on
            every later call in this process: the verdict is decided once).
    """
    refused = getattr(app.state, "arc_store_refused", None)
    if refused is not None:
        raise refused
    from clio_agent.arc.storage import make_arc_store  # noqa: PLC0415

    store = make_arc_store(data_dir=data_dir)
    verdict = inspect_store(store)
    app.state.arc_store_format = verdict
    if verdict.verdict == "unknown":
        logger.warning(
            "arc store format reason=%s error=%s: attaching anyway; a conversation in an "
            "older format answers 503 with cause %s",
            verdict.reason,
            verdict.error,
            ARC_STORE_FORMAT_OLD,
        )
    if verdict.verdict != "old":
        return store
    location = locate()
    summary, next_action = old_store_message(location)
    error = ArcStoreFormatOldError(
        f"{summary} {next_action}",
        {"store_dir": str(location.store_dir), "command": _COMMAND, **verdict.as_dict()},
    )
    logger.error(
        "clio-core store refused reason=%s store_dir=%s", error.error_type, location.store_dir
    )
    app.state.arc_store_refused = error
    sessions = getattr(app.state, "sessions", None)
    if sessions is not None:  # the conversation list says why, instead of "attaching"
        sessions.index.refuse_store(ARC_STORE_FORMAT_OLD, error.message)
    raise error


def row_for(verdict: StoreFormat, location: StoreLocation) -> IntegrationStatus:
    """The ``arc_store_format`` row for one verdict."""
    details: dict[str, Any] = {"store_dir": str(location.store_dir), **verdict.as_dict()}
    if verdict.verdict == "old":
        summary, next_action = old_store_message(location)
        return IntegrationStatus(
            name=ROW,
            state=IntegrationState.UNAVAILABLE,
            summary=summary,
            config_source=_SOURCE,
            next_action=next_action,
            details=details,
            required=True,
        )
    if verdict.verdict == "unknown":
        return IntegrationStatus(
            name=ROW,
            state=IntegrationState.DEGRADED,
            summary=(
                f"CLIO could not tell which version wrote its data in {location.store_dir} "
                f"({verdict.error}). Data from CLIO {LAST_OLD_FORMAT_RELEASE} or older cannot "
                "be opened by this version."
            ),
            config_source=_SOURCE,
            next_action=(
                "If conversations from before this upgrade do not open: " + _how_to_move(location)
            ),
            details=details,
            required=False,
        )
    summary = (
        "The CLIO data holds no conversations yet."
        if verdict.verdict == "empty"
        else f"The CLIO data was written in this version's format (CLIO {_version()})."
    )
    return IntegrationStatus(
        name=ROW,
        state=IntegrationState.READY,
        summary=summary,
        config_source=_SOURCE,
        next_action="",
        details=details,
        required=False,
    )


def health_rows(app: Any) -> list[IntegrationStatus]:
    """The row of ``GET /v1/health`` (none before the boot attach checked the store)."""
    verdict = getattr(app.state, "arc_store_format", None)
    if verdict is None:
        return []
    return [row_for(verdict, locate())]


@contextlib.contextmanager
def attached_store() -> Iterator[Any]:
    """Attach this home's clio-core store alone, and release it (stopping clio-core when
    this was its last client) however the block ends."""
    from clio_agent import paths  # noqa: PLC0415
    from clio_agent.arc.storage import (  # noqa: PLC0415
        ClioCoreStore,
        make_arc_store,
        release_runtime_client,
    )

    try:
        yield make_arc_store(data_dir=str(paths.arc_data_dir()))
    finally:
        release_runtime_client()
        ClioCoreStore.drop_native_handles()


def doctor_rows() -> list[IntegrationStatus]:
    """The row of ``clio-agent doctor``: reads the store only while no CLIO uses it."""
    from clio_agent.arc import history_mode  # noqa: PLC0415
    from clio_agent.arc.init_degradation import ArcStoreUnavailableError  # noqa: PLC0415
    from clio_agent.gact.store_migrate_command import _other_store_clients  # noqa: PLC0415

    location = locate()
    if history_mode.resolve().is_history or not location.exists:
        return []  # no clio-core, or no store created yet: nothing an older CLIO wrote
    if _other_store_clients():
        return [
            IntegrationStatus(
                name=ROW,
                state=IntegrationState.SKIPPED,
                summary=(
                    "CLIO is running and checked its data when it started; the running "
                    "CLIO's health shows the result."
                ),
                config_source=_SOURCE,
                next_action="Read the running CLIO's health (GET /v1/health), or stop it "
                "and run this doctor again.",
                details={"store_dir": str(location.store_dir), "reason": "store_in_use"},
                required=False,
            )
        ]
    try:
        with attached_store() as store:
            verdict = inspect_store(store)
    except ArcStoreUnavailableError as exc:  # typed: the store could not be attached
        verdict = StoreFormat("unknown", "store_unavailable", error=repr(exc))
    return [row_for(verdict, location)]
