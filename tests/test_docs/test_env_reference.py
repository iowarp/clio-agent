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

from scripts.gen_env_reference import (
    BOOTSTRAP_VARS,
    DOC_RELPATH,
    DOTENV_RELPATH,
    DYNAMIC_SECRET_VARS,
    OWNED_ELSEWHERE,
    _classify_tier,
    collect,
    generate,
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
    secret_names = {"CLIO_LM_API_KEY", "CLIO_ARGONNE_TOKEN", "ALCF_INFERENCE_TOKEN"}
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
    assert _classify_tier("CLIO_LM_ROUTER_TEMPERATURE") == "unmigrated"


def test_env_example_leaves_secrets_blank_and_comments_knobs() -> None:
    _, dotenv = generate(ROOT)
    lines = dotenv.splitlines()
    # Secrets render as an uncommented, blank assignment (never a real value).
    assert "CLIO_LM_API_KEY=" in lines
    assert "ALCF_INFERENCE_TOKEN=" in lines
    for line in lines:
        if line.startswith("CLIO_LM_API_KEY") or line.startswith("ALCF_INFERENCE_TOKEN"):
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


def test_env_read_wrapper_helpers_are_discovered() -> None:
    """Reads through a same-module helper (``_env_int``) count as env reads.

    Regression: the 11 ``CLIO_LEDGER_*`` knobs in ``gact/runtime/retention.py``
    were invisible because only literal ``os.environ`` calls matched.
    """
    _, env_only = collect(ROOT)
    by_env = {e.env: e for e in env_only}
    for name in ("CLIO_LEDGER_COMMAND_AUDIT_MAX", "CLIO_LEDGER_PENDING_DIFFS_HARD"):
        assert name in by_env, f"{name} (read via _env_int wrapper) missing from env-only vars"
        assert "src/clio_agent/gact/runtime/retention.py" in by_env[name].sources


def test_injected_env_mapping_reads_are_discovered() -> None:
    """``self.env.get(...)`` where ``self.env`` defaults to ``os.environ`` counts.

    Regression: ``runtime/status.py`` reads ``CLIO_DATA_DIR`` / ``CLIO_API_BASE``
    through an injected mapping that falls back to the real environment; both
    were absent from the reference.
    """
    _, env_only = collect(ROOT)
    by_env = {e.env: e for e in env_only}
    for name in ("CLIO_DATA_DIR", "CLIO_API_BASE"):
        assert name in by_env, f"{name} (read via self.env mapping) missing from env-only vars"
        assert "src/clio_agent/runtime/status.py" in by_env[name].sources


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
