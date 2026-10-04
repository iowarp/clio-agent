"""Compare CLIO and SPOTTER on recorded capture bytes, without fresh inference.

Run with the marketplace SPOTTER src directory on PYTHONPATH and its dependencies
installed. The query double below is deliberately confined to this test harness.
"""

from __future__ import annotations

import json
from typing import Any

from clio_schemas.attention import DECAYED_MAX, UNIFORM_MEAN
from spotter_ai.attention import AttentionEvidence

from clio_agent.gact.attention.profiles import reduce_steps
from clio_agent.gact.attention.store import AttentionStore
from tests.test_gact.test_attention._support import FIXTURES, descriptor_doc, fixture_flowcept


class ReplayQueries:
    """One recorded descriptor, exposed only to this offline qualification."""

    def query_attention_tasks(self, match: dict[str, Any], limit: int) -> list[dict[str, Any]]:
        """Filter through the existing recorded-fixture query implementation."""
        return fixture_flowcept().query_tasks(match, limit=limit) or []


def main() -> None:
    """Assert reader/reducer parity for both profiles and print a bounded receipt."""
    descriptor = descriptor_doc()
    request_id = descriptor["used"]["request_id"]
    response_id = request_id.rsplit("-", 1)[0]
    store = AttentionStore(fixture_flowcept(), files_dir=str(FIXTURES))
    summary = store.summary_for(response_id)
    selected = [3, 4, 5, 6, 7]
    rows = store.steps(summary, selected[0], selected[-1])
    reviewer = AttentionEvidence(ReplayQueries(), FIXTURES)
    receipts = []
    for profile in (UNIFORM_MEAN, DECAYED_MAX):
        actual = reviewer.inspect(response_id, selected, profile)
        expected = reduce_steps(rows, summary.prompt_tokens, profile)
        differences = [
            abs(token["score"] - expected.scores[token["position"]])
            for token in actual["top_tokens"]
        ]
        assert max(differences, default=0) < 1e-12
        assert abs(actual["residual_mass"] - expected.residual) < 1e-12
        assert actual["profile_revision"] == profile.revision
        receipts.append(
            {
                "profile": profile.name,
                "revision": profile.revision,
                "max_score_difference": max(differences, default=0),
                "residual_mass": actual["residual_mass"],
            }
        )
    print(
        json.dumps(
            {
                "qualification": "recorded replay, not fresh inference",
                "request_id": request_id,
                "capture_sha256": descriptor["attention_stats"]["sha256"],
                "steps": selected,
                "profiles": receipts,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
