"""CLIO's SearXNG setup verification, shipped as ``verify.py`` (stdlib only).

Run by the native supervisor's ``verify`` action in the service directory: one real
query through the JSON API must return at least one result with a URL. Prints the
evidence the supervisor records; the count is ``result_count``.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path


def verify(root: Path) -> dict[str, object]:
    """Run the verification query against the owned instance and return its evidence."""

    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    query = manifest["searxng"]["verify_query"]
    base = f"http://127.0.0.1:{int(manifest['port'])}"
    url = base + "/search?" + urllib.parse.urlencode({"q": query, "format": "json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=60) as response:
        body = json.load(response)
    results = [row for row in body.get("results") or [] if isinstance(row, dict) and row.get("url")]
    if not results:
        engines = [row[0] for row in body.get("unresponsive_engines") or [] if row][:10]
        raise RuntimeError(f"SearXNG returned no results; unresponsive engines: {engines}")
    engines_answered = sorted(
        {str(name) for row in results for name in row.get("engines") or [] if name}
    )
    return {
        "service": "searxng",
        "verification_id": str(uuid.uuid4()),
        "query": query,
        "result_count": len(results),
        "first_result_url": results[0]["url"],
        "engines_answered": engines_answered,
        "search_ok": True,
        "verified_at": time.time(),
    }


if __name__ == "__main__":
    try:
        print(json.dumps(verify(Path(__file__).resolve().parent)))
    except (OSError, ValueError, RuntimeError) as error:
        sys.stderr.write(f"SearXNG verification failed: {error}\n")
        sys.exit(4)
