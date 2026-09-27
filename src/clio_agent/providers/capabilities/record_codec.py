"""JSON round-trip for the model / deployment capability records.

A persisted copy of a record (the last-good catalog,
:mod:`clio_agent.providers.model_discovery.last_good`) must come back as the
SAME record: every known :class:`~clio_agent.providers.capabilities.records.Fact`
with its value, source, detail and evidence time. A hand-picked subset loses
whatever it forgot to list -- the last-good list used to restore only limits,
tools, modalities and task, so a served last-good OpenRouter catalog lost every
reasoning, structured-output, router, free and pricing fact.

The codec is driven by the records' own field annotations (``Fact[int]``,
``Fact[frozenset[str]]``, ``Fact[ThinkingSpec]``, ...), so a field added to a
record round-trips without touching this module. Unknown facts are not written;
a value that does not decode is dropped with a typed log reason (never guessed).
"""

from __future__ import annotations

import dataclasses
import logging
import types
import typing
from decimal import Decimal, InvalidOperation
from typing import Any, TypeVar, get_args, get_origin

from clio_agent.providers.capabilities.records import (
    DeploymentCapabilities,
    Fact,
    ModelCapabilities,
    unknown,
)

logger = logging.getLogger(__name__)

R = TypeVar("R", ModelCapabilities, DeploymentCapabilities)


class _Undecodable(ValueError):
    """A persisted value that does not fit its field's type."""


_FACT_TYPES: dict[str, dict[str, Any]] = {}


def _fact_types(record_type: type) -> dict[str, Any]:
    """``field name -> X`` for every ``Fact[X]`` field of a record dataclass (memoized)."""
    key = f"{record_type.__module__}.{record_type.__qualname__}"
    if key not in _FACT_TYPES:
        hints = typing.get_type_hints(record_type)
        _FACT_TYPES[key] = {
            name: get_args(hint)[0]
            for name, hint in hints.items()
            if get_origin(hint) is Fact and get_args(hint)
        }
    return _FACT_TYPES[key]


def _encode(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _encode(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, (frozenset, set)):
        return sorted(_encode(item) for item in value)
    if isinstance(value, tuple):
        return [_encode(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _encode(item) for key, item in value.items()}
    return value


def _decode(value: Any, hint: Any) -> Any:
    """Rebuild ``value`` as the annotated type ``hint`` (raises :class:`_Undecodable`)."""
    origin, args = get_origin(hint), get_args(hint)
    if hint is Any:
        return value
    if origin in (typing.Union, types.UnionType):
        if value is None and type(None) in args:
            return None
        for option in (arg for arg in args if arg is not type(None)):
            try:
                return _decode(value, option)
            except _Undecodable:
                continue
        raise _Undecodable(f"{value!r} fits none of {hint}")
    if origin is typing.Literal:
        if value in args:
            return value
        raise _Undecodable(f"{value!r} not in {args}")
    if origin is frozenset:
        if not isinstance(value, list):
            raise _Undecodable(f"expected a list for {hint}")
        return frozenset(_decode(item, args[0]) for item in value)
    if origin is tuple:
        if not isinstance(value, list):
            raise _Undecodable(f"expected a list for {hint}")
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(_decode(item, args[0]) for item in value)
        if len(args) != len(value):
            raise _Undecodable(f"expected {len(args)} items for {hint}")
        return tuple(_decode(item, arg) for item, arg in zip(value, args, strict=True))
    if origin is dict:
        if not isinstance(value, dict):
            raise _Undecodable(f"expected an object for {hint}")
        return {str(k): _decode(v, args[1] if args else Any) for k, v in value.items()}
    if hint is Decimal:
        try:
            return Decimal(str(value))
        except InvalidOperation as exc:
            raise _Undecodable(f"{value!r} is not a decimal") from exc
    if isinstance(hint, type) and dataclasses.is_dataclass(hint):
        if not isinstance(value, dict):
            raise _Undecodable(f"expected an object for {hint.__name__}")
        field_hints = typing.get_type_hints(hint)
        kwargs = {
            f.name: _decode(value[f.name], field_hints[f.name])
            for f in dataclasses.fields(hint)
            if f.name in value
        }
        return hint(**kwargs)
    if hint is float:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        raise _Undecodable(f"{value!r} is not a number")
    if hint is int and isinstance(value, bool):
        raise _Undecodable(f"{value!r} is a bool, not an int")
    if isinstance(hint, type) and isinstance(value, hint):
        return value
    raise _Undecodable(f"{value!r} is not a {hint}")


def encode_record(record: ModelCapabilities | DeploymentCapabilities) -> dict[str, Any]:
    """A JSON-safe dict of the record's identity fields and every KNOWN fact."""
    out: dict[str, Any] = {}
    fact_types = _fact_types(type(record))
    for f in dataclasses.fields(record):
        value = getattr(record, f.name)
        if f.name in fact_types:
            if isinstance(value, Fact) and value.known:
                out[f.name] = {
                    "value": _encode(value.value),
                    "source": value.source,
                    "observed_at": value.observed_at,
                    "detail": value.detail,
                }
        else:
            out[f.name] = _encode(value)
    return out


def decode_record(record_type: type[R], data: Any, **identity: Any) -> R | None:
    """Rebuild a record from :func:`encode_record` output, or ``None`` if unusable.

    Args:
        record_type: :class:`ModelCapabilities` or :class:`DeploymentCapabilities`.
        data: The persisted dict.
        **identity: Identity fields that override the persisted ones (e.g. the
            caller's current ``api_base`` for a deployment record).
    """
    if not isinstance(data, dict):
        return None
    fact_types = _fact_types(record_type)
    field_hints = typing.get_type_hints(record_type)
    kwargs: dict[str, Any] = {}
    for f in dataclasses.fields(record_type):
        if f.name in identity:
            kwargs[f.name] = identity[f.name]
            continue
        if f.name not in data:
            continue
        raw = data[f.name]
        if f.name not in fact_types:
            try:
                kwargs[f.name] = _decode(raw, field_hints[f.name])
            except _Undecodable as exc:
                logger.warning(
                    "record_codec: reason=undecodable_identity field=%s: %s", f.name, exc
                )
                return None
            continue
        try:
            kwargs[f.name] = Fact(
                value=_decode(raw["value"], fact_types[f.name]),
                source=raw["source"],
                observed_at=str(raw.get("observed_at") or ""),
                detail=str(raw.get("detail") or ""),
            )
        except (_Undecodable, KeyError, TypeError) as exc:
            logger.warning("record_codec: reason=undecodable_fact field=%s: %s", f.name, exc)
            kwargs[f.name] = unknown(f"persisted {f.name} did not decode")
    try:
        return record_type(**kwargs)
    except TypeError as exc:
        logger.warning(
            "record_codec: reason=record_missing_identity type=%s: %s", record_type.__name__, exc
        )
        return None


__all__ = ["decode_record", "encode_record"]
