// DOCS-F2: the agent looks at a view the person cannot see. The look waits (no timeout on
// CLIO's side, rule 6); the running "Look at view" row says "waiting to see the view" with
// Show it, and the view's in-tab row says "The agent is waiting to see this view.". After
// 30 s still hidden, the surface answers view_not_visible in words (the integration
// branch's 30 s answer, reconciled), so the agent can ask the person to show it.
//
//   PF_SESSION  "<workspace id>/<session id>" with a view in it
//   PF_HIDE     "tab": open the view in its tab, then close the canvas (tab not shown);
//               "scroll": keep the view in its card and scroll the card out of sight
//   PF_REQUEST  the request that makes the agent look (plain words)
//   PF_SHOW     "1": press Show it on the row while the look waits; "0": leave it (30 s)
// Prints one JSON line: what the row and card said, the look's outcome, the calls.
import { allowPending, api, heroShot, send, toolCalls } from "./_lib.mjs";

const env = process.env;
const words = (text) => (text || "").replace(/\s+/gu, " ").trim();

export default async function (page, rawShot) {
  const shot = async (name) => {
    const { width, height } = page.viewportSize();
    await page.mouse.move(width - 6, Math.round(height * 0.3));
    await page.waitForTimeout(600);
    return rawShot(name);
  };
  const [workspace, session] = env.PF_SESSION.split("/");
  await page.goto(new URL(`/workspaces/${workspace}/sessions/${session}`, page.url()).toString(), {
    waitUntil: "domcontentloaded",
  });
  await page.waitForTimeout(10000);
  // The composer holds a send while the widget catalog loads ("Preparing views…").
  await page
    .getByText("Preparing views…")
    .waitFor({ state: "hidden", timeout: 120000 })
    .catch(() => undefined);
  const report = { session: env.PF_SESSION, hide: env.PF_HIDE, log: [] };
  const inTab = page.locator("[data-mcp-app-in-tab]").filter({ visible: true }).last();
  if (env.PF_HIDE === "tab") {
    if (!(await inTab.count())) {
      const open = page.getByRole("button", { name: /Open in tab|Reopen/u }).filter({ visible: true });
      await open.last().click({ timeout: 20000 });
      await page.waitForTimeout(12000);
    }
    await shot("view-in-tab");
    const close = page.getByRole("button", { name: "Close workspace canvas" }).first();
    if (await close.count()) await close.click();
    await page.waitForTimeout(2000);
  }
  await send(page, env.PF_REQUEST);
  if (env.PF_HIDE === "scroll") {
    // The conversation follows the new message; scroll the card out of sight above it.
    await page.waitForTimeout(1500);
    await page.evaluate(() => {
      const card = [...document.querySelectorAll('[data-mcp-app-placement="inline"]')].at(-1);
      let box = card?.parentElement;
      while (box && !(box.scrollHeight > box.clientHeight + 20 && /auto|scroll/u.test(getComputedStyle(box).overflowY)))
        box = box.parentElement;
      if (box && card) box.scrollTop += card.getBoundingClientRect().bottom - box.getBoundingClientRect().top + 40;
    });
  }
  const row = page.locator('[data-slot="view-look-waiting"]').filter({ visible: true }).first();
  const until = Date.now() + 6 * 60_000;
  while (Date.now() < until && !(await row.isVisible().catch(() => false))) {
    await allowPending(session, report.log);
    const status = (await api("GET", `/v1/sessions/${session}`)).status;
    if (["idle", "completed", "error"].includes(status)) break;
    await page.waitForTimeout(1500);
  }
  report.rowWaiting = await row.isVisible().catch(() => false);
  if (report.rowWaiting) {
    await row.scrollIntoViewIfNeeded().catch(() => undefined);
    report.row = words(await row.locator("xpath=ancestor::*[@data-slot='tool-activity'][1]").innerText());
    const notice = page.locator('[data-slot="view-look-card-notice"]').filter({ visible: true });
    report.cardNotice = (await notice.count()) ? words(await notice.first().innerText()) : null;
    await shot("look-waiting");
    if (env.PF_SHOW === "1") {
      await row.getByRole("button", { name: "Show it" }).click();
      report.log.push("Show it");
      await page.waitForTimeout(3000);
      report.shownTab = await page.locator("[data-mcp-app-tab]").filter({ visible: true }).count();
      await shot("shown");
    }
  }
  const done = Date.now() + 10 * 60_000;
  while (Date.now() < done) {
    await allowPending(session, report.log);
    const status = (await api("GET", `/v1/sessions/${session}`)).status;
    if (["idle", "completed", "error"].includes(status)) break;
    await page.waitForTimeout(3000);
  }
  await page.waitForTimeout(4000);
  const looks = page.locator('[data-slot="tool-activity"]').filter({ hasText: "Look at view" });
  report.lookRows = [];
  for (let i = 0; i < (await looks.count()); i += 1) report.lookRows.push(words(await looks.nth(i).innerText()));
  await page.mouse.move(360, 500);
  await page.mouse.wheel(0, 20000);
  await page.waitForTimeout(1500);
  await shot("answered");
  if (env.PF_HERO === "1" && page.viewportSize().width >= 1200) await heroShot(page, shot, "hero");
  report.calls = await toolCalls(session);
  console.log(JSON.stringify(report));
}
