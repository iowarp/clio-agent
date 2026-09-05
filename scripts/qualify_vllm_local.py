"""Exercise a live vLLM OpenAI-compatible server through Agent's real LM factory."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from clio_agent.config import LMProviderConfig, create_lm


def call_model(*, api_base: str, model: str, max_tokens: int) -> dict[str, Any]:
    """Run one real Agent LM call and return bounded wire-facing evidence."""
    config = LMProviderConfig(
        provider="vllm",
        model=model,
        api_base=api_base,
        api_key="local-qualification",
        max_tokens=max_tokens,
    )
    lm = create_lm(config)
    started = time.perf_counter()
    result = lm(messages=[{"role": "user", "content": "Reply with only the word OK."}])
    return {
        "configured_max_tokens": max_tokens,
        "wire_max_tokens_present": "max_tokens" in lm.kwargs,
        "wire_max_tokens": lm.kwargs.get("max_tokens"),
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "result": str(result)[:500],
        "history_entries": len(lm.history),
    }


def parse_args() -> argparse.Namespace:
    """Parse qualification arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-base", default="http://127.0.0.1:18080/v1")
    parser.add_argument("--model", default="clio-local-qualification")
    parser.add_argument("--positive-cap", type=int, default=16)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    """Run omitted-cap and positive-cap calls and write exact JSON evidence."""
    args = parse_args()
    if args.positive_cap <= 0:
        raise ValueError("--positive-cap must be greater than zero")
    report = {
        "schema": "clio-agent.vllm-local-qualification.v1",
        "api_base": args.api_base,
        "model": args.model,
        "calls": [
            call_model(api_base=args.api_base, model=args.model, max_tokens=0),
            call_model(
                api_base=args.api_base,
                model=args.model,
                max_tokens=args.positive_cap,
            ),
        ],
    }
    if report["calls"][0]["wire_max_tokens_present"]:
        raise RuntimeError("default max_tokens=0 was sent as a finite provider cap")
    if report["calls"][1]["wire_max_tokens"] != args.positive_cap:
        raise RuntimeError("positive max_tokens did not reach the provider configuration")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
