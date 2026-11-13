// First-run journey 7: restart CLIO and come back, in the same browser.
//
//   JOURNEY_SESSION   the conversation's UI path (/workspaces/<w>/sessions/<s>)
//   JOURNEY_SHOW      a pattern for the card button pressed first so a view is in a tab
//                     (default: Show tab, Open in tab or Reopen), on the newest card
//   JOURNEY_ASK       a plain request sent after the restart (optional)
//
// The page keeps its browser storage across the restart (the person's own browser): it
// opens the conversation, puts the newest view in a tab, restarts the stack with
// scripts/cloud/stack.sh restart, reloads, and counts what the person meets: the tabs
// that came back, permission asks on the way, and the view's state line.
import { execFileSync } from "node:child_process";
import { ROOT, api } from "./_lib.mjs";

const settle = (page, ms) => page.waitForTimeout(ms);

async function permissionsAnswered(page, shot, record, label) {
  const card = page.locator('[data-slot="ask-card"][data-interaction-kind="permission"]');
  for (let i = 0; i < 6 && (await card.count()); i += 1) {
    await shot(`${label}-permission-${i + 1}`);
    record.permissions.push((await card.first().innerText()).replace(/\s+/g, " ").slice(0, 300));
    await card.first().getByRole("button", { name: "Allow for this session" }).first().click();
    await settle(page, 4000);
  }
}

async function viewBars(page) {
  const rows = [];
  for (const frame of page.frames()) {
    if (!/\/mcp-apps\/[^/]+\/sandbox/.test(frame.url())) continue;
    const text = await frame.locator("body").innerText().catch(() => "");
    rows.push(text.replace(/\s+/g, " ").slice(0, 120));
  }
  return rows;
}

export default async function (page, shot) {
  const path = process.env.JOURNEY_SESSION;
  if (!path) throw new Error("JOURNEY_SESSION is required");
  const sid = path.split("/").at(-1);
  const record = { sid, permissions: [], before: {}, after: {} };
  await page.goto(new URL(path, page.url()).toString(), { waitUntil: "domcontentloaded" });
  await settle(page, 10000);
  const pattern = new RegExp(process.env.JOURNEY_SHOW || "^(Show tab|Open in tab|Reopen)$", "u");
  const show = page.getByRole("button", { name: pattern });
  record.before.cardButtons = await show.allInnerTexts().catch(() => []);
  if (await show.count()) {
    await show.last().scrollIntoViewIfNeeded().catch(() => undefined);
    await show.last().click().catch(() => undefined);
  }
  await settle(page, 20000);
  await permissionsAnswered(page, shot, record, "before");
  record.before.tabs = await page.getByRole("tab").allInnerTexts().catch(() => []);
  record.before.views = await viewBars(page);
  await shot("before-restart");

  const started = Date.now();
  execFileSync("bash", ["scripts/cloud/stack.sh", "restart"], { cwd: ROOT, stdio: "ignore" });
  record.restartSeconds = Math.round((Date.now() - started) / 1000);
  await page.reload({ waitUntil: "domcontentloaded" }).catch(() => undefined);
  await settle(page, 15000);
  await shot("after-restart-reloaded");
  record.after.tabs = await page.getByRole("tab").allInnerTexts().catch(() => []);
  // A restored tab reopens when first shown: show each app tab in turn.
  for (const name of record.after.tabs.filter((t) => !/^Files$/u.test(t.trim()))) {
    await page.getByRole("tab", { name: name.trim() }).first().click().catch(() => undefined);
    await settle(page, 20000);
    await permissionsAnswered(page, shot, record, "after");
  }
  record.after.views = await viewBars(page);
  await shot("after-restart-views");
  if (process.env.JOURNEY_ASK) {
    const composer = page.locator('[data-slot="clio-composer"] [contenteditable="true"]').first();
    await composer.click();
    await page.keyboard.type(process.env.JOURNEY_ASK);
    await page.keyboard.press("Enter");
    let busy = false;
    for (let i = 0; i < 120; i += 1) {
      await settle(page, 3000);
      await permissionsAnswered(page, shot, record, "ask");
      const state = await api("GET", `/v1/sessions/${sid}`).catch(() => ({}));
      if (state.status && state.status !== "idle") busy = true;
      if (busy && state.status === "idle") break;
    }
    await settle(page, 4000);
    await shot("after-restart-answer");
  }
  console.log(`JOURNEY_RESTART ${JSON.stringify(record)}`);
}
