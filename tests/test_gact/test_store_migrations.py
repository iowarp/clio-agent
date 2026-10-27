"""Store migrations: the registry, the boot marker check and ``clio-agent migrate`` (R2)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.arc import history_mode
from clio_agent.gact import boot_timing, store_migrate_command, store_migrations
from clio_agent.gact.store_migrations import (
    MARKER_NOT_WRITTEN,
    OWED_AT_BOOT,
    StoreMigration,
    ensure_at_boot,
    health_rows,
    run_migration,
)
from clio_agent.runtime.status import IntegrationState
from clio_agent.ui.cli_commands import handle_local_command


class _Index:
    def __init__(self) -> None:
        self.markers: dict[str, dict[str, Any]] = {}

    def done_marker(self, name: str) -> dict[str, Any] | None:
        return self.markers.get(name)

    def put_done_marker(self, name: str, value: dict[str, Any]) -> None:
        self.markers[name] = value


class _History:
    def __init__(self) -> None:
        self.index = _Index()

    def confirm_write(self, write: Any) -> None:
        write()


def _app() -> Any:
    return SimpleNamespace(state=SimpleNamespace(task_history=_History()))


def _counting(name: str, calls: list[str], details: dict[str, Any] | None = None) -> StoreMigration:
    def run(_app: Any) -> dict[str, Any] | None:
        calls.append(name)
        return details

    return StoreMigration(name, f"test migration {name}", run)


def test_generic_marker_runs_once_and_a_second_run_is_a_no_op() -> None:
    app, calls = _app(), []
    item = _counting("task_event_lane", calls, {"sessions": 3})

    first = run_migration(app, item)
    second = run_migration(app, item)

    assert (first.status, first.details) == ("done", {"sessions": 3})
    assert second.status == "already_done"
    assert calls == ["task_event_lane"]
    marker = app.state.task_history.index.markers["task_event_lane"]
    assert marker["sessions"] == 3 and "completed_at" in marker


def test_a_run_that_leaves_its_marker_unwritten_is_failed_typed() -> None:
    app = _app()
    item = StoreMigration("stuck", "never records", lambda _app: None, owed=lambda _app: True)

    result = run_migration(app, item)

    assert (result.status, result.reason) == ("failed", MARKER_NOT_WRITTEN)


def test_boot_runs_an_owed_migration_once_as_a_typed_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, calls = _app(), []
    monkeypatch.setattr(store_migrations, "MIGRATIONS", (_counting("lane", calls),))

    assert ensure_at_boot(app, "lane") is True
    report = app.state.store_migrations_boot["lane"]
    assert (report.status, report.reason) == ("done", OWED_AT_BOOT)
    steps = [step["name"] for step in boot_timing.snapshot(app)["steps"]]
    assert steps == ["migration.lane"]
    (row,) = health_rows(app)
    assert row.state is IntegrationState.DEGRADED and row.required is False
    assert "lane" in row.summary

    # The next boot only reads the marker.
    later = SimpleNamespace(state=SimpleNamespace(task_history=app.state.task_history))
    assert ensure_at_boot(later, "lane") is False
    assert calls == ["lane"]
    assert boot_timing.snapshot(later)["steps"] == []
    (row,) = health_rows(later)
    assert row.state is IntegrationState.READY


def test_a_boot_migration_failure_is_reported_and_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(_app: Any) -> None:
        raise OSError("disk gone")

    monkeypatch.setattr(store_migrations, "MIGRATIONS", (StoreMigration("lane", "x", broken),))
    app = _app()

    with pytest.raises(OSError):
        ensure_at_boot(app, "lane")

    (row,) = health_rows(app)
    assert row.state is IntegrationState.UNAVAILABLE
    assert app.state.store_migrations_boot["lane"].reason == store_migrations.MIGRATION_FAILED


def test_health_has_no_row_before_boot_checked() -> None:
    assert health_rows(SimpleNamespace(state=SimpleNamespace())) == []


def test_registered_migrations_have_unique_names() -> None:
    names = [item.name for item in store_migrations.MIGRATIONS]
    assert len(names) == len(set(names))
    assert {
        "task_event_lane",
        "transcript_legacy_import",
        "task_index_build",
        "artifact_index",
    } <= set(names)
    with pytest.raises(KeyError):
        store_migrations.migration("unknown")


# ---- clio-agent migrate ----------------------------------------------------------


@pytest.fixture
def offline(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The command over a fake store: no clio-core, no other clients."""
    state: dict[str, Any] = {"closed": 0, "app": None, "attached": 0}

    class _IO:
        def submit(self, operation: Any, *args: Any) -> Any:
            class _Done:
                def result(self) -> Any:
                    return operation(*args)

            return _Done()

    def open_store() -> Any:
        app = _app()
        app.state.task_history.io = _IO()
        state["app"] = app
        return app

    def attach_app(core: Any) -> Any:
        state["attached"] += 1  # the whole app is built only when something is owed
        return core

    def close_store(_app: Any) -> None:
        state["closed"] += 1

    monkeypatch.setattr(history_mode, "resolve", lambda: history_mode.ContextMode(mode="clio_core"))
    monkeypatch.setattr(store_migrate_command, "_other_store_clients", lambda: [])
    monkeypatch.setattr(store_migrate_command, "_open_core", open_store)
    monkeypatch.setattr(store_migrate_command, "_attach_app", attach_app)
    monkeypatch.setattr(store_migrate_command, "_close_store", close_store)
    return state


