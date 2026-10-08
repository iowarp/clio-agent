"""A model served by its local snapshot path gets the template-scan layer (F016/F017)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clio_agent.providers.capabilities.local_snapshot import LocalSnapshotSource, is_local_snapshot

_QWEN3_LIKE_TEMPLATE = (
    "{%- for tool in tools %}{{ tool }}{%- endfor %}"
    "{%- if enable_thinking is defined and enable_thinking is false %}<think>\n\n</think>{%- endif %}"
)


def _snapshot(tmp_path: Path, *, template: str | None, generation: dict | None) -> Path:
    root = tmp_path / "Org--Model--abc123"
    root.mkdir()
    (root / "config.json").write_text("{}", encoding="utf-8")
    if template is not None:
        (root / "tokenizer_config.json").write_text(
            json.dumps({"chat_template": template}), encoding="utf-8"
        )
    if generation is not None:
        (root / "generation_config.json").write_text(json.dumps(generation), encoding="utf-8")
    return root


def test_is_local_snapshot_requires_an_absolute_model_dir(tmp_path: Path) -> None:
    root = _snapshot(tmp_path, template=None, generation=None)
    assert is_local_snapshot(str(root))
    assert not is_local_snapshot("Qwen/Qwen3-4B")
    assert not is_local_snapshot(str(tmp_path / "missing"))
    assert not is_local_snapshot("")


def test_snapshot_template_scan_states_thinking_tools_and_sampling(tmp_path: Path) -> None:
    root = _snapshot(
        tmp_path,
        template=_QWEN3_LIKE_TEMPLATE,
        generation={"temperature": 0.6, "top_p": 0.95, "top_k": 20},
    )
    facts = LocalSnapshotSource(str(root)).facts(str(root).lower())
    assert facts is not None
    assert facts.thinking.value is not None
    assert facts.thinking.value.mechanism == "on_off"
    assert facts.thinking.value.template_kwarg == "enable_thinking"
    assert facts.tools.value is True
    assert facts.sampling_thinking.value == {"temperature": 0.6, "top_p": 0.95, "top_k": 20.0}
    assert "local snapshot" in facts.thinking.detail


def test_snapshot_without_template_or_generation_states_nothing(tmp_path: Path) -> None:
    root = _snapshot(tmp_path, template=None, generation=None)
    assert LocalSnapshotSource(str(root)).facts(str(root)) is None


@pytest.mark.parametrize("dialect", ["vllm", "lm_studio"])
def test_handshake_wires_the_snapshot_layer_for_a_path_served_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dialect: str
) -> None:
    from clio_agent.providers.handshake.base import HandshakeContext
    from clio_agent.providers.handshake.openai_compat import OpenAICompatHandshake

    root = _snapshot(tmp_path, template=_QWEN3_LIKE_TEMPLATE, generation=None)
    handshake = OpenAICompatHandshake(provider=object())
    monkeypatch.setattr(handshake, "_endpoint_dialect", lambda _ctx: dialect)
    ctx = HandshakeContext(
        provider_id="vllm-test", provider_kind="vllm", api_base="http://127.0.0.1:1/v1"
    )
    source = handshake._hf_source(ctx, str(root).lower(), str(root))
    assert isinstance(source, LocalSnapshotSource)
    monkeypatch.setattr(handshake, "_endpoint_dialect", lambda _ctx: "openai")
    assert handshake._hf_source(ctx, str(root).lower(), str(root)) is None
