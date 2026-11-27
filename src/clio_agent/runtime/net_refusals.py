"""The egress CLIO's chokepoint refused, kept per channel so the refused call can say so.

A process the fence confines reaches the network only through its channel on the
chokepoint (:mod:`clio_agent.runtime.net_chokepoint`). When the chokepoint refuses a
connection the client sees only the wire: a bare ``403``, or a reset when a proxy in
between (Codex's managed proxy under MXC) had already answered ``200``. The tool's own
text then reads as a network failure, and the agent retries or blames the host.

The chokepoint records each refusal here with its typed reason and, when a person's
rule decided it, that rule, and with the calls using its channel at that moment. A tool
call watches the channels that serve it: the call scope opens a watch (:func:`watching`),
the seam that knows which channel serves the call joins it, and the call's result carries
the refusals made on those channels while it used them (:func:`call_notice`). The network
sees a process, not a request, so only a channel the call has to itself is named as the
call's own: one shell command's (:func:`channel_fields`), a kernel while its script holds
it (:func:`own_channel`). A channel calls share (a server's, the browser's; :func:`join`)
is named as what that process was refused while the call ran, with how many other calls
were using it. Work that settles after its call returns (a server's task) keeps the call's
watch until its result is recorded (:func:`outlast`, :func:`settled_notice`), and persists
its notice with the task so a restart keeps it.
"""

from __future__ import annotations

import itertools
import logging
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

#: Refusals kept per channel, and channels kept, so a refusal loop cannot grow memory.
_PER_CHANNEL = 64
_MAX_CHANNELS = 1024

#: The reasons the chokepoint itself decides; the gate's reasons come with its denial.
CHILD_HOST_NOT_ALLOWED = "child_host_not_allowed"
PRIVATE_ADDRESS = "private_address_unapproved"
TARGET_IS_CLIO = "egress_target_is_clio"
GATE_DENY = "gate_deny"

_PHRASES = {
    "policy_deny": "a rule denies this host",
    "write_policy_deny": "a rule denies sending data to this host",
    "person_denied": "the person denied the connection",
    "egress_denied_this_turn": "the person denied this host earlier in this turn",
    "egress_gate_timeout": "nobody answered the connection request in time",
    "egress_gate_prompt_cap_reached": "too many connection requests were already waiting",
    CHILD_HOST_NOT_ALLOWED: "this process may reach only the hosts it was started for",
    PRIVATE_ADDRESS: "it is a private, loopback or link-local address nobody approved",
    TARGET_IS_CLIO: "the address is CLIO's own service",
}


class EgressDenial(str):
    """A gate's ``"deny"`` that says why: its typed ``reason`` and the ``rule`` that decided.

    It equals ``"deny"``, so a caller comparing the decision is unchanged.
    """

    reason: str
    rule: str

    def __new__(cls, reason: str, rule: str = "") -> EgressDenial:
        value = super().__new__(cls, "deny")
        value.reason = reason
        value.rule = rule
        return value


@dataclass(frozen=True)
class EgressRefusal:
    """One refused connection on one channel."""

    child_id: str
    host: str
    port: int
    reason: str
    rule: str
    at: float
    seq: int = 0  # its place in the ledger, so a result reports it once
    #: The watches (tool calls) using the channel when it was refused: only they may name it.
    watchers: frozenset[int] = frozenset()


@dataclass(frozen=True)
class _Use:
    """How a call uses a channel: alone (``own``), or shared with other calls (``label``)."""

    own: bool
    label: str = ""


_OWN = _Use(True)
_LOCK = threading.Lock()
_SEQ = itertools.count(1)
_WATCH_IDS = itertools.count(1)
_LEDGER: dict[str, deque[EgressRefusal]] = {}


@dataclass(eq=False)
class _Watch:
    started: float
    id: int = field(default_factory=lambda: next(_WATCH_IDS))
    channels: dict[str, _Use] = field(default_factory=dict)
    reported: set[int] = field(default_factory=set)  # refusals a result already named
    #: Work that settles after the call (task handles), each with its persisting callback.
    outlasting: dict[str, Callable[[str], None] | None] = field(default_factory=dict)
    open: bool = True

    def unreported(self, found: list[EgressRefusal]) -> list[EgressRefusal]:
        """``found`` less what this call's results named; those are now named."""

        found = [r for r in found if r.seq not in self.reported]
        self.reported.update(r.seq for r in found)
        return found


