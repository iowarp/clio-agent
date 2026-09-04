"""Operator-tunable bounds on the public resource-conversion activity window.

Owner module (#775 no-accretion): ``resource_processing.py`` sits against its
size cap, and these bounds are read by two callers anyway --
``bounded_processing_events`` (which normalizes an untrusted converter's event
list into what clients are served) and ``wait_for_workspace_resource_processing``
(which polls a conversion task inside a caller-supplied budget).

Every value here is configuration under the ``resources.*`` namespace, per
``resource_tools.py``'s own doctrine -- "bounds are configuration, and one key
feeds both surfaces of the same semantic bound". A converter is a third-party
service (Docling today) whose event volume and message length CLIO does not
control, so how much of it is retained and served is a deployment decision, not
a compiled-in constant.
"""

from __future__ import annotations

from typing import NamedTuple

from clio_agent import conf


class ProcessingEventBounds(NamedTuple):
    """The three bounds applied to one converter's reported event list."""

    max_records: int
    message_chars: int
    stage_chars: int


def processing_event_bounds() -> ProcessingEventBounds:
    """Resolve the activity window's record count and per-field character caps.

    Config: ``resources.processing_event_max_records`` /
    ``resources.processing_event_message_chars`` /
    ``resources.processing_event_stage_chars`` (env
    ``CLIO_RESOURCE_PROCESSING_EVENT_MAX_RECORDS`` /
    ``..._MESSAGE_CHARS`` / ``..._STAGE_CHARS``). Resolved together because a
    caller that normalizes one field normalizes all three.
    """

    return ProcessingEventBounds(
        max_records=max(
            1,
            conf.resolve(
                "resources.processing_event_max_records",
                env="CLIO_RESOURCE_PROCESSING_EVENT_MAX_RECORDS",
                default=100,
                cast=conf.as_int,
            ),
        ),
        message_chars=max(
            1,
            conf.resolve(
                "resources.processing_event_message_chars",
                env="CLIO_RESOURCE_PROCESSING_EVENT_MESSAGE_CHARS",
                default=1000,
                cast=conf.as_int,
            ),
        ),
        stage_chars=max(
            1,
            conf.resolve(
                "resources.processing_event_stage_chars",
                env="CLIO_RESOURCE_PROCESSING_EVENT_STAGE_CHARS",
                default=80,
                cast=conf.as_int,
            ),
        ),
    )


def processing_poll_interval_s() -> float:
    """Seconds between conversion-status refreshes inside one bounded wait.

    Config: ``resources.processing_poll_interval_s`` /
    ``CLIO_RESOURCE_PROCESSING_POLL_INTERVAL_S``. This is cadence, not budget:
    the caller still supplies the deadline, and the wait sleeps the smaller of
    this interval and the time remaining. Deployments whose converter status
    endpoint is slow or rate-limited raise it; a local processor can lower it.
    """

    return max(
        0.01,
        conf.resolve(
            "resources.processing_poll_interval_s",
            env="CLIO_RESOURCE_PROCESSING_POLL_INTERVAL_S",
            default=0.5,
            cast=conf.as_float,
        ),
    )


__all__ = [
    "ProcessingEventBounds",
    "processing_event_bounds",
    "processing_poll_interval_s",
]
