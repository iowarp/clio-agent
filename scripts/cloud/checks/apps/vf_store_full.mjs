// View call fixes (VF, MC-F3 surfacing): clio-core refuses writes (its store is full) and
// the person keeps talking. Run on a stack started with a tiny store
// (CLIO_ARC_CTE_FILE_CAPACITY=1MB, a fresh .cloud-run/xdg) so a few plain turns fill it.
//
//   VF_TURNS    the most plain requests to send before giving up (default 12)
//   VF_SESSION  "<workspace id>/<session id>": continue that conversation instead
// Sends plain requests one after another in a new conversation, waiting for each turn,
// until a message is refused. Shoots the conversation after the refusal and prints one
// JSON line: the turns sent, the refusal's HTTP status and typed error, the alert and
// notice texts the page shows, and /v1/health's write row.
import { AGENT, api, openConversation, waitIdle } from "./_lib.mjs";

const sleep = (page, ms) => page.waitForTimeout(ms);

export default async function (page, shot) {
  const turns = Number(process.env.VF_TURNS || 12);
  let session = process.env.VF_SESSION?.split("/")[1];
  if (session) {
    const [workspace] = process.env.VF_SESSION.split("/");
    await page.goto(new URL(`/workspaces/${workspace}/sessions/${session}`, page.url()).toString());
    await sleep(page, 8000);
  } else session = (await openConversation(page, "vf-store-full")).session.id;
  const log = [];
  const refusals = [];
  page.on("response", async (response) => {
    // A send while a turn runs goes to the queue: both are the person's message.
    if (!/\/v1\/sessions\/[^/]+\/(messages|queued-messages)$/.test(new URL(response.url()).pathname))
      return;
    if (response.request().method() !== "POST" || response.ok()) return;
    refusals.push({ status: response.status(), body: (await response.text().catch(() => "")).slice(0, 600) });
  });
  let sent = 0;
  for (let i = 1; i <= turns && !refusals.length; i += 1) {
    const composer = page.locator('textarea, [contenteditable="true"]').last();
    await composer.waitFor({ state: "visible", timeout: 120000 });
    await composer.click();
    await page.keyboard.type(`Name one satellite gravity mission and its launch year, in one line (${i}).`);
    await page.keyboard.press("Enter");
    sent += 1;
    await sleep(page, 6000);
    if (!refusals.length) await waitIdle(page, session, log, 6);
  }
  await sleep(page, 4000);
  await shot("refused");
  const alerts = await page.locator('[role="alert"], [data-sonner-toast], [role="status"]').allInnerTexts();
  const health = await fetch(`${AGENT}/v1/health`).then((r) => r.json().then((body) => ({ status: r.status, body })));
  const write = (health.body.integrations || []).find((row) => row.name === "clio_core_write") || null;
  await sleep(page, 8000);
  await shot("refused-later");
  const persisted = await page.locator('[data-slot="store-refusal"], [role="alert"]').allInnerTexts();
  const session_ = await api("GET", `/v1/sessions/${session}`).catch((error) => ({ error: String(error) }));
  const result = {
    session,
    sent,
    refusals,
    alerts: alerts.map((text) => text.replace(/\s+/g, " ").slice(0, 300)),
    persisted: persisted.map((text) => text.replace(/\s+/g, " ").slice(0, 300)),
    health: { status: health.status, write },
    sessionStatus: session_.status || session_.error,
    log,
  };
  console.log(JSON.stringify(result));
  return result;
}
