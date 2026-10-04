"""Read structured host receipts instead of finding state words inside arbitrary logs."""

import json
from collections.abc import Iterable

from clio_agent.gact.infrastructure.models import ServiceObservation
from clio_agent.gact.infrastructure.node_service import MARKER


def parse_observation(output: Iterable[str]) -> ServiceObservation | None:
    """Use the last structured observation; reject malformed marked receipts."""
    found = None
    for chunk in output:
        for line in chunk.splitlines():
            if line.startswith(MARKER):
                value = json.loads(line[len(MARKER) :])
                if "phase" in value:
                    found = ServiceObservation.model_validate(value)
    return found
