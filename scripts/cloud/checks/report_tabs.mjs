// Child reports read as named tabs (the explorer tabs check, tool-calling plan small items).
//
// TABS_WORKSPACE and TABS_SESSION name a conversation whose agent spawned children and
// waited for them. The step opens its activity, opens every delegation and wait row, and
// prints the rows it opened and the tab names it finds, then captures the first tab list.
export default async function (page, shot) {
  const ws = process.env.TABS_WORKSPACE;
  const sid = process.env.TABS_SESSION;
  await page.goto(new URL(`/workspaces/${ws}/sessions/${sid}`, page.url()).toString());
  await page.waitForTimeout(6000);
  for (const group of await page.getByRole("button", { name: /used tools|ran commands|delegat|spawn/i }).all())
    await group.click().catch(() => undefined);
  await page.waitForTimeout(800);
  const opened = [];
  const rows = page.locator('[data-slot="tool-activity"]');
  for (let i = 0; i < (await rows.count()); i += 1) {
    const row = rows.nth(i);
    const label = ((await row.innerText().catch(() => "")) || "").split("\n")[0];
    if (!/Wait|Delegate|Spawn|agents/i.test(label)) continue;
    await row.locator("button").first().click().catch(() => undefined);
    opened.push(label.slice(0, 80));
  }
  await page.waitForTimeout(1500);
  const tabs = await page.getByRole("tab").allInnerTexts();
  const list = page.getByRole("tablist").first();
  if (await list.count()) await list.scrollIntoViewIfNeeded();
  await shot("report-tabs");
  console.log(JSON.stringify({ session: sid, opened, tabs }, null, 2));
}
