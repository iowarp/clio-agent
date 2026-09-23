"""The A2UI action-lifecycle owner package (S5).

docs/design/a2ui-compat-campaign-2026-09.md S5: every client-reported A2UI
event (action or error) becomes a durable, idempotent, correlated transcript
record and is delivered by session state, never by its name. See:

* :mod:`clio_agent.gact.a2ui_actions.record` — the record's shape,
  idempotency key, transcript encoding, and fold.
* :mod:`clio_agent.gact.a2ui_actions.delivery` — the shared idle/running/
  waiting_user agent-lane delivery matrix (actions AND repair errors).
* :mod:`clio_agent.gact.a2ui_actions.narration` — deterministic, bounded
  model-facing text.
* :mod:`clio_agent.gact.a2ui_actions.client_state` — client data-model
  filtering and client error-report ingestion.
* :mod:`clio_agent.gact.a2ui_actions.dispatcher` — the entry point,
  ``dispatch_action``, moved out of ``routes/a2ui.py``.
"""

from __future__ import annotations

from clio_agent.gact.a2ui_actions.dispatcher import dispatch_action
from clio_agent.gact.a2ui_actions.record import (
    ActionRecord,
    fold_action_records,
    mark_a2ui_action_consumed,
)

__all__ = [
    "ActionRecord",
    "dispatch_action",
    "fold_action_records",
    "mark_a2ui_action_consumed",
]
