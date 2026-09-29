"""Render a chat request exactly as vLLM tokenizes it.

vLLM applies the model's Hugging Face chat template (jinja2, sandboxed, with
``trim_blocks`` / ``lstrip_blocks`` and ``loopcontrols``) and then encodes the
string with ``add_special_tokens=False``. Doing the same here, with the same
``tokenizer.json`` and template, reproduces the server's ``prompt_token_ids``
token-for-token -- verified 32/32 on a recorded granite-4.2-30b run -- so token
boundaries computed here are the ones the attention connector scores.

Only the tokenizer files are needed (no weights): ``tokenizers`` and ``jinja2``
already ship with the core dependency set.
"""

from __future__ import annotations

import hashlib
import json
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _tojson(
    value: Any,
    ensure_ascii: bool = False,
    indent: int | None = None,
    separators: tuple[str, str] | None = None,
    sort_keys: bool = False,
) -> str:
    """The ``tojson`` filter with the keyword surface HF chat templates use."""
    return json.dumps(
        value,
        ensure_ascii=ensure_ascii,
        indent=indent,
        separators=separators,
        sort_keys=sort_keys,
    )


def _raise_exception(message: str) -> None:
    raise ValueError(message)


@dataclass(frozen=True)
class Encoded:
    """A string's token ids and per-token ``(char_start, char_end)`` offsets."""

    text: str
    ids: list[int]
    offsets: list[tuple[int, int]]

    def token_span(self, char_start: int, char_end: int) -> tuple[int, int]:
        """Token half-open span covering chars ``[char_start, char_end)``.

        Same rule as vllm-attn-connector's ``e2e_example.declare_ranges``:
        ``lo`` is the first token ending after ``char_start``; ``hi`` the first
        token starting at or after ``char_end``.
        """
        ends = [b for _, b in self.offsets]
        starts = [a for a, _ in self.offsets]
        lo = bisect_right(ends, char_start)
        hi = bisect_left(starts, char_end, lo=lo)
        return lo, max(lo, hi)

    def char_span(self, token_lo: int, token_hi: int) -> tuple[int, int]:
        """Char span of tokens ``[token_lo, token_hi)`` (empty span when empty)."""
        if token_hi <= token_lo or token_lo >= len(self.offsets):
            edge = self.offsets[token_lo][0] if token_lo < len(self.offsets) else len(self.text)
            return edge, edge
        return self.offsets[token_lo][0], self.offsets[min(token_hi, len(self.offsets)) - 1][1]


class ChatRenderer:
    """A model's tokenizer + chat template, rendering requests as vLLM does."""

    def __init__(
        self,
        *,
        tokenizer_json: str,
        chat_template: str,
        special_tokens: dict[str, str],
        identity: str,
    ) -> None:
        """Build from the raw ``tokenizer.json`` text and the template source."""
        from jinja2.ext import loopcontrols  # noqa: PLC0415
        from jinja2.sandbox import ImmutableSandboxedEnvironment  # noqa: PLC0415
        from tokenizers import Tokenizer  # noqa: PLC0415

        self._tokenizer = Tokenizer.from_str(tokenizer_json)
        env = ImmutableSandboxedEnvironment(
            trim_blocks=True, lstrip_blocks=True, extensions=[loopcontrols]
        )
        env.filters["tojson"] = _tojson
        env.globals["raise_exception"] = _raise_exception
        self._template = env.from_string(chat_template)
        self._special_tokens = dict(special_tokens)
        self.identity = identity
        self.template_sha = hashlib.sha256(chat_template.encode("utf-8")).hexdigest()[:16]

    @classmethod
    def from_dir(cls, path: Path, *, identity: str = "") -> ChatRenderer:
        """Load from a directory holding a HF tokenizer (``tokenizer.json`` + template)."""
        tokenizer_json = (path / "tokenizer.json").read_text(encoding="utf-8")
        config: dict[str, Any] = {}
        config_path = path / "tokenizer_config.json"
        if config_path.is_file():
            config = json.loads(config_path.read_text(encoding="utf-8"))
        template_path = path / "chat_template.jinja"
        if template_path.is_file():
            template = template_path.read_text(encoding="utf-8")
        else:
            template = config.get("chat_template") or ""
            if isinstance(template, list):  # named-template form
                template = next(
                    (t.get("template", "") for t in template if t.get("name") == "default"),
                    "",
                )
        if not template:
            raise ValueError(f"no chat template in {path}")
        special = {
            key: _token_text(config.get(key))
            for key in ("bos_token", "eos_token", "pad_token", "unk_token")
            if config.get(key)
        }
        return cls(
            tokenizer_json=tokenizer_json,
            chat_template=template,
            special_tokens=special,
            identity=identity or str(path),
        )

    def render(
        self,
        messages: list[dict[str, Any]],
        *,
        template_kwargs: dict[str, Any] | None = None,
        add_generation_prompt: bool = True,
    ) -> str:
        """The exact prompt string vLLM builds for ``messages``.

        ``template_kwargs`` is the request's ``chat_template_kwargs`` (e.g.
        ``enable_thinking``), which vLLM passes to the template the same way.
        """
        return self._template.render(
            messages=messages,
            add_generation_prompt=add_generation_prompt,
            **{**self._special_tokens, **(template_kwargs or {})},
        )

    def encode(self, text: str) -> Encoded:
        """Tokenize ``text`` without adding special tokens (vLLM's chat path)."""
        enc = self._tokenizer.encode(text, add_special_tokens=False)
        return Encoded(text=text, ids=list(enc.ids), offsets=[tuple(o) for o in enc.offsets])

    def render_encoded(
        self,
        messages: list[dict[str, Any]],
        template_kwargs: dict[str, Any] | None = None,
    ) -> Encoded:
        """Render then tokenize: the prompt exactly as the connector scores it."""
        return self.encode(self.render(messages, template_kwargs=template_kwargs))


def _token_text(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("content") or "")
    return str(value or "")
