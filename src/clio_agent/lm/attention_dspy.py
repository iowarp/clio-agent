"""DSPy subclass loaded only when the agent constructs its language model."""

from typing import Any

import dspy

from clio_agent.lm.attention_lm import attention_request


class AttentionLM(dspy.LM):
    """DSPy's provider engine with attention declarations enclosing trace callbacks."""

    def __call__(self, prompt: Any = None, *, messages: Any = None, **kwargs: Any) -> Any:
        with attention_request(self, prompt, messages, kwargs) as (value, rows, options):
            return super().__call__(value, messages=rows, **options)

    async def acall(self, prompt: Any = None, *, messages: Any = None, **kwargs: Any) -> Any:
        with attention_request(self, prompt, messages, kwargs) as (value, rows, options):
            return await super().acall(value, messages=rows, **options)
