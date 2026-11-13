// Connector discovery (CD rows): a plain request needs a connector that is off. The agent
// offers it, the offer card appears in the conversation (the connector card, intent
// "enable"), the person turns it on or says Not now, and the same turn continues.
//
//   OFFER_REQUEST  the plain request; "{root}" is replaced by the conversation's folder
//   OFFER_COPY     files or folders copied into that folder first (":"-separated, optional)
//   OFFER_ANSWER   "turn-on" (default) or "not-now"
//   OFFER_VALUES   JSON {"<row label>": "<value>"} typed into the card before Turn on
//   OFFER_ONLY_AGENT  "1": switch "Available in every conversation" off
//   OFFER_NAME     the workspace folder name (default "connector-offer")
//
// Needs a fresh config with the connector off. Nothing in the request names a tool.
import { cpSync } from "node:fs";
import path from "node:path";
import { allowPending, api, appFrames, heroShot, openConversation, send, showLastApp, toolCalls, waitIdle } from "./_lib.mjs";

export default async function (page, shot) {
  const log = [];
  const name = process.env.OFFER_NAME || "connector-offer";
  const { root, session } = await openConversation(page, name);
  for (const item of (process.env.OFFER_COPY || "").split(":").filter(Boolean))
    cpSync(item, path.join(root, path.basename(item)), { recursive: true });
  await send(page, (process.env.OFFER_REQUEST || "").replaceAll("{root}", root));
  const card = page.locator('[data-interaction-kind="connector_setup"][data-intent="enable"]').last();
  const until = Date.now() + 8 * 60_000;
  while (!(await card.isVisible())) {
    if (Date.now() > until) throw new Error("no offer card appeared");
    await allowPending(session.id, log);
    const status = (await api("GET", `/v1/sessions/${session.id}`)).status;
    if (["idle", "completed"].includes(status) && Date.now() > until - 6 * 60_000) {
      log.push("the turn ended without an offer");
      break;
    }
    await page.waitForTimeout(2000);
  }
  if (!(await card.isVisible())) {
    console.log(JSON.stringify({ session: session.id, calls: await toolCalls(session.id), log }, null, 2));
    throw new Error("the turn ended without an offer");
  }
  await card.scrollIntoViewIfNeeded();
  await page.waitForTimeout(1500);
  await shot("offer-card");
  const sidebar = page.getByRole("button", { name: "Toggle Sidebar" }).first();
  if (await page.getByRole("button", { name: "New conversation" }).first().isVisible().catch(() => false))
    await sidebar.click().catch(() => undefined);
  await page.waitForTimeout(1500);
  await card.scrollIntoViewIfNeeded();
  await shot("offer-card-hero");
  const values = JSON.parse(process.env.OFFER_VALUES || "{}");
  const more = card.getByRole("button", { name: /Optional settings/ });
  if (Object.keys(values).length && (await more.count())) await more.click();
  for (const [label, value] of Object.entries(values)) await card.getByLabel(label).fill(value);
  if (process.env.OFFER_ONLY_AGENT === "1") await card.getByRole("switch").click();
  if (Object.keys(values).length || process.env.OFFER_ONLY_AGENT === "1") await shot("offer-card-filled");
  const answer = process.env.OFFER_ANSWER || "turn-on";
  await card.getByRole("button", { name: answer === "not-now" ? /Not now/ : /Turn on/ }).click();
  log.push(`answered: ${answer}`);
  log.push(`settled: ${await waitIdle(page, session.id, log, 12)}`);
  await page.waitForTimeout(4000);
  await shot("answered");
  const frames = await showLastApp(page, await appFrames(page));
  if (frames.length) await shot("view");
  const messages = await api("GET", `/v1/sessions/${session.id}/messages`);
  const rows = messages.messages || [];
  const people = rows.filter((m) => m.role === "user").length;
  const connectors = (await api("GET", "/v1/connectors")).connectors.map(
    (c) => `${c.id}:${c.enabled ? JSON.stringify(c.scope) : "off"}:${c.state.status}`,
  );
  console.log(JSON.stringify({ session: session.id, calls: await toolCalls(session.id), frames, people, connectors, log }, null, 2));
  if (people !== 1) throw new Error(`expected one message from the person, found ${people}`);
  if (frames.length) await heroShot(page, shot, "view-hero");
}
