"""LIVE proof for capability (a): two experts, two ALCF providers, each its own
model — heterogeneous per-expert routing on REAL inference (epic #667, #668).

Gated by ``CLIO_RUN_LIVE=1``. Targets ALCF only (Sophia + Metis), never LM Studio,
so it never contends with the local GPU. Builds each expert's config through the
real ``_dynamic_agent_lm_config`` preset path, then makes a real completion call to
each distinct endpoint. Metis is best-effort (skips its leg on maintenance) — the
endpoint-distinctness contract is asserted unconditionally.

Env (set when CLIO_RUN_LIVE=1):
    CLIO_LM_PROVIDER=argonne
    CLIO_LM_API_BASE=https://inference-api.alcf.anl.gov/resource_server/sophia/vllm/v1
    CLIO_LM_MODEL=openai/gpt-oss-120b
    (plus Argonne Globus auth, auto-managed on this machine)
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from clio_agent.config import create_lm, load_config_from_env
from clio_agent.gact.app import _dynamic_agent_lm_config

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("CLIO_RUN_LIVE") != "1",
        reason="live ALCF run: set CLIO_RUN_LIVE=1 (and Argonne auth + CLIO_LM_* env)",
    ),
]


def _expert_cfg(preset_id: str, model: str = ""):
    """Build a per-expert config the way gact does for a declared (provider, model)."""
    base = load_config_from_env()
    if str(getattr(base, "provider", "")) in {"lmstudio", "lm_studio"}:
        pytest.skip("live run must target Argonne/ALCF, not LM Studio (leave it free)")
    base_agent = SimpleNamespace(_provider_config=base)
    agent_def = SimpleNamespace(
        default_provider=preset_id, default_model=model, parameters={}
    )
    return _dynamic_agent_lm_config(base_agent, agent_def)


def _say_ok(lm) -> str:
    out = lm("Reply with exactly the two letters: OK")
    assert out and isinstance(out, list), "no completion returned"
    return str(out[0]).strip()


def test_two_alcf_providers_route_distinctly_on_real_inference():
    sophia = _expert_cfg("argonne_sophia", "openai/gpt-oss-120b")
    metis = _expert_cfg("argonne_metis")

    # Contract (unconditional): two presets sharing the 'argonne' kind reach
    # their own endpoints, each with the Globus auth carried over.
    assert sophia.provider == "argonne" and metis.provider == "argonne"
    assert sophia.api_base.endswith("/sophia/vllm/v1")
    assert metis.api_base.endswith("/metis/api/v1")
    assert sophia.api_base != metis.api_base
    assert sophia.api_key and metis.api_key  # token carried over from base

    # Sophia must return real inference. The LM is built from sophia.api_base
    # (asserted above), so a successful completion proves routing to that endpoint.
    lm_sophia = create_lm(sophia)
    assert _say_ok(lm_sophia), "Sophia produced no answer"
    assert lm_sophia.history, "Sophia call recorded no history"

    # Metis is the documented fallback gateway — best-effort (may be in maintenance).
    lm_metis = create_lm(metis)
    try:
        answer = _say_ok(lm_metis)
    except Exception as exc:  # noqa: BLE001 — maintenance/unreachable is not a code failure
        pytest.skip(f"Metis live call failed (likely maintenance): {exc}")
    assert answer, "Metis produced no answer"
