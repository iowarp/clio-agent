"""Evaluation harness for the tool-calling campaign (``docs/design/sdk/tool-calling-plan.md``).

Standalone: it talks to a running GACT server over REST and never imports CLIO's
runtime. Modules:

- ``fixtures/``: seeded generators for P1 to P10 and H1 to H3, with a sha256
  manifest and per-task answer keys, plus the ``clio-eval`` plugin marketplace.
- ``servers/``: the synthetic MCP servers (``synthetic-home``, two distractors,
  ``records``) that the marketplace's plugins launch.
- ``runner``: runs tasks against a server and writes one result JSON per run.
- ``collect``: turns a session export into the normalized run shape.
- ``metrics`` and ``grade``: per-run numbers and mechanical answer-key grading.
- ``claude_code_ref``: the Claude Code reference on P1 to P4.
- ``summarize``: Markdown tables for the ledger.
"""
