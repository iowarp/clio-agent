"""What a CLIO-deployed model server applies by default, for discovery to report.

Registered with :mod:`clio_agent.providers.capabilities.server_defaults` when
the infrastructure routes start. It answers from the service records CLIO
already keeps: a running managed Ollama whose connection address matches, and
the default context its startup log stated (its effective ``context_length``,
read by :mod:`clio_agent.gact.infrastructure.effective_parameters`).
"""

from __future__ import annotations

from datetime import datetime, timezone

from clio_agent.gact.infrastructure.store import InfrastructureStore
from clio_agent.providers.api_base import native_root
from clio_agent.providers.capabilities.records import Fact
from clio_agent.providers.capabilities.server_defaults import ContextDefaultLookup


def ollama_context_default_lookup(store: InfrastructureStore) -> ContextDefaultLookup:
    """A lookup ``native root URL -> default context`` over ``store``'s managed Ollamas."""

    def lookup(root: str) -> Fact[int] | None:
        for record in store.services():
            if record.service_id != "ollama" or record.state != "running":
                continue
            if not record.connection_url or native_root(record.connection_url).rstrip("/") != root:
                continue
            row = next(
                (
                    row
                    for row in record.effective_parameters
                    if row.id == "context_length" and row.source == "server_report"
                ),
                None,
            )
            if row is None or not row.value.isdigit() or int(row.value) <= 0:
                return None
            return Fact(
                int(row.value),
                "server_report",
                datetime.now(timezone.utc).isoformat(),
                f"{row.detail} of the CLIO-managed Ollama on {record.target_id} "
                "(applies when a model without Modelfile num_ctx loads)",
            )
        return None

    return lookup


__all__ = ["ollama_context_default_lookup"]