#: The watches using each channel now, by watch id: who a refusal there is told to.
_ON_CHANNEL: dict[str, dict[int, _Watch]] = {}


def record(child_id: str, host: str, port: int, reason: str, rule: str = "") -> None:
    """Keep one refusal on ``child_id``'s channel (the shared listener has none: skipped).

    The refusal names the calls using the channel at that moment, never a call that joined
    it before or after; an outlasting call's task persists its notice
    (:func:`outlast`'s ``keep``).
    """

    if not child_id:
        return
    with _LOCK:
        using = _ON_CHANNEL.get(child_id, {})
        refusal = EgressRefusal(
            child_id,
            host,
            int(port or 0),
            reason,
            rule,
            time.monotonic(),
            next(_SEQ),
            frozenset(using),
        )
        kept = _LEDGER.get(child_id)
        if kept is None:
            if len(_LEDGER) >= _MAX_CHANNELS:
                oldest = next(iter(_LEDGER))
                _LEDGER.pop(oldest)
                logger.info("net refusals dropped reason=ledger_cap_reached child=%s", oldest)
            kept = _LEDGER[child_id] = deque(maxlen=_PER_CHANNEL)
        kept.append(refusal)
        keepers = [(h, k) for w in using.values() for h, k in w.outlasting.items() if k]
    for handle, keep in keepers:
        try:
            keep(handle)
        except Exception:  # noqa: BLE001 ? the chokepoint's answer must not depend on it
            logger.warning(
                "net refusals keep failed reason=keep_raised task=%s", handle, exc_info=True
            )


def refused(child_id: str, host: str, port: int, reason: str, rule: str = "") -> bool:
    """:func:`record` the refusal; ``False``, the chokepoint's answer for it."""

    record(child_id, host, port, reason, rule)
    return False


def forget(child_id: str) -> None:
    """Drop a closed channel's refusals."""

    with _LOCK:
        _LEDGER.pop(child_id, None)
        _ON_CHANNEL.pop(child_id, None)


def refusals(channels: set[str] | frozenset[str], since: float = 0.0) -> list[EgressRefusal]:
    """The refusals on ``channels`` made at or after ``since`` (monotonic), oldest first."""

    with _LOCK:
        found = [r for c in channels for r in _LEDGER.get(c, ()) if r.at >= since]
    return sorted(found, key=lambda r: r.at)


def _seen_by(watch: _Watch, channels: Iterable[str]) -> list[EgressRefusal]:
    """The refusals on ``channels`` made while ``watch`` was using them, oldest first."""

    with _LOCK:
        found = [r for c in channels for r in _LEDGER.get(c, ()) if watch.id in r.watchers]
    return sorted(found, key=lambda r: r.at)


def _register(watch: _Watch, child_id: str, use: _Use) -> None:
    with _LOCK:
        if child_id not in watch.channels or use.own:
            watch.channels[child_id] = use
        _ON_CHANNEL.setdefault(child_id, {})[watch.id] = watch


def _drop(watch: _Watch, channels: Iterable[str]) -> None:
    """Stop telling ``watch`` about refusals on ``channels`` (caller holds ``_LOCK``)."""

    for child_id in channels:
        using = _ON_CHANNEL.get(child_id)
        if using is not None:
            using.pop(watch.id, None)
            if not using:
                del _ON_CHANNEL[child_id]


#: One tool call's watch. A mutable holder, so a copied context (the app loop's task,
#: a worker thread) joins channels to the same call.
_WATCH: ContextVar[_Watch | None] = ContextVar("clio_net_refusal_watch", default=None)


@contextmanager
def watching() -> Iterator[None]:
    """Watch the channels a tool call joins; a nested call gets its own watch.

    When the call ends it stops using them, unless its work outlasts it (:func:`outlast`).
    """

    watch = _Watch(time.monotonic())
    token = _WATCH.set(watch)
    try:
        yield
    finally:
        _WATCH.reset(token)
        with _LOCK:
            watch.open = False
            if not watch.outlasting:
                _drop(watch, list(watch.channels))


