"""The egress CLIO's chokepoint refused, kept per channel so the refused call can say so.

A process the fence confines reaches the network only through its channel on the
chokepoint (:mod:`clio_agent.runtime.net_chokepoint`). When the chokepoint refuses a
connection the client sees only the wire: a bare ``403``, or a reset when a proxy in
between (Codex's managed proxy under MXC) had already answered ``200``. The tool's own
text then reads as a network failure, and the agent retries or blames the host.

The chokepoint records each refusal here with its typed reason and, when a person's
rule decided it, that rule. A tool call watches the channels that serve it: the call
scope opens a watch (:func:`watching`), the seam that knows which channel serves the
call joins it (:func:`join`), and the call's result carries the refusals made on those
channels while it ran (:func:`call_notice`). A seam whose channel is its own for one
command (the shell) reads that channel directly (:func:`channel_fields`). Work that
settles after its call returns (a server's task) keeps the call's watch until its result
is recorded (:func:`outlast`, :func:`settled_notice`).
"""

from __future__ import annotations

import itertools
import logging
import threading
import time
from collections import deque
from collections.abc import Iterator
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


_LOCK = threading.Lock()
_SEQ = itertools.count(1)
_LEDGER: dict[str, deque[EgressRefusal]] = {}


def record(child_id: str, host: str, port: int, reason: str, rule: str = "") -> None:
    """Keep one refusal on ``child_id``'s channel (the shared listener has none: skipped)."""

    if not child_id:
        return
    with _LOCK:
        refusal = EgressRefusal(
            child_id, host, int(port or 0), reason, rule, time.monotonic(), next(_SEQ)
        )
        kept = _LEDGER.get(child_id)
        if kept is None:
            if len(_LEDGER) >= _MAX_CHANNELS:
                oldest = next(iter(_LEDGER))
                _LEDGER.pop(oldest)
                logger.info("net refusals dropped reason=ledger_cap_reached child=%s", oldest)
            kept = _LEDGER[child_id] = deque(maxlen=_PER_CHANNEL)
        kept.append(refusal)


def refused(child_id: str, host: str, port: int, reason: str, rule: str = "") -> bool:
    """:func:`record` the refusal; ``False``, the chokepoint's answer for it."""

    record(child_id, host, port, reason, rule)
    return False


def forget(child_id: str) -> None:
    """Drop a closed channel's refusals."""

    with _LOCK:
        _LEDGER.pop(child_id, None)


def refusals(channels: set[str] | frozenset[str], since: float = 0.0) -> list[EgressRefusal]:
    """The refusals on ``channels`` made at or after ``since`` (monotonic), oldest first."""

    with _LOCK:
        found = [r for c in channels for r in _LEDGER.get(c, ()) if r.at >= since]
    return sorted(found, key=lambda r: r.at)


@dataclass
class _Watch:
    started: float
    channels: set[str] = field(default_factory=set)
    reported: set[int] = field(default_factory=set)  # refusals a result already named

    def unreported(self, found: list[EgressRefusal]) -> list[EgressRefusal]:
        """``found`` less what this call's results named; those are now named."""

        found = [r for r in found if r.seq not in self.reported]
        self.reported.update(r.seq for r in found)
        return found


#: One tool call's watch. A mutable holder, so a copied context (the app loop's task,
#: a worker thread) joins channels to the same call.
_WATCH: ContextVar[_Watch | None] = ContextVar("clio_net_refusal_watch", default=None)


@contextmanager
def watching() -> Iterator[None]:
    """Watch the channels a tool call joins; a nested call gets its own watch."""

    token = _WATCH.set(_Watch(time.monotonic()))
    try:
        yield
    finally:
        _WATCH.reset(token)


def join(child_id: str) -> None:
    """Attribute ``child_id``'s refusals, from this call's start, to this call."""

    watch = _WATCH.get()
    if watch is not None and child_id:
        watch.channels.add(child_id)


def channel_of(confinement: Any) -> str:
    """The channel a confined spawn's ``SandboxResult`` was given (``""`` when none)."""

    details = getattr(confinement, "details", None) or {}
    return str(details.get("net_child_id") or "") if isinstance(details, dict) else ""


def call_notice() -> str:
    """The refusals on the channels this call joined, once; ``""`` when none."""

    watch = _WATCH.get()
    if watch is None or not watch.channels:
        return ""
    found = watch.unreported(refusals(watch.channels, watch.started))
    watch.started = time.monotonic()  # a later read reports only newer refusals
    return notice_text(found)


#: Calls whose work outlasts them (a server's task), by task handle: the call's watch.
_OUTLASTING: dict[str, _Watch] = {}


def outlast(handle: str) -> None:
    """Keep this call's watch for ``handle``, work that settles after the call returns."""

    watch = _WATCH.get()
    if watch is None or not handle:
        return
    with _LOCK:
        if len(_OUTLASTING) >= _MAX_CHANNELS:
            dropped = _OUTLASTING.pop(next(iter(_OUTLASTING)))
            logger.info("net refusals dropped reason=outlasting_cap_reached at=%s", dropped.started)
        _OUTLASTING[handle] = watch


def settled_notice(handle: str) -> str:
    """The refusals made for ``handle`` since its call last reported (``""`` when none)."""

    with _LOCK:
        watch = _OUTLASTING.get(handle)
    if watch is None:
        return ""
    found = refusals(watch.channels, watch.started)
    return notice_text([r for r in found if r.seq not in watch.reported])


def settled(handle: str) -> None:
    """``handle``'s result is recorded: stop keeping its watch."""

    with _LOCK:
        _OUTLASTING.pop(handle, None)


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


def channel_fields(confinement: Any, since: float = 0.0) -> dict[str, str]:
    """``{"network_refused": text}`` for a spawn's own channel from ``since``, or ``{}``.

    What it names is this call's: the call's own notice does not name it again.
    """

    found = refusals({channel_of(confinement)} - {""}, since)
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


def notice_text(found: list[EgressRefusal]) -> str:
    """What the agent reads: each refused host once, why, and the rule that decided."""

    if not found:
        return ""
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
    return (
        "CLIO's sandbox refused network connections this call made. A connection error "
        "or reset in the tool's output for these hosts is this refusal:\n" + "\n".join(lines)
    )


def clear() -> None:
    """Forget every refusal (test isolation seam)."""

    with _LOCK:
        _LEDGER.clear()
        _OUTLASTING.clear()


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
    "record",
    "refusals",
    "refused",
    "settled",
    "settled_notice",
    "watching",
    "with_call_notice",
]
