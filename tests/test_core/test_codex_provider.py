"""Tests for the Codex LiteLLM CustomLLM provider
(iowarp/clio-agent#51).

The provider talks to a local ``codex`` binary via subprocess; tests
mock ``subprocess.run`` and the binary lookup so they run anywhere
without the Codex CLI installed.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from clio_agent.providers import codex_litellm
from clio_agent.providers.codex_litellm import (
    CodexCLIUnavailableError,
    CodexExecError,
    CodexLLM,
    _build_model_response,
    _messages_to_codex_prompt,
    _run_exec,
    ensure_registered,
)

# ---------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------


def _stub_run(stdout: str = "", stderr: str = "", returncode: int = 0):
    """Build a CompletedProcess-shaped MagicMock for subprocess.run."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.stdout = stdout
    proc.stderr = stderr
    return proc


@pytest.fixture(autouse=True)
def _reset_registration():
    """Each test starts with a clean LiteLLM provider map."""
    codex_litellm._reset_for_tests()
    yield
    codex_litellm._reset_for_tests()


# ---------------------------------------------------------------------
# message-flattening helper
# ---------------------------------------------------------------------


class TestMessagesToCodexPrompt:
    def test_single_user_message(self):
        prompt = _messages_to_codex_prompt(
            [{"role": "user", "content": "hello"}]
        )
        assert prompt == "USER: hello"

    def test_system_then_user(self):
        prompt = _messages_to_codex_prompt(
            [
                {"role": "system", "content": "be terse"},
                {"role": "user", "content": "ping"},
            ]
        )
        assert prompt == "SYSTEM: be terse\n\nUSER: ping"

    def test_vision_content_parts_flatten(self):
        prompt = _messages_to_codex_prompt(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "describe this"},
                        {"type": "image_url", "image_url": {"url": "..."}},
                    ],
                }
            ]
        )
        assert "describe this" in prompt

    def test_missing_content_renders_empty(self):
        prompt = _messages_to_codex_prompt(
            [{"role": "assistant"}]  # no content
        )
        assert prompt == "ASSISTANT:"


# ---------------------------------------------------------------------
# subprocess invocation
# ---------------------------------------------------------------------


class TestRunExec:
    def test_writes_prompt_to_stdin(self, tmp_path: Path):
        with (
            patch(
                "clio_agent.providers.codex_litellm._resolve_codex_binary",
                return_value="/usr/local/bin/codex",
            ),
            patch("subprocess.run") as run_mock,
            patch(
                "clio_agent.providers.codex_litellm.tempfile.gettempdir",
                return_value=str(tmp_path),
            ),
        ):
            run_mock.return_value = _stub_run(returncode=0)
            # Simulate Codex writing the last-message file. We don't
            # know the uuid; intercept Path.read_text on the last
            # created file.
            target = tmp_path
            (target / "preplaced.txt").write_text("hello back", encoding="utf-8")

            # Patch Path.read_text so the helper sees our text no
            # matter what filename it generated.
            with patch.object(Path, "read_text", return_value="hello back"):
                result = _run_exec(prompt="hi", model="gpt-5")

        assert result == "hello back"
        argv = run_mock.call_args[0][0]
        assert argv[0].endswith("codex")
        assert "exec" in argv
        assert "--model" in argv and argv[argv.index("--model") + 1] == "gpt-5"
        assert "--skip-git-repo-check" in argv
        assert "--sandbox" in argv
        # stdin contains the prompt.
        assert run_mock.call_args.kwargs["input"] == "hi"

    def test_non_zero_exit_raises(self, tmp_path: Path):
        with (
            patch(
                "clio_agent.providers.codex_litellm._resolve_codex_binary",
                return_value="/usr/local/bin/codex",
            ),
            patch("subprocess.run") as run_mock,
            patch(
                "clio_agent.providers.codex_litellm.tempfile.gettempdir",
                return_value=str(tmp_path),
            ),
        ):
            run_mock.return_value = _stub_run(returncode=1, stderr="boom")
            with pytest.raises(CodexExecError, match="returned 1"):
                _run_exec(prompt="hi", model="gpt-5")

    def test_timeout_raises(self, tmp_path: Path):
        with (
            patch(
                "clio_agent.providers.codex_litellm._resolve_codex_binary",
                return_value="/usr/local/bin/codex",
            ),
            patch("subprocess.run") as run_mock,
            patch(
                "clio_agent.providers.codex_litellm.tempfile.gettempdir",
                return_value=str(tmp_path),
            ),
        ):
            run_mock.side_effect = subprocess.TimeoutExpired(cmd="codex", timeout=1.0)
            with pytest.raises(CodexExecError, match="timed out"):
                _run_exec(prompt="hi", model="gpt-5", timeout=1.0)

    def test_missing_output_file_raises(self, tmp_path: Path):
        with (
            patch(
                "clio_agent.providers.codex_litellm._resolve_codex_binary",
                return_value="/usr/local/bin/codex",
            ),
            patch("subprocess.run") as run_mock,
            patch(
                "clio_agent.providers.codex_litellm.tempfile.gettempdir",
                return_value=str(tmp_path),
            ),
        ):
            run_mock.return_value = _stub_run(returncode=0)
            # Don't write the output file -> read_text raises FileNotFoundError.
            with pytest.raises(CodexExecError, match="no output file"):
                _run_exec(prompt="hi", model="gpt-5")


