// Two views of one stream each hold their own lease (E8, client half).
//
// VIEWS_ROOT names a fresh workspace folder inside CLIO_ALLOWED_ROOTS. The step asks, with a
// tool-directed prompt (mechanism only), for a background script writing one stream and two
// charts bound to it, then records every POST /v1/streams/<id>/watch the page sends: the
// stream, whether it releases, and its lease_id. It captures the two charts, collapses the
// first one's group so that view unmounts, and checks that the other keeps its lease.
import path from "node:path";

const AGENT = process.env.AGENT_URL || "http://127.0.0.1:8787";

async function api(method, route, body) {
  const response = await fetch(AGENT + route, {
    method,
    headers: { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await response.text();
  return text ? JSON.parse(text) : {};
}

export default async function (page, shot) {
  const root = process.env.VIEWS_ROOT;
  const watches = [];
  page.on("request", (request) => {
    const match = request.url().match(/\/v1\/streams\/([^/]+)\/watch/);
    if (!match || request.method() !== "POST") return;
    const body = JSON.parse(request.postData() || "{}");
    watches.push({ stream: match[1], release: Boolean(body.release), lease: body.lease_id || "" });
  });
  const workspace = await api("POST", "/v1/workspaces", { name: path.basename(root), root_path: root });
  const session = await api("POST", "/v1/sessions", { workspace_id: workspace.id });
  await page.goto(new URL(`/workspaces/${workspace.id}/sessions/${session.id}`, page.url()).toString());
  await page.waitForTimeout(5000);
  const field = page.locator('[contenteditable="true"]').last();
  await field.click();
  await page.keyboard.type(
    "Start a background run_code script labelled ticker that opens a stream named ticks with columns t and value and writes one row every second for 90 seconds. Then show two separate line charts in the conversation, each bound to that stream (t against value), one titled First view and one titled Second view.",
  );
  await page.keyboard.press("Enter");
  const until = Date.now() + 5 * 60_000;
  let quiet = 0;
  while (Date.now() < until && quiet < 3) {
    for (const row of (await api("GET", `/v1/permissions?session_id=${session.id}&status=pending`)).permissions || [])
      await api("POST", `/v1/permissions/${row.id}`, { action: "allow_session" });
    quiet = ["idle", "completed"].includes((await api("GET", `/v1/sessions/${session.id}`)).status) ? quiet + 1 : 0;
    await page.waitForTimeout(3000);
  }
  await page.waitForTimeout(4000);
  await shot("two-views");
  const holds = watches.filter((w) => !w.release);
  const leases = [...new Set(holds.map((w) => w.lease))];
  console.log(
    JSON.stringify(
      {
        session: session.id,
        workspace: workspace.id,
        watch_requests: watches.length,
        distinct_hold_leases: leases.length,
        streams: [...new Set(watches.map((w) => w.stream))],
        releases: watches.filter((w) => w.release).length,
      },
      null,
      2,
    ),
  );
}
