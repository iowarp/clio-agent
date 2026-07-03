"""Tests for the per-app :class:`ProviderProfileStore` (design §3.4 / §9 step 4).

The store is an immutable, RCU-swapped snapshot mapping profile-id -> ``LMSpec``
with one ``"default"`` entry. These tests prove:

* ``with_default`` / ``with_profile`` return a NEW snapshot, leaving the receiver
  unchanged (copy-on-write);
* the backing mapping is read-only (cannot be mutated through ``profiles()``);
* a concurrent reader always sees a whole old-or-new snapshot across a stream of
  atomic pointer swaps — never a torn multi-key mix;
* two ``build_app`` instances hold independent stores (two-app topology);
* boot seeds a ``"default"`` spec matching ``load_config_from_env()``.
"""

from __future__ import annotations

import threading
from types import MappingProxyType

import pytest

from clio_agent.config import load_config_from_env
from clio_agent.gact.app import build_app
from clio_agent.gact.providers.profile_store import (
    DEFAULT_PROFILE_ID,
    ProviderProfileStore,
)
from clio_agent.providers.lm_spec import LMSpec, spec_from_config


def _spec(model: str, provider: str = "lm_studio") -> LMSpec:
    """Return a minimal spec tagged by ``model`` (used as a generation marker)."""
    return LMSpec(provider=provider, model=model)


def test_seed_holds_single_default() -> None:
    """``seed`` produces a store whose only entry is the default profile."""
    spec = _spec("m0")
    store = ProviderProfileStore.seed(spec)
    assert store.default == spec
    assert store.get(DEFAULT_PROFILE_ID) == spec
    assert store.ids() == (DEFAULT_PROFILE_ID,)


def test_with_default_returns_new_snapshot_old_unchanged() -> None:
    """``with_default`` is copy-on-write: old store keeps its old default."""
    old = ProviderProfileStore.seed(_spec("old"))
    new = old.with_default(_spec("new"))

    assert new is not old
    assert new.default == _spec("new")
    assert old.default == _spec("old")  # receiver untouched


def test_with_profile_returns_new_snapshot_old_unchanged() -> None:
    """``with_profile`` adds a keyed profile without touching the receiver."""
    old = ProviderProfileStore.seed(_spec("d"))
    new = old.with_profile("expertA", _spec("a"))

    assert new is not old
    assert new.get("expertA") == _spec("a")
    assert new.default == _spec("d")  # default carried forward
    assert old.get("expertA") is None  # receiver has no such profile
    assert set(new.ids()) == {DEFAULT_PROFILE_ID, "expertA"}


def test_with_profile_rejects_empty_id() -> None:
    """An empty profile id is a programming error, not a silent no-op."""
    store = ProviderProfileStore.seed(_spec("d"))
    with pytest.raises(ValueError, match="non-empty"):
        store.with_profile("", _spec("x"))


def test_profiles_view_is_read_only() -> None:
    """The exposed mapping cannot be mutated (immutability guarantee)."""
    store = ProviderProfileStore.seed(_spec("d"))
    view = store.profiles()
    assert isinstance(view, MappingProxyType)
    with pytest.raises(TypeError):
        view["expertA"] = _spec("a")  # type: ignore[index]


def test_get_missing_returns_none() -> None:
    """An unregistered profile id resolves to ``None`` (no KeyError)."""
    store = ProviderProfileStore.seed(_spec("d"))
    assert store.get("does-not-exist") is None


def test_concurrent_reader_sees_whole_snapshot_across_swaps() -> None:
    """A reader always sees a self-consistent whole snapshot, never a torn mix.

    A writer thread repeatedly installs a NEW store where the default profile and
    a coupled ``"mirror"`` profile both carry the same generation marker, then
    swaps it atomically. A reader captures the store reference once and asserts
    the two coupled markers agree — which can only hold if it observed a whole
    old-or-new snapshot (the RCU guarantee), never a half-written multi-key
    state.
    """

    class _Holder:
        store: ProviderProfileStore

    holder = _Holder()
    holder.store = ProviderProfileStore.seed(_spec("gen-0")).with_profile("mirror", _spec("gen-0"))

    stop = threading.Event()
    errors: list[str] = []
    iterations = 4000

    def writer() -> None:
        for gen in range(1, iterations):
            marker = f"gen-{gen}"
            new = ProviderProfileStore.seed(_spec(marker)).with_profile("mirror", _spec(marker))
            holder.store = new  # atomic pointer swap
        stop.set()

    def reader() -> None:
        while not stop.is_set():
            snapshot = holder.store  # capture the reference ONCE
            default_marker = snapshot.default.model
            mirror = snapshot.get("mirror")
            mirror_marker = mirror.model if mirror is not None else None
            if default_marker != mirror_marker:
                errors.append(f"torn read: {default_marker!r} != {mirror_marker!r}")
                return

    threads = [
        threading.Thread(target=writer),
        threading.Thread(target=reader),
        threading.Thread(target=reader),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, errors[0]
    assert holder.store.default.model == f"gen-{iterations - 1}"


def test_two_build_app_instances_hold_independent_stores(tmp_path) -> None:
    """Two apps in one process hold independent stores (two-app topology).

    A pointer swap on one app's store must not be visible on the other — the
    #815 scope-mismatch fix: per-app state, not one shared process-global.
    """
    app_a = build_app(sessions_path=tmp_path / "a" / "s.json")
    app_b = build_app(sessions_path=tmp_path / "b" / "s.json")

    store_a = app_a.state.provider_profiles
    store_b = app_b.state.provider_profiles
    assert isinstance(store_a, ProviderProfileStore)
    assert isinstance(store_b, ProviderProfileStore)

    # Swap app A's default only.
    marker = _spec("only-a", provider="openai")
    app_a.state.provider_profiles = store_a.with_default(marker)

    assert app_a.state.provider_profiles.default == marker
    # App B is untouched — no cross-app interleave.
    assert app_b.state.provider_profiles is store_b
    assert app_b.state.provider_profiles.default != marker


def test_boot_seeds_default_matching_env_config(tmp_path) -> None:
    """build_app seeds a ``"default"`` spec matching ``load_config_from_env()``."""
    app = build_app(sessions_path=tmp_path / "s.json")

    store = app.state.provider_profiles
    assert isinstance(store, ProviderProfileStore)
    assert store.ids() == (DEFAULT_PROFILE_ID,)

    expected = spec_from_config(load_config_from_env())
    assert store.default == expected
