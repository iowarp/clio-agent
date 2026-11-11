#!/usr/bin/env bash
# Prepare a GitHub-hosted Ubuntu runner's apt for an Ubuntu mirror outage, before any
# `apt-get update`. Same measures as the Linux desktop deps steps in clio-bundles.yml (#2122):
#
# - The runner image's Launchpad PPAs hung apt for over an hour during a Launchpad outage
#   (v0.9.6-beta.1.5). Nothing CI installs comes from them, so they are disabled.
# - In the same outage the runners' resolver stopped answering for *.ubuntu.com and apt waited
#   out DNS on every package. Unresolvable archive hosts are pinned from Cloudflare's
#   DNS-over-HTTPS; apt still verifies the archive signatures.
# - Every later apt-get call retries a failed fetch 3 times and gives up on a stalled
#   connection after 20 s, so a dead mirror fails the step instead of eating its budget.
set -euo pipefail

for f in $(grep -rlE 'ppa\.launchpad(content)?\.net' /etc/apt/sources.list.d 2>/dev/null || true); do
  sudo mv "$f" "$f.disabled"
done

for h in azure.archive.ubuntu.com archive.ubuntu.com security.ubuntu.com ports.ubuntu.com; do
  if ! timeout 10 getent hosts "$h" >/dev/null; then
    ip=$(curl -fsS --max-time 10 -H 'accept: application/dns-json' "https://1.1.1.1/dns-query?name=$h&type=A" \
      | python3 -c 'import json,sys; print(next((a["data"] for a in json.load(sys.stdin).get("Answer", []) if a.get("type") == 1), ""))' || true)
    if [ -n "$ip" ]; then
      echo "$ip $h" | sudo tee -a /etc/hosts >/dev/null
      echo "::warning::$h does not resolve on this runner; pinned to $ip via DNS-over-HTTPS"
    fi
  fi
done

printf '%s\n' \
  'Acquire::Retries "3";' \
  'Acquire::http::Timeout "20";' \
  'Acquire::https::Timeout "20";' \
  | sudo tee /etc/apt/apt.conf.d/80-ci-retries >/dev/null
