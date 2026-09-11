from __future__ import annotations

from types import SimpleNamespace

from clio_agent.gact import usage


def test_last_prompt_usage_normalizes_litellm_cache_details(monkeypatch) -> None:
    lm = SimpleNamespace(
        model="openai/gpt-5",
        history=[
            {
                "model": "openai/gpt-5",
                "timestamp": "2026-09-11T10:00:00Z",
                "usage": {
                    "prompt_tokens": 1000,
                    "completion_tokens": 40,
                    "prompt_tokens_details": {"cached_tokens": 750},
                },
            }
        ],
    )
    monkeypatch.setattr(usage, "_all_known_lms", lambda _app: [lm])

    snapshot = usage._last_prompt_usage_from_history_slice({id(lm): 0}, SimpleNamespace())

    assert snapshot == {
        "used_tokens": 1000,
        "source": "provider",
        "model": "openai/gpt-5",
        "cache_read_tokens": 750,
        "cache_write_tokens": 0,
        "cache_tokens_measured": True,
    }


def test_estimated_prompt_usage_labels_unmeasured_cache(monkeypatch) -> None:
    monkeypatch.setattr(usage, "_all_known_lms", lambda _app: [])

    snapshot = usage._estimated_prompt_usage("abcdefgh", "anthropic/claude-sonnet")

    assert snapshot == {
        "used_tokens": 2,
        "source": "estimated",
        "model": "anthropic/claude-sonnet",
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "cache_tokens_measured": False,
    }
