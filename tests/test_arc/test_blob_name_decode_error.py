"""F018: a corrupt (non-UTF-8) blob name in a clio-core tag surfaces as a typed ARC error."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.arc import storage
from clio_agent.arc.blob_frame import BlobNameDecodeError

_CORRUPT = b"sess_94ddc7c07531___events~w~c3db7e\x00\x00\x80?514937"


class _Tag:
    def __init__(self, name: str) -> None:
        self.name = name

    def GetContainedBlobs(self) -> list[str]:  # noqa: N802 - binding name
        raise UnicodeDecodeError("utf-8", _CORRUPT, 37, 38, "invalid start byte")


def test_scan_reports_an_undecodable_name_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(storage, "call_with_liveness", lambda fn, **_: fn())
    stub: Any = SimpleNamespace(
        _live=lambda: None,
        _cte=SimpleNamespace(Tag=_Tag),
        tag=lambda kind: f"ns/{kind}",
        _gate=SimpleNamespace(port=9413, note_rpc_stalled=lambda *a, **k: None),
        _reconnect=lambda: None,
    )

    with pytest.raises(BlobNameDecodeError) as info:
        list(storage.ClioCoreStore.scan(stub, "segments"))

    err = info.value
    assert err.details["tag"] == "ns/segments"
    assert err.details["recovery"] == "quarantine_store"
    assert "sess_94ddc7c07531___events" in err.details["name_excerpt"]
    assert isinstance(err.__cause__, UnicodeDecodeError)
