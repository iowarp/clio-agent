"""Vendored tiktoken rank files stay offline-loadable (no delete + re-download).

litellm ships its bundled rank files with CRLF line endings, which fail tiktoken's
pinned sha256. tiktoken then deletes the vendored file and downloads the encoding,
which needs the network and races between concurrent processes (the
``test_lazy_tiktoken`` failure under ``pytest -n 2``). These tests prove the repair
makes the load offline and race-free. Network access is blocked in every test that
loads through tiktoken, so a regression fails instead of quietly downloading.
"""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path

import pytest

tiktoken_load = pytest.importorskip("tiktoken.load")

from clio_agent.lm import tiktoken_vendored as tv  # noqa: E402

_URL = "https://openaipublic.blob.core.windows.net/encodings/fixture_base.tiktoken"
_LF = b"".join(f"dG9rZW4{i:04d} {i}\n".encode() for i in range(2000))
_CRLF = _LF.replace(b"\n", b"\r\n")
_PIN = hashlib.sha256(_LF).hexdigest()


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Make any tiktoken download attempt fail loudly and record it."""

    import requests

    attempts: list[str] = []

    def refuse(url: str, *args: object, **kwargs: object) -> None:
        attempts.append(url)
        raise AssertionError(f"tiktoken tried to download {url}")

    monkeypatch.setattr(requests, "get", refuse)
    return attempts


def _crlf_cache(directory: Path) -> Path:
    path = directory / tv.cache_key(_URL)
    path.write_bytes(_CRLF)
    return path


def test_cache_key_matches_tiktoken() -> None:
    import hashlib as _h

    assert tv.cache_key(_URL) == _h.sha1(_URL.encode()).hexdigest()  # noqa: S324


def test_unrepaired_crlf_file_is_deleted_and_fetched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_network: list[str]
) -> None:
    """The defect, pinned: tiktoken deletes a CRLF vendored file and goes to the network."""

    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(tmp_path))
    path = _crlf_cache(tmp_path)
    with pytest.raises(AssertionError, match="tried to download"):
        tiktoken_load.read_file_cached(_URL, _PIN)
    assert no_network == [_URL]
    assert not path.exists()  # tiktoken removed the vendored copy


def test_repaired_file_loads_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_network: list[str]
) -> None:
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(tmp_path))
    path = _crlf_cache(tmp_path)
    assert tv.repair_rank_file(path, _PIN).outcome == "repaired"
    assert tiktoken_load.read_file_cached(_URL, _PIN) == _LF
    assert no_network == []
    # Same ranks either way: tiktoken parses with splitlines().
    assert _CRLF.splitlines() == _LF.splitlines()


def test_repair_outcomes_are_typed(tmp_path: Path) -> None:
    path = _crlf_cache(tmp_path)
    assert tv.repair_rank_file(path, _PIN).outcome == "repaired"
    assert tv.repair_rank_file(path, _PIN).outcome == "valid"  # idempotent
    assert path.read_bytes() == _LF
    assert tv.repair_rank_file(tmp_path / "missing", _PIN).outcome == "absent"
    other = tmp_path / "other"
    other.write_bytes(b"not a rank file\r\n")
    assert tv.repair_rank_file(other, _PIN).outcome == "unrecognised"
    assert other.read_bytes() == b"not a rank file\r\n"  # never rewritten
    assert list(tmp_path.glob("*.clio-tmp")) == []  # no temp files left behind


def test_concurrent_repair_and_load_never_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_network: list[str]
) -> None:
    """Many concurrent repair-then-load callers on one shared file: all succeed offline.

    This is the ``-n 2`` shape: several callers reach the same vendored file at once.
    Before the repair, each one deleted and re-fetched it. Now the first caller rewrites
    it under the repair lock and the rest see it valid, so no caller ever opens a
    missing file or one mid-replace (a Windows ``PermissionError`` when the rewrite was
    unserialised, which this test caught).
    """

    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(tmp_path))
    for _round in range(20):
        path = _crlf_cache(tmp_path)
        barrier = threading.Barrier(8)
        errors: list[BaseException] = []
        results: list[bytes] = []

        def worker(
            path: Path = path,
            barrier: threading.Barrier = barrier,
            errors: list[BaseException] = errors,
            results: list[bytes] = results,
        ) -> None:
            try:
                barrier.wait()
                outcome = tv.repair_rank_file(path, _PIN).outcome
                assert outcome in ("repaired", "valid")
                results.append(tiktoken_load.read_file_cached(_URL, _PIN))
            except BaseException as exc:  # noqa: BLE001 - surfaced by the assert below
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert errors == []
        assert results == [_LF] * 8
    assert no_network == []
    assert list(tmp_path.glob("*.clio-tmp")) == []


def test_repair_all_uses_the_pinned_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tv, "VENDORED_RANK_FILES", {_URL: _PIN})
    _crlf_cache(tmp_path)
    results = tv.repair_vendored_rank_files(tmp_path)
    assert [r.outcome for r in results] == ["repaired"]


def test_custom_cache_dir_means_nothing_vendored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CUSTOM_TIKTOKEN_CACHE_DIR", "/elsewhere")
    assert tv.repair_vendored_rank_files(force=True) == []


def test_installed_litellm_vendored_files_are_valid_after_repair(no_network: list[str]) -> None:
    """The real bundle: every vendored encoding is hash-valid and loads with no download."""

    directory = tv.litellm_vendored_dir()
    if directory is None:
        pytest.fail("litellm is a core dependency; its tokenizer bundle must be present")
    tv.repair_vendored_rank_files(directory)
    for url, pin in tv.VENDORED_RANK_FILES.items():
        path = directory / tv.cache_key(url)
        assert hashlib.sha256(path.read_bytes()).hexdigest() == pin, url
    import tiktoken

    enc = tiktoken.get_encoding("cl100k_base")
    assert enc.encode("hello world")
    assert no_network == []
