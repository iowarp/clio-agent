"""Where CLIO's logs live, and reading their tail within limits."""

from __future__ import annotations

from pathlib import Path

from clio_agent.runtime import log_sources
from clio_agent.runtime.log_sources import LogCandidate


def test_unix_candidates_follow_the_launcher(tmp_path: Path) -> None:
    home = tmp_path / "home"
    found = log_sources.log_candidates(
        env={"CLIO_LOG": str(tmp_path / "custom.log")},
        state_dir=tmp_path / "state",
        home=home,
        platform="linux",
        host="node7",
    )
    assert [(c.role, c.path) for c in found] == [
        ("server", tmp_path / "custom.log"),
        ("server", tmp_path / "state" / "clio-server.node7.log"),
        ("server", tmp_path / "state" / "clio-server.log"),
        ("gact", tmp_path / "state" / "gact-stderr.log"),
        ("desktop", home / ".local/share/ai.iowarp.clio.desktop/logs/backend-boot.log"),
        ("desktop", home / ".local/share/ai.iowarp.gact.desktop/logs/backend-boot.log"),
    ]


def test_macos_desktop_log_is_in_library_logs(tmp_path: Path) -> None:
    dirs = log_sources.desktop_log_dirs(env={}, home=tmp_path, platform="macos")
    assert dirs[0] == tmp_path / "Library/Logs/ai.iowarp.clio.desktop"


def test_windows_candidates_include_the_separate_error_log(tmp_path: Path) -> None:
    found = log_sources.log_candidates(
        env={"LOCALAPPDATA": str(tmp_path / "Local")},
        state_dir=tmp_path / "state",
        home=tmp_path,
        platform="windows",
    )
    names = [c.path.name for c in found if c.role == "server"]
    assert names == ["clio-server.log", "clio-server.err.log"]
    assert found[-2].path == tmp_path / "Local/ai.iowarp.clio.desktop/logs/backend-boot.log"


def test_duplicate_locations_are_listed_once(tmp_path: Path) -> None:
    state = tmp_path / "state"
    found = log_sources.log_candidates(
        env={"CLIO_LOG": str(state / "clio-server.h.log")},
        state_dir=state,
        home=tmp_path,
        platform="linux",
        host="h",
    )
    paths = [c.path for c in found]
    assert len(paths) == len(set(paths))
    assert found[0].origin == "CLIO_LOG"


def test_tail_returns_the_last_lines_and_says_earlier_ones_exist(tmp_path: Path) -> None:
    log = tmp_path / "server.log"
    log.write_text("".join(f"line {i}\n" for i in range(1, 2001)))
    tail = log_sources.read_tail(LogCandidate("server", log, "launcher"), 3)
    assert tail.status == "ok"
    assert tail.lines == ["line 1998", "line 1999", "line 2000"]
    assert tail.earlier_lines is True
    whole = log_sources.read_tail(LogCandidate("server", log, "launcher"), 5000)
    assert len(whole.lines) == 2000
    assert whole.earlier_lines is False


def test_tail_reads_across_block_boundaries_and_cuts_long_lines(tmp_path: Path) -> None:
    log = tmp_path / "server.log"
    long_line = "x" * 100_000
    log.write_text(f"first\n{long_line}\nlast\n")
    tail = log_sources.read_tail(LogCandidate("server", log, "launcher"), 2)
    assert tail.lines[1] == "last"
    assert tail.lines[0].endswith("[line cut]")
    assert len(tail.lines[0]) < 2100


def test_tail_decodes_invalid_utf8_instead_of_failing(tmp_path: Path) -> None:
    log = tmp_path / "server.log"
    log.write_bytes(b"ok \xff\xfe bytes\n")
    tail = log_sources.read_tail(LogCandidate("server", log, "launcher"), 10)
    assert tail.status == "ok"
    assert tail.lines[0].startswith("ok ")


def test_missing_and_empty_files_have_typed_reasons(tmp_path: Path) -> None:
    missing = log_sources.read_tail(LogCandidate("gact", tmp_path / "none.log", "launcher"), 10)
    assert (missing.status, missing.reason) == ("missing", "log_file_absent")
    empty_path = tmp_path / "empty.log"
    empty_path.write_text("")
    empty = log_sources.read_tail(LogCandidate("gact", empty_path, "launcher"), 10)
    assert (empty.status, empty.reason) == ("empty", "log_file_empty")
    folder = log_sources.read_tail(LogCandidate("gact", tmp_path, "launcher"), 10)
    assert (folder.status, folder.reason) == ("unreadable", "log_path_not_a_file")


def test_line_count_is_clamped() -> None:
    assert log_sources.clamp_lines(None) == log_sources.DEFAULT_TAIL_LINES == 500
    assert log_sources.clamp_lines(0) == 1
    assert log_sources.clamp_lines(10**9) == log_sources.MAX_TAIL_LINES
