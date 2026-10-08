"""DIRECTIVES 14: managed vLLM turns tool calling on from the model family by default."""

from __future__ import annotations

import json
from pathlib import Path

from clio_agent.gact.infrastructure.server_parameter_defaults import parser_defaults
from clio_agent.gact.infrastructure.server_parameters import compile_parameters


def _model(tmp_path: Path, model_type: str) -> str:
    (tmp_path / "config.json").write_text(json.dumps({"model_type": model_type}))
    return str(tmp_path)


def test_a_qwen3_model_gets_its_tool_and_reasoning_parsers(tmp_path: Path) -> None:
    assert parser_defaults(_model(tmp_path, "qwen3"), {}) == {
        "param.tool_call_parser": "hermes",
        "param.reasoning_parser": "qwen3",
    }


def test_a_users_explicit_parser_is_kept(tmp_path: Path) -> None:
    configuration = {"param.tool_call_parser": "qwen3_xml"}
    assert parser_defaults(_model(tmp_path, "qwen3"), configuration) == {
        "param.reasoning_parser": "qwen3"
    }


def test_an_unknown_family_or_unreadable_model_adds_nothing(tmp_path: Path) -> None:
    assert parser_defaults(_model(tmp_path, "exotic"), {}) == {}
    assert parser_defaults(str(tmp_path / "absent"), {}) == {}


def test_off_overrides_the_model_family_default_and_launches_no_parser(tmp_path: Path) -> None:
    configuration = {"param.tool_call_parser": "off", "param.reasoning_parser": "off"}
    assert parser_defaults(_model(tmp_path, "qwen3"), configuration) == {}
    compiled = compile_parameters("vllm", "native-cuda", configuration)
    assert compiled.flags == ()
    assert compiled.values == {"tool_call_parser": "off", "reasoning_parser": "off"}
    chosen = compile_parameters("vllm", "native-cuda", {"param.tool_call_parser": "hermes"})
    assert chosen.flags == ("--enable-auto-tool-choice", "--tool-call-parser", "hermes")
