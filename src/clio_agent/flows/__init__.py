"""Flows: an agent's orchestration program, run on the task layer.

See ``docs/design/sdk/orchestration-semantics.md``. A flow is a ``dspy.Module``
contributed by a plugin; :mod:`clio_agent.flows.runner` runs the agent's
selected flow for a turn, and :mod:`clio_agent.flows.context` gives it the
``ctx`` primitives, each an ordinary CLIO task.
"""
