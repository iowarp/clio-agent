"""Sign a fixture connector in through CLIO's own sign-in routes (no browser).

    uv run --no-sync python -B scripts/cloud/checks/apps/connector_sign_in.py \
        --agent http://127.0.0.1:8801 --ui http://127.0.0.1:5201 --connector fixture-signin

It starts ``POST /v1/mcp/configuration/<id>/sign-in`` as the web UI would (its
origin and ``/oauth/mcp`` return address), approves the synthetic consent page of
``tests/fixtures/oauth_mcp_server.py``, and hands the return address back to
``.../complete``, then waits for the flow to connect. Only for the fixture server.
"""

from __future__ import annotations

import argparse
import sys
import time
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent", required=True)
    parser.add_argument("--ui", required=True)
    parser.add_argument("--connector", required=True)
    parser.add_argument("--deny", action="store_true", help="refuse consent instead")
    args = parser.parse_args()
    origin = args.ui.rstrip("/")
    with httpx.Client(timeout=30) as client:
        started = client.post(
            f"{args.agent}/v1/mcp/configuration/{args.connector}/sign-in",
            headers={"Origin": origin},
            json={"redirect_uri": f"{origin}/oauth/mcp"},
        )
        started.raise_for_status()
        flow = started.json()
        consent = urlsplit(client.get(flow["authorization_url"]).headers.get("location", ""))
        page = f"{consent.scheme}://{consent.netloc}{consent.path}"
        nonce = parse_qs(consent.query)["id"][0]
        decision = "deny" if args.deny else "allow"
        back = client.get(page, params={"id": nonce, "decision": decision})
        callback = urljoin(page, back.headers["location"])
        done = client.post(
            f"{args.agent}/v1/mcp/configuration/{args.connector}/sign-in/{flow['flow_id']}/complete",
            json={"callback_url": callback},
        )
        done.raise_for_status()
        for _ in range(60):
            state = client.get(
                f"{args.agent}/v1/mcp/configuration/{args.connector}/sign-in/{flow['flow_id']}"
            ).json()
            if state["state"] not in {"connecting", "authorizing", "preparing"}:
                print(state["state"], state.get("error") or "", ",".join(state.get("tools") or []))
                return 0 if state["state"] == "connected" or args.deny else 1
            time.sleep(1)
    print("sign-in did not finish", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