def test_migrate_runs_pending_then_is_idempotent(
    offline: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        store_migrations, "MIGRATIONS", (_counting("one", calls), _counting("two", calls))
    )
    lines: list[str] = []

    code, payload = store_migrate_command.run_migrations(lines.append)

    assert code == store_migrate_command.EXIT_OK
    assert [row["status"] for row in payload["migrations"]] == ["done", "done"]
    assert any(line.startswith("migrate step=one status=done") for line in lines)
    assert offline["closed"] == 1 and offline["attached"] == 1

    # A second run against the same store: every migration reads already_done.
    history = offline["app"].state.task_history
    monkeypatch.setattr(
        store_migrate_command,
        "_open_core",
        lambda: SimpleNamespace(state=SimpleNamespace(task_history=history)),
    )
    code, payload = store_migrate_command.run_migrations(lines.append)
    assert code == store_migrate_command.EXIT_OK
    assert [row["status"] for row in payload["migrations"]] == ["already_done"] * 2
    assert calls == ["one", "two"]
    assert offline["attached"] == 1  # nothing owed: the re-run read markers only


def test_migrate_stops_at_a_failure(
    offline: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def broken(_app: Any) -> None:
        raise RuntimeError("bad record")

    monkeypatch.setattr(
        store_migrations,
        "MIGRATIONS",
        (StoreMigration("first", "x", broken), _counting("second", calls)),
    )

    code, payload = store_migrate_command.run_migrations(lambda _line: None)

    assert code == store_migrate_command.EXIT_FAILED
    assert payload["migrations"] == [
        {
            "name": "first",
            "status": "failed",
            "seconds": 0.0,
            "reason": "migration_failed",
            "details": {"error": "RuntimeError('bad record')"},
        }
    ]
    assert calls == [] and offline["closed"] == 1


def test_migrate_refuses_a_store_in_use(
    offline: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store_migrate_command, "_other_store_clients", lambda: [4242])
    lines: list[str] = []

    code, payload = store_migrate_command.run_migrations(lines.append)

    assert code == store_migrate_command.EXIT_IN_USE
    assert payload == {"status": "refused", "reason": "store_in_use", "pids": [4242]}
    assert offline["app"] is None
    assert "reason=store_in_use" in lines[0]


def test_migrate_skips_history_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        history_mode,
        "resolve",
        lambda: history_mode.ContextMode(mode="history", reason="no_clio_core"),
    )

    code, payload = store_migrate_command.run_migrations(lambda _line: None)

    assert code == store_migrate_command.EXIT_OK
    assert payload["reason"] == "history_mode"


def test_cli_dispatches_migrate(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def main(arguments: Any) -> int:
        seen.append(list(arguments))
        return 3

    monkeypatch.setattr(store_migrate_command, "main", main)

    with pytest.raises(SystemExit) as stopped:
        handle_local_command(["migrate", "--json"])

    assert stopped.value.code == 3 and seen == [["--json"]]


def test_boot_row_names_the_replay_the_restore_step_already_paid() -> None:
    app = _app()
    app.state.store_migrations_boot = {
        "task_index_build": store_migrations.MigrationResult(
            "task_index_build", "done", 2.0, OWED_AT_BOOT, {"replay_seconds": 5.0}
        )
    }

    (row,) = health_rows(app)

    assert "task_index_build (2.0s + 5.0s replay in task_history_restore)" in row.summary


def test_settling_after_the_build_publishes_only_terminal_outcomes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from clio_agent.gact import task_index_writes, task_publication

    def task(handle: str, **fields: Any) -> Any:
        base = {
            "handle": handle,
            "terminal_at": "2026-10-27T00:00:00Z",
            "kind": "Subagent",
            "settlement": "confirmed",
            "result": object(),
        }
        return SimpleNamespace(**{**base, **fields})

    tasks = {
        "done": task("done"),
        "session": task("session", kind="Session", settlement="never_started"),
        "running": task("running", terminal_at=None),
        "mcp": task("mcp", kind="MCP"),
        "no-result": task("no-result", result=None),
    }
    rows = [(f"t~{handle}", {"handle": handle}) for handle in [*tasks, "gone"]]
    published: list[str] = []

    def publish(_history: Any, current: Any) -> None:
        published.append(current.handle)
        rows[:] = [row for row in rows if row[1]["handle"] != current.handle]

    history = SimpleNamespace(
        index=SimpleNamespace(open_records=lambda prefix: list(rows)),
        task_snapshot=tasks.get,
    )
    monkeypatch.setattr(task_publication, "publish_task_notice", publish)
    monkeypatch.setattr(task_index_writes, "trim", lambda _history: None)

    report = task_publication.settle_published_outcomes(history)

    assert published == ["done", "session"]
    assert report == {"outcomes": 2, "open_tasks": 4}
