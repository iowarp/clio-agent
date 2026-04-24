"""Dev-only GACT server with a scripted fake agent.

Used by gact-tui's ``screenshot_clio_e2e.tape`` VHS recording +
CLIO-BBBBBBBBBB14 end-to-end smoke. It binds the same ``build_app``
the production ``clio-agent-gact`` console script builds, but with
``agent=FakeClioAgent()`` so the POST message path returns without
needing an LM, DSPy config, API keys, or Meridian.

Not an alternate production path — it lives under ``scripts/`` and
its entry point is spelled differently (``clio-agent-gact-smoke``)
so it can never be mistaken for the real thing in a deploy recipe.
Real Claude-in-the-loop validation lives in Phase D.

Run:

    uv run python scripts/gact_smoke_server.py --port 17777
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass

import uvicorn

from clio_agent.gact.app import build_app


@dataclass
class _Prediction:
    answer: str
    selected_expert: str
    routing_rationale: str = ""
    tools_called: list[dict] = None  # type: ignore[assignment]
    tokens: dict = None  # type: ignore[assignment]
    cost_usd: float = 0.0

    def __post_init__(self) -> None:
        if self.tools_called is None:
            self.tools_called = []
        if self.tokens is None:
            self.tokens = {}


class FakeClioAgent:
    """Minimal AgentLike for the smoke server.

    Picks a tier-2 expert by keyword match and returns a canned
    response. Deterministic so VHS recordings produce stable
    screenshots.
    """

    # Mirror the keyword table CLIO's tier-1 orchestrator uses so the
    # smoke response looks plausible to the TUI's v0.2 rendering.
    _KEYWORDS: dict[str, list[str]] = {
        "data_expert": [
            "hdf5", "parquet", "dataset", "csv", "analyze",
            "schema", "rows", "columns", "shape",
        ],
        "analysis_expert": [
            "statistics", "correlation", "distribution", "mean",
            "median", "p95", "p99", "histogram",
        ],
        "visualization_expert": [
            "plot", "chart", "graph", "visualize", "render",
        ],
    }

    def forward(self, question: str, session_id: str):
        q = question.lower()
        for expert, keywords in self._KEYWORDS.items():
            for kw in keywords:
                if kw in q:
                    return _Prediction(
                        answer=(
                            f"[smoke] {expert} would handle "
                            f"{question!r}. Returning a stub reply "
                            f"to prove the GACT wire path ({session_id})."
                        ),
                        selected_expert=expert,
                        routing_rationale=(
                            f"matched keyword {kw!r} -> {expert}"
                        ),
                        # A plausible tool-call trace for the post-
                        # hoc gutter render in the TUI.
                        tools_called=[
                            {
                                "name": f"{expert}.analyze",
                                "args": {"keyword": kw},
                                "ok": True,
                                "duration_ms": 24.7,
                                "cached": True,
                            },
                            {
                                "name": f"{expert}.summarise",
                                "args": {},
                                "ok": True,
                                "duration_ms": 12.3,
                                "cached": False,
                            },
                        ],
                        tokens={
                            "input": 342,
                            "output": 118,
                            "cache_read": 256,
                            "cache_write": 0,
                        },
                        cost_usd=0.00412,
                    )
        return _Prediction(
            answer=(
                "[smoke] No tier-2 expert matched. The tier-1 "
                "orchestrator answered directly."
            ),
            selected_expert="main",
            routing_rationale="no keyword match; fell through to tier-1",
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="clio-agent-gact-smoke",
        description=(
            "Dev-only GACT v0.2 server with a scripted fake agent. "
            "Use for VHS recordings + wire-path smoke tests; real "
            "LM-in-the-loop validation lives in gact-tui Phase D."
        ),
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=17777, type=int)
    args = parser.parse_args()

    app = build_app(agent=FakeClioAgent(), arc=FakeARC())
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


class FakeARC:
    """Populates /v1/memory/stats with believable numbers so the TUI
    renders its cache chip. Plumbing-only; the VHS screenshot needs
    non-zero hits/misses to exercise the traffic-lit chip."""

    def get_cache_stats(self) -> dict:
        return {"hits": 87, "misses": 13, "hit_rate": 0.87, "capacity": 1000}


if __name__ == "__main__":
    main()
