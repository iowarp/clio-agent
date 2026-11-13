"""``web_search`` and ``web_fetch``: source rotation, questions, filters, page reading.

``web_search`` tries the person's web sources in order. A source that is resting
(rate limit, used-up allowance, failure) is passed over; when its *allowance* is
used up CLIO asks the person once whether to switch (see :mod:`.broker`). Every
pass-over is a typed note in the result. The result is compact; the source's full
answer is saved to a file named in ``payload_file``.

``web_fetch`` reads a page locally first. Only when that fails for a reason a
hosted reader can fix (blocked, empty, script-only, a PDF) and the URL is public
does it ask an approved source's hosted reader, and it says so.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from clio_agent.providers.api_key_store import ProviderApiKeyStore
from clio_agent.search import broker, http, web_sources
from clio_agent.search.http import SourceError
from clio_agent.search.reasons import SearchNote, note
from clio_agent.search.records import save_payload
from clio_agent.search.scheduler import (
    SCHEDULER,
    WEB_PATIENCE_S,
    CallEvents,
    QueueBusy,
    caller_id,
    queue_notes,
)
from clio_agent.search.settings import (
    SERVICE_SOURCE,
    SearchSettings,
    key_ref,
    load_search_settings,
)
from clio_agent.search.terms import terms_of
from clio_agent.search.web_sources import WebQuery
from clio_agent.tools.servers.web_limits import SEARCH_LIMITS

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_RESTING_FOR_ALLOWANCE = (http.ALLOWANCE_USED,)


@dataclass
class SearchFailure(Exception):
    """A search that could not answer, with its typed reason and notes."""

    code: str
    detail: str
    notes: list[SearchNote]

    def as_result(self) -> dict[str, Any]:
        """The tool result for the failure."""

        primary = note(self.code, detail=self.detail).as_dict()
        return {"ok": False, "error": primary, "notes": [n.as_dict() for n in self.notes]}


def source_key(source: str) -> str:
    """The person's saved key for ``source`` (``""`` when none)."""

    try:
        return ProviderApiKeyStore().load(key_ref(source))
    except Exception:  # noqa: BLE001 - an unreadable key store means "no key", reported in Settings
        return ""


def check_query(query: WebQuery) -> None:
    """Refuse malformed arguments with a typed error naming the fix."""

    if not query.query.strip():
        raise ValueError("query must not be empty")
    for name in ("after", "before"):
        value = getattr(query, name)
        if value and not _DATE.match(value):
            raise ValueError(f"{name} must be a date written YYYY-MM-DD")
    if query.kind not in web_sources.KINDS:
        raise ValueError(f"kind must be one of {', '.join(web_sources.KINDS)}")
    if not 1 <= query.limit <= 50:
        raise ValueError("limit must be between 1 and 50")


def _usable(source: str) -> bool:
    return not SEARCH_LIMITS.declined(source) and SEARCH_LIMITS.cooldown(source) is None


async def _approved(source: str, settings: SearchSettings) -> bool:
    if settings.is_approved(source):
        return True
    if SEARCH_LIMITS.declined(source):
        return False
    answer = await broker.gate(broker.approval(source))
    return answer == broker.APPROVE


async def web_search(query: WebQuery) -> dict[str, Any]:
    """Answer one search through the first source that can.

    Raises:
        broker.AskPerson: the person must answer a question first.
        broker.WaitingForPerson: another caller's question is still unanswered.
        SearchFailure: no source could answer.
    """

    check_query(query)
    settings = load_search_settings()
    if not settings.enabled:
        raise SearchFailure("search_disabled", "", [])
    sources = list(settings.web_sources())
    if not sources:
        raise SearchFailure("search_no_source", "", [])
    notes: list[SearchNote] = []
    index = 0
    while index < len(sources):
        source = sources[index]
        if not await _approved(source, settings):
            notes.append(note("search_not_approved", source))
            index += 1
            continue
        resting = SEARCH_LIMITS.cooldown(source)
        if resting is not None:
            until = resting.until
            later = [name for name in sources[index + 1 :] if _usable(name)]
            if resting.kind in _RESTING_FOR_ALLOWANCE and later and not settings.auto_switch:
                answer = await broker.gate(broker.exhausted(source, resting.epoch, until, later[0]))
                if answer in (broker.STOP, broker.ADD_KEY):
                    detail = (
                        "add a key in Settings → Web search" if answer == broker.ADD_KEY else ""
                    )
                    raise SearchFailure("search_stopped_by_person", detail, notes)
            code = (
                "search_source_allowance_used"
                if resting.kind == http.ALLOWANCE_USED
                else "search_source_rate_limited"
                if resting.kind == http.RATE_LIMITED
                else "search_source_failed"
            )
            notes.append(note(code, source, resting.detail, _iso(until)))
            index += 1
            continue
        key = source_key(source)
        events = CallEvents()
        try:
            answer_ = await SCHEDULER.call(
                source,
                lambda source=source, key=key: web_sources.search(
                    source, query, key=key, timeout=settings.request_timeout_s,
                    service_url=settings.service_url,
                ),
                terms=terms_of(source, settings, has_key=bool(key)),
                caller=caller_id(),
                patience_s=WEB_PATIENCE_S,
                events=events,
            )  # fmt: skip
        except QueueBusy as exc:
            notes.append(note("search_source_busy", source, exc.detail))
            index += 1
            continue
        except SourceError as exc:
            if SEARCH_LIMITS.cooldown(source) is None:  # not resting: report it and move on
                notes.append(note("search_source_failed", source, exc.detail))
                index += 1
            continue  # the queue rested the source: the branch above decides
        finally:
            notes.extend(queue_notes(source, events))
        if index:
            notes.append(note("search_rotated", source, f"after {', '.join(sources[:index])}"))
        return _result(query, source, answer_, notes, settings)
    raise SearchFailure("search_all_sources_unavailable", "", notes)


