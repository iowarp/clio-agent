"""Values a flow handles: call results, the workspace, and plain-data helpers."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import dspy

from clio_agent.flows.outcomes import CallRefused


def _as_prediction(value: Any) -> Any:
    """A flow may return a prediction, a call result, a mapping with ``answer`` or text."""

    if isinstance(value, dspy.Prediction):
        return value
    if isinstance(value, CallResult):
        value = value.to_dict()
    if isinstance(value, Mapping):
        data = dict(value)
        answer = data.pop("answer", None)
        if answer is None:
            answer = json.dumps(data, indent=2, sort_keys=True, default=str)
        return dspy.Prediction(answer=str(answer), **data)
    return dspy.Prediction(answer="" if value is None else str(value))


def render_input(value: Any) -> str:
    """The assignment text a child receives for a call's input."""

    if isinstance(value, str):
        return value
    if isinstance(value, CallResult):
        return value.answer
    try:
        return "Input:\n```json\n" + json.dumps(value, indent=2, sort_keys=True) + "\n```"
    except (TypeError, ValueError):
        return f"Input:\n{value!r}"


def _plain(value: Any) -> Any:
    """JSON-able form of a call input (call results become their outputs)."""

    if isinstance(value, CallResult):
        return value.to_dict()
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


class CallResult(Mapping[str, Any]):
    """A settled call's result: its outputs, readable as a mapping or by attribute.

    ``result.answer`` is the child's answer; any other attribute reads the output
    of that name, then the child's typed ``workflow_state`` section of that name.
    """

    def __init__(
        self,
        value: Any,
        *,
        handle: str = "",
        target: str = "",
        kind: str = "",
        index: int = -1,
        model: str = "",
    ) -> None:
        self._value = value
        self.handle = handle
        #: ``account/model`` the call's child ran on ("" for a tool or an unknown child).
        self.model = model
        self.target = target
        self.kind = kind
        self.index = index

    @property
    def value(self) -> Any:
        return self._value

    @property
    def outputs(self) -> dict[str, Any]:
        return dict(self._value) if isinstance(self._value, Mapping) else {"value": self._value}

    @property
    def answer(self) -> str:
        if isinstance(self._value, Mapping):
            return str(self._value.get("answer", "") or "")
        return "" if self._value is None else str(self._value)

    @property
    def workflow_state(self) -> dict[str, Any]:
        state = self.outputs.get("workflow_state")
        return dict(state) if isinstance(state, Mapping) else {}

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        outputs = self.outputs
        if name in outputs:
            return outputs[name]
        state = self.workflow_state
        if name in state:
            return state[name]
        raise AttributeError(f"call result for {self.target!r} has no {name!r}")

    def __getitem__(self, key: str) -> Any:
        return self.outputs[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.outputs)

    def __len__(self) -> int:
        return len(self.outputs)

    def __str__(self) -> str:
        return self.answer

    def __repr__(self) -> str:
        return f"CallResult(target={self.target!r}, handle={self.handle!r})"

    def to_dict(self) -> dict[str, Any]:
        return {**self.outputs, "handle": self.handle, "target": self.target}


class FlowWorkspace:
    """The session's workspace folder. Writes are side effects: run them through ``ctx.step``."""

    def __init__(self, root: Path | None) -> None:
        self.root = root

    def _path(self, relative: str) -> Path:
        if self.root is None:
            raise CallRefused("this session has no workspace folder", reason_code="no_workspace")
        path = (self.root / relative).resolve()
        if self.root.resolve() not in path.parents and path != self.root.resolve():
            raise ValueError(f"{relative!r} is outside the workspace")
        return path

    def read(self, relative: str) -> str:
        return self._path(relative).read_text(encoding="utf-8")

    def write(self, relative: str, text: str) -> str:
        path = self._path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return str(path)

    def append(self, relative: str, value: Any) -> str:
        path = self._path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line.rstrip("\n") + "\n")
        return str(path)
