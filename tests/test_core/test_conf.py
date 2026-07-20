"""Tests for the file→env→default config resolver (clio_agent.conf)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from clio_agent import conf, paths
from clio_agent.conf import ConfigStore, as_bool, as_csv, as_float, as_int


def _store(
    tmp_path: Path,
    *,
    user: str | None = None,
    workspace: str | None = None,
    env: dict[str, str] | None = None,
) -> ConfigStore:
    """Build a hermetic ConfigStore with injected user/workspace YAML + env."""
    home = tmp_path / "home"
    cwd = tmp_path / "cwd"
    # Write the user YAML to the OS-correct dir ConfigStore actually reads, using
    # the SAME env map it will use to discover it (portable across Linux/macOS/Windows).
    user_dir = paths.user_config_dir_for(home, env or {})
    user_dir.mkdir(parents=True, exist_ok=True)
    (cwd / ".clio").mkdir(parents=True, exist_ok=True)
    if user is not None:
        (user_dir / "config.yaml").write_text(user, encoding="utf-8")
    if workspace is not None:
        (cwd / ".clio" / "config.yaml").write_text(workspace, encoding="utf-8")
    return ConfigStore(home=home, cwd=cwd, env=env if env is not None else {})


class TestPrecedence:
    def test_file_only(self, tmp_path):
        s = _store(tmp_path, user="debug:\n  level: high\n")
        assert s.resolve("debug.level", env="CLIO_DEBUG", default="low") == "high"

    def test_env_only(self, tmp_path):
        s = _store(tmp_path, env={"CLIO_DEBUG": "med"})
        assert s.resolve("debug.level", env="CLIO_DEBUG", default="low") == "med"

    def test_file_wins_over_env(self, tmp_path):
        # The deliberate inverse of 12-factor: the file is the source of truth.
        s = _store(tmp_path, user="debug:\n  level: high\n", env={"CLIO_DEBUG": "med"})
        assert s.resolve("debug.level", env="CLIO_DEBUG", default="low") == "high"

    def test_default_when_neither(self, tmp_path):
        s = _store(tmp_path)
        assert s.resolve("debug.level", env="CLIO_DEBUG", default="low") == "low"

    def test_empty_env_is_absent(self, tmp_path):
        # A blank/whitespace env value falls through to default (matches the
        # existing os.environ.get(...).strip() truthiness checks).
        s = _store(tmp_path, env={"CLIO_DEBUG": "   "})
        assert s.resolve("debug.level", env="CLIO_DEBUG", default="low") == "low"

    def test_workspace_overlays_user(self, tmp_path):
        s = _store(
            tmp_path,
            user="debug:\n  level: low\n  only: a\n",
            workspace="debug:\n  level: high\n",
        )
        # workspace overrides level, user-only key survives the deep merge
        assert s.resolve("debug.level", env="X", default="off") == "high"
        assert s.resolve("debug.only", env="Y", default="") == "a"


class TestCasting:
    def test_float_from_env_string(self, tmp_path):
        s = _store(tmp_path, env={"CLIO_MAX_LM_CALL_S": "600"})
        assert (
            s.resolve("limits.lm_call_s", env="CLIO_MAX_LM_CALL_S", default=1800.0, cast=as_float)
            == 600.0
        )

    def test_int_from_yaml_scalar(self, tmp_path):
        s = _store(tmp_path, user="limits:\n  max_steps: 12\n")
        assert s.resolve("limits.max_steps", env="X", default=8, cast=as_int) == 12

    def test_csv_from_yaml_list(self, tmp_path):
        s = _store(tmp_path, user="debug:\n  only:\n    - lm_call\n    - settle\n")
        assert s.resolve("debug.only", env="X", default=[], cast=as_csv) == ["lm_call", "settle"]

    def test_csv_from_env_string(self, tmp_path):
        s = _store(tmp_path, env={"CLIO_DEBUG_ONLY": "lm_call, settle ,"})
        assert s.resolve("debug.only", env="CLIO_DEBUG_ONLY", default=[], cast=as_csv) == [
            "lm_call",
            "settle",
        ]

    def test_bad_cast_raises(self, tmp_path):
        s = _store(tmp_path, env={"CLIO_MAX_LM_CALL_S": "not-a-number"})
        with pytest.raises(ValueError):
            s.resolve("limits.lm_call_s", env="CLIO_MAX_LM_CALL_S", default=1.0, cast=as_float)

    def test_default_is_not_cast(self, tmp_path):
        # cast applies only to file/env values, never to the default.
        s = _store(tmp_path)
        sentinel = object()
        assert s.resolve("missing.key", env="MISSING", default=sentinel, cast=as_int) is sentinel


class TestCastHelpers:
    @pytest.mark.parametrize("raw", ["1", "true", "YES", "on", True, 1])
    def test_as_bool_true(self, raw):
        assert as_bool(raw) is True

    @pytest.mark.parametrize("raw", ["0", "false", "NO", "off", "", False, 0])
    def test_as_bool_false(self, raw):
        assert as_bool(raw) is False

    def test_as_bool_garbage_raises(self):
        with pytest.raises(ValueError):
            as_bool("maybe")

    def test_as_int_rejects_bool(self):
        with pytest.raises(ValueError):
            as_int(True)

    def test_as_float_rejects_bool(self):
        with pytest.raises(ValueError):
            as_float(False)


class TestStoreLifecycle:
    def test_reload_picks_up_file_change(self, tmp_path):
        s = _store(tmp_path, user="debug:\n  level: low\n")
        assert s.resolve("debug.level", env="X", default="off") == "low"
        # mutate the user file, then reload
        (paths.user_config_dir_for(tmp_path / "home", {}) / "config.yaml").write_text(
            "debug:\n  level: high\n", encoding="utf-8"
        )
        assert s.resolve("debug.level", env="X", default="off") == "low"  # cached
        s.reload()
        assert s.resolve("debug.level", env="X", default="off") == "high"

    def test_missing_files_are_empty(self, tmp_path):
        home = tmp_path / "home"
        cwd = tmp_path / "cwd"
        s = ConfigStore(home=home, cwd=cwd, env={})
        assert s.data == {}
        assert s.resolve("anything.here", env="X", default="d") == "d"

    def test_invalid_yaml_is_empty(self, tmp_path):
        s = _store(tmp_path, user="this: : : not valid yaml\n")
        # malformed file degrades to {} rather than raising
        assert s.resolve("this", env="X", default="d") == "d"

    def test_no_home_directory_skips_user_layer(self, tmp_path, monkeypatch, caplog):
        """Regression (#769 Slice 2): a cold store must not crash when no home
        directory is resolvable — on Windows ``Path.home()`` raises
        ``RuntimeError`` when USERPROFILE/HOME are absent (scrubbed test envs).
        The user layer degrades to absent with a logged reason; workspace file,
        env, and defaults still resolve."""
        cwd = tmp_path / "cwd"
        (cwd / ".clio").mkdir(parents=True)
        (cwd / ".clio" / "config.yaml").write_text("debug:\n  level: high\n", encoding="utf-8")

        def _no_home() -> Path:
            raise RuntimeError("Could not determine home directory.")

        monkeypatch.setattr(Path, "home", staticmethod(_no_home))
        s = ConfigStore(cwd=cwd, env={"CLIO_ONLY_ENV": "from-env"})
        with caplog.at_level(logging.DEBUG, logger="clio_agent.conf"):
            # Workspace file layer survives the missing user layer.
            assert s.resolve("debug.level", env="X", default="low") == "high"
        # The degradation is surfaced, not silent.
        assert any("no home directory" in rec.getMessage() for rec in caplog.records)
        # Env + default tiers keep working.
        assert s.resolve("some.key", env="CLIO_ONLY_ENV", default="d") == "from-env"
        assert s.resolve("missing.key", env="MISSING", default="d") == "d"

    def test_xdg_config_home_honoured(self, tmp_path):
        home = tmp_path / "home"
        xdg = tmp_path / "explicit-xdg"
        (xdg / "clio-agent").mkdir(parents=True)
        (xdg / "clio-agent" / "config.yaml").write_text("debug:\n  level: high\n", encoding="utf-8")
        s = ConfigStore(home=home, cwd=tmp_path / "cwd", env={"XDG_CONFIG_HOME": str(xdg)})
        assert s.resolve("debug.level", env="X", default="low") == "high"


class TestCommittedDefaultsLayer:
    """The committed ``config.defaults.yaml`` base layer (below env, above in-code)."""

    def _defaults_file(self, tmp_path: Path, body: str) -> Path:
        path = tmp_path / "committed.defaults.yaml"
        path.write_text(body, encoding="utf-8")
        return path

    def _store_with_defaults(
        self,
        tmp_path: Path,
        defaults_body: str,
        *,
        user: str | None = None,
        workspace: str | None = None,
        env: dict[str, str] | None = None,
    ) -> ConfigStore:
        home = tmp_path / "home"
        cwd = tmp_path / "cwd"
        user_dir = paths.user_config_dir_for(home, env or {})
        user_dir.mkdir(parents=True, exist_ok=True)
        (cwd / ".clio").mkdir(parents=True, exist_ok=True)
        if user is not None:
            (user_dir / "config.yaml").write_text(user, encoding="utf-8")
        if workspace is not None:
            (cwd / ".clio" / "config.yaml").write_text(workspace, encoding="utf-8")
        return ConfigStore(
            home=home,
            cwd=cwd,
            env=env if env is not None else {},
            defaults_path=self._defaults_file(tmp_path, defaults_body),
        )

    def test_committed_default_used_when_no_file_or_env(self, tmp_path):
        # A flat dotted-key hit in the base layer returns that value, NOT the
        # in-code default arg (they are equal in production; here they differ so
        # the layer is provably exercised).
        s = self._store_with_defaults(tmp_path, 'arc.store: "cte"\n')
        assert s.resolve("arc.store", env="CLIO_ARC_STORE", default="INCODE") == "cte"

    def test_in_code_default_when_key_absent_from_committed(self, tmp_path):
        s = self._store_with_defaults(tmp_path, 'arc.store: "cte"\n')
        assert s.resolve("other.key", env="CLIO_OTHER", default="INCODE") == "INCODE"

    def test_env_overrides_committed_default(self, tmp_path):
        s = self._store_with_defaults(
            tmp_path, 'arc.store: "cte"\n', env={"CLIO_ARC_STORE": "local"}
        )
        assert s.resolve("arc.store", env="CLIO_ARC_STORE", default="INCODE") == "local"

    def test_user_file_overrides_env_and_committed(self, tmp_path):
        s = self._store_with_defaults(
            tmp_path,
            'arc.store: "cte"\n',
            user="arc:\n  store: fromfile\n",
            env={"CLIO_ARC_STORE": "local"},
        )
        assert s.resolve("arc.store", env="CLIO_ARC_STORE", default="INCODE") == "fromfile"

    def test_workspace_file_wins_over_all_lower_layers(self, tmp_path):
        s = self._store_with_defaults(
            tmp_path,
            'arc.store: "cte"\n',
            user="arc:\n  store: fromuser\n",
            workspace="arc:\n  store: fromworkspace\n",
            env={"CLIO_ARC_STORE": "local"},
        )
        assert s.resolve("arc.store", env="CLIO_ARC_STORE", default="INCODE") == "fromworkspace"

    def test_full_precedence_committed_lt_user_lt_workspace_lt_env(self, tmp_path):
        # Distinct value per layer, one knob per layer, proving the ordering:
        # committed < user-file < workspace-file, and env > committed but < files.
        defaults = 'a.only_committed: "C"\nb.env_beats_this: "C"\n'
        user = "a:\n  only_committed: U\nc:\n  user_key: U\n"
        workspace = "c:\n  user_key: W\n"
        env = {"CLIO_B": "E"}
        s = self._store_with_defaults(tmp_path, defaults, user=user, workspace=workspace, env=env)
        # committed value surfaces only when no file/env shadows it
        assert s.resolve("a.only_committed", env="X", default="D") == "U"  # user > committed
        assert s.resolve("b.env_beats_this", env="CLIO_B", default="D") == "E"  # env > committed
        assert s.resolve("c.user_key", env="X", default="D") == "W"  # workspace > user
        assert s.resolve("d.nowhere", env="X", default="D") == "D"  # in-code default

    def test_int_and_bool_committed_defaults_cast(self, tmp_path):
        s = self._store_with_defaults(tmp_path, "arc.cache_capacity: 1000\nsome.flag: false\n")
        cap = s.resolve("arc.cache_capacity", env="X", default=1, cast=as_int)
        assert cap == 1000 and isinstance(cap, int)
        assert s.resolve("some.flag", env="X", default=True, cast=as_bool) is False

    def test_missing_packaged_file_degrades_to_in_code_default(self, tmp_path, caplog):
        s = ConfigStore(
            home=tmp_path / "home",
            cwd=tmp_path / "cwd",
            env={},
            defaults_path=tmp_path / "does-not-exist.yaml",
        )
        with caplog.at_level(logging.WARNING, logger="clio_agent.conf"):
            assert s.resolve("arc.store", env="X", default="INCODE") == "INCODE"
        assert any(
            "config_defaults_layer_unavailable" in rec.getMessage()
            and "missing_packaged_file" in rec.getMessage()
            for rec in caplog.records
        )

    def test_corrupt_packaged_file_degrades_to_in_code_default(self, tmp_path, caplog):
        bad = tmp_path / "corrupt.yaml"
        bad.write_text("this: : : not valid yaml\n", encoding="utf-8")
        s = ConfigStore(home=tmp_path / "home", cwd=tmp_path / "cwd", env={}, defaults_path=bad)
        with caplog.at_level(logging.WARNING, logger="clio_agent.conf"):
            assert s.resolve("arc.store", env="X", default="INCODE") == "INCODE"
        assert any(
            "config_defaults_layer_unavailable" in rec.getMessage()
            and "corrupt_packaged_file" in rec.getMessage()
            for rec in caplog.records
        )

    def test_reload_clears_committed_defaults_cache(self, tmp_path):
        path = self._defaults_file(tmp_path, 'arc.store: "cte"\n')
        s = ConfigStore(home=tmp_path / "home", cwd=tmp_path / "cwd", env={}, defaults_path=path)
        assert s.resolve("arc.store", env="X", default="INCODE") == "cte"
        path.write_text('arc.store: "local"\n', encoding="utf-8")
        assert s.resolve("arc.store", env="X", default="INCODE") == "cte"  # cached
        s.reload()
        assert s.resolve("arc.store", env="X", default="INCODE") == "local"


def test_module_level_resolve_uses_live_env(monkeypatch, tmp_path):
    # The process-wide store reads os.environ live; point its file layer at an
    # empty dir so only env/default are exercised.
    monkeypatch.setattr(conf, "_STORE", ConfigStore(home=tmp_path, cwd=tmp_path))
    monkeypatch.setenv("CLIO_DEBUG", "high")
    assert conf.resolve("debug.level", env="CLIO_DEBUG", default="low") == "high"
    monkeypatch.delenv("CLIO_DEBUG", raising=False)
    assert conf.resolve("debug.level", env="CLIO_DEBUG", default="low") == "low"
