"""Drift guard for the generated environment-variable reference (#769).

Mirrors ``tests/test_docs/test_prompt_alignment_reference_matrix.py``: the
committed ``docs/ENVIRONMENT.md`` and ``.env.example`` must match what
``scripts/gen_env_reference.py`` produces from the current source tree. When a
knob is added, renamed, or a default changes, regenerate::

    uv run python scripts/gen_env_reference.py

and commit the updated artifacts.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from scripts.gen_env_reference import (
    BOOTSTRAP_VARS,
    DEFAULTS_RELPATH,
    DOC_RELPATH,
    DOTENV_RELPATH,
    DYNAMIC_SECRET_VARS,
    OWNED_ELSEWHERE,
    _classify_tier,
    collect,
    generate,
    generate_defaults,
)

ROOT = Path(__file__).resolve().parents[2]


def test_environment_md_matches_source_tree() -> None:
    markdown, _ = generate(ROOT)
    committed = (ROOT / DOC_RELPATH).read_text(encoding="utf-8")
    assert committed == markdown, (
        "docs/ENVIRONMENT.md is stale; run `python scripts/gen_env_reference.py`."
    )


def test_env_example_matches_source_tree() -> None:
    _, dotenv = generate(ROOT)
    committed = (ROOT / DOTENV_RELPATH).read_text(encoding="utf-8")
    assert committed == dotenv, ".env.example is stale; run `python scripts/gen_env_reference.py`."


def test_config_defaults_yaml_matches_source_tree() -> None:
    """Drift guard: the committed base-layer defaults must mirror the in-code defaults.

    ``config.defaults.yaml`` is generated from the same ``conf.resolve`` ``default=``
    arguments the runtime falls back to, so a changed in-code default without a
    regeneration diverges the below-env base layer from the operative fallback.
    Regenerate with ``uv run python scripts/gen_env_reference.py``.
    """
    committed = (ROOT / DEFAULTS_RELPATH).read_text(encoding="utf-8")
    assert committed == generate_defaults(ROOT), (
        "src/clio_agent/config.defaults.yaml is stale; run `python scripts/gen_env_reference.py`."
    )


def test_config_defaults_yaml_is_flat_and_parses() -> None:
    """The committed defaults document is a flat dotted-key mapping the store reads."""
    committed = (ROOT / DEFAULTS_RELPATH).read_text(encoding="utf-8")
    assert committed.startswith("# GENERATED")
    assert "DO NOT EDIT" in committed.splitlines()[0]
    data = yaml.safe_load(committed) or {}
    assert isinstance(data, dict)
    # Every emitted key is a flat dotted string (not a nested mapping), so the
    # below-env layer's exact-key lookup finds it.
    for key, value in data.items():
        assert "." in key or key.isidentifier(), key
        assert not isinstance(value, dict), f"{key} must be a flat scalar, not nested"
    # Sanity: a couple of known knobs surface with the expected in-code defaults.
    assert data["arc.store"] == "cte"
    assert data["agents.disable_default_registry_bootstrap"] is False


def test_config_defaults_omits_dynamic_and_unset_knobs() -> None:
    """Computed/unset defaults are comments only, so resolution uses the in-code default."""
    resolved, _ = collect(ROOT)
    committed = yaml.safe_load((ROOT / DEFAULTS_RELPATH).read_text(encoding="utf-8")) or {}
    for r in resolved:
        if r.key and (r.dynamic_expr or r.default == ""):
            assert r.key not in committed, (
                f"{r.key} has a dynamic/unset default and must not carry a value"
            )
    # The two knobs Part 2 keeps env-driven are omitted (dynamic/unset).
    assert "tools.file_policy.allowed_roots" not in committed  # computed
    assert "lm.model" not in committed  # unset


def test_generated_files_carry_the_do_not_edit_banner() -> None:
    markdown, dotenv = generate(ROOT)
    assert markdown.startswith("<!-- GENERATED")
    assert "DO NOT EDIT" in markdown.splitlines()[0]
    assert dotenv.startswith("# GENERATED")
    assert "DO NOT EDIT" in dotenv.splitlines()[0]


def test_generator_is_deterministic() -> None:
    first = generate(ROOT)
    second = generate(ROOT)
    assert first == second


def test_resolved_and_env_only_sets_are_disjoint() -> None:
    resolved, env_only = collect(ROOT)
    resolved_vars = {r.env for r in resolved}
    env_vars = {e.env for e in env_only}
    # A conf.resolve knob may keep a bare-env legacy fallback; the resolved
    # entry is authoritative and must not be double-listed as env-only.
    assert resolved_vars.isdisjoint(env_vars)


def test_every_resolved_knob_has_a_config_key_and_tracked_env() -> None:
    resolved, _ = collect(ROOT)
    assert resolved, "expected at least one conf.resolve knob"
    for r in resolved:
        assert r.env.startswith(("CLIO_", "ALCF_"))
        assert r.key, f"{r.env} resolved without a dotted config key"


def test_secret_tokens_are_classified_secret_not_leaked_with_defaults() -> None:
    resolved, env_only = collect(ROOT)
    secret_names = {
        "CLIO_LM_API_KEY",
        "CLIO_ARGONNE_TOKEN",
        "ALCF_INFERENCE_TOKEN",
        "CLIO_RELAY_API_TOKEN",
    }
    # Secrets must never appear as a file-resolvable knob (would invite a value
    # in config.yaml / .env.example).
    assert secret_names.isdisjoint({r.env for r in resolved})
    secret_env = {e.env: e for e in env_only if e.tier == "secret"}
    for name in secret_names:
        assert name in secret_env, f"{name} should be discovered as an env-only secret"


def test_tier_classification_rules() -> None:
    assert _classify_tier("CLIO_USER_DIR") == "bootstrap"
    assert _classify_tier("CLIO_ENV_FILE") == "bootstrap"
    assert _classify_tier("CLIO_LM_API_KEY") == "secret"
    assert _classify_tier("ALCF_INFERENCE_TOKEN") == "secret"
    assert _classify_tier("CLIO_CRED_OPENAI_MAIN") == "secret"
    assert _classify_tier("CLIO_RUNTIME_STATE_DIR") == "unmigrated"


def test_env_example_leaves_secrets_blank_and_comments_knobs() -> None:
    _, dotenv = generate(ROOT)
    lines = dotenv.splitlines()
    # Secrets render as an uncommented, blank assignment (never a real value).
    assert "CLIO_LM_API_KEY=" in lines
    assert "ALCF_INFERENCE_TOKEN=" in lines
    assert "CLIO_RELAY_API_TOKEN=" in lines
    for line in lines:
        if line.startswith(("CLIO_LM_API_KEY", "ALCF_INFERENCE_TOKEN", "CLIO_RELAY_API_TOKEN")):
            assert line.endswith("="), f"secret carries a value: {line!r}"
    # Configured knobs are commented out so an untouched copy overrides nothing.
    assert "# CLIO_LM_PROVIDER=lm_studio" in lines


def test_bare_resolve_imports_are_discovered_as_resolved_knobs() -> None:
    """``from clio_agent.conf import resolve`` call sites are conf knobs too.

    Regression: the walker only matched attribute calls (``conf.resolve``), so
    the three config.py knobs resolved via a bare imported ``resolve(...)``
    were silently absent from both artifacts.
    """
    resolved, _ = collect(ROOT)
    by_env = {r.env: r for r in resolved}
    for name in (
        "CLIO_LM_TOKEN_LIVENESS",
        "CLIO_LM_TRANSIENT_RETRIES",
        "CLIO_LM_TRANSIENT_BACKOFF_S",
    ):
        assert name in by_env, f"{name} (bare resolve() call) missing from resolved knobs"
    liveness = by_env["CLIO_LM_TOKEN_LIVENESS"]
    # The bare-imported ``cast=as_bool`` must map to the bool label, not "str".
    assert liveness.type_ == "bool"
    assert liveness.default == "true"
    assert liveness.key == "runtime.lm_token_liveness"


def test_conf_resolve_wrapper_knobs_are_discovered() -> None:
    """Knobs resolved through a single-hop ``conf.resolve`` helper are configured knobs.

    Regression (#985 move 2): the 11 ``CLIO_LEDGER_*`` retention knobs and the 3
    ``CLIO_RESIDENT_LEDGERS_*`` knobs resolve through same-module helpers
    (``_resolve_positive_int`` / ``_resolve_positive_float``) that forward their
    ``(key, env, default)`` into ``conf.resolve``. The generator followed only
    ``os.environ`` wrappers before, so the resident-ledger trio was DROPPED entirely
    and the ledger-retention knobs were misclassified as env-only.
    """
    resolved, env_only = collect(ROOT)
    by_env = {r.env: r for r in resolved}
    env_names = {e.env for e in env_only}
    expected = {
        "CLIO_LEDGER_COMMAND_AUDIT_MAX": ("gact.ledger_retention.command_audit.max", "int", "2000"),
        "CLIO_LEDGER_PENDING_DIFFS_HARD": (
            "gact.ledger_retention.pending_diffs.hard",
            "int",
            "1000",
        ),
        "CLIO_RESIDENT_LEDGERS_MAX_BYTES": (
            "gact.resident_ledgers.max_bytes",
            "int",
            "536870912",
        ),
        "CLIO_RESIDENT_LEDGERS_TTL_S": ("gact.resident_ledgers.idle_ttl_s", "float", "1800.0"),
    }
    for name, (key, type_, default) in expected.items():
        assert name in by_env, f"{name} (via conf.resolve wrapper) missing from resolved knobs"
        assert name not in env_names, f"{name} must not double-list as env-only"
        record = by_env[name]
        assert record.key == key
        assert record.type_ == type_
        # The wrapper default (a literal or a dataclass-field default) renders concretely.
        assert record.default == default, f"{name} default {record.default!r} != {default!r}"


def test_status_data_dir_and_api_base_are_config_first() -> None:
    """``runtime/status.py`` resolves ``CLIO_DATA_DIR`` / ``CLIO_API_BASE`` config-first.

    Regression (#985 move 1): both were read through an injected ``self.env`` mapping
    (env-only). They now resolve file → env → default through ``conf`` and must be
    discovered as configured knobs, not env-only.
    """
    resolved, env_only = collect(ROOT)
    by_env = {r.env: r for r in resolved}
    env_names = {e.env for e in env_only}
    for name, key in (("CLIO_DATA_DIR", "paths.data_dir"), ("CLIO_API_BASE", "runtime.api_base")):
        assert name in by_env, f"{name} missing from resolved knobs after migration"
        assert name not in env_names, f"{name} must no longer be env-only"
        assert by_env[name].key == key
        assert by_env[name].source == "src/clio_agent/runtime/status.py"


def test_conf_resolve_wrapper_following_locked(tmp_path) -> None:
    """The generator follows a same-module helper that forwards params into conf.resolve.

    A fixture module wraps ``conf.resolve`` behind ``_resolve_wrapped`` and calls it
    with literal args (one default a plain int, one a dataclass-field default via
    ``cls.<attr>``). Both must surface as resolved knobs with the wrapper's inferred
    ``int`` coercion and their concrete defaults — locking the wrapper-following (and
    the class-attribute-default resolution) against regression.
    """
    module = """\
from __future__ import annotations

from dataclasses import dataclass

from clio_agent import conf


def _resolve_wrapped(key: str, env: str, default: int) -> int:
    raw = conf.resolve(key, env=env, default=default)
    return conf.as_int(raw)


@dataclass(frozen=True)
class _Fixture:
    limit: int = 4096

    @classmethod
    def build(cls) -> int:
        return _resolve_wrapped("fixture.limit", "CLIO_FIXTURE_LIMIT", cls.limit)


FOO = _resolve_wrapped("fixture.foo", "CLIO_FIXTURE_FOO", 7)
"""
    pkg = tmp_path / "src" / "clio_agent"
    pkg.mkdir(parents=True)
    (pkg / "wrapfixture.py").write_text(module, encoding="utf-8")

    resolved, env_only = collect(tmp_path)
    by_env = {r.env: r for r in resolved}
    env_names = {e.env for e in env_only}

    assert "CLIO_FIXTURE_FOO" in by_env, "literal-default wrapper call was not followed"
    foo = by_env["CLIO_FIXTURE_FOO"]
    assert foo.key == "fixture.foo"
    assert foo.type_ == "int"  # inferred from the wrapper's conf.as_int call
    assert foo.default == "7"

    assert "CLIO_FIXTURE_LIMIT" in by_env, "class-attr-default wrapper call was not followed"
    limit = by_env["CLIO_FIXTURE_LIMIT"]
    assert limit.key == "fixture.limit"
    assert limit.type_ == "int"
    assert limit.default == "4096", "the dataclass-field default must render concretely"

    # A wrapper knob is authoritative — never double-listed as env-only.
    assert env_names.isdisjoint({"CLIO_FIXTURE_FOO", "CLIO_FIXTURE_LIMIT"})


# The retired kill-switches from prior deletion campaigns. Each survives ONLY as a
# doc comment or a SABOTAGE test asserting it is inert; NONE may have a src reader.
_RETIRED_ENV_SWITCHES = (
    "CLIO_REACTV2",
    "CLIO_CODEX_APP_SERVER",
    "CLIO_TRANSCRIPT_PROJECTION",
    "CLIO_AGENT_ENABLE_LEGACY_NATIVE_EXPERTS",
    "CLIO_LM_ROUTER_TEMPERATURE",
)


def test_retired_env_switches_have_no_readers() -> None:
    """Baseline-0 guard: no retired kill-switch may re-acquire a src reader.

    ``collect()`` is AST-grounded — it finds real reads (``os.environ`` / ``conf.resolve``
    / wrappers / injected mappings), never a comment mention — so a retired name absent
    from BOTH the resolved and env-only sets proves nothing in ``src/clio_agent`` reads
    it. If any of these reappears, a deleted legacy pathway has silently come back to
    life under configuration (#985 / #775), and this guard fails.
    """
    resolved, env_only = collect(ROOT)
    discovered = {r.env for r in resolved} | {e.env for e in env_only}
    live = [name for name in _RETIRED_ENV_SWITCHES if name in discovered]
    assert not live, f"retired env switches re-acquired a src reader: {live}"


def test_dynamic_cred_prefix_is_documented_as_secret_pattern() -> None:
    """The runtime-named ``CLIO_CRED_*`` family surfaces via the curated entry."""
    assert "CLIO_CRED_<PROVIDER>_<ACCOUNT>" in DYNAMIC_SECRET_VARS
    _, env_only = collect(ROOT)
    by_env = {e.env: e for e in env_only}
    entry = by_env["CLIO_CRED_<PROVIDER>_<ACCOUNT>"]
    assert entry.tier == "secret"
    assert entry.sources == ["src/clio_agent/providers/credentials.py"]
    _, dotenv = generate(ROOT)
    # A placeholder is not a valid assignment; it must render commented out.
    assert "# CLIO_CRED_<PROVIDER>_<ACCOUNT>=" in dotenv.splitlines()


def test_write_only_env_file_loaded_marker_is_not_a_knob() -> None:
    """``CLIO_ENV_FILE_LOADED`` is only *written* by the dotenv loader.

    Regression: it sat in BOOTSTRAP_VARS as a dead classification entry; the
    agent never reads it, so it must not be classified or documented as a knob.
    """
    assert "CLIO_ENV_FILE_LOADED" not in BOOTSTRAP_VARS
    resolved, env_only = collect(ROOT)
    discovered = {r.env for r in resolved} | {e.env for e in env_only}
    assert "CLIO_ENV_FILE_LOADED" not in discovered


def test_owned_elsewhere_vars_are_not_read_in_source() -> None:
    resolved, env_only = collect(ROOT)
    discovered = {r.env for r in resolved} | {e.env for e in env_only}
    # Owned-elsewhere vars are consumed outside src/clio_agent; they must not be
    # AST-discovered, otherwise the curated note is wrong.
    assert discovered.isdisjoint(set(OWNED_ELSEWHERE))
