// The plugins the agent may offer (ledger OL1 to OL5): Settings -> Extensions -> "Plugins
// <agent> can offer" opens every candidate with a tick box, the default ones marked.
//
//   OL_MODE     "view" (default): open the list and shoot it (OL1)
//               "choose": untick OL_UNTICK, tick OL_TICK (comma-separated entry names),
//                 save, and shoot the list again (OL2)
//               "reset": Reset to default, save, and shoot the list (OL5)
//               "context": open OL_PATH (a conversation's UI path) and its context view,
//                 expand "Plugins you can add" and shoot it (OL3)
//               "reminder": Prompts & commands -> Reminders, the session_facts_plugins row's
//                 "Choose the plugins ... can offer" opens the same list
//   OL_SEARCH   view: a term typed into the search field before the second shot
//
// Prints OFFER_LIST {...}: what the list showed (count line, ticked names, default marks)
// and, after a save, the service's list.
import { api } from "./_lib.mjs";

const names = (value) => (value || "").split(",").map((v) => v.trim()).filter(Boolean);

async function openList(page) {
  await page.goto(new URL("/settings/plugins", page.url()).toString(), { waitUntil: "domcontentloaded" });
  const row = page.locator('[data-slot="offer-list-row"]');
  await row.waitFor({ state: "visible", timeout: 60000 });
  await page.getByRole("button", { name: "Choose…" }).waitFor({ state: "visible", timeout: 60000 });
  await page.waitForFunction(
    () => !document.querySelector('[data-slot="offer-list-row"] button[disabled]'),
    null,
    { timeout: 60000 },
  );
  return row;
}

async function shown(page) {
  const dialog = page.getByRole("dialog").last();
  return {
    count: (await dialog.locator('[data-slot="offer-list-count"]').innerText()).replace(/\s+/g, " "),
    ticked: await dialog
      .locator('[data-slot="offer-list-entry"]:has([role="checkbox"][aria-checked="true"]) label > span:first-child > span:first-child')
      .allInnerTexts(),
    defaults: await dialog.locator('[data-slot="offer-list-entry"]').filter({ hasText: "Default" }).count(),
    rows: await dialog.locator('[data-slot="offer-list-entry"]').count(),
  };
}

export default async function (page, shot) {
  const mode = process.env.OL_MODE || "view";
  const record = { mode };
  if (mode === "context") {
    await page.goto(new URL(process.env.OL_PATH, page.url()).toString(), { waitUntil: "domcontentloaded" });
    await page.waitForTimeout(8000);
    // The conversation rail holds the context view (its used total, bar and sections).
    if (!(await page.locator('[aria-label="Context sections"]').count()))
      await page.getByRole("button", { name: "Conversation rail" }).first().click();
    await page.waitForTimeout(3000);
    if (!(await page.locator('[aria-label="Context sections"]').count()))
      await page.getByText("Context", { exact: true }).first().click();
    await page.waitForTimeout(2000);
    const sections = page.locator('[aria-label="Context sections"]').last();
    await sections.waitFor({ state: "visible", timeout: 30000 });
    record.sections = (await sections.locator("summary").allInnerTexts()).map((s) => s.replace(/\s+/g, " "));
    const offer = sections.locator("details").filter({ hasText: "Plugins you can add" }).first();
    if (await offer.count()) {
      await offer.locator("summary").click();
      await page.waitForTimeout(800);
      record.items = (await offer.locator("li").allInnerTexts()).map((s) => s.replace(/\s+/g, " "));
      await offer.scrollIntoViewIfNeeded();
    }
    await shot("context");
    console.log(`OFFER_LIST ${JSON.stringify(record)}`);
    return;
  }
  if (mode === "reminder") {
    await page.goto(new URL("/settings/prompts", page.url()).toString(), { waitUntil: "domcontentloaded" });
    await page.getByRole("radio", { name: "Reminders" }).or(page.getByRole("tab", { name: "Reminders" })).first().click();
    const title = page.getByText("Session facts, plugins not in this conversation", { exact: true });
    await title.waitFor({ state: "visible", timeout: 60000 });
    await page.mouse.wheel(0, -200);
    await title.evaluate((el) => el.scrollIntoView({ block: "center" }));
    await title.hover();
    const action = page.getByRole("button", { name: /^Choose the plugins .* can offer$/ });
    await action.waitFor({ state: "visible", timeout: 10000 });
    await action.hover();
    await page.waitForTimeout(1200);
    await shot("reminder-row");
    await action.click();
    await page.getByRole("dialog").last().locator('[data-slot="offer-list-entry"]').first().waitFor();
    record.before = await shown(page);
    await shot("reminder-list");
    console.log(`OFFER_LIST ${JSON.stringify(record)}`);
    return;
  }
  const row = await openList(page);
  record.row = (await row.innerText()).replace(/\s+/g, " ");
  await shot("row");
  await page.getByRole("button", { name: "Choose…" }).click();
  const dialog = page.getByRole("dialog").last();
  await dialog.locator('[data-slot="offer-list-entry"]').first().waitFor({ state: "visible" });
  await page.waitForTimeout(1500);
  record.before = await shown(page);
  await shot("list");
  if (mode === "view") {
    // The boundary of the default list: the last default entries and the first beyond.
    const boundary = dialog.locator('[data-slot="offer-list-entry"]').nth(19);
    await boundary.scrollIntoViewIfNeeded();
    await page.waitForTimeout(800);
    await shot("list-boundary");
    if (process.env.OL_SEARCH) {
      await dialog.getByRole("searchbox").fill(process.env.OL_SEARCH);
      await page.waitForTimeout(800);
      record.search = await shown(page);
      await shot("list-search");
    }
  }
  if (mode === "choose" || mode === "reset") {
    if (mode === "choose") {
      for (const name of [...names(process.env.OL_UNTICK), ...names(process.env.OL_TICK)]) {
        const box = dialog.getByRole("checkbox", { name: `Bring in ${name}`, exact: true });
        await box.scrollIntoViewIfNeeded();
        await box.click();
        await page.waitForTimeout(300);
      }
    } else {
      await dialog.getByRole("button", { name: "Reset to default" }).click();
      await page.waitForTimeout(500);
    }
    record.chosen = await shown(page);
    const last = names(process.env.OL_TICK).at(-1);
    if (last) {
      await dialog.getByRole("checkbox", { name: `Bring in ${last}`, exact: true }).scrollIntoViewIfNeeded();
      await page.waitForTimeout(500);
    }
    await shot("chosen");
    await dialog.getByRole("button", { name: /^Save/ }).click();
    await page.waitForTimeout(2500);
    record.toast = await page.locator("[data-sonner-toast]").allInnerTexts().catch(() => []);
    record.rowAfter = (await row.innerText()).replace(/\s+/g, " ");
    await shot("saved");
    const list = await api("GET", "/v1/plugins/offer-list");
    record.service = {
      listed_count: list.listed_count,
      include: list.include,
      exclude: list.exclude,
      listed: list.entries.filter((e) => e.listed).map((e) => e.name),
    };
  }
  console.log(`OFFER_LIST ${JSON.stringify(record)}`);
}