def _iso(epoch_seconds: float) -> str:
    import datetime as _dt  # noqa: PLC0415

    return _dt.datetime.fromtimestamp(epoch_seconds, tz=_dt.UTC).isoformat(timespec="minutes")


def _result(
    query: WebQuery,
    source: str,
    answer: web_sources.SourceAnswer,
    notes: list[SearchNote],
    settings: SearchSettings,
) -> dict[str, Any]:
    kept, by_clio = web_sources.post_filter(answer, query)
    if by_clio:
        detail = ", ".join(by_clio)
        notes.append(note("search_filters_approximate", source, detail))
    payload = save_payload(
        "search",
        source,
        {
            "query": query.__dict__,
            "source": source,
            "results": [row.__dict__ for row in answer.results],
            "raw": answer.raw,
        },
    )
    if not payload:
        notes.append(note("search_payload_not_saved", source))
    row: dict[str, Any] = {
        "ok": True,
        "query": query.query,
        "source": source,
        "results": [item.compact(settings.snippet_chars) for item in kept],
        "notes": [n.as_dict() for n in notes],
    }
    if payload:
        row["payload_file"] = payload
    if by_clio:
        row["filters_applied_by_clio"] = by_clio
    return row


# ----------------------------------------------------------------- web_fetch

_TOKEN_PARAMS = re.compile(
    r"^(token|access_token|id_token|auth|authorization|key|api_?key|sig|signature|"
    r"x-amz-signature|x-amz-credential|x-goog-signature|sv|se|sp|code|session|password)$",
    re.IGNORECASE,
)
_PRIVATE_SUFFIXES = (".local", ".internal", ".lan", ".home", ".corp", ".intranet", ".localhost")


def is_public_url(url: str) -> bool:
    """Whether a hosted reader may see ``url``: public host, no credentials, no tokens."""

    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or parts.username or parts.password:
        return False
    host = (parts.hostname or "").lower()
    if not host or "." not in host or host == "localhost" or host.endswith(_PRIVATE_SUFFIXES):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        return False
    return not any(_TOKEN_PARAMS.match(name) for name, _ in parse_qsl(parts.query))


def _readers(settings: SearchSettings) -> list[str]:
    enabled = set(settings.web_sources())
    return [
        name
        for name in web_sources.READERS
        if name in enabled and settings.is_approved(name) and _usable(name)
    ]


async def hosted_read(url: str, settings: SearchSettings, why: str) -> dict[str, Any] | None:
    """Read ``url`` through the first approved hosted reader, or ``None``."""

    if not is_public_url(url):
        return {"note": note("fetch_private_url", detail=why)}
    for source in _readers(settings):
        key = source_key(source)
        try:
            title, text = await SCHEDULER.call(
                source,
                lambda source=source, key=key: web_sources.read_page(source, url, key=key),
                terms=terms_of(source, settings, has_key=bool(key)),
                caller=caller_id(),
                patience_s=WEB_PATIENCE_S,
            )
        except SourceError:
            continue
        if text.strip():
            return {
                "title": title,
                "text": text,
                "reader": source,
                "note": note("fetch_hosted_reader", source, why),
            }
    return None


__all__ = [
    "SERVICE_SOURCE",
    "SearchFailure",
    "check_query",
    "hosted_read",
    "is_public_url",
    "source_key",
    "web_search",
]
