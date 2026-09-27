"""Test support for the attention view: the real-run fixture and a query fake.

The fixture (``tests/fixtures/attention/earthscope_a00d067a.json.gz``, built by
``build_fixture.py`` from Flowcept job 3188205) carries one real CLIO lm.call,
its granite tokenization (ids + offsets), and the connector's real attention
rows reshaped to the proposed storage contract. The chat template is granite's
real ``chat_template.jinja``; only the 7 MB tokenizer is replaced by the
fixture's recorded encodings.
"""

from __future__ import annotations

import copy
import gzip
import json
import re
from functools import cache
from pathlib import Path
from typing import Any

import numpy as np
from tokenizers import Tokenizer, models, pre_tokenizers, trainers

from clio_agent.gact.attention.chat_render import ChatRenderer, Encoded
from clio_agent.gact.parts import Part
from clio_agent.gact.types import Message

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "attention"
# Normalized: a CRLF checkout (core.autocrlf) must not change the rendered prompt.
TEMPLATE = (
    (FIXTURES / "granite_chat_template.jinja").read_text(encoding="utf-8").replace("\r\n", "\n")
)
SID = "sess_fixture"


@cache
def fixture() -> dict[str, Any]:
    """The decoded real-run fixture."""
    with gzip.open(FIXTURES / "earthscope_a00d067a.json.gz", "rt", encoding="utf-8") as fh:
        return json.load(fh)


def tiny_tokenizer_json(corpus: list[str] | None = None) -> str:
    """A small byte-level BPE trained in-process (real ``tokenizers`` machinery)."""
    tok = Tokenizer(models.BPE())
    tok.pre_tokenizer = pre_tokenizers.ByteLevel(add_prefix_space=False)
    trainer = trainers.BpeTrainer(
        vocab_size=400,
        special_tokens=["<|im_start|>", "<|im_end|>"],
        initial_alphabet=pre_tokenizers.ByteLevel.alphabet(),
    )
    tok.train_from_iterator(corpus or ["hello world tool result answer " * 20], trainer)
    return tok.to_str()


def template_renderer(identity: str = "fixture/granite") -> ChatRenderer:
    """Granite's real template with a tiny tokenizer (for rendering text only)."""
    return ChatRenderer(
        tokenizer_json=tiny_tokenizer_json(),
        chat_template=TEMPLATE,
        special_tokens={"bos_token": "<s>", "eos_token": "<|im_end|>"},
        identity=identity,
    )


class FixtureRenderer:
    """Renders with granite's template; encodes with the recorded granite tokens."""

    identity = "ibm-granite/granite-4.2-30b"
    template_sha = "fixture"

    def __init__(self) -> None:
        self._text = template_renderer()
        data = fixture()
        self._content = data["lm_call"]["content"]

    def render(self, messages: list[dict[str, Any]], **kwargs: Any) -> str:
        return self._text.render(messages, **kwargs)

    def render_encoded(
        self, messages: list[dict[str, Any]], template_kwargs: dict[str, Any] | None = None
    ) -> Encoded:
        text = self.render(messages, template_kwargs=template_kwargs)
        data = fixture()
        if messages != data["lm_call"]["messages"]:
            raise AssertionError("FixtureRenderer only knows the fixture's prompt")
        return Encoded(
            text=text,
            ids=list(data["prompt"]["ids"]),
            offsets=[tuple(o) for o in data["prompt"]["offsets"]],
        )

    def encode(self, text: str) -> Encoded:
        data = fixture()
        if text != self._content:
            raise AssertionError("FixtureRenderer only knows the fixture's output")
        return Encoded(
            text=text,
            ids=list(data["output"]["ids"]),
            offsets=[tuple(o) for o in data["output"]["offsets"]],
        )


def _pack(values: list[float] | list[int], dtype: str) -> bytes:
    return np.asarray(values, dtype=np.dtype(dtype).newbyteorder("<")).tobytes()


def summary_doc(**over: Any) -> dict[str, Any]:
    """The fixture summary as a contract ``decode_attention`` task."""
    data = copy.deepcopy(fixture())
    s = data["summary"]
    summary = {
        "schema": "vllm_attn_connector.attention.v1",
        "segments": s["segments"],
        "segment_mean_mass": s["segment_mean_mass"],
        "residual_mean": s["residual_mean"],
        "token_index_base": "query",
        "top_pct": s["top_pct"],
        "attn_sum": _pack(s["attn_sum"], "f4"),
        "prompt_token_ids": _pack(data["prompt"]["ids"], "i4"),
        **s["health"],
    }
    summary.update(over.pop("summary", {}))
    doc = {
        "type": "task",
        "task_id": f"{data['request_id']}:g0",
        "activity_id": "decode_attention",
        "workflow_id": data["workflow"]["workflow_id"],
        "used": {
            "request_id": data["request_id"],
            "num_prompt_tokens": s["num_prompt_tokens"],
            "num_decode_tokens": s["num_decode_tokens"],
        },
        "generated": {"attention_summary": summary},
    }
    doc.update(over)
    return doc


