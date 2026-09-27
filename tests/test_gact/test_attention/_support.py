"""Test support for the attention view: the real-run fixture and a query fake.

The fixture (``tests/fixtures/attention/earthscope_9478ecf2.json.gz`` plus the
SafeTensors file under ``vllm-attn-9e11e1571c5f/``, built by
``build_fixture.py`` from the job 3237185 attention bundle) carries one real
CLIO lm.call, its granite tokenization (ids + offsets), the connector's real
descriptor task and its real file (rows cut to their top 64 entries). The chat
template is granite's real ``chat_template.jinja``; only the 7 MB tokenizer is
replaced by the fixture's recorded encodings.
"""

from __future__ import annotations

import copy
import gzip
import json
import re
from functools import cache
from pathlib import Path
from typing import Any

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
    with gzip.open(FIXTURES / "earthscope_9478ecf2.json.gz", "rt", encoding="utf-8") as fh:
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


def descriptor_doc(**over: Any) -> dict[str, Any]:
    """The fixture's real ``decode_attention`` descriptor task (``over`` merges shallowly)."""
    doc = copy.deepcopy(fixture()["descriptor"])
    stats = over.pop("attention_stats", None)
    if stats is not None:
        doc["attention_stats"] = {**doc["attention_stats"], **stats}
    doc.update(over)
    return doc


def request_id() -> str:
    """The fixture's vLLM request id."""
    return str(fixture()["descriptor"]["used"]["request_id"])


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
    """The fixture's descriptor + connector workflow in the fake store."""
    data = fixture()
    return FakeFlowcept([descriptor_doc()], [data["workflow"]])


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


def _history(messages: list[dict[str, Any]]) -> tuple[str, str, str]:
    """(earlier user words, earlier answer, current request) from the question field."""
    question = messages[1]["content"].split("[[ ## question ## ]]", 1)[1]
    question = question.split("[[ ## images ## ]]", 1)[0]
    history, current = question.split("=== Current request ===", 1)
    first_user = history.split("\nUser: ", 1)[1].split("\nAssistant: ", 1)[0]
    last_answer = history.rsplit("\nAssistant: ", 1)[1].rstrip()
    return first_user.strip(), last_answer.strip(), current.strip()


def _message(mid: str, tid: str, role: str, parts: list[Part]) -> Message:
    now = "2026-09-27T05:30:00Z"
    return Message(
        id=mid,
        session_id=SID,
        turn_id=tid,
        role=role,
        created_at=now,
        updated_at=now,
        parts=parts,
    )


def fixture_transcript(model_message_role: str = "assistant") -> list[Message]:
    """The session as CLIO's transcript shows it, from the fixture's own records."""
    data = fixture()
    call = data["lm_call"]
    turn = call["turn_id"]
    messages = call["messages"]
    earlier_user, earlier_answer, question = _history(messages)
    parts = []
    for n, (name, value, args) in enumerate(_tool_results(messages)):
        thought = messages[2 + 2 * n]["content"].split("[[ ## next_thought ## ]]", 1)[1]
        thought = thought.split("[[ ## tool_calls ## ]]", 1)[0].strip()
        parts.append(
            Part(id=f"call_{n}", type="tool_call", tool_name=name, thought=thought, input=args)
        )
        text = value if isinstance(value, str) else json.dumps(value)
        parts.append(
            Part(
                id=f"result_{n}",
                type="tool_result",
                tool_name=name,
                content=[Part(type="text", text=text)],
            )
        )
    parts.append(
        Part(
            id="call_sel",
            type="tool_call",
            tool_name="create_a2ui_surface",
            thought=fixture_thought(),
        )
    )
    early = "msg_user_earlier"
    return [
        _message(early, early, "user", [Part(id="u_e", type="text", text=earlier_user)]),
        _message(
            "msg_asst_earlier",
            early,
            "assistant",
            [Part(id="a_e", type="text", text=earlier_answer)],
        ),
        _message(turn, turn, "user", [Part(id="u0", type="text", text=question)]),
        _message("msg_asst_1", turn, model_message_role, parts),
    ]
