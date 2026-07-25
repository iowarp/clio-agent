"""B-codex-1 (#974): codex backend detection + profile synth/validate + config + prefix.

Host-agnostic unit coverage — the real ``codex sandbox`` spawn (a UAC-gated elevated fence)
is the OWNER-GATED live gate and is NEVER exercised here. Every test drives the flow with
INJECTED fakes (faked ``which`` + ``version_reader``, an explicit ``platform`` string, a
``tmp_path`` cache dir), so nothing spawns codex or touches the user's ``~/.codex``. Pinned:

* :func:`detect_codex` — absent / present+supported (win32 prefers ``codex.cmd``, asserted via
  the fake ``which`` resolution order) / present+old-version / version-unreadable;
* :func:`is_codex_version_supported` at/above/below the floor + empty;
* :func:`synthesize_codex_profile` + :func:`validate_codex_profile` round-trip + typed drift;
* :func:`codex_layer_name` content-addressing (deterministic, territory/name-sensitive, prefix);
* :func:`write_codex_layer` — the ``-p`` layer file in the DEFAULT home (windows.sandbox gate
  only when elevated+win32; TOML-escaped fs table; NO BOM; prune reaps only clio-sb-* layers);
* :func:`codex_prefix` exact argv shape with ``-p`` layer selection.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from clio_agent.runtime import sandbox_codex as sc

# --------------------------------------------------------------------------- #
# Injected fakes — never a real codex / which probe.                            #
# --------------------------------------------------------------------------- #


def _fake_which(mapping: dict[str, str]):
    """A ``shutil.which`` stand-in resolving only the names in ``mapping`` (else ``None``)."""

    def _which(name: str) -> str | None:
        return mapping.get(name)

    return _which


# --------------------------------------------------------------------------- #
# is_codex_version_supported / parse_version.                                   #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("version", "supported"),
    [
        ("0.145.0", True),  # exactly the floor
        ("0.146.0", True),  # above the floor
        ("1.0.0", True),  # well above
        ("0.144.9", False),  # below the floor
        ("0.100.0", False),  # well below
        ("", False),  # empty / unreadable is never supported
        ("   ", False),  # whitespace-only is empty
        ("garbage", False),  # unparseable → (0,0,0) → unsupported
    ],
)
def test_is_codex_version_supported(version: str, supported: bool) -> None:
    assert sc.is_codex_version_supported(version) is supported


def test_parse_version_handles_suffix_and_junk() -> None:
    assert sc.parse_version("0.145.0-beta.1") == (0, 145, 0)
    assert sc.parse_version("v0.145.0") == (0, 145, 0)
    assert sc.parse_version("nonsense") == (0, 0, 0)


# --------------------------------------------------------------------------- #
# detect_codex — every dependency injected.                                     #
# --------------------------------------------------------------------------- #


def test_detect_codex_absent_is_typed_not_installed() -> None:
    """No codex on PATH → installed=False + typed codex_not_installed (never a raw miss)."""
    det = sc.detect_codex(
        which=_fake_which({}),
        version_reader=lambda _b: "should-not-be-called",
        platform="win32",
    )
    assert det.installed is False
    assert det.binary_path == ""
    assert det.version == ""
    assert det.reason == sc.REASON_CODEX_NOT_INSTALLED


def test_detect_codex_win32_prefers_codex_cmd() -> None:
    """win32 resolution order prefers the launchable codex.cmd over codex.exe/codex (#1025)."""
    which = _fake_which(
        {
            "codex.cmd": "C:\\tools\\codex.cmd",
            "codex.exe": "C:\\tools\\codex.exe",
            "codex": "C:\\tools\\codex",
        }
    )
    det = sc.detect_codex(which=which, version_reader=lambda _b: "0.145.0", platform="win32")
    assert det.installed is True
    assert det.binary_path == "C:\\tools\\codex.cmd"  # .cmd wins the order
    assert det.version == "0.145.0"
    assert det.reason == sc.REASON_CODEX_DETECTED


def test_detect_codex_win32_falls_to_exe_then_plain() -> None:
    """When codex.cmd is absent, resolution falls to codex.exe, then the plain name."""
    det_exe = sc.detect_codex(
        which=_fake_which({"codex.exe": "C:\\t\\codex.exe", "codex": "C:\\t\\codex"}),
        version_reader=lambda _b: "0.145.0",
        platform="win32",
    )
    assert det_exe.binary_path == "C:\\t\\codex.exe"
    det_plain = sc.detect_codex(
        which=_fake_which({"codex": "C:\\t\\codex"}),
        version_reader=lambda _b: "0.145.0",
        platform="win32",
    )
    assert det_plain.binary_path == "C:\\t\\codex"


def test_detect_codex_non_win32_uses_plain_name_only() -> None:
    """Off-win32 only the extensionless ``codex`` name is probed (no .cmd/.exe)."""
    seen: list[str] = []

    def _which(name: str) -> str | None:
        seen.append(name)
        return "/usr/local/bin/codex" if name == "codex" else None

    det = sc.detect_codex(which=_which, version_reader=lambda _b: "0.145.0", platform="linux")
    assert det.installed is True
    assert det.binary_path == "/usr/local/bin/codex"
    assert seen == ["codex"]  # never probed .cmd/.exe off-Windows


def test_detect_codex_old_version_is_unsupported() -> None:
    """Present but below the validated floor → typed codex_version_unsupported (not detected)."""
    det = sc.detect_codex(
        which=_fake_which({"codex": "/usr/bin/codex"}),
        version_reader=lambda _b: "0.100.0",
        platform="linux",
    )
    assert det.installed is True
    assert det.version == "0.100.0"
    assert det.reason == sc.REASON_CODEX_VERSION_UNSUPPORTED


def test_detect_codex_unreadable_version_is_unsupported() -> None:
    """Present but the version could not be read (probe returned "") → unsupported, not trusted."""
    det = sc.detect_codex(
        which=_fake_which({"codex": "/usr/bin/codex"}),
        version_reader=lambda _b: "",
        platform="linux",
    )
    assert det.installed is True
    assert det.version == ""
    assert det.reason == sc.REASON_CODEX_VERSION_UNSUPPORTED


def test_detect_codex_passes_resolved_binary_to_version_reader() -> None:
    """The version_reader receives the RESOLVED binary path (so the default can spawn it)."""
    captured: list[str] = []

    def _reader(binary: str) -> str:
        captured.append(binary)
        return "0.145.0"

    sc.detect_codex(
        which=_fake_which({"codex": "/opt/codex"}), version_reader=_reader, platform="linux"
    )
    assert captured == ["/opt/codex"]


def test_parse_codex_version_banner() -> None:
    """The default reader's banner parse pulls X.Y.Z out of ``codex-cli X.Y.Z``."""
    assert sc._parse_codex_version_banner("codex-cli 0.145.0") == "0.145.0"
    assert sc._parse_codex_version_banner("codex-cli 0.145.0\n") == "0.145.0"
    assert sc._parse_codex_version_banner("no version here") == ""


# --------------------------------------------------------------------------- #
# synthesize_codex_profile + validate_codex_profile.                            #
# --------------------------------------------------------------------------- #


def test_synthesize_profile_win32_read_anywhere_write_fence() -> None:
    """win32: drive anchors → "read" (read-anywhere), the workspace → "write" (the fence)."""
    profile = sc.synthesize_codex_profile(["D:\\ws"], profile_name="clio", platform="win32")
    fs = profile["filesystem"]
    assert fs["D:\\ws"] == "write"
    assert fs["D:\\"] == "read"  # the write root's drive anchor is a read grant
    assert set(profile) == {"description", "filesystem", "network"}
    # The synthesized table passes clio's own validation (round-trip).
    sc.validate_codex_profile(profile)


def test_synthesize_profile_multiple_drives_dedup_anchors() -> None:
    """Two write roots on distinct drives → both drive anchors granted read, both roots write."""
    profile = sc.synthesize_codex_profile(["C:\\a", "D:\\b", "D:\\c"], platform="win32")
    fs = profile["filesystem"]
    assert fs["C:\\"] == "read"
    assert fs["D:\\"] == "read"
    assert fs["C:\\a"] == "write"
    assert fs["D:\\b"] == "write"
    assert fs["D:\\c"] == "write"


def test_synthesize_profile_posix_root_is_read_anywhere() -> None:
    """Off-win32 the single filesystem root ``/`` is the read-anywhere grant.

    Keys are normalized with ``str(Path(r))`` (the module's rule), so expectations use the same
    normalization to stay portable — a posix path renders backslash-form under WindowsPath on
    the Windows dev box, forward-slash on Linux CI.
    """
    profile = sc.synthesize_codex_profile(["/home/u/ws"], platform="linux")
    fs = profile["filesystem"]
    assert fs["/"] == "read"  # the literal read-anywhere root is not path-normalized
    assert fs[str(Path("/home/u/ws"))] == "write"


def test_synthesize_profile_explicit_read_roots() -> None:
    """Explicit read_roots override the drive-root default (still write-fenced)."""
    profile = sc.synthesize_codex_profile(["/ws"], read_roots=["/data", "/ref"], platform="linux")
    fs = profile["filesystem"]
    assert fs[str(Path("/data"))] == "read"
    assert fs[str(Path("/ref"))] == "read"
    assert fs[str(Path("/ws"))] == "write"


def test_synthesize_profile_write_wins_over_read_overlap() -> None:
    """When a root is both a read and write root, the write grant WINS (the fence boundary)."""
    profile = sc.synthesize_codex_profile(["/shared"], read_roots=["/shared"], platform="linux")
    assert profile["filesystem"][str(Path("/shared"))] == "write"


def test_validate_profile_rejects_bad_top_key() -> None:
    """A stray top-level key is typed codex_profile_rejected (defensive against drift)."""
    bad = {"filesystem": {"/ws": "write"}, "bogus_top": 1}
    with pytest.raises(sc.CodexProfileError) as exc:
        sc.validate_codex_profile(bad)
    assert exc.value.reason == sc.REASON_CODEX_PROFILE_REJECTED


def test_validate_profile_rejects_bad_fs_mode() -> None:
    """A filesystem value outside {read, write, deny} is typed codex_profile_rejected."""
    bad = {"filesystem": {"/ws": "readwrite"}}
    with pytest.raises(sc.CodexProfileError) as exc:
        sc.validate_codex_profile(bad)
    assert exc.value.reason == sc.REASON_CODEX_PROFILE_REJECTED


def test_validate_profile_rejects_unhashable_fs_mode() -> None:
    """A drifted UNHASHABLE filesystem mode (a list/dict) is a typed CodexProfileError, not a
    bare TypeError from the ``in`` membership test (N1: guard the mode with isinstance first)."""
    bad = {"filesystem": {"/ws": ["write"]}}
    with pytest.raises(sc.CodexProfileError) as exc:
        sc.validate_codex_profile(bad)
    assert exc.value.reason == sc.REASON_CODEX_PROFILE_REJECTED


def test_validate_profile_rejects_empty_fs_key() -> None:
    """An empty/blank filesystem key is rejected (a non-empty string is required)."""
    with pytest.raises(sc.CodexProfileError):
        sc.validate_codex_profile({"filesystem": {"   ": "write"}})


def test_validate_profile_rejects_missing_filesystem() -> None:
    """A profile without a filesystem table is rejected."""
    with pytest.raises(sc.CodexProfileError):
        sc.validate_codex_profile({"description": "x"})


def test_validate_profile_rejects_non_string_description() -> None:
    """A non-string description is rejected."""
    with pytest.raises(sc.CodexProfileError):
        sc.validate_codex_profile({"description": 5, "filesystem": {"/ws": "write"}})


def test_validate_profile_rejects_non_dict() -> None:
    """A non-dict profile is rejected."""
    with pytest.raises(sc.CodexProfileError):
        sc.validate_codex_profile(["not", "a", "table"])


# --------------------------------------------------------------------------- #
# network egress table (Recipe A) — synth emits it, validate closes on drift.   #
# --------------------------------------------------------------------------- #


def test_synthesize_profile_emits_network_table() -> None:
    """synth emits the REQUIRED Recipe-A network table (enabled/full/allow_upstream_proxy)."""
    profile = sc.synthesize_codex_profile(["/ws"], platform="linux")
    assert profile["network"] == {
        "enabled": True,
        "mode": "full",
        "allow_upstream_proxy": True,
    }
    # The synthesized table round-trips clio's own validation.
    sc.validate_codex_profile(profile)


def _valid_profile_with_net(network: Any) -> dict[str, Any]:
    """A minimal otherwise-valid profile carrying ``network`` (isolates the network assertion)."""
    return {"filesystem": {"/ws": "write"}, "network": network}


def test_validate_profile_rejects_missing_network() -> None:
    """clio always synthesizes network, so a MISSING network table is drift (typed reject)."""
    with pytest.raises(sc.CodexProfileError) as exc:
        sc.validate_codex_profile({"filesystem": {"/ws": "write"}})
    assert exc.value.reason == sc.REASON_CODEX_PROFILE_REJECTED
    assert "network" in str(exc.value)


def test_validate_profile_rejects_bad_net_mode() -> None:
    """A network mode outside {full, limited} is typed codex_profile_rejected."""
    bad = _valid_profile_with_net(
        {"enabled": True, "mode": "wideopen", "allow_upstream_proxy": True}
    )
    with pytest.raises(sc.CodexProfileError) as exc:
        sc.validate_codex_profile(bad)
    assert exc.value.reason == sc.REASON_CODEX_PROFILE_REJECTED
    assert "mode" in str(exc.value)


def test_validate_profile_rejects_unhashable_net_mode() -> None:
    """A drifted UNHASHABLE network mode (a list/dict) is a typed CodexProfileError, not a bare
    TypeError from the ``in`` membership test (N1: guard the mode with isinstance first)."""
    bad = _valid_profile_with_net(
        {"enabled": True, "mode": {"nested": "table"}, "allow_upstream_proxy": True}
    )
    with pytest.raises(sc.CodexProfileError) as exc:
        sc.validate_codex_profile(bad)
    assert exc.value.reason == sc.REASON_CODEX_PROFILE_REJECTED


def test_validate_profile_rejects_net_extra_key() -> None:
    """An extra key in the network table (e.g. a stray ``mitm`` bool) is rejected."""
    bad = _valid_profile_with_net(
        {"enabled": True, "mode": "full", "allow_upstream_proxy": True, "mitm": False}
    )
    with pytest.raises(sc.CodexProfileError) as exc:
        sc.validate_codex_profile(bad)
    assert exc.value.reason == sc.REASON_CODEX_PROFILE_REJECTED


def test_validate_profile_rejects_non_bool_enabled() -> None:
    """A non-bool ``enabled`` (an int is NOT a bool) is rejected — no silent coercion."""
    bad = _valid_profile_with_net({"enabled": 1, "mode": "full", "allow_upstream_proxy": True})
    with pytest.raises(sc.CodexProfileError) as exc:
        sc.validate_codex_profile(bad)
    assert exc.value.reason == sc.REASON_CODEX_PROFILE_REJECTED
    assert "enabled" in str(exc.value)


def test_validate_profile_rejects_non_bool_allow_upstream() -> None:
    """A non-bool ``allow_upstream_proxy`` is rejected."""
    bad = _valid_profile_with_net({"enabled": True, "mode": "full", "allow_upstream_proxy": "yes"})
    with pytest.raises(sc.CodexProfileError):
        sc.validate_codex_profile(bad)


# --------------------------------------------------------------------------- #
# codex_layer_name — content-addressed, distinctive clio-sb prefix.             #
# --------------------------------------------------------------------------- #


def test_layer_name_is_deterministic_and_prefixed() -> None:
    """Same (profile, territory) → same name; the distinctive clio-sb prefix makes prune safe."""
    profile = sc.synthesize_codex_profile(["/ws"], platform="linux")
    name1 = sc.codex_layer_name("clio", profile)
    name2 = sc.codex_layer_name("clio", profile)
    assert name1 == name2  # deterministic content address
    assert name1.startswith("clio-sb-")
    assert name1 == f"{sc.CODEX_LAYER_PREFIX}-{name1.split('-')[-1]}"
    assert len(name1.split("-")[-1]) == 8  # 8-char sha


def test_layer_name_differs_by_territory() -> None:
    """Different write territory → different layer name (no clobber of a shared layer)."""
    prof_a = sc.synthesize_codex_profile(["/ws-a"], platform="linux")
    prof_b = sc.synthesize_codex_profile(["/ws-b"], platform="linux")
    assert sc.codex_layer_name("clio", prof_a) != sc.codex_layer_name("clio", prof_b)


def test_layer_name_differs_by_profile_name() -> None:
    """Different profile name → different layer name (the name is part of the address)."""
    profile = sc.synthesize_codex_profile(["/ws"], platform="linux")
    assert sc.codex_layer_name("clioa", profile) != sc.codex_layer_name("cliob", profile)


# --------------------------------------------------------------------------- #
# write_codex_layer — real-TOML layer in the DEFAULT home, NO BOM, safe prune.  #
# --------------------------------------------------------------------------- #


def test_write_layer_elevated_win32_shape_and_no_bom(tmp_path: Path) -> None:
    """win32 + elevated: the layer file carries the [windows] gate + fs table, and NO BOM."""
    profile = sc.synthesize_codex_profile(["D:\\ws"], profile_name="clio", platform="win32")
    layer = sc.write_codex_layer(
        "clio", profile, elevated=True, codex_home=tmp_path, platform="win32"
    )
    config = tmp_path / f"{layer}.config.toml"
    assert config.is_file()

    raw = config.read_bytes()
    assert raw[:3] != b"\xef\xbb\xbf"  # critical: NO UTF-8 BOM (breaks codex's TOML parser)

    text = config.read_text(encoding="utf-8")
    assert "[windows]" in text
    assert 'sandbox = "elevated"' in text
    assert "[permissions.clio.filesystem]" in text
    # Windows path backslashes are DOUBLED (real TOML), and the grants are present.
    assert '"D:\\\\ws" = "write"' in text
    assert '"D:\\\\" = "read"' in text


def test_write_layer_omits_windows_block_off_win32(tmp_path: Path) -> None:
    """Off-win32 the [windows] gate is omitted (a no-op key) — only the fs table is written."""
    profile = sc.synthesize_codex_profile(["/ws"], platform="linux")
    layer = sc.write_codex_layer(
        "clio", profile, elevated=True, codex_home=tmp_path, platform="linux"
    )
    text = (tmp_path / f"{layer}.config.toml").read_text(encoding="utf-8")
    assert "[windows]" not in text
    assert 'sandbox = "elevated"' not in text
    assert "[permissions.clio.filesystem]" in text


def test_write_layer_omits_windows_block_when_not_elevated(tmp_path: Path) -> None:
    """win32 but NOT elevated → no [windows] gate (only the fs table)."""
    profile = sc.synthesize_codex_profile(["D:\\ws"], platform="win32")
    layer = sc.write_codex_layer(
        "clio", profile, elevated=False, codex_home=tmp_path, platform="win32"
    )
    text = (tmp_path / f"{layer}.config.toml").read_text(encoding="utf-8")
    assert "[windows]" not in text


def test_write_layer_emits_network_block_no_mitm(tmp_path: Path) -> None:
    """The rendered layer carries the Recipe-A [permissions.<p>.network] block, and NO mitm line."""
    profile = sc.synthesize_codex_profile(["/ws"], profile_name="clio", platform="linux")
    layer = sc.write_codex_layer(
        "clio", profile, elevated=False, codex_home=tmp_path, platform="linux"
    )
    text = (tmp_path / f"{layer}.config.toml").read_text(encoding="utf-8")
    assert "[permissions.clio.network]" in text
    assert "enabled = true" in text
    assert 'mode = "full"' in text
    assert "allow_upstream_proxy = true" in text
    # mitm is a TOML TABLE in codex v0.145, not a bool — it must NOT be emitted at all.
    assert "mitm" not in text


def test_render_layer_toml_network_block_shape() -> None:
    """_render_layer_toml emits the network block reading values from the profile (not hardcoded)."""
    profile = sc.synthesize_codex_profile(["/ws"], profile_name="clio", platform="linux")
    body = sc._render_layer_toml("clio", profile, elevated=False)
    assert "[permissions.clio.filesystem]" in body
    net_idx = body.index("[permissions.clio.network]")
    fs_idx = body.index("[permissions.clio.filesystem]")
    assert fs_idx < net_idx  # network block follows the filesystem block
    block = body[net_idx:]
    assert "enabled = true" in block
    assert 'mode = "full"' in block
    assert "allow_upstream_proxy = true" in block
    assert "mitm" not in body


def test_write_layer_validates_before_writing(tmp_path: Path) -> None:
    """A drifted profile is rejected BEFORE anything reaches disk (typed, no partial write)."""
    with pytest.raises(sc.CodexProfileError):
        sc.write_codex_layer(
            "clio", {"filesystem": {"/ws": "bogus"}}, elevated=True, codex_home=tmp_path
        )
    assert list(tmp_path.glob("*.config.toml")) == []


def test_write_layer_prune_keeps_only_clio_layers(tmp_path: Path) -> None:
    """The prune reaps ONLY clio-sb-*.config.toml — NEVER config.toml or an unrelated file."""
    # A user config.toml and an unrelated file that must survive any number of prunes.
    (tmp_path / "config.toml").write_text("# user config\n", encoding="utf-8")
    (tmp_path / "auth.json").write_text("{}", encoding="utf-8")
    # Write well over the keep bound of distinct clio layers.
    for i in range(sc.CODEX_LAYER_KEEP + 8):
        profile = sc.synthesize_codex_profile([f"/ws-{i}"], platform="linux")
        sc.write_codex_layer("clio", profile, elevated=False, codex_home=tmp_path, platform="linux")
    clio_layers = list(tmp_path.glob(sc.CODEX_LAYER_GLOB))
    assert len(clio_layers) <= sc.CODEX_LAYER_KEEP  # bounded — no unbounded leak
    # The user's files are untouched by the prune.
    assert (tmp_path / "config.toml").is_file()
    assert (tmp_path / "auth.json").is_file()


def test_write_layer_reuses_env_codex_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With no explicit home, $CODEX_HOME is used (the real ~/.codex, never a fresh dir)."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    profile = sc.synthesize_codex_profile(["/ws"], platform="linux")
    layer = sc.write_codex_layer("clio", profile, elevated=False, platform="linux")
    assert (tmp_path / f"{layer}.config.toml").is_file()


# --------------------------------------------------------------------------- #
# codex_prefix — exact argv shape with -p layer selection.                      #
# --------------------------------------------------------------------------- #


def test_codex_prefix_layer_argv_shape() -> None:
    """The prefix selects the -p layer, then --permission-profile / -C / -- passthrough."""
    prefix = sc.codex_prefix(
        "C:\\tools\\codex.cmd", "clio", "D:\\ws", layer_name="clio-sb-abcd1234"
    )
    assert prefix == [
        "C:\\tools\\codex.cmd",
        "sandbox",
        "-p",
        "clio-sb-abcd1234",
        "--permission-profile",
        "clio",
        "-C",
        "D:\\ws",
        "--",
    ]


def test_codex_prefix_stringifies_path_workspace() -> None:
    """A Path workspace is stringified into the argv (composable at the spawn point)."""
    prefix = sc.codex_prefix("codex", "clio", Path("/home/u/ws"), layer_name="clio-sb-0")
    assert prefix[0] == "codex"
    assert prefix[-1] == "--"
    assert prefix[prefix.index("-p") + 1] == "clio-sb-0"
    assert isinstance(prefix[prefix.index("-C") + 1], str)