def step_docs() -> list[dict[str, Any]]:
    """Contract ``decode_attention_step`` tasks: real rows for the window, tokens for all."""
    data = fixture()
    rows = data["steps"]
    docs = []
    for step, token_index, token_id in data["step_tokens"]:
        gen: dict[str, Any] = {"token_index": token_index, "token_id": token_id}
        row = rows.get(str(step))
        if row is None:
            gen.update(pos=b"", max=b"", mean=b"", residual=1.0)
        else:
            gen.update(
                pos=_pack(row["pos"], "i4"),
                max=_pack(row["max"], "f4"),
                mean=_pack(row["mean"], "f4"),
                head=_pack(row["head"], "i2"),
                residual=row["residual"],
            )
        docs.append(
            {
                "type": "task",
                "task_id": f"{data['request_id']}:g0:s{step}",
                "activity_id": "decode_attention_step",
                "used": {"request_id": data["request_id"], "kv_cache_group_id": 0, "step": step},
                "generated": gen,
            }
        )
    return docs


def _get(doc: dict[str, Any], dotted: str) -> Any:
    cur: Any = doc
    for key in dotted.split("."):
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur


def _match(doc: dict[str, Any], flt: dict[str, Any]) -> bool:
    for key, cond in flt.items():
        value = _get(doc, key)
        if isinstance(cond, dict):
            if "$regex" in cond and not (
                isinstance(value, str) and re.search(cond["$regex"], value)
            ):
                return False
            if "$gte" in cond and not (value is not None and value >= cond["$gte"]):
                return False
            if "$lte" in cond and not (value is not None and value <= cond["$lte"]):
                return False
        elif value != cond:
            return False
    return True


class FakeFlowcept:
    """In-memory stand-in for Flowcept's ``task_query``/``workflow_query`` (test only)."""

    def __init__(
        self,
        tasks: list[dict[str, Any]],
        workflows: list[dict[str, Any]] | None = None,
        fail: bool = False,
    ) -> None:
        self.tasks = tasks
        self.workflows = workflows or []
        self.fail = fail
        self.queries: list[dict[str, Any]] = []

    def query_tasks(
        self,
        filter: dict[str, Any],
        *,
        projection: list[str] | None = None,
        sort: list[tuple[str, int]] | None = None,
        limit: int = 0,
    ) -> list[dict[str, Any]] | None:
        self.queries.append(filter)
        if self.fail:
            return None
        rows = [d for d in self.tasks if _match(d, filter)]
        for key, order in reversed(sort or []):
            rows.sort(key=lambda d: _get(d, key), reverse=order < 0)
        return rows[:limit] if limit else rows

    def query_workflows(self, filter: dict[str, Any]) -> list[dict[str, Any]] | None:
        return [w for w in self.workflows if _match(w, filter)]


def fixture_flowcept() -> FakeFlowcept:
    """The fixture's summary + steps + connector workflow in the fake store."""
    data = fixture()
    return FakeFlowcept([summary_doc(), *step_docs()], [data["workflow"]])


def _tool_results(messages: list[dict[str, Any]]) -> list[tuple[str, str, dict[str, Any]]]:
    """``(tool name, value, args)`` per tool call/result pair recorded in the prompt."""
    out = []
    for i, message in enumerate(messages):
        if message["role"] != "user" or "[[ ## tool_call_results ## ]]" not in message["content"]:
            continue
        body = message["content"].split("[[ ## tool_call_results ## ]]", 1)[1].strip()
        calls = messages[i - 1]["content"].split("[[ ## tool_calls ## ]]", 1)[1]
        calls = calls.split("[[ ## completed ## ]]", 1)[0].strip()
        call_args = json.loads(calls)["tool_calls"]
        for entry, call in zip(json.loads(body)["tool_call_results"], call_args, strict=False):
            out.append((entry["name"], entry["value"], call.get("args") or {}))
    return out


def fixture_thought() -> str:
    """The generated ``next_thought`` of the fixture call (what the user selects)."""
    content = fixture()["lm_call"]["content"]
    body = content.split("[[ ## next_thought ## ]]", 1)[1]
    return body.split("[[ ## tool_calls ## ]]", 1)[0].strip()


def fixture_transcript(model_message_role: str = "assistant") -> list[Message]:
    """The turn as CLIO's transcript shows it, from the fixture's own records."""
    data = fixture()
    call = data["lm_call"]
    turn = call["turn_id"]
    messages = call["messages"]
    question = messages[1]["content"].split("[[ ## question ## ]]", 1)[1]
    question = question.split("[[ ## images ## ]]", 1)[0].strip()
    # The question field wraps the user's words in session state; the transcript
    # shows only the words the user typed.
    question = question[question.index("I want recent ground-motion") :]
    parts = []
    for n, (name, value, args) in enumerate(_tool_results(messages)):
        thought = messages[2 + 2 * n]["content"].split("[[ ## next_thought ## ]]", 1)[1]
        thought = thought.split("[[ ## tool_calls ## ]]", 1)[0].strip()
        parts.append(
            Part(id=f"call_{n}", type="tool_call", tool_name=name, thought=thought, input=args)
        )
        parts.append(
            Part(
                id=f"result_{n}",
                type="tool_result",
                tool_name=name,
                content=[Part(type="text", text=value)],
            )
        )
    parts.append(
        Part(
            id="call_sel",
            type="tool_call",
            tool_name="ndp_stage_resource",
            thought=fixture_thought(),
        )
    )
    now = "2026-09-21T18:40:00Z"
    return [
        Message(
            id=turn,
            session_id=SID,
            turn_id=turn,
            role="user",
            created_at=now,
            updated_at=now,
            parts=[Part(id="u0", type="text", text=question)],
        ),
        Message(
            id="msg_asst_1",
            session_id=SID,
            turn_id=turn,
            role=model_message_role,
            created_at=now,
            updated_at=now,
            parts=parts,
        ),
    ]
