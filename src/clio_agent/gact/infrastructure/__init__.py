"""CLIO infrastructure; importing platform helpers does not boot its service stack."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from clio_agent.gact.infrastructure.runtime import InfrastructureRuntime
    from clio_agent.gact.infrastructure.store import InfrastructureStore
    from clio_agent.gact.infrastructure.transport import InfrastructureTransportRegistry

__all__ = ["InfrastructureRuntime", "InfrastructureStore", "InfrastructureTransportRegistry"]


def __getattr__(name: str) -> Any:
    """Load public infrastructure classes only when a caller requests them."""
    modules = {
        "InfrastructureRuntime": "runtime",
        "InfrastructureStore": "store",
        "InfrastructureTransportRegistry": "transport",
    }
    if name not in modules:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(f"{__name__}.{modules[name]}"), name)
    globals()[name] = value
    return value
