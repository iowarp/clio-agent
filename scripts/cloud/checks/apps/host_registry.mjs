// AQ16 and AQ17 against a stack whose registry knobs were lowered in its config.yaml
// (gact.mcp_apps.registry_ttl_s and gact.mcp_apps.registry_limit, see the ledger).
// CHECK_SCENARIO=ttl: one app in a tab and one inline, left untouched past the expiry.
// CHECK_SCENARIO=limit: more apps pinned in tabs than the limit, then one inline.
import {
  check,
  expectText,
  log,
  openConversation,
  sendAndSettle,
  sessionIdFrom,
  viewIn,
} from "./_app_view.mjs";
import { alive, appsOf } from "./host_live_set.mjs";

async function openInTab(page, topic) {
  await sendAndSettle(page, `Open the host panel for the topic ${topic}.`);
  const inline = viewIn(page, "inline");
  await expectText(inline.locator("#topic"), new RegExp(topic), 60000);
  const before = await page.locator("[data-mcp-app-tab]").count();
  await inline.locator("#fullscreen").click();
  const deadline = Date.now() + 30000;
  while ((await page.locator("[data-mcp-app-tab]").count()) <= before) {
    if (Date.now() > deadline) throw new Error(`no tab opened for ${topic}`);
    await page.waitForTimeout(250);
  }
}

export default async function (page, shot) {
  const scenario = process.env.CHECK_SCENARIO || "ttl";
  await openConversation(page, `host-registry-${scenario}`);
  const idleMs = Number(process.env.CHECK_IDLE_MS || 75000);
  if (scenario === "ttl") {
    await openInTab(page, "kappa");
    await sendAndSettle(page, "Open the host panel for the topic lambda.");
    await expectText(viewIn(page, "inline").locator("#topic"), /lambda/, 60000);
    const sid = sessionIdFrom(page);
    log("session", sid);
    const [tabApp, inlineApp] = (await appsOf(sid)).slice(-2);
    await shot("before-idle");
    log("idle for ms", idleMs);
    await page.waitForTimeout(idleMs);
    const statuses = { tab: await alive(sid, tabApp, page), inline: await alive(sid, inlineApp, page) };
    log("after idle", statuses);
    check(statuses.tab === 200, "the tab app outlives the expiry");
    check(statuses.inline === 404, "the idle inline app expired");
    await shot("after-idle");
    return;
  }
  const count = Number(process.env.CHECK_PINNED || 3);
  for (let index = 0; index < count; index += 1) await openInTab(page, `pin${index + 1}`);
  await sendAndSettle(page, "Open the host panel for the topic loose.");
  await expectText(viewIn(page, "inline").locator("#topic"), /loose/, 60000);
  const sid = sessionIdFrom(page);
  log("session", sid);
  const apps = (await appsOf(sid)).slice(-(count + 1));
  const statuses = [];
  for (const app of apps) statuses.push(await alive(sid, app, page));
  log("statuses (pinned..., inline)", statuses);
  check(statuses.every((status) => status === 200), "every pinned app and the inline app are live");
  check((await page.locator("[data-mcp-app-tab]").count()) === count, "one tab per pinned app");
  await shot("pinned-over-limit");
}