def join(child_id: str, *, label: str = "") -> None:
    """This call uses ``child_id``'s channel, which other calls may use too.

    ``label`` names what owns the channel for the agent (``"the taskfetch server"``): a
    refusal there is told to every call using it then, as made by that process while the
    call ran, never as this call's own.
    """

    watch = _WATCH.get()
    if watch is None or not child_id:
        return
    if watch.open or watch.outlasting:
        _register(watch, child_id, _Use(False, label))
    else:  # an ended call no longer uses anything
        watch.channels.setdefault(child_id, _Use(False, label))


@contextmanager
def own_channel(child_id: str) -> Iterator[Callable[[], dict[str, str]]]:
    """This call alone uses ``child_id`` for the block (a kernel while its lock is held).

    Yields a reader of ``{"network_refused": text}`` for what was refused there during
    the block, named as this call's own (``{}`` when nothing was).
    """

    watch = _WATCH.get() or _Watch(time.monotonic())
    if child_id:
        _register(watch, child_id, _OWN)
    try:
        yield lambda: _fields(watch, {child_id} - {""})
    finally:
        with _LOCK:
            _drop(watch, [child_id])


def _fields(watch: _Watch, channels: set[str]) -> dict[str, str]:
    text = _text(watch, watch.unreported(_seen_by(watch, channels)))
    return {"network_refused": text} if text else {}


def channel_of(confinement: Any) -> str:
    """The channel a confined spawn's ``SandboxResult`` was given (``""`` when none)."""

    details = getattr(confinement, "details", None) or {}
    return str(details.get("net_child_id") or "") if isinstance(details, dict) else ""


def call_notice() -> str:
    """The refusals on the channels this call used, while it used them, once; ``""``."""

    watch = _WATCH.get()
    if watch is None or not watch.channels:
        return ""
    return _text(watch, watch.unreported(_seen_by(watch, watch.channels)))


#: Calls whose work outlasts them (a server's task), by task handle: the call's watch.
_OUTLASTING: dict[str, _Watch] = {}


def outlast(handle: str, keep: Callable[[str], None] | None = None) -> None:
    """Keep this call's watch for ``handle``, work that settles after the call returns.

    The call keeps using its channels until :func:`settled`. ``keep(handle)`` runs after
    each refusal told to it, on the refusing thread: it persists :func:`settled_notice`
    with the task, so a restart does not lose it. It must only schedule work.
    """

    watch = _WATCH.get()
    if watch is None or not handle:
        return
    with _LOCK:
        if len(_OUTLASTING) >= _MAX_CHANNELS:
            old = next(iter(_OUTLASTING))
            _release(old)
            logger.info("net refusals dropped reason=outlasting_cap_reached task=%s", old)
        _OUTLASTING[handle] = watch
        watch.outlasting[handle] = keep


def _release(handle: str) -> None:
    """Stop keeping ``handle``'s watch (caller holds ``_LOCK``)."""

    watch = _OUTLASTING.pop(handle, None)
    if watch is None:
        return
    watch.outlasting.pop(handle, None)
    if not watch.open and not watch.outlasting:
        _drop(watch, list(watch.channels))


def settled_notice(handle: str) -> str:
    """The refusals told to ``handle``'s call since its call last reported (``""`` if none)."""

    with _LOCK:
        watch = _OUTLASTING.get(handle)
    if watch is None:
        return ""
    found = _seen_by(watch, watch.channels)
    return _text(watch, [r for r in found if r.seq not in watch.reported])


def settled(handle: str) -> None:
    """``handle``'s result is recorded: stop keeping its watch."""

    with _LOCK:
        _release(handle)


def with_call_notice(value: Any) -> Any:
    """``value`` with :func:`call_notice` added: appended to text, a key on a mapping."""

    text = call_notice()
    if not text:
        return value
    if isinstance(value, str):
        return f"{value}\n\n{text}"
    if isinstance(value, dict):
        return {**value, "network_refused": text}
    return {"result": value, "network_refused": text}


