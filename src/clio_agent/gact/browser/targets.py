"""A target: the JSON description of a Playwright locator, rebuilt here on the host.

Element-addressing browser tools take a ``target``::

    {"steps": [{"by": "role", "role": "row", "name": "HDF5", "exact": false},
               {"by": "filter", "has_text": "2026"},
               {"by": "nth", "index": 0}],
     "frame": [{"by": "css", "css": "iframe#results"}]}

``frame`` is a chain of steps, each naming an iframe whose content the next steps address.
``by`` is one of ``ref``, ``role``, ``text``, ``label``, ``placeholder``, ``alt``, ``title``,
``test_id``, ``css``, ``filter``, ``nth``, ``first`` and ``last``. ``ref`` is an aria ref
(``f9e14``) from the latest snapshot of that page. Playwright's strictness applies: an
action on a target matching several elements fails with the count, so the script narrows it.
The kernel's ``browser`` binding builds these specs; nothing here guesses an element.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from clio_agent.gact.browser.settings import BrowserError

__all__ = ["STEP_KINDS", "describe", "locator_for"]

REASON = "browser_target_invalid"
_GET_BY = {
    "text": ("get_by_text", "text"),
    "label": ("get_by_label", "text"),
    "placeholder": ("get_by_placeholder", "text"),
    "alt": ("get_by_alt_text", "text"),
    "title": ("get_by_title", "text"),
}
STEP_KINDS = frozenset(
    {*_GET_BY, "ref", "role", "test_id", "css", "filter", "nth", "first", "last"}
)
_ROLE_OPTIONS = ("checked", "disabled", "expanded", "include_hidden", "level", "pressed")


def _text(step: Mapping[str, Any], key: str) -> str:
    value = step.get(key)
    if not isinstance(value, str) or not value:
        raise BrowserError(REASON, f"a {step.get('by')!r} step needs a non-empty {key!r}")
    return value


def _apply(root: Any, page: Any, step: Any, first: bool) -> Any:
    if not isinstance(step, Mapping):
        raise BrowserError(REASON, f"each step is an object, not {type(step).__name__}")
    by = step.get("by")
    if by not in STEP_KINDS:
        raise BrowserError(REASON, f"unknown step {by!r}; use one of {sorted(STEP_KINDS)}")
    if by == "ref":
        if not first:
            raise BrowserError(REASON, "a ref step comes first: refs name one element")
        return page.get_by_ref(_text(step, "ref"))
    if by == "role":
        options = {k: step[k] for k in _ROLE_OPTIONS if k in step}
        if "name" in step:
            options["name"] = str(step["name"])
            options["exact"] = bool(step.get("exact", False))
        return root.get_by_role(_text(step, "role"), **options)
    if by in _GET_BY:
        method, key = _GET_BY[by]
        return getattr(root, method)(_text(step, key), exact=bool(step.get("exact", False)))
    if by == "test_id":
        return root.get_by_test_id(_text(step, "test_id"))
    if by == "css":
        return root.locator(_text(step, "css"))
    if first:
        raise BrowserError(REASON, f"a {by!r} step narrows a locator; it cannot come first")
    if by == "filter":
        options: dict[str, Any] = {}
        for key in ("has_text", "has_not_text"):
            if key in step:
                options[key] = str(step[key])
        if "visible" in step:
            options["visible"] = bool(step["visible"])
        if not options:
            raise BrowserError(REASON, "a filter step needs has_text, has_not_text or visible")
        return root.filter(**options)
    if by == "nth":
        index = step.get("index")
        if not isinstance(index, int) or isinstance(index, bool):
            raise BrowserError(REASON, "an nth step needs an integer index")
        return root.nth(index)
    return root.first if by == "first" else root.last


def _chain(root: Any, page: Any, steps: Any) -> Any:
    if not isinstance(steps, list) or not steps:
        raise BrowserError(REASON, "steps is a non-empty list")
    current = root
    for index, step in enumerate(steps):
        current = _apply(current, page, step, first=index == 0)
    return current


def locator_for(page: Any, spec: Any) -> Any:
    """The Playwright locator ``spec`` describes, on ``page``.

    Raises:
        BrowserError: ``browser_target_invalid`` naming what is wrong with the spec.
    """
    if not isinstance(spec, Mapping):
        raise BrowserError(REASON, "a target is an object with a steps list")
    root: Any = page
    frames = spec.get("frame") or []
    if not isinstance(frames, list):
        raise BrowserError(REASON, "frame is a list of steps, one per nested iframe")
    for frame in frames:
        root = _chain(root, page, [frame] if isinstance(frame, Mapping) else frame).content_frame
    return _chain(root, page, spec.get("steps"))


def _step_text(step: Mapping[str, Any]) -> str:
    by = step.get("by")
    if by == "ref":
        return f"ref {step.get('ref')}"
    if by == "role":
        name = step.get("name")
        return f'{step.get("role")} "{name}"' if name else str(step.get("role"))
    if by in _GET_BY:
        return f'{by} "{step.get("text")}"'
    if by == "test_id":
        return f"test id {step.get('test_id')}"
    if by == "css":
        return str(step.get("css"))
    if by == "filter":
        text = step.get("has_text") or step.get("has_not_text")
        return f'with "{text}"' if text else "visible"
    if by == "nth":
        return f"#{step.get('index')}"
    return str(by)


def describe(spec: Any) -> str:
    """A short human form of a target: ``button "Save changes"``, ``ref f9e14``."""
    if not isinstance(spec, Mapping):
        return ""
    steps = spec.get("steps") if isinstance(spec.get("steps"), list) else []
    text = " ".join(_step_text(s) for s in steps if isinstance(s, Mapping))
    if spec.get("frame"):
        text += " in a frame"
    return text[:160]
