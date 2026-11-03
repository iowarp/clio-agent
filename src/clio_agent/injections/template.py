"""Injection text is a sandboxed Jinja template: variables, ternaries and ``{% if %}``.

``{{ name }}``, ``{{ plan.location }}``, ``{{ "a" if x else "b" }}`` and ``{% if %}``
blocks render against the variables the firing code and the session state supply
(:mod:`clio_agent.injections.state`). The environment is Jinja's
``ImmutableSandboxedEnvironment``: a template reads the values it is given and cannot
call into objects, mutate them or reach Python internals, so a plugin's text never runs
code. ``StrictUndefined`` makes a name the template uses but nobody supplied (or a
misspelt attribute such as ``plan.locaton``) a typed :class:`InjectionTemplateError`,
never an empty string.
"""

from __future__ import annotations

from collections.abc import Mapping
from functools import lru_cache
from typing import Any

from jinja2 import StrictUndefined, TemplateError, meta
from jinja2.sandbox import ImmutableSandboxedEnvironment

from clio_agent.errors import ClioError

__all__ = ["InjectionTemplateError", "names_used", "render_text"]

_ENV = ImmutableSandboxedEnvironment(
    undefined=StrictUndefined, autoescape=False, keep_trailing_newline=False
)


class InjectionTemplateError(ClioError):
    """An injection's text cannot be rendered: bad syntax or a name nobody supplied."""

    reason = "injection_template_invalid"

    def __init__(self, injection: str, problem: str) -> None:
        self.injection = injection
        self.problem = problem
        super().__init__(
            f"the injection {injection} cannot be rendered: {problem}",
            error_type=self.reason,
            details={"injection": injection, "problem": problem},
        )


@lru_cache(maxsize=512)
def _compiled(text: str) -> Any:
    return _ENV.from_string(text)


def names_used(text: str, injection: str = "") -> frozenset[str]:
    """The top-level names ``text`` reads (``plan`` for ``{{ plan.location }}``).

    Raises:
        InjectionTemplateError: the text is not a valid template.
    """
    try:
        return frozenset(meta.find_undeclared_variables(_ENV.parse(text)))
    except TemplateError as exc:
        raise InjectionTemplateError(injection, f"invalid template: {exc}") from exc


def render_text(text: str, variables: Mapping[str, Any], injection: str = "") -> str:
    """``text`` rendered with ``variables``, stripped.

    Raises:
        InjectionTemplateError: invalid syntax, an unsupplied name or attribute, or an
            operation the sandbox refuses.
    """
    try:
        return str(_compiled(text).render(**dict(variables))).strip()
    except TemplateError as exc:  # UndefinedError and SecurityError are TemplateErrors
        raise InjectionTemplateError(injection, str(exc) or type(exc).__name__) from exc
