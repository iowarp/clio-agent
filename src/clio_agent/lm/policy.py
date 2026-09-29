"""CLIO's LM call policy: how many times DSPy retries, and the typed truncation error.

DSPy 3.4 owns retries (``dspy.LM(num_retries=...)``): it retries only a retryable
provider error, backs off honoring ``retry-after``, and never re-issues a call that has
already streamed anything. CLIO sets the count; nothing else wraps a call.
"""

from __future__ import annotations

from clio_agent.errors import ProviderError

__all__ = ["LMOutputTruncatedError", "lm_retries"]


def lm_retries() -> int:
    """DSPy's retry count for a retryable provider failure (``limits.lm_transient_retries``)."""
    from clio_agent.conf import as_float, resolve  # noqa: PLC0415

    return max(
        0,
        int(
            resolve(
                "limits.lm_transient_retries",
                env="CLIO_LM_TRANSIENT_RETRIES",
                default=2.0,
                cast=as_float,
            )
        ),
    )


class LMOutputTruncatedError(ProviderError):
    """The provider exhausted its output budget before completing the response."""

    def __init__(self, model: str) -> None:
        super().__init__(
            f"Model {model!r} output was truncated (finish_reason=length). "
            "Increase the configured output limit or reduce the requested output; "
            "with no client cap, check the serving model's output/context limits.",
            details={"reason": "output_truncated", "finish_reason": "length", "model": model},
        )
