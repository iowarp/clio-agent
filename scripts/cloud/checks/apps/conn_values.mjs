// Connector values per conversation (ledger CV-Q1 to CV-Q6).
//
//   CV_MODE      "send" (default) or "view"
//   CV_WORKSPACE send: the workspace folder name under .cloud-run/apps-work/ (the same
//                one for every conversation of the scenario)
//   CV_SESSION   send: an existing conversation's UI path to continue (else a new one)
//   CV_REQUEST   send: the plain request typed in the composer
//   CV_PATH      view: the conversation's UI path; its newest view card is shown
//                (Show tab, Open in tab or Reopen), permission asks answered for the session
//   CV_HERO      "0" skips the hero shot
//   CV_CARD      send: a connector card the agent raises is answered: "add" (default) or
//                "decline". CV_CARD_VALUES is {row label: value} typed into it (optional
//                rows are opened first) and CV_SAVE_DEFAULT a comma list of row labels whose
//                "Save as default" is ticked.
//
// Prints CONN_VALUES {...}: the session, its tool calls, the conversation's own connector
// values (session metadata) and the text of every app frame on the page.
import { allowPending, api, heroShot, openConversation, send, toolCalls, waitIdle } from "./_lib.mjs";

async function viewBars(page) {
  const rows = [];
  for (const frame of page.frames()) {
    if (!/\/mcp-apps\/[^/]+\/sandbox/.test(frame.url())) continue;
    const text = await frame.locator("body").innerText().catch(() => "");
    rows.push(text.replace(/\s+/g, " ").slice(0, 160));
  }
  return rows;
}

async function answerCards(page, shot, record, label) {
  const card = page.locator('[data-slot="ask-card"][data-interaction-kind="permission"]');
  for (let i = 0; i < 8 && (await card.count()); i += 1) {
    await shot(`${label}-permission-${i + 1}`);
    record.permissions.push((await card.first().innerText()).replace(/\s+/g, " ").slice(0, 200));
    await card.first().getByRole("button", { name: "Allow for this session" }).first().click();
    await page.waitForTimeout(4000);
  }
}

/** Answer a connector card in the conversation as the person would. */
async function answerConnectorCard(page, shot, record) {
  const card = page.locator('[data-slot="ask-card"][data-interaction-kind="connector_setup"]').first();
  if (!(await card.count()) || !(await card.isVisible().catch(() => false))) return false;
  const id = await card.getAttribute("id").catch(() => null);
  if (id && record.answered.includes(id)) return false; // answered; its reply is on the way
  if (id) record.answered.push(id);
  const label = `card-${record.cards.length + 1}`;
  await card.scrollIntoViewIfNeeded().catch(() => undefined);
  const optional = card.getByRole("button", { name: /^Optional settings/u });
  if (await optional.count()) await optional.first().click().catch(() => undefined);
  const values = JSON.parse(process.env.CV_CARD_VALUES || "{}");
  for (const [row, value] of Object.entries(values)) {
    const field = card.getByLabel(row, { exact: true });
    if (await field.count()) await field.first().fill(value);
  }
  for (const row of (process.env.CV_SAVE_DEFAULT || "").split(",").filter(Boolean)) {
    const box = card.locator(`[data-need]`).filter({ hasText: row }).getByRole("checkbox", { name: "Save as default" });
    if (await box.count()) await box.first().click();
  }
  await page.waitForTimeout(800);
  await shot(label);
  record.cards.push((await card.innerText()).replace(/\s+/g, " ").slice(0, 400));
  const answer = process.env.CV_CARD === "decline" ? /^(Not now|Decline)/u : /^1?\s*(Add|Continue|Turn on)/u;
  await card.getByRole("button", { name: answer }).first().click();
  await page.waitForTimeout(3000);
  return true;
}

async function finish(page, shot, record, sid) {
  record.tools = await toolCalls(sid).catch((e) => [`error ${e}`]);
  const session = await api("GET", `/v1/sessions/${sid}`);
  record.connector_values = (session.metadata || {}).connector_values || null;
  record.views = await viewBars(page);
  await shot("conversation");
  if (process.env.CV_HERO !== "0" && (await page.locator("[data-mcp-app-tab]:visible").count())) {
    await heroShot(page, shot, "hero");
  }
  console.log(`CONN_VALUES ${JSON.stringify(record)}`);
}

export default async function (page, shot) {
  const record = { mode: process.env.CV_MODE || "send", permissions: [], log: [], cards: [], answered: [] };
  await page.getByText("Loading workspace").waitFor({ state: "hidden", timeout: 180000 }).catch(() => {});
  if (record.mode === "view") {
    const path = process.env.CV_PATH;
    if (!path) throw new Error("CV_PATH is required");
    const sid = path.split("/").at(-1);
    record.session = sid;
    await page.goto(new URL(path, page.url()).toString(), { waitUntil: "domcontentloaded" });
    await page.waitForTimeout(10000);
    const show = page.getByRole("button", { name: /^(Show tab|Open in tab|Reopen)$/u });
    record.cardButtons = await show.allInnerTexts().catch(() => []);
    if (await show.count()) {
      await show.last().scrollIntoViewIfNeeded().catch(() => undefined);
      await show.last().click().catch(() => undefined);
    }
    for (let i = 0; i < 8; i += 1) {
      await page.waitForTimeout(5000);
      await answerCards(page, shot, record, "view");
    }
    await page.waitForTimeout(15000);
    await finish(page, shot, record, sid);
    return;
  }
  let sid;
  if (process.env.CV_SESSION) {
    await page.goto(new URL(process.env.CV_SESSION, page.url()).toString(), { waitUntil: "domcontentloaded" });
    await page.waitForTimeout(8000);
    sid = process.env.CV_SESSION.split("/").at(-1);
    record.path = process.env.CV_SESSION;
  } else {
    const opened = await openConversation(page, process.env.CV_WORKSPACE || "conn-values");
    sid = opened.session.id;
    record.path = `/workspaces/${opened.workspace.id}/sessions/${sid}`;
    record.workspace = opened.root;
  }
  record.session = sid;
  await send(page, process.env.CV_REQUEST || "Open my dataset in ParaView.");
  await page.waitForTimeout(5000);
  const until = Date.now() + 15 * 60_000;
  let quiet = 0;
  while (Date.now() < until && quiet < 3) {
    await allowPending(sid, record.log);
    if (await answerConnectorCard(page, shot, record)) quiet = 0;
    const state = await api("GET", `/v1/sessions/${sid}`);
    quiet = ["idle", "completed"].includes(state.status) ? quiet + 1 : 0;
    await page.waitForTimeout(3000);
  }
  await waitIdle(page, sid, record.log, 2);
  await page.waitForTimeout(8000);
  await finish(page, shot, record, sid);
}
