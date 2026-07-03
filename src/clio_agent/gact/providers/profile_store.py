"""Immutable, per-app provider-profile registry (design §3.4 / §9 step 4).

``app.state.provider_profiles`` holds a :class:`ProviderProfileStore` — an
**immutable snapshot** mapping a profile-id to an
:class:`~clio_agent.providers.lm_spec.LMSpec`, always carrying a ``"default"``
entry. The snapshot is never mutated in place; a change produces a *new* whole
snapshot which the caller installs by a single atomic pointer assignment
(``app.state.provider_profiles = old.with_default(spec)``). Under the GIL that
assignment is atomic, so a concurrent reader always sees either the old or the
new whole snapshot — never a torn, half-written multi-key mix. This is the RCU
(read-copy-update) discipline from design §3.4 and replaces the process-global
``os.environ`` + dspy ``main_thread_config`` mutation the reverted lock (48969af)
tried to guard.

The store is **per-app** (per FastAPI instance): two ``build_app`` instances in
one process hold two independent stores, so the two-app test topology no longer
races a single shared global (design §1, the #815 scope-mismatch finding).

This module is deliberately **additive / shadow**: the store is seeded at boot
but nothing routes LM resolution through it yet (design §9 step 4). The write
side (``_apply_lm_provider`` demotion) and the read side (per-expert resolution)
land in later steps.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Mapping

    from clio_agent.providers.lm_spec import LMSpec

__all__ = ["DEFAULT_PROFILE_ID", "ProviderProfileStore"]

#: The reserved profile-id every store carries. One default profile === today's
#: single-default LM, which is what keeps the change additive (RULE 2).
DEFAULT_PROFILE_ID = "default"


@dataclass(frozen=True)
class ProviderProfileStore:
    """An immutable snapshot mapping profile-id → :class:`LMSpec` (design §3.4).

    Instances are treated as *values*: every mutating operation returns a **new**
    store, leaving the receiver unchanged. Callers install a new snapshot by an
    atomic pointer swap on ``app.state.provider_profiles`` — never by mutating a
    live store. The backing mapping is wrapped in a :class:`types.MappingProxyType`
    so it cannot be mutated even through :meth:`profiles`.

    The store always contains a :data:`DEFAULT_PROFILE_ID` entry; construct one
    with :meth:`seed`.
    """

    _profiles: "Mapping[str, LMSpec]"

    @classmethod
    def seed(cls, default_spec: "LMSpec") -> "ProviderProfileStore":
        """Create a store holding a single ``"default"`` profile.

        Args:
            default_spec: The spec bound to :data:`DEFAULT_PROFILE_ID`. At boot
                this is ``spec_from_config(load_config_from_env())`` so the
                default profile matches today's single-default LM.

        Returns:
            A new store whose only entry is the default profile.
        """
        return cls(MappingProxyType({DEFAULT_PROFILE_ID: default_spec}))

    @property
    def default(self) -> "LMSpec":
        """Return the spec bound to :data:`DEFAULT_PROFILE_ID`."""
        return self._profiles[DEFAULT_PROFILE_ID]

    def get(self, profile_id: str) -> "LMSpec | None":
        """Return the spec for ``profile_id``, or ``None`` if unregistered."""
        return self._profiles.get(profile_id)

    def ids(self) -> tuple[str, ...]:
        """Return the registered profile ids (``"default"`` always present)."""
        return tuple(self._profiles.keys())

    def profiles(self) -> "Mapping[str, LMSpec]":
        """Return a read-only view of the whole snapshot.

        The returned mapping is the store's own :class:`MappingProxyType`; it
        cannot be mutated, so exposing it does not break immutability.
        """
        return self._profiles

    def with_default(self, spec: "LMSpec") -> "ProviderProfileStore":
        """Return a NEW store with the default profile replaced by ``spec``.

        The receiver is left unchanged (copy-on-write). Install the result by an
        atomic pointer swap: ``app.state.provider_profiles = store.with_default(spec)``.

        Args:
            spec: The new default spec.

        Returns:
            A new store; every other profile is carried forward unchanged.
        """
        updated = dict(self._profiles)
        updated[DEFAULT_PROFILE_ID] = spec
        return type(self)(MappingProxyType(updated))

    def with_profile(self, profile_id: str, spec: "LMSpec") -> "ProviderProfileStore":
        """Return a NEW store with ``profile_id`` set to ``spec``.

        The receiver is left unchanged (copy-on-write). ``profile_id`` may be any
        non-empty id; use :meth:`with_default` for the conventional default swap.

        Args:
            profile_id: The profile id to add or replace. Must be non-empty.
            spec: The spec to bind to ``profile_id``.

        Returns:
            A new store with the given profile added/replaced.

        Raises:
            ValueError: If ``profile_id`` is empty.
        """
        if not profile_id:
            raise ValueError("profile_id must be a non-empty string")
        updated = dict(self._profiles)
        updated[profile_id] = spec
        return type(self)(MappingProxyType(updated))