def channel_fields(confinement: Any) -> dict[str, str]:
    """``{"network_refused": text}`` for a spawn's own channel (one command's), or ``{}``.

    What it names is this call's: the call's own notice does not name it again.
    """

    found = refusals({channel_of(confinement)} - {""})
    watch = _WATCH.get()
    text = notice_text(watch.unreported(found) if watch is not None else found)
    return {"network_refused": text} if text else {}


def confined_fields(confinement: Any, **diagnosis: Any) -> dict[str, Any]:
    """The result fields a confined spawn's refusals earn, for a seam that owns its result.

    ``boundary``: the write the fence refused
    (:func:`clio_agent.runtime.sandbox_diagnosis.diagnose_confined`, given ``diagnosis``).
    ``network_refused``: the egress refused on the spawn's own channel, which its output
    shows only as a 403 or a reset. ``{}`` when neither.
    """
    from clio_agent.runtime.sandbox_diagnosis import diagnose_confined  # noqa: PLC0415

    boundary = diagnose_confined(confinement, **diagnosis)
    return {**({"boundary": boundary} if boundary else {}), **channel_fields(confinement)}


def _lines(found: list[EgressRefusal]) -> str:
    counted: dict[tuple[str, int, str, str], int] = {}
    for refusal in found:
        key = (refusal.host, refusal.port, refusal.reason, refusal.rule)
        counted[key] = counted.get(key, 0) + 1
    lines = []
    for (host, port, reason, rule), count in counted.items():
        why = _PHRASES.get(reason, f"CLIO could not decide ({reason})")
        decided = f"rule: {rule}; " if rule else ""
        times = f", {count} connections" if count > 1 else ""
        lines.append(f"- {host}:{port}: {why} ({decided}reason: {reason}{times})")
    return "\n".join(lines)


def notice_text(found: list[EgressRefusal]) -> str:
    """What the agent reads for its own channel: each refused host once, why, and the rule."""

    if not found:
        return ""
    return (
        "CLIO's sandbox refused network connections this call made. A connection error "
        "or reset in the tool's output for these hosts is this refusal:\n" + _lines(found)
    )


def _others(count: int) -> str:
    if count == 0:
        return "no other call was using it then"
    return f"{count} other call{'s were' if count > 1 else ' was'} using it then"


def _text(watch: _Watch, found: list[EgressRefusal]) -> str:
    """``found`` as ``watch``'s call reads it: its own channels' refusals as its own; a
    shared channel's as made by that process while the call ran, with how many other
    calls were using it, since the network sees the process, not the request."""

    own = [r for r in found if watch.channels.get(r.child_id, _OWN).own]
    shared: dict[str, list[EgressRefusal]] = {}
    for refusal in found:
        use = watch.channels.get(refusal.child_id, _OWN)
        if not use.own:
            shared.setdefault(use.label or "a process this call uses", []).append(refusal)
    parts = [notice_text(own)] if own else []
    for label, refusals_ in shared.items():
        others = {w for r in refusals_ for w in r.watchers} - {watch.id}
        parts.append(
            f"While this call ran, CLIO's sandbox refused network connections that {label} "
            f"made. This call uses it, but CLIO cannot tell which request made them "
            f"({_others(len(others))}). A connection error or reset in the tool's output "
            "for these hosts may be this refusal:\n" + _lines(refusals_)
        )
    return "\n\n".join(parts)


def clear() -> None:
    """Forget every refusal (test isolation seam)."""

    with _LOCK:
        _LEDGER.clear()
        _OUTLASTING.clear()
        _ON_CHANNEL.clear()


__all__ = [
    "CHILD_HOST_NOT_ALLOWED",
    "GATE_DENY",
    "PRIVATE_ADDRESS",
    "TARGET_IS_CLIO",
    "EgressDenial",
    "EgressRefusal",
    "call_notice",
    "channel_fields",
    "channel_of",
    "clear",
    "confined_fields",
    "forget",
    "join",
    "notice_text",
    "outlast",
    "own_channel",
    "record",
    "refusals",
    "refused",
    "settled",
    "settled_notice",
    "watching",
    "with_call_notice",
]
