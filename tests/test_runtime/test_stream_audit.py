"""``stream_audit`` envelope contract: ``stage`` cannot be bound twice or clobbered."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clio_agent.runtime.stream_audit import RESERVED_AUDIT_KEYS, stream_audit


def test_a_record_carries_the_stage_and_the_caller_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audit_log = tmp_path / "audit.jsonl"
    monkeypatch.setenv("CLIO_STREAM_AUDIT_LOG", str(audit_log))

    stream_audit("unit.stage", migration_stage="local_sync", count=2)

    (row,) = [json.loads(line) for line in audit_log.read_text(encoding="utf-8").splitlines()]
    assert row["stage"] == "unit.stage"
    assert row["migration_stage"] == "local_sync"
    assert row["count"] == 2


@pytest.mark.parametrize("key", sorted(RESERVED_AUDIT_KEYS))
@pytest.mark.parametrize("enabled", [True, False])
def test_a_reserved_envelope_key_in_the_fields_is_refused(
    key: str, enabled: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A forwarded ``**row`` carrying ``stage``/``ts``/``iso`` fails loud and typed.

    Before: ``stage`` raised ``TypeError: got multiple values for argument
    'stage'`` and ``ts``/``iso`` silently overwrote the envelope. The check runs
    with the audit log disabled too, so a colliding caller fails in ordinary
    test runs rather than only when the log is configured.
    """

    audit_log = tmp_path / "audit.jsonl"
    if enabled:
        monkeypatch.setenv("CLIO_STREAM_AUDIT_LOG", str(audit_log))
    else:
        monkeypatch.delenv("CLIO_STREAM_AUDIT_LOG", raising=False)

    with pytest.raises(ValueError, match="reserved envelope key"):
        stream_audit("unit.stage", **{key: "collides"})
    assert not audit_log.exists()