class TestResolveBinary:
    def test_unavailable_raises(self):
        with (
            patch("shutil.which", return_value=None),
            pytest.raises(CodexCLIUnavailableError, match="codex"),
        ):
            from clio_agent.providers.codex_litellm import (
                _resolve_codex_binary,
            )
            _resolve_codex_binary()


# ---------------------------------------------------------------------
# ModelResponse shape
# ---------------------------------------------------------------------


class TestBuildModelResponse:
    def test_shape(self):
        resp = _build_model_response(text="hello", model="gpt-5")
        assert resp.choices[0].message.content == "hello"
        assert resp.model == "codex/gpt-5"
        assert resp.usage.total_tokens == 0
        assert resp.object == "chat.completion"


# ---------------------------------------------------------------------
# CustomLLM end-to-end
# ---------------------------------------------------------------------


class TestCodexLLM:
    def test_completion_routes_through_run_exec(self):
        handler = CodexLLM()
        with patch(
            "clio_agent.providers.codex_litellm._run_exec",
            return_value="answer text",
        ) as run_mock:
            resp = handler.completion(
                model="codex/gpt-5",
                messages=[{"role": "user", "content": "hi"}],
                api_base="",
                custom_prompt_dict={},
                model_response=None,  # type: ignore[arg-type]
                print_verbose=lambda *_: None,
                encoding=None,
                api_key=None,
                logging_obj=None,
                optional_params={},
            )
        run_mock.assert_called_once()
        # The 'codex/' prefix gets stripped before invoking the CLI.
        assert run_mock.call_args.kwargs["model"] == "gpt-5"
        assert resp.choices[0].message.content == "answer text"

    def test_completion_passes_sandbox_override(self):
        handler = CodexLLM()
        with patch(
            "clio_agent.providers.codex_litellm._run_exec",
            return_value="ok",
        ) as run_mock:
            handler.completion(
                model="codex/gpt-5",
                messages=[{"role": "user", "content": "hi"}],
                api_base="",
                custom_prompt_dict={},
                model_response=None,  # type: ignore[arg-type]
                print_verbose=lambda *_: None,
                encoding=None,
                api_key=None,
                logging_obj=None,
                optional_params={"codex_sandbox": "workspace-write"},
            )
        assert run_mock.call_args.kwargs["sandbox"] == "workspace-write"


# ---------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------


class TestEnsureRegistered:
    def test_registers_once(self):
        import litellm

        before = len(litellm.custom_provider_map)
        ensure_registered()
        after_first = len(litellm.custom_provider_map)
        ensure_registered()
        after_second = len(litellm.custom_provider_map)

        # First call adds one entry; second call is a no-op.
        assert after_first == before + 1
        assert after_second == after_first

    def test_registered_entry_points_at_codex(self):
        import litellm

        ensure_registered()
        codex_entries = [
            e for e in litellm.custom_provider_map if e.get("provider") == "codex"
        ]
        assert len(codex_entries) == 1
        assert isinstance(codex_entries[0]["custom_handler"], CodexLLM)
